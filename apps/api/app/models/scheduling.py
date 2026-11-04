import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPKMixin
from app.models.enums import CallbackStatus, CallJobStatus, MeetingStatus, WhatsAppMessageStatus


class CallJob(Base, UUIDPKMixin, TenantScopedMixin, TimestampMixin):
    """
    The call queue. A partial-unique constraint (enforced in the migration)
    guarantees only one active (pending/locked/dispatched) job can exist
    per lead at a time - this is the DB-level half of duplicate-call
    protection; the Redis lock in the eligibility engine is the other half.
    """
    __tablename__ = "call_jobs"

    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default=CallJobStatus.PENDING.value, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_call_job_tenant_idempotency"),
    )


class Callback(Base, UUIDPKMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "callbacks"

    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    call_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True)
    callback_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata", nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default=CallbackStatus.SCHEDULED.value, nullable=False)


class Meeting(Base, UUIDPKMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "meetings"

    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    call_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default=MeetingStatus.REQUESTED.value, nullable=False)
    calendar_provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    external_event_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    join_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)


class WhatsAppMessage(Base, UUIDPKMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "whatsapp_messages"

    lead_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True, index=True)
    call_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True)
    meeting_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True)
    to_number: Mapped[str] = mapped_column(String(32), nullable=False)
    template_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default=WhatsAppMessageStatus.QUEUED.value, nullable=False)
    provider_message_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class AutomationRule(Base, UUIDPKMixin, TenantScopedMixin, TimestampMixin):
    """Built-in event -> condition -> action automations, stored so they can be toggled per tenant."""
    __tablename__ = "automation_rules"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    trigger_event: Mapped[str] = mapped_column(String(64), nullable=False)
    condition_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    action_json: Mapped[str] = mapped_column(Text, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(default=True, nullable=False)


class AutomationRunLog(Base, UUIDPKMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "automation_run_logs"

    rule_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True, index=True)
    trigger_event: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    success: Mapped[bool] = mapped_column(nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
