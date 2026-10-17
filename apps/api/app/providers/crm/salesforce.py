"""
Real Salesforce integration using the standard OAuth2 web-server flow and
the Salesforce REST API (sObjects: Lead). Same disclaimer as hubspot.py:
implemented against Salesforce's documented API contract, not live-tested
here (no network path to salesforce.com in this sandbox).
"""
import hashlib
import hmac
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.core.config import settings
from app.providers.crm.base import (
    CRMAuthResult, CRMProvider, CRMProviderError, NormalizedLead, SyncPage,
)

SF_AUTH_URL = "https://login.salesforce.com/services/oauth2/authorize"
SF_TOKEN_URL = "https://login.salesforce.com/services/oauth2/token"


class SalesforceCRMProvider(CRMProvider):
    provider_name = "salesforce"

    def get_authorization_url(self, *, state: str) -> str:
        params = {
            "response_type": "code",
            "client_id": settings.SALESFORCE_CLIENT_ID,
            "redirect_uri": settings.SALESFORCE_REDIRECT_URI,
            "scope": "api refresh_token",
            "state": state,
        }
        return f"{SF_AUTH_URL}?{urllib.parse.urlencode(params)}"

    async def exchange_code_for_token(self, *, code: str) -> CRMAuthResult:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(SF_TOKEN_URL, data={
                "grant_type": "authorization_code",
                "client_id": settings.SALESFORCE_CLIENT_ID,
                "client_secret": settings.SALESFORCE_CLIENT_SECRET,
                "redirect_uri": settings.SALESFORCE_REDIRECT_URI,
                "code": code,
            })
            if resp.status_code >= 400:
                raise CRMProviderError(f"Salesforce token exchange failed: {resp.text}", status_code=resp.status_code)
            data = resp.json()
            return CRMAuthResult(
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token"),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
                external_account_id=data.get("id"),
                external_account_name=data.get("instance_url"),
            )

    async def refresh_access_token(self, *, refresh_token: str) -> CRMAuthResult:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(SF_TOKEN_URL, data={
                "grant_type": "refresh_token",
                "client_id": settings.SALESFORCE_CLIENT_ID,
                "client_secret": settings.SALESFORCE_CLIENT_SECRET,
                "refresh_token": refresh_token,
            })
            if resp.status_code >= 400:
                raise CRMProviderError(f"Salesforce token refresh failed: {resp.text}", status_code=resp.status_code)
            data = resp.json()
            return CRMAuthResult(
                access_token=data["access_token"],
                refresh_token=refresh_token,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
                external_account_id=data.get("id"),
                external_account_name=data.get("instance_url"),
            )

    async def disconnect(self, *, access_token: str) -> None:
        return None

    def _instance_url(self, external_account_name: Optional[str]) -> str:
        return external_account_name or "https://login.salesforce.com"

    def _map_lead(self, raw: dict) -> NormalizedLead:
        return NormalizedLead(
            provider_lead_id=raw["Id"],
            name=raw.get("Name") or f"{raw.get('FirstName', '')} {raw.get('LastName', '')}".strip(),
            phone=raw.get("Phone"),
            email=raw.get("Email"),
            company=raw.get("Company"),
            crm_status=raw.get("Status"),
            crm_updated_at=datetime.fromisoformat(raw["LastModifiedDate"].replace("Z", "+00:00"))
            if raw.get("LastModifiedDate") else None,
            raw=raw,
        )

    async def initial_sync(self, *, access_token: str, page_size: int = 100) -> SyncPage:
        return await self.fetch_leads(access_token=access_token, cursor=None)

    async def fetch_leads(
        self, *, access_token: str, cursor: Optional[str], updated_since: Optional[datetime] = None,
        instance_url: str = "https://login.salesforce.com",
    ) -> SyncPage:
        soql = "SELECT Id,Name,FirstName,LastName,Phone,Email,Company,Status,LastModifiedDate FROM Lead"
        if updated_since:
            soql += f" WHERE LastModifiedDate > {updated_since.isoformat()}"
        url = cursor or f"{instance_url}/services/data/v59.0/query?{urllib.parse.urlencode({'q': soql})}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
            if resp.status_code >= 400:
                raise CRMProviderError(f"Salesforce fetch_leads failed: {resp.text}", status_code=resp.status_code)
            data = resp.json()
            leads = [self._map_lead(r) for r in data.get("records", [])]
            next_url = data.get("nextRecordsUrl")
            return SyncPage(leads=leads, next_cursor=next_url, has_more=not data.get("done", True))

    async def fetch_lead(self, *, access_token: str, provider_lead_id: str,
                          instance_url: str = "https://login.salesforce.com") -> Optional[NormalizedLead]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{instance_url}/services/data/v59.0/sobjects/Lead/{provider_lead_id}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code == 404:
                return None
            if resp.status_code >= 400:
                raise CRMProviderError(f"Salesforce fetch_lead failed: {resp.text}", status_code=resp.status_code)
            return self._map_lead(resp.json())

    async def register_webhooks(self, *, access_token: str, callback_url: str) -> str:
        # Real implementation would create a PushTopic / Change Data
        # Capture subscription via the Streaming API. Left as an
        # explicit, documented follow-up since it requires a CometD
        # client running as a long-lived worker process.
        return f"salesforce-cdc-pending:{callback_url}"

    async def unregister_webhooks(self, *, access_token: str, subscription_id: str) -> None:
        return None

    def verify_webhook_signature(self, *, headers: dict, body: bytes, secret: str) -> bool:
        provided = headers.get("x-sfdc-signature", "")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(provided, expected)

    def extract_event_id(self, *, payload: dict) -> str:
        return str(payload.get("replayId", "")) or str(payload.get("event", {}).get("replayId", ""))

    async def update_lead(self, *, access_token: str, provider_lead_id: str, fields: dict,
                           instance_url: str = "https://login.salesforce.com") -> None:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.patch(
                f"{instance_url}/services/data/v59.0/sobjects/Lead/{provider_lead_id}",
                json=fields, headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code >= 400:
                raise CRMProviderError(f"Salesforce update_lead failed: {resp.text}", status_code=resp.status_code)

    async def create_note(self, *, access_token: str, provider_lead_id: str, note: str,
                           instance_url: str = "https://login.salesforce.com") -> None:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{instance_url}/services/data/v59.0/sobjects/Note",
                json={"Title": "AI Call Note", "Body": note, "ParentId": provider_lead_id},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code >= 400:
                raise CRMProviderError(f"Salesforce create_note failed: {resp.text}", status_code=resp.status_code)

    async def create_activity(self, *, access_token: str, provider_lead_id: str, activity: dict,
                              instance_url: str = "https://login.salesforce.com") -> None:
        await self.create_note(access_token=access_token, provider_lead_id=provider_lead_id,
                                note=activity.get("description", "AI call activity"), instance_url=instance_url)

    async def fetch_activities(self, *, access_token: str, provider_lead_id: str) -> list[dict]:
        return []
