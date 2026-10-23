import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from management import jobs
from management.models import Incident, Job
from portal.models import Base


class AIDeadlineTests(unittest.TestCase):
    def test_ai_uses_configured_timeout(self):
        settings = SimpleNamespace(
            ai_api_key='unit-key',
            ai_endpoint='https://unit.invalid',
            ai_timeout_seconds=60,
        )
        result = {
            'summary': 'unit',
            'hypotheses': [],
            'evidence_ids': ['e1'],
            'next_checks': [],
            'limitations': 'unit',
        }
        response = httpx.Response(200, json={
            'choices': [{'message': {'content': json.dumps(result)}}]
        })
        with patch.object(jobs, 'post_with_deadline', return_value=response) as post:
            actual = jobs.external(
                'ai', 'live', settings, {}, [{'id': 'e1'}], 'unit-model'
            )
        self.assertEqual(actual, result)
        self.assertEqual(post.call_args.args[1], 60)

    def test_total_deadline_stops_slow_request(self):
        class SlowClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, *args, **kwargs):
                await asyncio.sleep(0.05)

        with patch.object(jobs.httpx, 'AsyncClient', SlowClient):
            with self.assertRaises(TimeoutError):
                jobs.post_with_deadline('https://unit.invalid', 0.001)

    def test_slack_keeps_five_second_timeout(self):
        settings = SimpleNamespace(
            manage_public_url='http://unit.invalid',
            slack_webhook_url='https://hooks.slack.com/unit',
        )
        incident = {
            'id': 'i1', 'target_id': 'portal-login',
            'rule_id': 'login_failures', 'synthetic': True,
        }
        with patch.object(
            jobs, 'post_with_deadline',
            return_value=httpx.Response(200, text='ok')
        ) as post:
            self.assertEqual(
                jobs.external('slack', 'live', settings, incident, [], ''),
                {'sent': True},
            )
        self.assertEqual(post.call_args.args[1], 5)

    def test_ai_job_cannot_be_reclaimed_after_old_lease(self):
        engine = create_engine(
            'sqlite://',
            poolclass=StaticPool,
            connect_args={'check_same_thread': False},
        )
        try:
            Base.metadata.create_all(engine)
            factory = sessionmaker(engine, expire_on_commit=False)
            at = datetime(2026, 1, 1, tzinfo=timezone.utc)
            settings = SimpleNamespace(
                ai_mode='live', slack_mode='demo',
                ai_model='unit-model', ai_timeout_seconds=60,
            )

            with factory.begin() as db:
                incident = Incident(
                    target_id='portal-login', rule_id='login_failures',
                    synthetic=True, detected_at=at,
                )
                db.add(incident)
                db.flush()
                job = Job(incident_id=incident.id, kind='ai', next_at=at)
                db.add(job)
                db.flush()
                job_id = job.id

            def perform(*args):
                with factory() as db:
                    running = db.get(Job, job_id)
                    self.assertGreater(
                        jobs.utc(running.lease_until),
                        at + timedelta(seconds=60),
                    )
                claimed = jobs.run_one(
                    factory, settings, 'ai',
                    at=at + timedelta(seconds=46),
                    perform=lambda *args: self.fail('Duplicate AI execution'),
                )
                self.assertFalse(claimed)
                return {'summary': 'unit'}

            self.assertTrue(jobs.run_one(
                factory, settings, 'ai', at=at, perform=perform
            ))

            with factory() as db:
                completed = db.get(Job, job_id)
                self.assertEqual(completed.status, 'succeeded')
                self.assertEqual(completed.attempts, 1)
                followups = db.scalars(
                    select(Job).where(Job.kind == 'slack_ai')
                ).all()
                self.assertEqual(len(followups), 1)
        finally:
            engine.dispose()


if __name__ == '__main__':
    unittest.main()
