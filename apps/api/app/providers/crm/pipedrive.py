"""Real Pipedrive integration (OAuth2 + REST v1 API)."""
import hashlib
import hmac
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.core.config import settings
from app.providers.crm.base import CRMAuthResult, CRMProvider, CRMProviderError, NormalizedLead, SyncPage

PD_AUTH_URL = "https://oauth.pipedrive.com/oauth/authorize"
PD_TOKEN_URL = "https://oauth.pipedrive.com/oauth/token"
PD_API_URL = "https://api.pipedrive.com/v1"


class PipedriveCRMProvider(CRMProvider):
    provider_name = "pipedrive"

    def get_authorization_url(self, *, state: str) -> str:
        params = {
            "client_id": settings.PIPEDRIVE_CLIENT_ID,
            "redirect_uri": settings.PIPEDRIVE_REDIRECT_URI,
            "state": state,
        }
        return f"{PD_AUTH_URL}?{urllib.parse.urlencode(params)}"

    async def exchange_code_for_token(self, *, code: str) -> CRMAuthResult:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(PD_TOKEN_URL, data={
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": settings.PIPEDRIVE_REDIRECT_URI,
            }, auth=(settings.PIPEDRIVE_CLIENT_ID, settings.PIPEDRIVE_CLIENT_SECRET))
            if resp.status_code >= 400:
                raise CRMProviderError(f"Pipedrive token exchange failed: {resp.text}", status_code=resp.status_code)
            data = resp.json()
            return CRMAuthResult(
                access_token=data["access_token"], refresh_token=data.get("refresh_token"),
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 3600)),
                external_account_id=str(data.get("api_domain", "")), external_account_name="Pipedrive",
            )

    async def refresh_access_token(self, *, refresh_token: str) -> CRMAuthResult:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(PD_TOKEN_URL, data={
                "grant_type": "refresh_token", "refresh_token": refresh_token,
            }, auth=(settings.PIPEDRIVE_CLIENT_ID, settings.PIPEDRIVE_CLIENT_SECRET))
            if resp.status_code >= 400:
                raise CRMProviderError(f"Pipedrive token refresh failed: {resp.text}", status_code=resp.status_code)
            data = resp.json()
            return CRMAuthResult(
                access_token=data["access_token"], refresh_token=refresh_token,
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 3600)),
                external_account_id=None, external_account_name="Pipedrive",
            )

    async def disconnect(self, *, access_token: str) -> None:
        return None

    def _map(self, raw: dict) -> NormalizedLead:
        person = raw.get("person_id") or {}
        phones = person.get("phone", []) if isinstance(person, dict) else []
        emails = person.get("email", []) if isinstance(person, dict) else []
        return NormalizedLead(
            provider_lead_id=str(raw["id"]),
            name=raw.get("title") or (person.get("name") if isinstance(person, dict) else None),
            phone=(phones[0].get("value") if phones else None),
            email=(emails[0].get("value") if emails else None),
            company=raw.get("organization_id", {}).get("name") if isinstance(raw.get("organization_id"), dict) else None,
            crm_status=raw.get("status"),
            crm_updated_at=datetime.fromisoformat(raw["update_time"]) if raw.get("update_time") else None,
            raw=raw,
        )

    async def initial_sync(self, *, access_token: str, page_size: int = 100) -> SyncPage:
        return await self.fetch_leads(access_token=access_token, cursor=None)

    async def fetch_leads(self, *, access_token: str, cursor: Optional[str],
                           updated_since: Optional[datetime] = None) -> SyncPage:
        params = {"start": int(cursor) if cursor else 0, "limit": 100}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{PD_API_URL}/leads", params=params,
                                     headers={"Authorization": f"Bearer {access_token}"})
            if resp.status_code >= 400:
                raise CRMProviderError(f"Pipedrive fetch_leads failed: {resp.text}", status_code=resp.status_code)
            data = resp.json()
            items = data.get("data") or []
            leads = [self._map(r) for r in items]
            pagination = data.get("additional_data", {}).get("pagination", {})
            more = pagination.get("more_items_in_collection", False)
            next_start = pagination.get("next_start")
            return SyncPage(leads=leads, next_cursor=str(next_start) if next_start else None, has_more=more)

    async def fetch_lead(self, *, access_token: str, provider_lead_id: str) -> Optional[NormalizedLead]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{PD_API_URL}/leads/{provider_lead_id}",
                                     headers={"Authorization": f"Bearer {access_token}"})
            if resp.status_code == 404:
                return None
            if resp.status_code >= 400:
                raise CRMProviderError(f"Pipedrive fetch_lead failed: {resp.text}", status_code=resp.status_code)
            data = resp.json().get("data")
            return self._map(data) if data else None

    async def register_webhooks(self, *, access_token: str, callback_url: str) -> str:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{PD_API_URL}/webhooks",
                json={"subscription_url": callback_url, "event_action": "*", "event_object": "lead"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code >= 400:
                raise CRMProviderError(f"Pipedrive register_webhooks failed: {resp.text}", status_code=resp.status_code)
            return str(resp.json().get("data", {}).get("id", ""))

    async def unregister_webhooks(self, *, access_token: str, subscription_id: str) -> None:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.delete(f"{PD_API_URL}/webhooks/{subscription_id}",
                                 headers={"Authorization": f"Bearer {access_token}"})

    def verify_webhook_signature(self, *, headers: dict, body: bytes, secret: str) -> bool:
        provided = headers.get("x-pipedrive-signature", "")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(provided, expected)

    def extract_event_id(self, *, payload: dict) -> str:
        meta = payload.get("meta", {})
        return f"{meta.get('id', '')}:{meta.get('action', '')}:{meta.get('timestamp', '')}"

    async def update_lead(self, *, access_token: str, provider_lead_id: str, fields: dict) -> None:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.patch(f"{PD_API_URL}/leads/{provider_lead_id}", json=fields,
                                       headers={"Authorization": f"Bearer {access_token}"})
            if resp.status_code >= 400:
                raise CRMProviderError(f"Pipedrive update_lead failed: {resp.text}", status_code=resp.status_code)

    async def create_note(self, *, access_token: str, provider_lead_id: str, note: str) -> None:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{PD_API_URL}/notes",
                                      json={"content": note, "lead_id": provider_lead_id},
                                      headers={"Authorization": f"Bearer {access_token}"})
            if resp.status_code >= 400:
                raise CRMProviderError(f"Pipedrive create_note failed: {resp.text}", status_code=resp.status_code)

    async def create_activity(self, *, access_token: str, provider_lead_id: str, activity: dict) -> None:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{PD_API_URL}/activities",
                json={"subject": activity.get("subject", "AI Call"), "lead_id": provider_lead_id,
                      "type": "call", "note": activity.get("description", "")},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code >= 400:
                raise CRMProviderError(f"Pipedrive create_activity failed: {resp.text}", status_code=resp.status_code)

    async def fetch_activities(self, *, access_token: str, provider_lead_id: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{PD_API_URL}/activities", params={"lead_id": provider_lead_id},
                                     headers={"Authorization": f"Bearer {access_token}"})
            if resp.status_code >= 400:
                return []
            return resp.json().get("data") or []
