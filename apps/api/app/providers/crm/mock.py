"""
Deterministic, fully in-process CRM provider used for local development,
CI tests and demo mode. No network calls are made. This is what
CRM_PROVIDER_MODE=mock wires up by default so the whole platform is
runnable and testable without any real CRM credentials.
"""
import hashlib
import hmac
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.providers.crm.base import CRMAuthResult, CRMProvider, NormalizedLead, SyncPage

# A small fixed dataset representing "what's in the CRM". Calling
# initial_sync/fetch_leads against this provider always returns the same
# shape of data a real provider would, so downstream sync/eligibility/call
# logic can be exercised end-to-end in tests.
_MOCK_LEADS = [
    {"id": "mock-1001", "name": "Rahul Sharma", "phone": "+919876543210", "email": "rahul@example.com",
     "company": "Sharma Textiles", "status": "New"},
    {"id": "mock-1002", "name": "Neha Verma", "phone": "+919876543211", "email": "neha@example.com",
     "company": "Verma Retail", "status": "New"},
    {"id": "mock-1003", "name": "Amit Kumar", "phone": "+919876543212", "email": "amit@example.com",
     "company": "Kumar Logistics", "status": "Follow-up"},
    {"id": "mock-1004", "name": "Priya Singh", "phone": "+919876543213", "email": "priya@example.com",
     "company": "Singh Exports", "status": "New"},
]


class MockCRMProvider(CRMProvider):
    provider_name = "mock"

    def get_authorization_url(self, *, state: str) -> str:
        return f"/mock-crm/authorize?state={state}"

    async def exchange_code_for_token(self, *, code: str) -> CRMAuthResult:
        return CRMAuthResult(
            access_token=f"mock-access-{uuid.uuid4().hex[:12]}",
            refresh_token=f"mock-refresh-{uuid.uuid4().hex[:12]}",
            expires_at=None,
            external_account_id="mock-account-1",
            external_account_name="Mock CRM Sandbox",
        )

    async def refresh_access_token(self, *, refresh_token: str) -> CRMAuthResult:
        return CRMAuthResult(
            access_token=f"mock-access-{uuid.uuid4().hex[:12]}",
            refresh_token=refresh_token,
            expires_at=None,
            external_account_id="mock-account-1",
            external_account_name="Mock CRM Sandbox",
        )

    async def disconnect(self, *, access_token: str) -> None:
        return None

    def _to_normalized(self, raw: dict) -> NormalizedLead:
        return NormalizedLead(
            provider_lead_id=raw["id"],
            name=raw.get("name"),
            phone=raw.get("phone"),
            email=raw.get("email"),
            company=raw.get("company"),
            crm_status=raw.get("status"),
            crm_updated_at=datetime.now(timezone.utc),
            raw=raw,
        )

    async def initial_sync(self, *, access_token: str, page_size: int = 100) -> SyncPage:
        return SyncPage(
            leads=[self._to_normalized(r) for r in _MOCK_LEADS],
            next_cursor=None,
            has_more=False,
        )

    async def fetch_leads(
        self, *, access_token: str, cursor: Optional[str], updated_since=None
    ) -> SyncPage:
        return SyncPage(leads=[self._to_normalized(r) for r in _MOCK_LEADS], next_cursor=None, has_more=False)

    async def fetch_lead(self, *, access_token: str, provider_lead_id: str) -> Optional[NormalizedLead]:
        for raw in _MOCK_LEADS:
            if raw["id"] == provider_lead_id:
                return self._to_normalized(raw)
        return None

    async def register_webhooks(self, *, access_token: str, callback_url: str) -> str:
        return f"mock-webhook-sub-{uuid.uuid4().hex[:8]}"

    async def unregister_webhooks(self, *, access_token: str, subscription_id: str) -> None:
        return None

    def verify_webhook_signature(self, *, headers: dict, body: bytes, secret: str) -> bool:
        provided = headers.get("x-mock-signature", "")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(provided, expected)

    def extract_event_id(self, *, payload: dict) -> str:
        return payload.get("event_id") or f"generated-{uuid.uuid4().hex}"

    async def update_lead(self, *, access_token: str, provider_lead_id: str, fields: dict) -> None:
        return None

    async def create_note(self, *, access_token: str, provider_lead_id: str, note: str) -> None:
        return None

    async def create_activity(self, *, access_token: str, provider_lead_id: str, activity: dict) -> None:
        return None

    async def fetch_activities(self, *, access_token: str, provider_lead_id: str) -> list[dict]:
        return []
