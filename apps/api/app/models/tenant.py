import uuid
from datetime import time

from sqlalchemy import Boolean, Integer, String, Time
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPKMixin


class Tenant(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    plan: Mapped[str] = mapped_column(String(50), default="trial", nullable=False)


class TenantBusinessRules(Base, UUIDPKMixin, TimestampMixin):
    """
    Configurable business rules per tenant. This table is the single
    source of truth the Eligibility Engine and Automation Engine read
    from - nothing here may be hard-coded into route handlers.
    """
    __tablename__ = "tenant_business_rules"

    tenant_id: Mapped[uuid.UUID] = mapped_column(unique=True, nullable=False, index=True)

    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata", nullable=False)
    calling_hours_start: Mapped[time] = mapped_column(Time, default=time(9, 0), nullable=False)
    calling_hours_end: Mapped[time] = mapped_column(Time, default=time(19, 0), nullable=False)
    calling_days_mask: Mapped[int] = mapped_column(
        Integer, default=0b0111110, nullable=False
    )  # bit0=Sunday .. bit6=Saturday; default Mon-Fri

    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    retry_interval_minutes: Mapped[int] = mapped_column(Integer, default=240, nullable=False)

    auto_call_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    recording_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    crm_auto_update_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    whatsapp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    meeting_booking_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    callback_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # comma-separated list of CRM statuses this tenant allows calling for;
    # empty string means "all statuses except explicit DNC lists"
    allowed_crm_statuses: Mapped[str] = mapped_column(String(1000), default="", nullable=False)

    consent_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
