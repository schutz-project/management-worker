import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
import httpx
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from portal.models import Base
from management.models import Incident, Evidence, IncidentEvidence, Job, Action, Target
from management.domain import record_logs, closure_reasons
from management.jobs import external, run_one


class SecurityDetectionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(self.engine)
        self.at = datetime.now(timezone.utc)

    def tearDown(self):
        self.engine.dispose()

    def event(self, n, write=False, **updates):
        row = dict(event_id=str(uuid.uuid4()), occurred_at=(self.at-timedelta(seconds=4-n)).isoformat(),
            service='portal-api', event_type='authorization_denied' if write else 'authentication_required',
            reason_code='FORBIDDEN' if write else 'AUTH_REQUIRED', status_code=403 if write else 401,
            route='/api/posts/{post_id}' if write else '/api/employees', method='PUT' if write else 'GET',
            account_ref='test_user' if write else None, source_ref='lab-a', test_run_id='run-a', synthetic=True)
        row.update(updates)
        return row

    def collect(self, rows, at=None):
        at = at or self.at
        with self.factory.begin() as db:
            record_logs(db, rows, at, at)

    def count(self, model):
        with self.factory() as db:
            return db.scalar(select(func.count()).select_from(model))

    def test_two_denials_no_incident_three_denials_one_incident(self):
        rows = [self.event(i) for i in range(3)]
        self.collect(rows[:2]); self.assertEqual(self.count(Incident), 0)
        self.collect(rows); self.assertEqual(self.count(Incident), 1)
        self.assertEqual(self.count(IncidentEvidence), 3)
        self.assertEqual(self.count(Job), 2)
        self.collect(rows); self.assertEqual(self.count(Incident), 1)
        self.assertEqual(self.count(Evidence), 3)

    def test_write_denial_is_separate_security_rule(self):
        self.collect([self.event(i, write=True) for i in range(3)])
        with self.factory() as db:
            incident = db.scalar(select(Incident))
            self.assertEqual(incident.rule_id, 'unauthorized_write')
            self.assertTrue(incident.synthetic)

    def test_groups_do_not_mix_source_account_run_route_or_rule(self):
        for field, values in [('source_ref', ['a','b']), ('account_ref', ['a','b']),
                              ('test_run_id', ['a','b']), ('route', ['/api/employees','/api/posts'])]:
            rows = [self.event(i, **{'test_run_id': 'run-'+field, field: values[i % 2]})
                    for i in range(3)]
            # Different run IDs across the cases prevent unrelated fixture mixing.
            self.collect(rows)
            self.assertEqual(self.count(Incident), 0, field)
        self.collect([self.event(0, test_run_id='read-write'), self.event(1, test_run_id='read-write'),
                      self.event(2, write=True, test_run_id='read-write')])
        self.assertEqual(self.count(Incident), 0)

    def test_more_than_sixty_seconds_does_not_trigger(self):
        self.collect([self.event(i, occurred_at=(self.at-timedelta(seconds=n)).isoformat())
                      for i,n in enumerate([0,31,62])])
        self.assertEqual(self.count(Incident), 0)

    def test_unsigned_malformed_future_old_or_unapproved_route_is_ignored(self):
        for updates in [dict(synthetic=False), dict(test_run_id=''), dict(source_ref='bad/label'),
                        dict(occurred_at='bad'), dict(occurred_at='2026-01-01T00:00:00'),
                        dict(occurred_at=(self.at+timedelta(seconds=1)).isoformat()),
                        dict(occurred_at=(self.at-timedelta(minutes=6)).isoformat()),
                        dict(route='/secret?token=raw'), dict(method=[]), dict(route={}), dict(account_ref=1), dict(status_code=200),
                        dict(reason_code='CSRF_INVALID'), dict(service='other')]:
            self.collect([self.event(i, **updates) for i in range(3)])
        self.assertEqual(self.count(Incident), 0)
        self.assertEqual(self.count(Evidence), 0)

    def test_unsafe_payload_is_not_stored(self):
        self.collect([self.event(i, password='secret', authorization='secret') for i in range(3)])
        with self.factory() as db:
            for row in db.scalars(select(Evidence)):
                self.assertNotIn('password', row.payload)
                self.assertNotIn('authorization', row.payload)
                self.assertEqual(row.payload['method'], 'GET')

    def test_security_closure_requires_quiet_fresh_collection_and_stopped_test(self):
        self.collect([self.event(i) for i in range(3)])
        with self.factory.begin() as db:
            incident = db.scalar(select(Incident)); incident.status = 'in_progress'
            db.add(Action(incident_id=incident.id, kind='action', content='operator checked', actor_id=1))
        with self.factory() as db:
            incident = db.scalar(select(Incident))
            reasons = closure_reasons(db, incident, self.at)
            self.assertIn('TEST_STOP_REQUIRED', reasons)
            self.assertIn('FAILURE_FREE_WINDOW_REQUIRED', reasons)
        later = self.at+timedelta(seconds=70)
        self.collect([], later)
        with self.factory() as db:
            incident = db.scalar(select(Incident))
            self.assertEqual(closure_reasons(db, incident, later, True), [])
            self.assertIn('LOG_COLLECTION_STALE', closure_reasons(db, incident, later+timedelta(seconds=61), True))

    def test_closed_trial_does_not_reopen_from_replayed_evidence(self):
        rows = [self.event(i) for i in range(3)]; self.collect(rows)
        with self.factory.begin() as db:
            incident = db.scalar(select(Incident)); incident.status='closed'; incident.open_key=None
            db.get(Target, incident.target_id).security_cutoff=self.at
        self.collect(rows, self.at+timedelta(seconds=2))
        self.assertEqual(self.count(Incident), 1)

    def test_new_events_after_closure_can_create_a_new_incident_without_old_evidence(self):
        rows = [self.event(i) for i in range(3)]; self.collect(rows)
        with self.factory.begin() as db:
            incident = db.scalar(select(Incident)); incident.status='closed'; incident.open_key=None
            db.get(Target, incident.target_id).security_cutoff=self.at
        later = self.at+timedelta(seconds=90)
        fresh = [self.event(i, occurred_at=(later-timedelta(seconds=3-i)).isoformat()) for i in range(3)]
        self.collect(fresh, later)
        self.assertEqual(self.count(Incident), 2)
        with self.factory() as db:
            current = db.scalar(select(Incident).where(Incident.status != 'closed'))
            linked = set(db.scalars(select(IncidentEvidence.evidence_id).where(IncidentEvidence.incident_id==current.id)))
            self.assertEqual(linked, {'event:'+r['event_id'] for r in fresh})

    def test_login_detection_is_preserved(self):
        self.collect([self.event(i, event_type='login_failed', reason_code='INVALID_CREDENTIALS',
            route='/api/auth/login', method='POST', account_ref='test_user') for i in range(5)])
        with self.factory() as db:
            self.assertEqual(db.scalar(select(Incident)).rule_id, 'login_failures')


class SecurityAIAndNotificationTests(unittest.TestCase):
    setUp = SecurityDetectionTests.setUp
    tearDown = SecurityDetectionTests.tearDown
    event = SecurityDetectionTests.event
    collect = SecurityDetectionTests.collect
    def test_ai_scope_and_prompt_do_not_equate_trial_to_broken_credentials(self):
        answer = dict(summary='권한 거부 기록', hypotheses=[], evidence_ids=['unit'],
                      next_checks=['시험 응답과 원본 데이터 비교'], limitations='추가 차단 미확인')
        settings=SimpleNamespace(ai_api_key='fake',ai_endpoint='https://example.invalid',ai_timeout_seconds=1)
        response=httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(answer)}}]})
        with patch('management.jobs.post_with_deadline', return_value=response) as send:
            external('ai','live',settings,{'rule_id':'unauthorized_write','synthetic':True},
                     [{'id':'unit','payload':{}}], 'fake')
        messages=send.call_args.kwargs['json']['messages']
        scope=json.loads(messages[1]['content'])['incident']['observation_scope']
        self.assertEqual(scope['data_modification'], 'unconfirmed')
        self.assertIn('Never advise reusing credentials', messages[0]['content'])
        self.assertIn('If no stronger evidence exists', messages[0]['content'])

    def test_closed_message_does_not_claim_recovery_or_blocking(self):
        settings=SimpleNamespace(manage_public_url='https://example.invalid')
        result=external('slack_closed','demo',settings,
            {'id':'unit','target_id':'sec:unit','rule_id':'unauthorized_read','synthetic':True}, [], '')
        self.assertIn('관리자가 사건을 종료',result['message'])
        self.assertIn('공격 차단을 자동으로 의미하지 않습니다',result['message'])
        self.assertFalse(result['sent'])

    def test_closed_job_cannot_send_for_open_incident(self):
        self.collect([self.event(i) for i in range(3)])
        with self.factory.begin() as db:
            incident=db.scalar(select(Incident))
            db.add(Job(incident_id=incident.id,kind='slack_closed',next_at=self.at))
        calls=[]
        run_one(self.factory,SimpleNamespace(),'slack_closed',at=self.at,perform=lambda *a:calls.append(a))
        self.assertEqual(calls,[])
        with self.factory() as db:
            self.assertEqual(db.scalar(select(Job).where(Job.kind=='slack_closed')).error_code,'INCIDENT_NOT_CLOSED')
