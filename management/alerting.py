"""Bounded Prometheus rule snapshots; no outbound notifications in this module."""
import hashlib,json,math,re
from datetime import datetime,timezone
from types import SimpleNamespace
from sqlalchemy import select,update
from management.models import PrometheusAlertState,Incident,Evidence,Job
from management.domain import detect,utc

from management.alert_policy import RULES,RULE_IDS
JOBS={'prometheus','portal-otel','portal-health','portal-db-health','ec2-node'}


def stamp(value):
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None: raise ValueError('INVALID_EVALUATION_TIME')
    return parsed.timestamp()


def bounded_json(response):
    response.raise_for_status()
    if len(response.content)>1024*1024: raise ValueError('PROM_RESPONSE_TOO_LARGE')
    data=response.json()
    if not isinstance(data,dict) or data.get('status')!='success' or data.get('warnings'):
        raise ValueError('PROM_QUERY_FAILED')
    return data['data']


def identity(name,labels):
    safe={k:labels[k] for k in ('job','instance','check') if k in labels}
    if any(not isinstance(v,str) or not 1<=len(v)<=120 or not re.fullmatch(r'[A-Za-z0-9._:/\[\]-]+',v) for v in safe.values()):
        raise ValueError('INVALID_ALERT_LABELS')
    if name=='MonitoringTargetDown' and (safe.get('job') not in JOBS or not safe.get('instance')):
        raise ValueError('UNKNOWN_SCRAPE_TARGET')
    if 'check' in safe and safe['check'] not in {'proxy','app','database'}:
        raise ValueError('UNKNOWN_CHECK')
    digest=hashlib.sha256((name+'\n'+json.dumps(safe,sort_keys=True)).encode()).hexdigest()
    return digest,'pa-'+digest[:32],safe


def fetch_rules(client,url,at):
    data=bounded_json(client.get(url.rstrip('/')+'/api/v1/rules',params={'type':'alert'}))
    groups=data.get('groups')
    if not isinstance(groups,list) or len(groups)>64 or data.get('groupNextToken'):
        raise ValueError('INCOMPLETE_RULES')
    result={}
    for group in groups:
        rules=group.get('rules',[])
        if not isinstance(rules,list) or len(rules)>256: raise ValueError('INVALID_RULES')
        for rule in rules:
            name=rule.get('name')
            # PortalProbeFailed stays with record_probe to prevent double incidents.
            if name not in RULES: continue
            if name in result: raise ValueError('DUPLICATE_RULE')
            result[name]=None
            try: evaluated=stamp(rule.get('lastEvaluation',group.get('lastEvaluation')))
            except (ValueError,TypeError,AttributeError): continue
            if rule.get('type')!='alerting' or rule.get('health')!='ok' or rule.get('lastError') or not -5<=at.timestamp()-evaluated<=60:
                continue
            alerts=rule.get('alerts')
            if not isinstance(alerts,list) or len(alerts)>128: raise ValueError('INVALID_ALERTS')
            active={}
            for alert in alerts:
                labels=alert.get('labels')
                if not isinstance(labels,dict) or labels.get('alertname')!=name or alert.get('state') not in {'pending','firing'}:
                    raise ValueError('INVALID_ALERT')
                digest,key,safe=identity(name,labels)
                if digest in active: raise ValueError('DUPLICATE_ALERT')
                active[digest]={'id':digest,'target':key,'labels':safe,'state':alert['state'],'severity':labels.get('severity') if labels.get('severity') in {'warning','critical'} else 'warning'}
            result[name]={'evaluated':evaluated,'active':active}
    return {name:rule for name,rule in result.items() if rule is not None}


def vector(client,url,query):
    data=bounded_json(client.get(url.rstrip('/')+'/api/v1/query',params={'query':query}))
    rows=data.get('result')
    if data.get('resultType')!='vector' or not isinstance(rows,list) or len(rows)>256:
        raise ValueError('INVALID_VECTOR')
    return rows


def scalar(client,url,query):
    rows=vector(client,url,query)
    if len(rows)!=1: return None
    value=float(rows[0]['value'][1])
    return value if math.isfinite(value) else None


def recovery_snapshot(client,url,at):
    """Positive evidence. Missing metrics are never interpreted as recovered."""
    out={}
    try:
        values=vector(client,url,'up');times=vector(client,url,'timestamp(up)')
        def rows_map(rows):
            result={}
            for row in rows:
                labels=row['metric']
                if labels.get('job') not in JOBS: continue
                key=tuple(labels.get(k,'') for k in ('job','instance','check'))
                if key in result: raise ValueError('AMBIGUOUS_UP')
                result[key]=float(row['value'][1])
            return result
        v,t=rows_map(values),rows_map(times)
        out['MonitoringTargetDown']={k:(v[k]==1 and k in t and -5<=at.timestamp()-t[k]<=60) for k in v}
    except Exception: pass
    try:
        source=scalar(client,url,'max(timestamp(portal_http_requests))')
        fresh=source is not None and -5<=at.timestamp()-source<=60
        out['PortalTelemetryMissing']=fresh
        total=scalar(client,url,'sum(rate(portal_http_requests[5m]))')
        errors=scalar(client,url,'sum(rate(portal_http_requests{http_response_status_code=~"5.."}[5m])) or vector(0)')
        out['PortalHighServerErrorRate']=fresh and total is not None and total>0 and errors is not None and errors>=0 and errors/total<=.05
    except Exception: pass
    return out


def recovered(name,labels,snapshot):
    value=snapshot.get(name)
    if name=='MonitoringTargetDown':
        return isinstance(value,dict) and value.get(tuple(labels.get(k,'') for k in ('job','instance','check'))) is True
    return value is True


def mark_unknown(db,names=None):
    query=update(PrometheusAlertState)
    if names is not None: query=query.where(PrometheusAlertState.rule_name.in_(names))
    db.execute(query.values(state='unknown',normal_samples=0))


def record_rules(db,rules,recovery,at):
    mark_unknown(db,set(RULES)-set(rules))
    for name,rule in rules.items():
        sample=rule['evaluated'];active=rule['active']
        opened=db.scalars(select(PrometheusAlertState).join(Incident,Incident.id==PrometheusAlertState.incident_id).where(PrometheusAlertState.rule_name==name,Incident.status!='closed').with_for_update(of=PrometheusAlertState)).all()
        states={s.id:s for s in opened}
        for digest,alert in active.items():
            if digest not in states:
                state=db.scalar(select(PrometheusAlertState).where(PrometheusAlertState.id==digest).with_for_update())
                if state is None:
                    state=PrometheusAlertState(id=digest,target_key=alert['target'],rule_name=name,labels=alert['labels'])
                    db.add(state);db.flush()
                states[digest]=state
        for digest,state in states.items():
            if sample<=state.last_sample: continue
            alert=active.get(digest)
            status=alert['state'] if alert else 'normal' if recovered(name,state.labels,recovery) else 'unknown'
            consecutive=sample-state.last_sample<=60
            state.normal_samples=(state.normal_samples+1 if consecutive else 1) if status=='normal' else 0
            state.last_sample=sample;state.evaluated_at=datetime.fromtimestamp(sample,timezone.utc);state.state=status
            evidence=Evidence(id='prom:'+digest[:32]+':'+str(sample),target_id=state.target_key,kind='prometheus',occurred_at=state.evaluated_at,payload={'source':'prometheus','alert_name':name,'rule_id':RULES[name],'labels':state.labels,'severity':alert['severity'] if alert else 'warning','state':status,'evaluated_at':state.evaluated_at.isoformat()})
            db.add(evidence);db.flush()
            if status=='firing':
                incident=detect(db,SimpleNamespace(id=state.target_key),RULES[name],[evidence],False)
                state.incident_id=incident.id
            elif state.incident_id:
                incident=db.scalar(select(Incident).where(Incident.id==state.incident_id).with_for_update())
                if incident and incident.status!='closed':
                    from management.models import IncidentEvidence
                    db.add(IncidentEvidence(incident_id=incident.id,evidence_id=evidence.id))
                    if state.normal_samples>=2 and not db.scalar(select(Job).where(Job.incident_id==incident.id,Job.kind=='slack_recovery')):
                        db.add(Job(incident_id=incident.id,kind='slack_recovery'))

