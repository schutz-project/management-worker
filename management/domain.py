"""Detection and recovery rules; safe evidence, no outbound calls."""
from datetime import timezone, timedelta
from sqlalchemy import select, update, func
from management.models import Target, Evidence, Incident, IncidentEvidence, Job, Action
from portal.models import now

def utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value

def target(db,id):
    row=db.scalar(select(Target).where(Target.id==id).with_for_update())
    if row is None:
        row=Target(id=id); db.add(row); db.flush()
    return row

def detect(db,state,rule,evidence,synthetic=False,run_id=None):
    opened=db.scalar(select(Incident).where(Incident.open_key==state.id+':'+rule).with_for_update())
    if opened is None:
        opened=Incident(target_id=state.id,rule_id=rule,open_key=state.id+':'+rule,synthetic=synthetic,test_run_id=run_id)
        db.add(opened); db.flush()
        db.add_all([Job(incident_id=opened.id,kind='slack'),Job(incident_id=opened.id,kind='ai')])
    for e in evidence:
        if db.get(IncidentEvidence,(opened.id,e.id)) is None: db.add(IncidentEvidence(incident_id=opened.id,evidence_id=e.id))
    return opened

def record_probe(db,id,sample,ok,duration,at=None,status_code=None,synthetic=False):
    at=at or now(); state=target(db,id)
    # Re-evaluating the same scrape must not create three artificial failures.
    if sample<=state.last_sample: return
    state.last_sample=sample; state.last_checked_at=at; state.collection_at=at
    state.last_ok=ok; state.duration_ms=duration
    availability_failure=not ok and (status_code is None or status_code==0 or 500<=status_code<=599)
    state.failures=state.failures+1 if availability_failure else 0
    state.slows=state.slows+1 if ok and duration>2000 else 0
    state.healthy=state.healthy+1 if ok and duration<=2000 else 0
    row=Evidence(id='probe:'+id+':'+str(sample),target_id=id,kind='check',occurred_at=at,payload={'probe_success':ok,'status_code':status_code,'duration_ms':duration,'sample_timestamp':sample,'synthetic':synthetic})
    db.add(row); db.flush()
    for rule,count in [('availability_failure',state.failures),('response_delay',state.slows)]:
        if count>=3:
            rows=db.scalars(select(Evidence).where(Evidence.target_id==id,Evidence.kind=='check').order_by(Evidence.occurred_at.desc()).limit(3)).all()
            detect(db,state,rule,rows,synthetic,'probe-observation' if synthetic else None)

SAFE_KEYS={'event_id','occurred_at','service','event_type','request_id','trace_id','span_id','route','status_code','duration_ms','db_duration_ms','reason_code','synthetic','test_run_id','source_ref','account_ref','result_count'}
def record_logs(db,logs,end,at=None):
    at=at or now(); state=target(db,'portal-login')
    for log in logs:
        if not isinstance(log,dict) or log.get('event_type')!='login_failed': continue
        event_id=log.get('event_id')
        if not isinstance(event_id,str) or not 1<=len(event_id)<=80: continue
        eid='event:'+event_id
        if db.get(Evidence,eid): continue
        try:
            from datetime import datetime
            occurred=utc(datetime.fromisoformat(log['occurred_at'].replace('Z','+00:00')))
        except (ValueError,TypeError,KeyError): continue
        if occurred>at+timedelta(seconds=5): continue
        # Incoming event text is untrusted. Keep bounded known fields only.
        safe={k:v for k,v in log.items() if k in SAFE_KEYS and (v is None or type(v) in (str,int,float,bool))}
        safe={k:v[:160] if isinstance(v,str) else v for k,v in safe.items()}
        db.add(Evidence(id=eid,target_id='portal-login',kind='login',occurred_at=occurred,payload=safe))
    db.flush()
    state.log_collection_at=at; state.log_cursor=end
    cutoff=max(at-timedelta(minutes=5),utc(state.security_cutoff) or at-timedelta(minutes=5))
    rows=db.scalars(select(Evidence).where(Evidence.target_id=='portal-login',Evidence.occurred_at>cutoff).order_by(Evidence.occurred_at,Evidence.id)).all()
    groups={}
    for row in rows:
        e=row.payload
        if e.get('synthetic') is not True or not e.get('source_ref') or not e.get('account_ref'): continue
        group=(e['account_ref'],e['source_ref'])
        window=groups.setdefault(group,[])
        window[:]=[r for r in window if utc(r.occurred_at)>=utc(row.occurred_at)-timedelta(seconds=60)]
        window.append(row)
        if len(window)>=5:
            detect(db,state,'login_failures',window,True,e.get('test_run_id'))

def closure_reasons(db,incident,at=None,test_stopped=False):
    at=at or now(); reasons=[]
    if incident.status!='in_progress': reasons.append('ACTION_REQUIRED')
    if not db.scalar(select(func.count()).select_from(Action).where(Action.incident_id==incident.id,Action.kind=='action')): reasons.append('ACTION_REQUIRED')
    state=db.scalar(select(Target).where(Target.id==incident.target_id).with_for_update())
    if incident.rule_id=='login_failures':
        if not test_stopped: reasons.append('TEST_STOP_REQUIRED')
        if state is None or not state.log_collection_at or (at-utc(state.log_collection_at)).total_seconds()>60:
            reasons.append('LOG_COLLECTION_STALE')
        last=db.scalar(select(func.max(Evidence.occurred_at)).where(Evidence.target_id==incident.target_id,Evidence.kind=='login'))
        if last is None or (at-utc(last)).total_seconds()<60: reasons.append('FAILURE_FREE_WINDOW_REQUIRED')
    else:
        if state is None or state.healthy<2: reasons.append('TWO_HEALTHY_CHECKS_REQUIRED')
        if state is None or not state.collection_at or (at-utc(state.collection_at)).total_seconds()>60: reasons.append('CHECK_COLLECTION_STALE')
        # App/DB failure recovery must also have fresh successful monitoring samples.
    return sorted(set(reasons))

def change(db,incident,values):
    version=incident.version
    result=db.execute(update(Incident).where(Incident.id==incident.id,Incident.version==version).values(**values,version=version+1))
    if result.rowcount!=1: return False
    db.refresh(incident); return True
