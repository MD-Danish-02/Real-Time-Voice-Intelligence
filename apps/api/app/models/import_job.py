import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPKMixin


class LeadImportJob(Base, UUIDPKMixin, TenantScopedMixin, TimestampMixin):
    """
    Tracks one Excel/CSV lead-import end to end: uploaded -> previewed
    (sheets/columns detected) -> validated (rows checked, dupes found) ->
    importing (background task running) -> completed/failed. The
    uploaded file stays on disk (storage_key) until confirm/expiry so the
    background task can re-read it with the chosen column mapping.
    """
    __tablename__ = "lead_import_jobs"

    source_type: Mapped[str] = mapped_column(String(16), nullable=False)  # excel | csv
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    sheet_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="uploaded", nullable=False)

    column_mapping_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    total_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    valid_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    invalid_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicate_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    imported_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    errors_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # first N row-level errors, for display
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
