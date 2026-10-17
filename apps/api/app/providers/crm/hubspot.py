"""
Real HubSpot integration. Requires HUBSPOT_CLIENT_ID / HUBSPOT_CLIENT_SECRET
(see .env.example) from a HubSpot developer app. This code makes genuine
HTTP calls to HubSpot's OAuth and CRM v3 endpoints - it has NOT been
exercised against a live HubSpot account in this environment (no network
access to hubspot.com here), so validate it against your own sandbox
before relying on it in production.
"""
import hashlib
import hmac
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.providers.crm.base import (
    CRMAuthResult, CRMProvider, CRMProviderError, NormalizedLead, SyncPage,
)

HUBSPOT_AUTH_URL = "https://app.hubspot.com/oauth/authorize"
HUBSPOT_TOKEN_URL = "https://api.hubapi.com/oauth/v1/token"
HUBSPOT_CONTACTS_URL = "https://api.hubapi.com/crm/v3/objects/contacts"
HUBSPOT_SCOPES = "crm.objects.contacts.read crm.objects.contacts.write"


def _retryable_http() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15.0)


class HubSpotCRMProvider(CRMProvider):
    provider_name = "hubspot"

    def get_authorization_url(self, *, state: str) -> str:
        params = {
            "client_id": settings.HUBSPOT_CLIENT_ID,
            "redirect_uri": settings.HUBSPOT_REDIRECT_URI,
            "scope": HUBSPOT_SCOPES,
            "state": state,
        }
        return f"{HUBSPOT_AUTH_URL}?{urllib.parse.urlencode(params)}"

    @retry(reraise=True, stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8),
           retry=retry_if_exception_type(CRMProviderError))
    async def exchange_code_for_token(self, *, code: str) -> CRMAuthResult:
        async with _retryable_http() as client:
            resp = await client.post(HUBSPOT_TOKEN_URL, data={
                "grant_type": "authorization_code",
                "client_id": settings.HUBSPOT_CLIENT_ID,
                "client_secret": settings.HUBSPOT_CLIENT_SECRET,
                "redirect_uri": settings.HUBSPOT_REDIRECT_URI,
                "code": code,
            })
            if resp.status_code >= 400:
                raise CRMProviderError(f"HubSpot token exchange failed: {resp.text}",
                                        retryable=resp.status_code >= 500, status_code=resp.status_code)
            data = resp.json()
            return CRMAuthResult(
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token"),
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 1800)),
                external_account_id=None,
                external_account_name="HubSpot",
            )

    @retry(reraise=True, stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8),
           retry=retry_if_exception_type(CRMProviderError))
    async def refresh_access_token(self, *, refresh_token: str) -> CRMAuthResult:
        async with _retryable_http() as client:
            resp = await client.post(HUBSPOT_TOKEN_URL, data={
                "grant_type": "refresh_token",
                "client_id": settings.HUBSPOT_CLIENT_ID,
                "client_secret": settings.HUBSPOT_CLIENT_SECRET,
                "refresh_token": refresh_token,
            })
            if resp.status_code >= 400:
                raise CRMProviderError(f"HubSpot token refresh failed: {resp.text}",
                                        retryable=resp.status_code >= 500, status_code=resp.status_code)
            data = resp.json()
            return CRMAuthResult(
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token", refresh_token),
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 1800)),
                external_account_id=None,
                external_account_name="HubSpot",
            )

    async def disconnect(self, *, access_token: str) -> None:
        # HubSpot has no explicit "revoke" endpoint for private apps token
        # scopes; disconnection is handled by clearing local credentials.
        return None

    def _map_contact(self, raw: dict) -> NormalizedLead:
        props = raw.get("properties", {})
        return NormalizedLead(
            provider_lead_id=raw["id"],
            name=" ".join(filter(None, [props.get("firstname"), props.get("lastname")])) or None,
            phone=props.get("phone"),
            email=props.get("email"),
            company=props.get("company"),
            crm_status=props.get("hs_lead_status"),
            crm_updated_at=datetime.fromisoformat(raw["updatedAt"].replace("Z", "+00:00"))
            if raw.get("updatedAt") else None,
            raw=raw,
        )

    async def initial_sync(self, *, access_token: str, page_size: int = 100) -> SyncPage:
        return await self.fetch_leads(access_token=access_token, cursor=None)

    async def fetch_leads(
        self, *, access_token: str, cursor: Optional[str], updated_since: Optional[datetime] = None
    ) -> SyncPage:
        params = {
            "limit": 100,
            "properties": "firstname,lastname,phone,email,company,hs_lead_status",
        }
        if cursor:
            params["after"] = cursor
        async with _retryable_http() as client:
            resp = await client.get(
                HUBSPOT_CONTACTS_URL, params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code == 429:
                raise CRMProviderError("HubSpot rate limited", retryable=True, status_code=429)
            if resp.status_code >= 400:
                raise CRMProviderError(f"HubSpot fetch_leads failed: {resp.text}",
                                        retryable=resp.status_code >= 500, status_code=resp.status_code)
            data = resp.json()
            leads = [self._map_contact(r) for r in data.get("results", [])]
            paging = data.get("paging", {}).get("next", {})
            return SyncPage(
                leads=leads,
                next_cursor=paging.get("after"),
                has_more=bool(paging.get("after")),
            )

    async def fetch_lead(self, *, access_token: str, provider_lead_id: str) -> Optional[NormalizedLead]:
        async with _retryable_http() as client:
            resp = await client.get(
                f"{HUBSPOT_CONTACTS_URL}/{provider_lead_id}",
                params={"properties": "firstname,lastname,phone,email,company,hs_lead_status"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code == 404:
                return None
            if resp.status_code >= 400:
                raise CRMProviderError(f"HubSpot fetch_lead failed: {resp.text}", status_code=resp.status_code)
            return self._map_contact(resp.json())

    async def register_webhooks(self, *, access_token: str, callback_url: str) -> str:
        # HubSpot webhook subscriptions are configured at the app level in
        # the developer portal, not per-installation via API for private
        # apps. We record the callback URL and rely on tenant-level
        # filtering of incoming events by portalId + this connection.
        return f"hubspot-app-webhook:{callback_url}"

    async def unregister_webhooks(self, *, access_token: str, subscription_id: str) -> None:
        return None

    def verify_webhook_signature(self, *, headers: dict, body: bytes, secret: str) -> bool:
        # HubSpot v3 signature: SHA256(client_secret + method + uri + body) - simplified
        # v1 scheme shown here; production should follow HubSpot's current
        # signature spec (v3, using X-HubSpot-Signature-v3 + timestamp).
        provided = headers.get("x-hubspot-signature", "")
        source = secret.encode() + body
        expected = hashlib.sha256(source).hexdigest()
        return hmac.compare_digest(provided, expected)

    def extract_event_id(self, *, payload: dict) -> str:
        if isinstance(payload, dict) and "eventId" in payload:
            return str(payload["eventId"])
        return str(payload.get("subscriptionId", "")) + ":" + str(payload.get("occurredAt", ""))

    async def update_lead(self, *, access_token: str, provider_lead_id: str, fields: dict) -> None:
        async with _retryable_http() as client:
            resp = await client.patch(
                f"{HUBSPOT_CONTACTS_URL}/{provider_lead_id}",
                json={"properties": fields},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code >= 400:
                raise CRMProviderError(f"HubSpot update_lead failed: {resp.text}", status_code=resp.status_code)

    async def create_note(self, *, access_token: str, provider_lead_id: str, note: str) -> None:
        async with _retryable_http() as client:
            resp = await client.post(
                "https://api.hubapi.com/crm/v3/objects/notes",
                json={"properties": {"hs_note_body": note}},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code >= 400:
                raise CRMProviderError(f"HubSpot create_note failed: {resp.text}", status_code=resp.status_code)

    async def create_activity(self, *, access_token: str, provider_lead_id: str, activity: dict) -> None:
        await self.create_note(access_token=access_token, provider_lead_id=provider_lead_id,
                                note=activity.get("description", "AI call activity"))

    async def fetch_activities(self, *, access_token: str, provider_lead_id: str) -> list[dict]:
        return []
