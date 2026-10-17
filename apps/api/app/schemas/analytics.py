from typing import Optional

from pydantic import BaseModel


class CallsAnalytics(BaseModel):
    total: int
    inbound: int
    outbound: int
    answered: int
    missed: int
    failed: int
    average_duration_seconds: Optional[float]


class LeadsAnalytics(BaseModel):
    total: int
    new: int
    contacted: int
    interested: int
    not_interested: int
    callback: int
    meeting_requested: int
    converted: int


class MeetingsAnalytics(BaseModel):
    requested: int
    booked: int
    completed: int
    canceled: int


class WhatsAppAnalytics(BaseModel):
    sent: int
    delivered: int
    read: int
    failed: int


class CRMSyncAnalytics(BaseModel):
    imported: int
    new_detected: int
    updated: int
    sync_failures: int
    last_sync_at: Optional[str]


class AnalyticsOverview(BaseModel):
    calls: CallsAnalytics
    leads: LeadsAnalytics
    meetings: MeetingsAnalytics
    whatsapp: WhatsAppAnalytics
    crm_sync: CRMSyncAnalytics
