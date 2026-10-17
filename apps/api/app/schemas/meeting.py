import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class MeetingOut(BaseModel):
    id: uuid.UUID
    lead_id: uuid.UUID
    title: str
    scheduled_at: Optional[datetime]
    duration_minutes: int
    status: str
    join_url: Optional[str]

    class Config:
        from_attributes = True


class MeetingCreateRequest(BaseModel):
    lead_id: uuid.UUID
    title: str
    scheduled_at: datetime
    duration_minutes: int = 30
