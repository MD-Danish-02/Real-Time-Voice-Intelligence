import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPKMixin
from app.models.enums import CallStatus


class Call(Base, UUIDPKMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "calls"

    lead_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL"), nullable=True, index=True
    )

    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default=CallStatus.QUEUED.value, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    telephony_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_call_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    from_number: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    to_number: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # Dedicated Asterisk channel identifier (e.g. "1698547281.42") tracked separately from provider_call_id (Plivo Request UUID).
    # NOTE: Tracked on instance/session in Phase 1 to avoid UndefinedColumn error prior to migration.
    # Migration required before database persistence: ALTER TABLE calls ADD COLUMN asterisk_channel_id VARCHAR(255);
    @property
    def asterisk_channel_id(self) -> Optional[str]:
        return getattr(self, "_asterisk_channel_id", None)

    @asterisk_channel_id.setter
    def asterisk_channel_id(self, val: Optional[str]) -> None:
        self._asterisk_channel_id = val

    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    answered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    recording_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True)
    transcript_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True)

    end_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class CallEvent(Base, UUIDPKMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "call_events"

    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Transcript(Base, UUIDPKMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "transcripts"

    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), nullable=False, index=True)
    full_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class TranscriptSegment(Base, UUIDPKMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "transcript_segments"

    transcript_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transcripts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    speaker: Mapped[str] = mapped_column(String(16), nullable=False)  # ai | customer
    text: Mapped[str] = mapped_column(Text, nullable=False)
    started_at_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


class Recording(Base, UUIDPKMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "recordings"

    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), nullable=False, index=True)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retention_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class CallAnalysis(Base, UUIDPKMixin, TenantScopedMixin, TimestampMixin):
    """Structured, LLM-produced call intelligence, validated by a Pydantic schema before persistence."""
    __tablename__ = "call_analyses"

    call_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("calls.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    intent: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    lead_status: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    interest_level: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    sentiment: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    requirements_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    objections_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    questions_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    action_items_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meeting_requested: Mapped[bool] = mapped_column(default=False, nullable=False)
    follow_up_required: Mapped[bool] = mapped_column(default=False, nullable=False)
    next_action: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    callback_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
