import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class CRMConnectRequest(BaseModel):
    provider: str
    # For non-OAuth providers (custom_api) or google_sheets config:
    config: Optional[dict] = None


class CRMOAuthCallback(BaseModel):
    code: str
    state: str


class CRMConnectionOut(BaseModel):
    id: uuid.UUID
    provider: str
    status: str
    external_account_name: Optional[str]
    webhook_enabled: bool
    last_successful_sync_at: Optional[datetime]
    last_attempted_sync_at: Optional[datetime] = None
    last_error: Optional[str]
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class CRMSyncJobOut(BaseModel):
    id: uuid.UUID
    job_type: str
    status: str
    records_fetched: int
    records_created: int
    records_updated: int
    records_failed: int
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    error: Optional[str]

    class Config:
        from_attributes = True
