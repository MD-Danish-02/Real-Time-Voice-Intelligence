import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPKMixin


class IntegrationCredential(Base, UUIDPKMixin, TenantScopedMixin, TimestampMixin):
    """Tenant-owned OAuth credentials for non-CRM integrations."""
    __tablename__ = "integration_credentials"
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_access_token: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_refresh_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    external_account_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    connected_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (UniqueConstraint("tenant_id", "provider", name="uq_integration_credentials_tenant_provider"),)
