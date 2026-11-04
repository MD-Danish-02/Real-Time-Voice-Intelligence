import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPKMixin
from app.models.enums import CRMConnectionStatus, CRMEventStatus, SyncJobStatus


class CRMConnection(Base, UUIDPKMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "crm_connections"

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=CRMConnectionStatus.PENDING.value, nullable=False)

    # OAuth / API credentials - always stored encrypted at rest (see
    # app.core.crypto). Never returned to the frontend in full.
    encrypted_access_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    encrypted_refresh_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    external_account_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    external_account_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    field_mapping_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    webhook_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    webhook_subscription_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    webhook_secret: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    sync_cursor: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_attempted_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_successful_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    connected_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", name="uq_crm_connection_tenant_provider"),
    )


class CRMSyncJob(Base, UUIDPKMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "crm_sync_jobs"

    crm_connection_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)  # initial | incremental | reconciliation
    status: Mapped[str] = mapped_column(String(32), default=SyncJobStatus.QUEUED.value, nullable=False)

    pages_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_fetched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class CRMEvent(Base, UUIDPKMixin, TenantScopedMixin, TimestampMixin):
    """
    Every inbound webhook event is persisted here BEFORE processing, keyed
    on (tenant_id, provider, provider_event_id) so a duplicate delivery
    can never be processed twice.
    """
    __tablename__ = "crm_events"

    crm_connection_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)

    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=CRMEventStatus.RECEIVED.value, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider", "provider_event_id",
            name="uq_crm_event_tenant_provider_event",
        ),
    )
