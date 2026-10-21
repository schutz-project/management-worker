import json,time,logging
from datetime import datetime,timezone,timedelta
from threading import Event,Thread
import httpx
from sqlalchemy import select,text
from portal.models import now
from portal.db import build_engine,session_factory
from management.config import WorkerSettings
from management.models import Target
from management.domain import record_probe,record_logs,utc
from management.jobs import run_one

def vector(client,url,expr):
    r=client.get(url+'/api/v1/query',params={'query':expr}); r.raise_for_status(); data=r.json()
    if data.get('status')!='success': raise ValueError('QUERY_FAILED')
    return {item['metric'].get('check'):float(item['value'][1]) for item in data['data']['result']}

def loki_rows(client,url,start,end,depth=0):
    r=client.get(url+'/loki/api/v1/query_range',params={'query':'{service="portal-api"} | json | event_type="login_failed"','start':int(start.timestamp()*1e9),'end':int(end.timestamp()*1e9),'direction':'forward','limit':500}); r.raise_for_status()
    data=r.json()
    if data.get('status')!='success': raise ValueError('QUERY_FAILED')
    values=[v for stream in data['data']['result'] for v in stream['values']]
    if len(values)>=500:
        if depth>=12: raise ValueError('LOG_RANGE_TOO_DENSE')
        middle=start+(end-start)/2
        return loki_rows(client,url,start,middle,depth+1)+loki_rows(client,url,middle,end,depth+1)
    result=[]
    for _,raw in values:
        try: result.append(json.loads(raw))
        except ValueError: continue
    return result

def collect(factory,settings,client):
    current=now()
    try:
        success=vector(client,settings.prometheus_url,'probe_success{job=~"portal-health|portal-db-health"}')
        timestamps=vector(client,settings.prometheus_url,'timestamp(probe_success{job=~"portal-health|portal-db-health"})')
        durations=vector(client,settings.prometheus_url,'probe_duration_seconds{job=~"portal-health|portal-db-health"}')
        statuses=vector(client,settings.prometheus_url,'probe_http_status_code{job=~"portal-health|portal-db-health"}')
        with factory.begin() as db:
            for check in ['proxy','app','database']:
                if check not in success or check not in timestamps or check not in durations: continue
                sample=timestamps[check]
                if current.timestamp()-sample>60 or sample>current.timestamp()+5: continue
                record_probe(db,check,sample,success[check]==1,durations[check]*1000,at=datetime.fromtimestamp(sample,timezone.utc),status_code=int(statuses.get(check,0)),synthetic=True)
        logging.info('probe collection complete')
    except Exception: logging.warning('probe collection unavailable (details suppressed)')
    try:
        with factory() as db:
            row=db.get(Target,'portal-login'); cursor=utc(row.log_cursor) if row and row.log_cursor else current-timedelta(minutes=5)
        # Overlap accommodates delayed ingestion. Max 7 days matches Loki retention.
        start=max(cursor-timedelta(minutes=5),current-timedelta(days=7))
        rows=loki_rows(client,settings.loki_url,start,current)
        with factory.begin() as db: record_logs(db,rows,current,current)
        logging.info('login event collection complete')
    except Exception: logging.warning('login event collection unavailable (details suppressed)')

def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    settings=WorkerSettings(); engine=build_engine(settings.database_url); factory=session_factory(engine)
    stop=Event(); lock=None
    if engine.dialect.name=='postgresql':
        lock=engine.connect()
        if not lock.scalar(text('SELECT pg_try_advisory_lock(839201)')): raise RuntimeError('Another management worker owns the lock')
    def job_loop(kind):
        while not stop.is_set():
            try: run_one(factory,settings,kind)
            except Exception: logging.warning('%s job storage unavailable',kind)
            stop.wait(1)
    threads=[Thread(target=job_loop,args=(kind,),daemon=True) for kind in ['slack','ai','slack_ai']]
    for thread in threads: thread.start()
    try:
        with httpx.Client(timeout=5,trust_env=False) as client:
            while not stop.is_set():
                if lock is not None: lock.execute(text('SELECT 1'))
                started=time.monotonic(); collect(factory,settings,client)
                stop.wait(max(0,settings.check_interval-(time.monotonic()-started)))
    except KeyboardInterrupt: pass
    finally:
        stop.set()
        for thread in threads: thread.join(timeout=20)
        if lock: lock.close()
        engine.dispose()
if __name__=='__main__': main()
