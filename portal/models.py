import uuid
from datetime import datetime, timezone
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Identity, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

def now():
    return datetime.now(timezone.utc)

class Base(DeclarativeBase):
    pass

class Employee(Base):
    __tablename__ = 'employees'
    employee_id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    employee_code: Mapped[str] = mapped_column(String(10), unique=True)
    name: Mapped[str] = mapped_column(String(50))
    department: Mapped[str] = mapped_column(String(50))
    position: Mapped[str] = mapped_column(String(30))
    email: Mapped[str] = mapped_column(String(100), unique=True)
    extension: Mapped[str | None] = mapped_column(String(10))
    employment_status: Mapped[str] = mapped_column(String(20), default='active', server_default='active')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        CheckConstraint("employment_status IN ('active','inactive')", name='ck_employee_status'),
        CheckConstraint('length(trim(name)) BETWEEN 1 AND 50', name='ck_employee_name'),
        CheckConstraint('length(trim(employee_code)) BETWEEN 1 AND 10', name='ck_employee_employee_code'),
        CheckConstraint('length(trim(department)) BETWEEN 1 AND 50', name='ck_employee_department'),
        CheckConstraint('length(trim(position)) BETWEEN 1 AND 30', name='ck_employee_position'),
        CheckConstraint('length(trim(email)) BETWEEN 1 AND 100', name='ck_employee_email'),
        Index('ix_employee_name_status', 'name', 'employment_status'),
    )

class UserAccount(Base):
    __tablename__ = 'user_accounts'
    user_id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey('employees.employee_id', ondelete='RESTRICT'), unique=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default='true')
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        CheckConstraint("role IN ('admin','user')", name='ck_user_role'),
        CheckConstraint('failed_attempts >= 0', name='ck_user_failed_attempts'),
        CheckConstraint('length(trim(username)) BETWEEN 1 AND 50', name='ck_username'),
    )

class LoginSession(Base):
    __tablename__ = 'sessions'
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(ForeignKey('user_accounts.user_id', ondelete='RESTRICT'), index=True)
    session_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint('expires_at > created_at', name='ck_session_expiry'),)
