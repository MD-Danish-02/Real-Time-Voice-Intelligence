import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPKMixin
from app.models.enums import CallingStatus


class Lead(Base, UUIDPKMixin, TenantScopedMixin, TimestampMixin):
    """
    Local, tenant-scoped mirror of a CRM lead/contact. The CRM remains the
    source of truth for CRM fields; this table is the operational record
    the platform actually calls and reasons about.
    """
    __tablename__ = "leads"

    crm_connection_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True, index=True)
    crm_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_lead_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    normalized_phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    company: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    crm_status: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    crm_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    source_metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    custom_fields_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "crm_provider", "provider_lead_id",
            name="uq_lead_tenant_provider_leadid",
        ),
    )


class LeadCallState(Base, UUIDPKMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "lead_call_state"

    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )

    calling_status: Mapped[str] = mapped_column(String(32), default=CallingStatus.NOT_CALLED.value, nullable=False)

    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_call_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True)
    last_call_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_call_result: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    next_call_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    callback_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    do_not_call: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    manual_do_not_call: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    eligible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Redis distributed lock key currently held for this lead, if any
    # (defense-in-depth alongside the DB unique constraint on active call_jobs).
    lock_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
