import json
import unittest
from datetime import timedelta
from unittest.mock import patch
from types import SimpleNamespace
import httpx
from sqlalchemy import select

from management.jobs import external, JobFailure, run_one
from management.models import Job
from test_prometheus_alerts import Fixture


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.settings=SimpleNamespace(ai_api_key='fake',ai_endpoint='https://example.invalid',ai_timeout_seconds=60)
        self.evidence=[{'id':'prom:unit:1','kind':'prometheus','payload':{'state':'firing'}}]
        self.answer={'summary':'기록 시점에 메트릭 수집 실패. 실제 서비스 중단은 미확인입니다.',
                     'hypotheses':['수집 경로 오류 가능성'], 'evidence_ids':['prom:unit:1'],
                     'next_checks':['기능 점검과 수집 오류를 비교 확인'], 'limitations':'컨테이너 상태 근거 없음'}

    def response(self):
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(self.answer)}}]})

    def test_scrape_scope_is_explicit_and_does_not_mutate_stored_incident(self):
        incident={'id':'unit','rule_id':'monitoring_target_down','target_id':'pa-unit'}
        original=dict(incident)
        with patch('management.jobs.post_with_deadline',return_value=self.response()) as send:
            self.assertEqual(external('ai','live',self.settings,incident,self.evidence,'fake'),self.answer)
        messages=send.call_args.kwargs['json']['messages']
        payload=json.loads(messages[1]['content'])
        scope=payload['incident']['observation_scope']
        self.assertEqual(scope['service_availability'],'unknown')
        self.assertEqual(scope['container_running'],'unknown')
        self.assertEqual(scope['cause'],'unknown')
        self.assertEqual(payload['evidence'],self.evidence)
        self.assertEqual(incident,original)
        self.assertIn('not a stopped application',messages[0]['content'])
        self.assertIn('functional probe',messages[0]['content'])
        self.assertIn('Korean',messages[0]['content'])

    def test_other_rules_keep_original_payload_without_scrape_inference(self):
        incident={'id':'unit','rule_id':'login_failures','target_id':'portal-login'}
        with patch('management.jobs.post_with_deadline',return_value=self.response()) as send:
            external('ai','live',self.settings,incident,self.evidence,'fake')
        messages=send.call_args.kwargs['json']['messages']
        self.assertEqual(json.loads(messages[1]['content'])['incident'],incident)
        self.assertNotIn('For MonitoringTargetDown',messages[0]['content'])

    def test_output_must_still_cite_supplied_evidence(self):
        self.answer['evidence_ids']=['invented']
        with patch('management.jobs.post_with_deadline',return_value=self.response()):
            with self.assertRaises(JobFailure) as error:
                external('ai','live',self.settings,{'rule_id':'monitoring_target_down'},self.evidence,'fake')
        self.assertEqual(error.exception.code,'AI_EVIDENCE_INVALID')


class VersionTests(Fixture):
    def test_prompt_version_is_saved_with_normal_ai_job_processing(self):
        self.apply(self.rules())
        run_one(self.factory,self.settings(),'ai',at=self.at+timedelta(seconds=1),perform=lambda *args:{'summary':'모의'})
        with self.factory() as db:
            job=db.scalar(select(Job).where(Job.kind=='ai'))
            self.assertEqual(job.prompt_version,'evidence-scrape-scope-v2')


if __name__=='__main__':unittest.main()
