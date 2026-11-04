"""
CRM provider abstraction. Every CRM integration (HubSpot, Salesforce,
Zoho, Pipedrive, Google Sheets, Custom API, and the local Mock provider
used for development/testing) implements this same interface, so the
rest of the platform never contains provider-specific branching.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.providers.provider_safety import require_mock_provider


@dataclass
class NormalizedLead:
    provider_lead_id: str
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    company: Optional[str] = None
    crm_status: Optional[str] = None
    crm_updated_at: Optional[datetime] = None
    raw: dict = field(default_factory=dict)


@dataclass
class CRMAuthResult:
    access_token: str
    refresh_token: Optional[str]
    expires_at: Optional[datetime]
    external_account_id: Optional[str]
    external_account_name: Optional[str]


@dataclass
class SyncPage:
    leads: list[NormalizedLead]
    next_cursor: Optional[str]
    has_more: bool


class CRMProviderError(Exception):
    """Raised for any provider-side failure (auth, rate limit, network)."""

    def __init__(self, message: str, *, retryable: bool = True, status_code: Optional[int] = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class CRMProvider(ABC):
    """Base class every CRM adapter must implement."""

    provider_name: str = "base"

    def __new__(cls, *args, **kwargs):
        require_mock_provider(cls.provider_name, "CRM")
        return super().__new__(cls)

    # --- OAuth / connection lifecycle ---
    @abstractmethod
    def get_authorization_url(self, *, state: str) -> str:
        ...

    @abstractmethod
    async def exchange_code_for_token(self, *, code: str) -> CRMAuthResult:
        ...

    @abstractmethod
    async def refresh_access_token(self, *, refresh_token: str) -> CRMAuthResult:
        ...

    @abstractmethod
    async def disconnect(self, *, access_token: str) -> None:
        ...

    # --- Sync ---
    @abstractmethod
    async def initial_sync(self, *, access_token: str, page_size: int = 100) -> SyncPage:
        ...

    @abstractmethod
    async def fetch_leads(
        self, *, access_token: str, cursor: Optional[str], updated_since: Optional[datetime] = None
    ) -> SyncPage:
        ...

    @abstractmethod
    async def fetch_lead(self, *, access_token: str, provider_lead_id: str) -> Optional[NormalizedLead]:
        ...

    # --- Webhooks ---
    @abstractmethod
    async def register_webhooks(self, *, access_token: str, callback_url: str) -> str:
        """Returns the provider-side webhook subscription id."""
        ...

    @abstractmethod
    async def unregister_webhooks(self, *, access_token: str, subscription_id: str) -> None:
        ...

    @abstractmethod
    def verify_webhook_signature(self, *, headers: dict, body: bytes, secret: str) -> bool:
        ...

    @abstractmethod
    def extract_event_id(self, *, payload: dict) -> str:
        ...

    # --- Write-back ---
    @abstractmethod
    async def update_lead(self, *, access_token: str, provider_lead_id: str, fields: dict) -> None:
        ...

    @abstractmethod
    async def create_note(self, *, access_token: str, provider_lead_id: str, note: str) -> None:
        ...

    @abstractmethod
    async def create_activity(self, *, access_token: str, provider_lead_id: str, activity: dict) -> None:
        ...

    @abstractmethod
    async def fetch_activities(self, *, access_token: str, provider_lead_id: str) -> list[dict]:
        ...
