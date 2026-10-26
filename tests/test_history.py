import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from portal.models import Base
from management.models import Incident, Action, Evidence, IncidentEvidence, Job
from management.history import historical_context, safe_note
from management.jobs import run_one, external, JobFailure


class HistoryFixture(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(self.engine)
        self.at = datetime.now(timezone.utc)
        with self.factory.begin() as db:
            current = Incident(target_id='app', rule_id='availability_failure',
                               synthetic=False, detected_at=self.at)
            db.add(current); db.flush(); self.current_id = current.id
            evidence = Evidence(id='current', target_id='app', kind='check',
                                occurred_at=self.at, payload={'status_code': 500})
            db.add(evidence); db.flush()
            db.add(IncidentEvidence(incident_id=current.id, evidence_id='current'))
            db.add(Job(incident_id=current.id, kind='ai', next_at=self.at))

    def tearDown(self):
        self.engine.dispose()

    def prior(self, days=1, target='app', rule='availability_failure',
              synthetic=False, status='closed', action=True, late=False):
        with self.factory.begin() as db:
            end = self.at - timedelta(days=days)
            case = Incident(target_id=target, rule_id=rule, synthetic=synthetic,
                            status=status, detected_at=end-timedelta(minutes=2),
                            closed_at=end if status == 'closed' else None)
            db.add(case); db.flush(); key = case.id
            if action:
                db.add(Action(incident_id=key, kind='action', actor_id=1,
                              content='설정을 확인하고 서비스 상태를 재점검함',
                              created_at=end+timedelta(seconds=1 if late else -1)))
            return key

    def context(self):
        with self.factory() as db:
            return historical_context(db, db.get(Incident, self.current_id))

    def settings(self, enabled=True):
        return SimpleNamespace(ai_history_enabled=enabled, ai_mode='demo',
                               ai_model='', ai_timeout_seconds=60, slack_mode='demo')

class HistoryTests(HistoryFixture):
    def test_filters_scope_status_age_and_missing_actions(self):
        wanted = self.prior()
        for kw in [dict(target='database'), dict(rule='response_delay'),
                   dict(synthetic=True), dict(status='new'), dict(days=91),
                   dict(days=-1), dict(days=0), dict(action=False), dict(late=True)]:
            self.prior(**kw)
        context = self.context()
        self.assertEqual([r['id'] for r in context], ['history:'+wanted])
        self.assertEqual(context[0]['kind'], 'historical_context')
        self.assertNotIn('actor_id', json.dumps(context))

    def test_limit_three_newest_and_three_notes(self):
        keys = [self.prior(days=n) for n in range(1, 6)]
        with self.factory.begin() as db:
            for n in range(10):
                db.add(Action(incident_id=keys[0], kind='action', actor_id=1,
                              content='x'*1000, created_at=self.at-timedelta(days=1, seconds=n+2)))
        context = self.context()
        self.assertEqual([r['id'] for r in context], ['history:'+k for k in keys[:3]])
        self.assertEqual(len(context[0]['payload']['action_notes']), 3)
        self.assertTrue(all(len(n)<=500 for n in context[0]['payload']['action_notes']))

    def test_no_history_is_empty(self):
        self.assertEqual(self.context(), [])

    def test_synthetic_current_only_uses_synthetic_history(self):
        self.prior(); expected = self.prior(synthetic=True)
        with self.factory.begin() as db:
            db.get(Incident, self.current_id).synthetic = True
        self.assertEqual(self.context()[0]['id'], 'history:'+expected)

    def test_redacts_known_secrets_and_urls(self):
        result = safe_note('password="hello world" token=abc Bearer xyz https://site/path?q=secret\x00')
        for secret in ['hello', 'abc', 'xyz', 'site', '\x00']:
            self.assertNotIn(secret, result)

    def test_default_setting_is_disabled(self):
        from management.config import WorkerSettings
        self.assertFalse(WorkerSettings(_env_file=None, database_url='sqlite://').ai_history_enabled)

    def test_disabled_leaves_current_evidence_only(self):
        self.prior(); captured = []
        run_one(self.factory, self.settings(False), 'ai', at=self.at,
                perform=lambda *args: captured.extend(args[4]) or {})
        self.assertEqual([r['id'] for r in captured], ['current'])

    def test_ai_retry_reuses_frozen_history_and_version(self):
        first = self.prior(days=2); captured = []
        def fail(*args):
            captured.append(args[4]); raise JobFailure('AI_TIMEOUT')
        run_one(self.factory, self.settings(), 'ai', at=self.at, perform=fail)
        self.prior(days=1)
        run_one(self.factory, self.settings(False), 'ai', at=self.at+timedelta(seconds=6), perform=fail)
        self.assertEqual(captured[0], captured[1])
        self.assertEqual(captured[0][1]['id'], 'history:'+first)
        with self.factory() as db:
            self.assertEqual(db.scalar(select(Job)).prompt_version, 'evidence-history-scrape-v2')

    def test_slack_does_not_receive_history(self):
        self.prior()
        with self.factory.begin() as db:
            db.add(Job(incident_id=self.current_id, kind='slack', next_at=self.at))
        captured = []
        run_one(self.factory, self.settings(), 'slack', at=self.at,
                perform=lambda *args: captured.extend(args[4]) or {})
        self.assertEqual([r['id'] for r in captured], ['current'])

    def test_no_current_evidence_does_not_add_history(self):
        self.prior()
        with self.factory.begin() as db:
            db.query(IncidentEvidence).delete()
        captured = []
        run_one(self.factory, self.settings(), 'ai', at=self.at,
                perform=lambda *args: captured.extend(args[4]) or {})
        self.assertEqual(captured, [])

    def test_live_payload_and_current_citation_requirement(self):
        self.prior(); evidence = [{'id':'current','kind':'check','payload':{}}]+self.context()
        settings = SimpleNamespace(ai_api_key='fake', ai_endpoint='https://example.invalid', ai_timeout_seconds=60)
        answer = dict(summary='관측 사실과 가설을 구분함', hypotheses=[],
                      evidence_ids=[evidence[1]['id']], next_checks=[], limitations='과거 조치 효과 미검증')
        def response():
            return httpx.Response(200, json={'choices':[{'message':{'content':json.dumps(answer)}}]})
        with patch('management.jobs.post_with_deadline', return_value=response()) as send:
            with self.assertRaises(JobFailure) as raised:
                external('ai','live',settings,{},evidence,'fake')
            self.assertEqual(raised.exception.code, 'AI_CURRENT_EVIDENCE_REQUIRED')
            prompt = send.call_args.kwargs['json']['messages'][0]['content']
            self.assertIn('not current evidence', prompt)
        answer['evidence_ids'].append('current')
        with patch('management.jobs.post_with_deadline', return_value=response()):
            self.assertEqual(external('ai','live',settings,{},evidence,'fake'), answer)


class CombinedScopeTests(HistoryFixture):
    def test_scrape_scope_and_history_are_both_sent_without_current_outage_claim(self):
        with self.factory.begin() as db:
            db.get(Incident,self.current_id).rule_id='monitoring_target_down'
        key=self.prior(rule='monitoring_target_down')
        settings=self.settings()
        settings.ai_mode='live';settings.ai_api_key='fake'
        settings.ai_endpoint='https://example.invalid';settings.ai_model='fake'
        answer=dict(summary='기록 시점의 수집 실패이며 실제 중단은 미확인입니다.',
                    hypotheses=[], evidence_ids=['current','history:'+key],
                    next_checks=[], limitations='과거 조치 효과는 미검증입니다.')
        response=httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(answer)}}]})
        with patch('management.jobs.post_with_deadline',return_value=response) as send:
            run_one(self.factory,settings,'ai',at=self.at)
        messages=send.call_args.kwargs['json']['messages']
        payload=json.loads(messages[1]['content'])
        self.assertEqual(payload['incident']['observation_scope']['service_availability'],'unknown')
        self.assertIn('not a stopped application',messages[0]['content'])
        self.assertIn('not current evidence',messages[0]['content'])
        self.assertIn('Korean',messages[0]['content'])
        self.assertEqual(payload['evidence'][1]['id'],'history:'+key)
        with self.factory() as db:
            job=db.scalar(select(Job).where(Job.kind=='ai'))
            self.assertEqual(job.status,'succeeded')
            self.assertEqual(job.prompt_version,'evidence-history-scrape-v2')

    def test_disabled_history_still_preserves_pr8_scope(self):
        self.prior()
        settings=SimpleNamespace(ai_api_key='fake',ai_endpoint='https://example.invalid',ai_timeout_seconds=60)
        answer=dict(summary='수집 실패·서비스 상태 미확인',hypotheses=[],evidence_ids=['current'],next_checks=[],limitations='근거 제한')
        response=httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(answer)}}]})
        evidence=[{'id':'current','kind':'check','payload':{}}]
        with patch('management.jobs.post_with_deadline',return_value=response) as send:
            external('ai','live',settings,{'rule_id':'monitoring_target_down'},evidence,'fake')
        payload=json.loads(send.call_args.kwargs['json']['messages'][1]['content'])
        self.assertEqual(payload['evidence'],evidence)
        self.assertEqual(payload['incident']['observation_scope']['container_running'],'unknown')

    def test_enabling_history_does_not_retrofit_a_saved_input(self):
        captured=[]
        def fail(*args):
            captured.append(args[4]);raise JobFailure('AI_TIMEOUT')
        run_one(self.factory,self.settings(False),'ai',at=self.at,perform=fail)
        self.prior()
        run_one(self.factory,self.settings(True),'ai',at=self.at+timedelta(seconds=6),perform=fail)
        self.assertEqual(captured[0],captured[1])
        self.assertEqual([r['id'] for r in captured[1]],['current'])
        with self.factory() as db:
            self.assertEqual(db.scalar(select(Job)).prompt_version,'evidence-scrape-scope-v2')

    def test_ninety_day_boundary_and_invalid_case_chronology(self):
        expected=self.prior(days=90)
        invalid=self.prior(days=1)
        with self.factory.begin() as db:
            db.get(Incident,invalid).detected_at=self.at
        self.assertEqual([r['id'] for r in self.context()],['history:'+expected])


if __name__ == '__main__':
    unittest.main()

