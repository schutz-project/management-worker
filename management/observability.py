"""Fixed, admin-only observability queries with bounded safe output and cache."""
import asyncio,json,math,re,time
from datetime import datetime,timezone
import httpx

REQUESTS='sum(rate(portal_http_requests{job="portal-otel"}[2m]))'
METRICS={
 'requests':REQUESTS,
 'errors':'(sum(rate(portal_http_requests{job="portal-otel",http_response_status_code=~"5.."}[2m])) or vector(0)) and on() '+REQUESTS,
 'app_ms':'histogram_quantile(0.95,sum by (le) (rate(portal_http_duration_bucket{job="portal-otel"}[5m])))',
 'db_ms':'histogram_quantile(0.95,sum by (le) (rate(portal_db_duration_bucket{job="portal-otel"}[5m])))',
}
RESOURCE={
 'cpu':'100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{job="ec2-node",mode="idle"}[5m])))',
 'memory':'100 * (1 - node_memory_MemAvailable_bytes{job="ec2-node"} / node_memory_MemTotal_bytes{job="ec2-node"})',
 'disk':'max by (instance) (100 * (1 - node_filesystem_avail_bytes{job="ec2-node",mountpoint="/",fstype!~"tmpfs|overlay"} / node_filesystem_size_bytes{job="ec2-node",mountpoint="/",fstype!~"tmpfs|overlay"}))',
}
FIELDS={'timestamp','occurred_at','request_id','trace_id','span_id','event_type','route','method','status','status_code','duration_ms','db_duration_ms','request_time','upstream_response_time','upstream_status','reason_code','synthetic','test_run_id'}
ROUTES={'other','/','/health','/proxy-health','/internal/check/employee','/api/auth/context','/api/auth/login','/api/auth/logout','/api/employees','/api/notices','/api/posts','/api/posts/{post_id}','/api/dashboard','/api/observability','/api/incidents','/api/incidents/{id}','/api/incidents/{id}/accept','/api/incidents/{id}/actions','/api/incidents/{id}/close'}
def number(value):
    try: result=float(value)
    except (ValueError,TypeError,OverflowError): return None
    return result if math.isfinite(result) else None

def safe_log(raw):
    try: row=json.loads(raw)
    except (ValueError,TypeError): return None
    if not isinstance(row,dict): return None
    result={k:(v[:160] if isinstance(v,str) else v) for k,v in row.items() if k in FIELDS and (v is None or type(v) in (str,int,float,bool))}
    for key in ['request_id','trace_id','span_id']:
        if key in result and (not isinstance(result[key],str) or not re.fullmatch(r'[a-fA-F0-9-]{16,40}',result[key])): result.pop(key)
    if result.get('route') not in ROUTES: result['route']='other'
    for key,value in list(result.items()):
        if type(value) is float and not math.isfinite(value): result.pop(key)
    return result

class Observer:
    def __init__(self,settings):
        self.settings=settings;self.cached=None;self.cached_at=0;self.lock=asyncio.Lock()

    async def snapshot(self):
        async with self.lock:
            if self.cached is not None and time.monotonic()-self.cached_at<15: return self.cached
            async with httpx.AsyncClient(timeout=3,trust_env=False) as client:
                result=await self.collect(client)
            self.cached=result;self.cached_at=time.monotonic();return result

    async def collect(self,client,end=None):
        end=end or time.time(); start=end-900
        async def prom(expr,range_query=False):
            params={'query':expr,'start':start,'end':end,'step':30} if range_query else {'query':expr,'time':end}
            async with asyncio.timeout(4):
                response=await client.get(self.settings.prometheus_url.rstrip('/')+'/api/v1/'+('query_range' if range_query else 'query'),params=params)
                response.raise_for_status(); data=response.json()
                if data.get('status')!='success': raise ValueError('QUERY_FAILED')
                return data['data']['result']
        async def logs(service):
            async with asyncio.timeout(4):
                response=await client.get(self.settings.loki_url.rstrip('/')+'/loki/api/v1/query_range',params={'query':'{service="'+service+'"}','start':int((end-300)*1e9),'end':int(end*1e9),'limit':20,'direction':'backward'})
                response.raise_for_status();data=response.json()
                if data.get('status')!='success': raise ValueError('QUERY_FAILED')
                rows=[]
                for stream in data['data']['result']:
                    for stamp,raw in stream['values']:
                        safe=safe_log(raw)
                        if safe is not None: rows.append({'collected_at':datetime.fromtimestamp(int(stamp)/1e9,timezone.utc).isoformat(),'fields':safe})
                rows.sort(key=lambda row:row['collected_at'],reverse=True)
                return rows[:20]
        tasks={'source':prom('max(timestamp(portal_http_requests{job="portal-otel"}))'),'node_source':prom('max by (instance) (timestamp(node_memory_MemTotal_bytes{job="ec2-node"}))')}
        tasks.update({key:prom(expr,True) for key,expr in METRICS.items()});tasks.update({key:prom(expr) for key,expr in RESOURCE.items()})
        tasks.update({service:logs(service) for service in ['portal-api','portal-nginx']})
        results=await asyncio.gather(*tasks.values(),return_exceptions=True); data=dict(zip(tasks,results))
        def fresh(stamp):
            value=number(stamp);return value is not None and -5<=end-value<=60
        source=data['source'];source_ok=not isinstance(source,Exception)
        source_stamp=source[0]['value'][1] if source_ok and source else None
        source_fresh=fresh(source_stamp)
        metrics={}
        for key in METRICS:
            result=data[key];status='unavailable' if isinstance(result,Exception) or not source_ok else 'stale' if source_stamp is not None and not source_fresh else 'no_data'
            points=[]; value=None
            if not isinstance(result,Exception) and source_fresh and result:
                points=[[float(t)*1000,number(v)] for t,v in result[0].get('values',[])][-120:]
                if points and points[-1][0]/1000>=end-60: value=points[-1][1]
                if value is not None: status='ok'
            metrics[key]={'status':status,'value':value,'points':points}
        nodes={};source=data['node_source']
        if not isinstance(source,Exception):
            for row in source[:20]:
                instance=row['metric'].get('instance','unknown')[:120];nodes[instance]={'instance':instance,'status':'ok' if fresh(row['value'][1]) else 'stale','cpu':None,'memory':None,'disk':None}
        for key in RESOURCE:
            if not isinstance(data[key],Exception):
                for row in data[key][:20]:
                    instance=row['metric'].get('instance','unknown')[:120]
                    if instance in nodes and nodes[instance]['status']=='ok': nodes[instance][key]=number(row['value'][1])
        resource_status='unavailable' if isinstance(source,Exception) else 'ok' if nodes else 'no_data'
        return {'as_of':datetime.fromtimestamp(end,timezone.utc).isoformat(),'window_minutes':15,'metrics':metrics,'resources':{'status':resource_status,'items':list(nodes.values())},'logs':{service:{'status':'unavailable' if isinstance(data[service],Exception) else 'ok','items':[] if isinstance(data[service],Exception) else data[service]} for service in ['portal-api','portal-nginx']}}
