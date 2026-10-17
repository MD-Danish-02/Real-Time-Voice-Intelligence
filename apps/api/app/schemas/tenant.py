from datetime import time
from typing import Optional

from pydantic import BaseModel


class BusinessRulesOut(BaseModel):
    timezone: str
    calling_hours_start: time
    calling_hours_end: time
    calling_days_mask: int
    max_attempts: int
    retry_interval_minutes: int
    auto_call_enabled: bool
    recording_enabled: bool
    crm_auto_update_enabled: bool
    whatsapp_enabled: bool
    meeting_booking_enabled: bool
    callback_enabled: bool
    allowed_crm_statuses: str
    consent_required: bool

    class Config:
        from_attributes = True


class BusinessRulesUpdate(BaseModel):
    timezone: Optional[str] = None
    calling_hours_start: Optional[time] = None
    calling_hours_end: Optional[time] = None
    calling_days_mask: Optional[int] = None
    max_attempts: Optional[int] = None
    retry_interval_minutes: Optional[int] = None
    auto_call_enabled: Optional[bool] = None
    recording_enabled: Optional[bool] = None
    crm_auto_update_enabled: Optional[bool] = None
    whatsapp_enabled: Optional[bool] = None
    meeting_booking_enabled: Optional[bool] = None
    callback_enabled: Optional[bool] = None
    allowed_crm_statuses: Optional[str] = None
    consent_required: Optional[bool] = None
