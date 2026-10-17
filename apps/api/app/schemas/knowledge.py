import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class KnowledgeTextCreate(BaseModel):
    source_type: str = Field(pattern="^(faq|product|service|custom_text)$")
    title: str
    content: str


class KnowledgeWebsiteCreate(BaseModel):
    url: str


class KnowledgeDocumentOut(BaseModel):
    id: uuid.UUID
    source_type: str
    title: str
    status: str
    chunk_count: int
    error: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class KnowledgeSearchResult(BaseModel):
    content: str
    score: float
    document_title: str
    document_id: uuid.UUID
