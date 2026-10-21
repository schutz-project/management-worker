from datetime import datetime
import uuid
from sqlalchemy import String, Integer, DateTime, Boolean, JSON, Text, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from portal.models import Base, now

def key(): return str(uuid.uuid4())
class Target(Base):
    __tablename__='monitor_targets'
    id: Mapped[str]=mapped_column(String(40),primary_key=True)
    last_sample: Mapped[float]=mapped_column(default=0.)
    last_checked_at: Mapped[datetime | None]=mapped_column(DateTime(timezone=True))
    collection_at: Mapped[datetime | None]=mapped_column(DateTime(timezone=True))
    last_ok: Mapped[bool | None]=mapped_column(Boolean)
    duration_ms: Mapped[float | None]=mapped_column()
    failures: Mapped[int]=mapped_column(default=0)
    slows: Mapped[int]=mapped_column(default=0)
    healthy: Mapped[int]=mapped_column(default=0)
    log_collection_at: Mapped[datetime | None]=mapped_column(DateTime(timezone=True))
    log_cursor: Mapped[datetime | None]=mapped_column(DateTime(timezone=True))
    security_cutoff: Mapped[datetime | None]=mapped_column(DateTime(timezone=True))

class Evidence(Base):
    __tablename__='monitor_evidence'
    id: Mapped[str]=mapped_column(String(100),primary_key=True)
    target_id: Mapped[str]=mapped_column(String(40),index=True)
    kind: Mapped[str]=mapped_column(String(20))
    occurred_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),index=True)
    collected_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
    payload: Mapped[dict]=mapped_column(JSON)

class Incident(Base):
    __tablename__='incidents'
    id: Mapped[str]=mapped_column(String(40),primary_key=True,default=key)
    target_id: Mapped[str]=mapped_column(String(40),index=True)
    rule_id: Mapped[str]=mapped_column(String(40))
    open_key: Mapped[str | None]=mapped_column(String(100),unique=True)
    status: Mapped[str]=mapped_column(String(20),default='new')
    detected_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
    synthetic: Mapped[bool]=mapped_column(Boolean,default=False)
    test_run_id: Mapped[str | None]=mapped_column(String(40))
    accepted_by: Mapped[int | None]=mapped_column(ForeignKey('user_accounts.user_id'))
    accepted_at: Mapped[datetime | None]=mapped_column(DateTime(timezone=True))
    closed_by: Mapped[int | None]=mapped_column(ForeignKey('user_accounts.user_id'))
    closed_at: Mapped[datetime | None]=mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None]=mapped_column(Text)
    version: Mapped[int]=mapped_column(Integer,default=1)

class IncidentEvidence(Base):
    __tablename__='incident_evidence'
    incident_id: Mapped[str]=mapped_column(ForeignKey('incidents.id'),primary_key=True)
    evidence_id: Mapped[str]=mapped_column(ForeignKey('monitor_evidence.id'),primary_key=True)

class Action(Base):
    __tablename__='incident_actions'
    id: Mapped[str]=mapped_column(String(40),primary_key=True,default=key)
    incident_id: Mapped[str]=mapped_column(ForeignKey('incidents.id'),index=True)
    kind: Mapped[str]=mapped_column(String(20))
    content: Mapped[str]=mapped_column(Text)
    actor_id: Mapped[int]=mapped_column(ForeignKey('user_accounts.user_id'))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)

class Job(Base):
    __tablename__='incident_jobs'
    id: Mapped[str]=mapped_column(String(40),primary_key=True,default=key)
    incident_id: Mapped[str]=mapped_column(ForeignKey('incidents.id'),index=True)
    kind: Mapped[str]=mapped_column(String(20))
    status: Mapped[str]=mapped_column(String(20),default='pending')
    mode: Mapped[str]=mapped_column(String(20),default='demo')
    attempts: Mapped[int]=mapped_column(Integer,default=0)
    next_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
    lease_until: Mapped[datetime | None]=mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None]=mapped_column(String(40))
    result: Mapped[dict | None]=mapped_column(JSON)
    input_evidence: Mapped[list | None]=mapped_column(JSON)
    error_code: Mapped[str | None]=mapped_column(String(40))
    model: Mapped[str | None]=mapped_column(String(100))
    prompt_version: Mapped[str | None]=mapped_column(String(40))
    __table_args__=(UniqueConstraint('incident_id','kind'),)

class Attempt(Base):
    __tablename__='job_attempts'
    id: Mapped[str]=mapped_column(String(40),primary_key=True,default=key)
    job_id: Mapped[str]=mapped_column(ForeignKey('incident_jobs.id'),index=True)
    number: Mapped[int]=mapped_column(Integer)
    started_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
    finished_at: Mapped[datetime | None]=mapped_column(DateTime(timezone=True))
    outcome: Mapped[str]=mapped_column(String(40),default='running')
    error_code: Mapped[str | None]=mapped_column(String(40))
