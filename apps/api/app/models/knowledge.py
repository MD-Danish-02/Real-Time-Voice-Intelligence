import uuid
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPKMixin
from app.models.enums import KnowledgeStatus

# Embedding dimensionality - matches the configured embedding model
# (see app.providers.voice.llm / knowledge service). 384 fits small local
# sentence-transformers models used in the mock/dev embedding provider.
EMBEDDING_DIM = 384


class KnowledgeDocument(Base, UUIDPKMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "knowledge_documents"

    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    original_filename: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    storage_key: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    raw_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default=KnowledgeStatus.UPLOADED.value, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    uploaded_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True)


class KnowledgeChunk(Base, UUIDPKMixin, TenantScopedMixin, TimestampMixin):
    __tablename__ = "knowledge_chunks"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    embedding: Mapped[list] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
