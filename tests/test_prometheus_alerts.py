import json,unittest
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from unittest.mock import patch
import httpx
from sqlalchemy import create_engine,select,func
from sqlalchemy.orm import sessionmaker
from management.alerting import fetch_rules,identity,record_rules,mark_unknown,recovery_snapshot
from management.alert_policy import recovery_ready
from management.models import PrometheusAlertState,Incident,Job,Attempt,Evidence
from management.jobs import run_one,JobFailure
from portal.models import Base

class Client:
    def __init__(self,data): self.data=data
    def get(self,url,params):
        return httpx.Response(200,json=self.data,request=httpx.Request('GET',url))

class Fixture(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://');Base.metadata.create_all(self.engine)
        self.factory=sessionmaker(self.engine)
        self.at=datetime.now(timezone.utc)
        self.name='MonitoringTargetDown';self.labels={'job':'ec2-node','instance':'service:9100'}
    def tearDown(self): self.engine.dispose()
    def rules(self,at=None,state='firing',name=None):
        name=name or self.name;at=at or self.at
        digest,key,labels=identity(name,self.labels if name==self.name else {})
        active={} if state=='inactive' else {digest:{'id':digest,'target':key,'labels':labels,'state':state,'severity':'warning'}}
        return {name:{'evaluated':at.timestamp(),'active':active}}
    def apply(self,rules,recovery=None,at=None):
        with self.factory.begin() as db: record_rules(db,rules,recovery or {},at or self.at)
    def count(self,cls):
        with self.factory() as db: return db.scalar(select(func.count()).select_from(cls))
    def incident(self):
        with self.factory() as db:
            row=db.scalar(select(Incident));db.expunge(row);return row
    def healthy(self):return {self.name:{('ec2-node','service:9100',''):True}}
    def settings(self): return SimpleNamespace(ai_mode='demo',slack_mode='demo',ai_timeout_seconds=60,ai_model='',manage_public_url='https://manage.invalid')

class AlertTests(Fixture):
    def test_repeat_and_restart_deduplicate_incident_and_initial_jobs(self):
        self.apply(self.rules());self.apply(self.rules());self.apply(self.rules(self.at+timedelta(seconds=30)))
        self.assertEqual(self.count(Incident),1);self.assertEqual(self.count(Job),2);self.assertEqual(self.count(Evidence),2)
    def test_distinct_targets_do_not_collapse(self):
        self.apply(self.rules());self.labels={'job':'ec2-node','instance':'other:9100'};self.apply(self.rules(self.at+timedelta(seconds=30)))
        self.assertEqual(self.count(Incident),2)
    def test_pending_never_creates_an_incident(self):
        self.apply(self.rules(state='pending'));self.assertEqual(self.count(Incident),0)
    def test_absence_without_positive_metrics_is_unknown(self):
        self.apply(self.rules());at=self.at+timedelta(seconds=30);self.apply(self.rules(at,state='inactive'),at=at)
        with self.factory() as db:
            state=db.scalar(select(PrometheusAlertState));self.assertEqual(state.state,'unknown');self.assertEqual(state.normal_samples,0)
        self.assertEqual(self.count(Job),2)
    def test_two_distinct_normal_evaluations_queue_one_recovery_without_closing(self):
        self.apply(self.rules());at=self.at+timedelta(seconds=30)
        self.apply(self.rules(at,state='inactive'),self.healthy(),at);self.apply(self.rules(at,state='inactive'),self.healthy(),at)
        self.assertEqual(self.count(Job),2)
        at+=timedelta(seconds=30);self.apply(self.rules(at,state='inactive'),self.healthy(),at)
        self.assertEqual(self.count(Job),3);self.assertEqual(self.incident().status,'new')
        self.apply(self.rules(at+timedelta(seconds=30),state='inactive'),self.healthy(),at+timedelta(seconds=30));self.assertEqual(self.count(Job),3)
        with self.factory() as db:self.assertTrue(recovery_ready(db,self.incident(),at+timedelta(seconds=30)))
    def test_collection_failure_resets_normal_observations(self):
        self.apply(self.rules());at=self.at+timedelta(seconds=30);self.apply(self.rules(at,state='inactive'),self.healthy(),at)
        self.apply({},self.healthy(),at+timedelta(seconds=1))
        at+=timedelta(seconds=30);self.apply(self.rules(at,state='inactive'),self.healthy(),at);self.assertEqual(self.count(Job),2)
    def test_recovery_send_rechecks_current_state(self):
        self.apply(self.rules())
        for n in [30,60]:
            at=self.at+timedelta(seconds=n);self.apply(self.rules(at,state='inactive'),self.healthy(),at)
        self.apply(self.rules(self.at+timedelta(seconds=90)),at=self.at+timedelta(seconds=90))
        sent=[];run_one(self.factory,self.settings(),'slack_recovery',at=self.at+timedelta(seconds=91),perform=lambda *a:sent.append(a))
        self.assertEqual(sent,[])
    def test_slack_timeout_is_not_automatically_retried(self):
        self.apply(self.rules());sent=[]
        def fail(*a):sent.append(1);raise TimeoutError()
        run_one(self.factory,self.settings(),'slack',at=self.at+timedelta(seconds=1),perform=fail)
        run_one(self.factory,self.settings(),'slack',at=self.at+timedelta(seconds=60),perform=fail)
        self.assertEqual(len(sent),1)
        with self.factory() as db:
            job=db.scalar(select(Job).where(Job.kind=='slack'));self.assertEqual(job.status,'failed');self.assertEqual(job.error_code,'DELIVERY_UNKNOWN')
    def test_all_three_new_rules_and_no_business_target_rows(self):
        from management.alert_policy import RULES
        from management.models import Target
        for name in RULES:self.apply(self.rules(name=name))
        self.assertEqual(self.count(Incident),3);self.assertEqual(self.count(Target),0)
    def test_disabled_collection_does_not_fetch_rules(self):
        from management.worker import collect
        settings=self.settings();settings.prometheus_alerts_enabled=False;settings.prometheus_url='http://unit.invalid';settings.loki_url='http://unit.invalid'
        with patch('management.worker.fetch_rules') as fetch:collect(self.factory,settings,Client({}))
        fetch.assert_not_called()

class ParserTests(unittest.TestCase):
    def setUp(self):self.at=datetime.now(timezone.utc)
    def data(self,name='MonitoringTargetDown',health='ok',seconds=0):
        return {'status':'success','data':{'groups':[{'rules':[{'name':name,'type':'alerting','health':health,'lastEvaluation':(self.at+timedelta(seconds=seconds)).isoformat(),'alerts':[]}]}]}}
    def test_unhealthy_stale_or_missing_rule_is_not_recovery(self):
        for data in [self.data(health='err'),self.data(seconds=-90),self.data(seconds=30),{'status':'success','data':{'groups':[]}}]:self.assertEqual(fetch_rules(Client(data),'http://unit',self.at),{})
    def test_probe_rule_is_ignored_to_prevent_double_detection(self):
        self.assertEqual(fetch_rules(Client(self.data(name='PortalProbeFailed')),'http://unit',self.at),{})
    def test_rule_query_error_and_unsafe_labels_fail_closed(self):
        with self.assertRaises(ValueError):fetch_rules(Client({'status':'error'}),'http://unit',self.at)
        with self.assertRaises(ValueError):identity('MonitoringTargetDown',{'job':'unknown','instance':'secret'})
        with self.assertRaises(ValueError):identity('MonitoringTargetDown',{'job':'ec2-node','instance':'<@all>'})
    def test_recovery_requires_positive_fresh_data_not_empty_vector(self):
        data={'status':'success','data':{'resultType':'vector','result':[]}}
        result=recovery_snapshot(Client(data),'http://unit',self.at)
        self.assertFalse(result.get('PortalTelemetryMissing'));self.assertFalse(result.get('PortalHighServerErrorRate'))


class DeliveryTests(Fixture):
    # Reuse the fixture and exercise delivery independently of real webhooks.
    def test_recovery_is_delivered_only_once_per_incident(self):
        self.apply(self.rules())
        for n in [30,60]:
            at=self.at+timedelta(seconds=n);self.apply(self.rules(at,state='inactive'),self.healthy(),at)
        sent=[]
        run_one(self.factory,self.settings(),'slack_recovery',at=self.at+timedelta(seconds=61),perform=lambda *a:sent.append(a) or {'sent':False})
        run_one(self.factory,self.settings(),'slack_recovery',at=self.at+timedelta(seconds=62),perform=lambda *a:sent.append(a))
        self.assertEqual(len(sent),1)
    def test_expired_slack_lease_is_not_blindly_resent(self):
        self.apply(self.rules())
        with self.factory.begin() as db:
            job=db.scalar(select(Job).where(Job.kind=='slack'));job.status='running';job.attempts=1;job.lease_until=self.at-timedelta(seconds=1)
            db.add(Attempt(job_id=job.id,number=1,started_at=self.at-timedelta(seconds=50)))
        sent=[];run_one(self.factory,self.settings(),'slack',at=self.at,perform=lambda *a:sent.append(a))
        self.assertEqual(sent,[])
        with self.factory() as db:self.assertEqual(db.scalar(select(Job).where(Job.kind=='slack')).error_code,'DELIVERY_UNKNOWN')

class RecoveryMetricTests(unittest.TestCase):
    def test_fresh_rule_keeps_only_known_evidence_labels(self):
        at=datetime.now(timezone.utc)
        rule={'name':'MonitoringTargetDown','type':'alerting','health':'ok','lastEvaluation':at.isoformat(),'alerts':[{'state':'firing','labels':{'alertname':'MonitoringTargetDown','job':'ec2-node','instance':'unit:9100','severity':'warning','ignored':'untrusted'}}]}
        result=fetch_rules(Client({'status':'success','data':{'groups':[{'rules':[rule]}]}}),'http://unit',at)
        labels=next(iter(result['MonitoringTargetDown']['active'].values()))['labels']
        self.assertEqual(labels,{'job':'ec2-node','instance':'unit:9100'})
    def test_zero_request_volume_is_not_high_error_recovery(self):
        at=datetime.now(timezone.utc)
        class Metrics:
            def __init__(self,total):self.total=total
            def get(self,url,params):
                query=params['query'];value=at.timestamp() if 'timestamp(portal_' in query else self.total if query=='sum(rate(portal_http_requests[5m]))' else 0
                return httpx.Response(200,json={'status':'success','data':{'resultType':'vector','result':[{'metric':{},'value':[at.timestamp(),str(value)]}]}},request=httpx.Request('GET',url))
        self.assertFalse(recovery_snapshot(Metrics(0),'http://unit',at)['PortalHighServerErrorRate'])
        self.assertTrue(recovery_snapshot(Metrics(1),'http://unit',at)['PortalHighServerErrorRate'])
if __name__=='__main__':unittest.main()    def test_retired_blackbox_job_is_not_valid_alert_target(self):
        from management.alerting import JOBS
        self.assertNotIn('blackbox-exporter', JOBS)
        self.assertTrue({'portal-otel', 'portal-health', 'portal-db-health', 'ec2-node'} <= JOBS)
        with self.assertRaisesRegex(ValueError, 'UNKNOWN_SCRAPE_TARGET'):
            identity('MonitoringTargetDown', {'job': 'blackbox-exporter', 'instance': 'blackbox:9115'})


