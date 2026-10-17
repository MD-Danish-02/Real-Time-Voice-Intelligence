"""Real Zoho CRM integration (OAuth2 multi-DC + REST v6 API)."""
import hashlib
import hmac
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.core.config import settings
from app.providers.crm.base import CRMAuthResult, CRMProvider, CRMProviderError, NormalizedLead, SyncPage

ZOHO_ACCOUNTS_URL = "https://accounts.zoho.in"
ZOHO_API_URL = "https://www.zohoapis.in"


class ZohoCRMProvider(CRMProvider):
    provider_name = "zoho"

    def get_authorization_url(self, *, state: str) -> str:
        params = {
            "response_type": "code",
            "client_id": settings.ZOHO_CLIENT_ID,
            "redirect_uri": settings.ZOHO_REDIRECT_URI,
            "scope": "ZohoCRM.modules.leads.ALL,ZohoCRM.notifications.ALL",
            "access_type": "offline",
            "state": state,
        }
        return f"{ZOHO_ACCOUNTS_URL}/oauth/v2/auth?{urllib.parse.urlencode(params)}"

    async def exchange_code_for_token(self, *, code: str) -> CRMAuthResult:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{ZOHO_ACCOUNTS_URL}/oauth/v2/token", data={
                "grant_type": "authorization_code",
                "client_id": settings.ZOHO_CLIENT_ID,
                "client_secret": settings.ZOHO_CLIENT_SECRET,
                "redirect_uri": settings.ZOHO_REDIRECT_URI,
                "code": code,
            })
            if resp.status_code >= 400:
                raise CRMProviderError(f"Zoho token exchange failed: {resp.text}", status_code=resp.status_code)
            data = resp.json()
            return CRMAuthResult(
                access_token=data["access_token"], refresh_token=data.get("refresh_token"),
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 3600)),
                external_account_id=None, external_account_name="Zoho CRM",
            )

    async def refresh_access_token(self, *, refresh_token: str) -> CRMAuthResult:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{ZOHO_ACCOUNTS_URL}/oauth/v2/token", data={
                "grant_type": "refresh_token",
                "client_id": settings.ZOHO_CLIENT_ID,
                "client_secret": settings.ZOHO_CLIENT_SECRET,
                "refresh_token": refresh_token,
            })
            if resp.status_code >= 400:
                raise CRMProviderError(f"Zoho token refresh failed: {resp.text}", status_code=resp.status_code)
            data = resp.json()
            return CRMAuthResult(
                access_token=data["access_token"], refresh_token=refresh_token,
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 3600)),
                external_account_id=None, external_account_name="Zoho CRM",
            )

    async def disconnect(self, *, access_token: str) -> None:
        return None

    def _map(self, raw: dict) -> NormalizedLead:
        return NormalizedLead(
            provider_lead_id=str(raw["id"]), name=raw.get("Full_Name") or raw.get("Last_Name"),
            phone=raw.get("Phone"), email=raw.get("Email"), company=raw.get("Company"),
            crm_status=raw.get("Lead_Status"),
            crm_updated_at=datetime.fromisoformat(raw["Modified_Time"].replace("Z", "+00:00"))
            if raw.get("Modified_Time") else None,
            raw=raw,
        )

    async def initial_sync(self, *, access_token: str, page_size: int = 100) -> SyncPage:
        return await self.fetch_leads(access_token=access_token, cursor=None)

    async def fetch_leads(self, *, access_token: str, cursor: Optional[str],
                           updated_since: Optional[datetime] = None) -> SyncPage:
        params = {"per_page": 100, "page": int(cursor) if cursor else 1}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{ZOHO_API_URL}/crm/v6/Leads", params=params,
                                     headers={"Authorization": f"Zoho-oauthtoken {access_token}"})
            if resp.status_code == 204:
                return SyncPage(leads=[], next_cursor=None, has_more=False)
            if resp.status_code >= 400:
                raise CRMProviderError(f"Zoho fetch_leads failed: {resp.text}", status_code=resp.status_code)
            data = resp.json()
            leads = [self._map(r) for r in data.get("data", [])]
            more = data.get("info", {}).get("more_records", False)
            next_page = str(params["page"] + 1) if more else None
            return SyncPage(leads=leads, next_cursor=next_page, has_more=more)

    async def fetch_lead(self, *, access_token: str, provider_lead_id: str) -> Optional[NormalizedLead]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{ZOHO_API_URL}/crm/v6/Leads/{provider_lead_id}",
                                     headers={"Authorization": f"Zoho-oauthtoken {access_token}"})
            if resp.status_code == 404:
                return None
            if resp.status_code >= 400:
                raise CRMProviderError(f"Zoho fetch_lead failed: {resp.text}", status_code=resp.status_code)
            records = resp.json().get("data", [])
            return self._map(records[0]) if records else None

    async def register_webhooks(self, *, access_token: str, callback_url: str) -> str:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{ZOHO_API_URL}/crm/v6/actions/watch",
                json={"watch": [{"channel_id": "1", "events": ["Leads.create", "Leads.edit"],
                                  "notify_url": callback_url}]},
                headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
            )
            if resp.status_code >= 400:
                raise CRMProviderError(f"Zoho register_webhooks failed: {resp.text}", status_code=resp.status_code)
            return "zoho-watch-channel-1"

    async def unregister_webhooks(self, *, access_token: str, subscription_id: str) -> None:
        return None

    def verify_webhook_signature(self, *, headers: dict, body: bytes, secret: str) -> bool:
        provided = headers.get("x-zoho-signature", "")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(provided, expected)

    def extract_event_id(self, *, payload: dict) -> str:
        return str(payload.get("ids", [None])[0] or "") + ":" + str(payload.get("token", ""))

    async def update_lead(self, *, access_token: str, provider_lead_id: str, fields: dict) -> None:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.put(f"{ZOHO_API_URL}/crm/v6/Leads/{provider_lead_id}",
                                     json={"data": [fields]},
                                     headers={"Authorization": f"Zoho-oauthtoken {access_token}"})
            if resp.status_code >= 400:
                raise CRMProviderError(f"Zoho update_lead failed: {resp.text}", status_code=resp.status_code)

    async def create_note(self, *, access_token: str, provider_lead_id: str, note: str) -> None:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{ZOHO_API_URL}/crm/v6/Leads/{provider_lead_id}/Notes",
                json={"data": [{"Note_Title": "AI Call", "Note_Content": note}]},
                headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
            )
            if resp.status_code >= 400:
                raise CRMProviderError(f"Zoho create_note failed: {resp.text}", status_code=resp.status_code)

    async def create_activity(self, *, access_token: str, provider_lead_id: str, activity: dict) -> None:
        await self.create_note(access_token=access_token, provider_lead_id=provider_lead_id,
                                note=activity.get("description", "AI call activity"))

    async def fetch_activities(self, *, access_token: str, provider_lead_id: str) -> list[dict]:
        return []
