import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class LeadOut(BaseModel):
    id: uuid.UUID
    name: Optional[str]
    phone: Optional[str]
    email: Optional[str]
    company: Optional[str]
    crm_status: Optional[str]
    crm_provider: str
    created_at: datetime
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    source_type: Optional[str] = None
    calling_status: str = "not_called"
    attempt_count: int = 0
    do_not_call: bool = False
    eligible: bool = False

    class Config:
        from_attributes = True


class LeadCallStateOut(BaseModel):
    calling_status: str
    attempt_count: int
    last_call_at: Optional[datetime]
    last_call_result: Optional[str]
    next_call_at: Optional[datetime]
    callback_at: Optional[datetime]
    do_not_call: bool
    eligible: bool

    class Config:
        from_attributes = True


class LeadDetailOut(LeadOut):
    call_state: Optional[LeadCallStateOut]
