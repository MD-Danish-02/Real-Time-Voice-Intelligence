import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CallOut(BaseModel):
    id: uuid.UUID
    lead_id: Optional[uuid.UUID]
    direction: str
    status: str
    attempt_number: int
    telephony_provider: str
    started_at: Optional[datetime]
    answered_at: Optional[datetime]
    ended_at: Optional[datetime]
    duration_seconds: Optional[int]

    class Config:
        from_attributes = True


class TranscriptSegmentOut(BaseModel):
    speaker: str
    text: str
    started_at_ms: int
    confidence: Optional[float]

    class Config:
        from_attributes = True


class CallAnalysisOut(BaseModel):
    summary: Optional[str]
    intent: Optional[str]
    lead_status: Optional[str]
    interest_level: Optional[str]
    sentiment: Optional[str]
    next_action: Optional[str]
    meeting_requested: bool
    follow_up_required: bool
    confidence: Optional[float]

    class Config:
        from_attributes = True


class CallDetailOut(CallOut):
    transcript_segments: list[TranscriptSegmentOut] = []
    analysis: Optional[CallAnalysisOut] = None


class OutboundDialRequest(BaseModel):
    # Plivo requires an E.164 destination; reject local/ambiguous dial strings.
    to_number: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")
    lead_id: Optional[uuid.UUID] = None
    custom_prompt: Optional[str] = None
