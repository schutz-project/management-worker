"""Bounded trial-only access-denial detection. No blocking or external calls."""
import hashlib
import json
import re
from datetime import datetime, timedelta
from sqlalchemy import select, func
from management.models import Evidence, Incident, Target

SECURITY_RULES = {'unauthorized_read', 'unauthorized_write'}
SECURITY_EVENTS = {'authentication_required', 'authorization_denied'}
READ_ROUTES = {'/api/employees', '/api/posts', '/api/notices'}
WRITE_ROUTES = {'/api/posts/{post_id}'}
THRESHOLD = 3


def classify(payload):
    if not isinstance(payload, dict) or payload.get('service') != 'portal-api':
        return None
    if any(not isinstance(payload.get(k), str) for k in ('event_type','reason_code','method','route')):
        return None
    if payload.get('account_ref') is not None and not isinstance(payload['account_ref'], str):
        return None
    if (payload.get('event_type') == 'authentication_required'
        and payload.get('reason_code') == 'AUTH_REQUIRED'
        and payload.get('status_code') == 401
        and payload.get('method') == 'GET' and payload.get('route') in READ_ROUTES):
        return 'unauthorized_read'
    if (payload.get('event_type') == 'authorization_denied'
        and payload.get('reason_code') == 'FORBIDDEN'
        and payload.get('status_code') == 403
        and payload.get('method') in {'PUT', 'DELETE'}
        and payload.get('route') in WRITE_ROUTES and payload.get('account_ref')):
        return 'unauthorized_write'
    return None


def collect_access_denials(db, logs, end, at):
    from management.domain import target, detect, utc, SAFE_KEYS
    touched = {}
    for payload in logs:
        rule = classify(payload)
        if rule is None or payload.get('synthetic') is not True:
            continue
        if not all(isinstance(payload.get(k), str)
                   and re.fullmatch(r'[A-Za-z0-9_-]{1,40}', payload[k])
                   for k in ('source_ref', 'test_run_id')):
            continue
        eid = payload.get('event_id')
        if not isinstance(eid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', eid):
            continue
        try:
            observed = datetime.fromisoformat(payload['occurred_at'].replace('Z', '+00:00'))
            if observed.utcoffset() is None:
                continue
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        if not at - timedelta(minutes=5) <= observed <= at:
            continue
        account = payload.get('account_ref') or 'anonymous'
        if not isinstance(account, str) or len(account) > 160:
            continue
        group = [rule, payload['test_run_id'], payload['source_ref'], account, payload['route']]
        key = 'sec:' + hashlib.sha256(json.dumps(group).encode()).hexdigest()[:32]
        state = target(db, key)
        if state.security_cutoff and observed <= utc(state.security_cutoff):
            continue
        if db.get(Evidence, 'event:' + eid):
            continue
        safe = {k: v[:160] if isinstance(v, str) else v
                for k, v in payload.items() if k in SAFE_KEYS
                and (v is None or type(v) in (str, int, float, bool))}
        db.add(Evidence(id='event:' + eid, target_id=key, kind='security',
                        occurred_at=observed, payload=safe))
        touched[key] = state
    db.flush()
    # Successful collection advances each active security target even on an empty batch.
    states = db.scalars(select(Target).join(Incident, Incident.target_id == Target.id).where(
        Incident.rule_id.in_(SECURITY_RULES), Incident.status != 'closed')).all()
    for state in {s.id: s for s in [*states, *touched.values()]}.values():
        state.log_collection_at = at
        state.log_cursor = end
        cutoff = max(at - timedelta(minutes=5), utc(state.security_cutoff) or at - timedelta(minutes=5))
        rows = db.scalars(select(Evidence).where(Evidence.target_id == state.id,
            Evidence.kind == 'security', Evidence.occurred_at > cutoff,
            Evidence.occurred_at <= at).order_by(Evidence.occurred_at, Evidence.id)).all()
        window = []
        for row in rows:
            window = [r for r in window if utc(r.occurred_at) >= utc(row.occurred_at) - timedelta(seconds=60)]
            window.append(row)
            if len(window) >= THRESHOLD:
                detect(db, state, classify(row.payload), window, True, row.payload['test_run_id'])


def closure_checks(db, incident, at):
    from management.domain import utc
    state = db.get(Target, incident.target_id)
    reasons = []
    if state is None or state.log_collection_at is None or not 0 <= (at - utc(state.log_collection_at)).total_seconds() <= 60:
        reasons.append('LOG_COLLECTION_STALE')
    last = db.scalar(select(func.max(Evidence.occurred_at)).where(
        Evidence.target_id == incident.target_id, Evidence.kind == 'security'))
    if last is None or (at - utc(last)).total_seconds() < 60:
        reasons.append('FAILURE_FREE_WINDOW_REQUIRED')
    if last is not None and state is not None and state.log_collection_at is not None and utc(state.log_collection_at) <= utc(last):
        reasons.append('LOG_COLLECTION_STALE')
    return reasons
