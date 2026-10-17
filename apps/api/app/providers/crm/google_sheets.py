"""
Two lightweight, config-driven adapters:

- GoogleSheetsCRMProvider: treats a Google Sheet as the lead source, via
  the Sheets API v4 with a service-account or OAuth token. Good fit for
  Danish's earlier Sheets-based automation pattern, now behind the same
  provider interface as every other CRM.
- CustomAPICRMProvider: a generic REST adapter driven entirely by a
  tenant-supplied base_url + auth header + JSON field mapping, for any
  CRM/lead source that exposes a simple REST API (or is proxied through
  one, e.g. a CSV-import microservice).

Both make real HTTP calls; there is no live sheet/endpoint to test
against in this sandbox, so validate against a real spreadsheet/API
before production use.
"""
import hashlib
import hmac
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote, urlsplit

import httpx

from app.core.config import settings
from app.providers.crm.base import CRMAuthResult, CRMProvider, CRMProviderError, NormalizedLead, SyncPage
from app.security.ssrf import UnsafeOutboundURL, validate_public_url

SHEETS_API_URL = "https://sheets.googleapis.com/v4/spreadsheets"


class GoogleSheetsCRMProvider(CRMProvider):
    """
    Config (stored on the CRMConnection.field_mapping_json) is expected to
    contain: {"spreadsheet_id": "...", "range": "Leads!A2:F", "columns":
    ["name","phone","email","company","status"]}. Auth is a Google OAuth
    access token with the spreadsheets.readonly (or full) scope.
    """
    provider_name = "google_sheets"

    AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"

    def get_authorization_url(self, *, state: str) -> str:
        if not settings.GOOGLE_CLIENT_ID:
            raise CRMProviderError(
                "Google OAuth is not configured on this server - set GOOGLE_CLIENT_ID and "
                "GOOGLE_CLIENT_SECRET in .env before connecting Google Sheets.", retryable=False,
            )
        import urllib.parse
        params = {
            "client_id": settings.GOOGLE_CLIENT_ID, "redirect_uri": settings.GOOGLE_SHEETS_REDIRECT_URI,
            "response_type": "code", "access_type": "offline", "prompt": "consent",
            "scope": "https://www.googleapis.com/auth/spreadsheets.readonly", "state": state,
        }
        return f"{self.AUTH_URL}?{urllib.parse.urlencode(params)}"

    async def list_sheets(self, *, access_token: str, spreadsheet_id: str) -> list[str]:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            resp = await client.get(
                f"{SHEETS_API_URL}/{quote(spreadsheet_id, safe='')}",
                params={"fields": "sheets.properties.title"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code >= 400:
                raise CRMProviderError(f"Google Sheets metadata failed: {resp.text}", status_code=resp.status_code)
            return [sheet.get("properties", {}).get("title", "") for sheet in resp.json().get("sheets", [])
                    if sheet.get("properties", {}).get("title")]

    async def get_headers(self, *, access_token: str, spreadsheet_id: str, sheet_name: str) -> list[str]:
        escaped_name = sheet_name.replace("'", "''")
        range_name = f"'{escaped_name}'!1:1"
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            resp = await client.get(
                f"{SHEETS_API_URL}/{quote(spreadsheet_id, safe='')}/values/{quote(range_name, safe='')}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code >= 400:
                raise CRMProviderError(f"Google Sheets header read failed: {resp.text}", status_code=resp.status_code)
            rows = resp.json().get("values", [])
            return [str(value).strip() for value in rows[0]] if rows else []

    async def exchange_code_for_token(self, *, code: str) -> CRMAuthResult:
        if not settings.GOOGLE_CLIENT_ID:
            raise CRMProviderError("Google OAuth is not configured on this server.", retryable=False)
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(self.TOKEN_URL, data={
                "code": code, "client_id": settings.GOOGLE_CLIENT_ID, "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_SHEETS_REDIRECT_URI, "grant_type": "authorization_code",
            })
            if resp.status_code >= 400:
                raise CRMProviderError(f"Google token exchange failed: {resp.text}", status_code=resp.status_code)
            data = resp.json()
            return CRMAuthResult(access_token=data["access_token"], refresh_token=data.get("refresh_token"),
                                 expires_at=datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 3600)),
                                 external_account_id=None, external_account_name="Google Sheets")

    async def refresh_access_token(self, *, refresh_token: str) -> CRMAuthResult:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(self.TOKEN_URL, data={
                "refresh_token": refresh_token, "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET, "grant_type": "refresh_token",
            })
            if resp.status_code >= 400:
                raise CRMProviderError(f"Google token refresh failed: {resp.text}", status_code=resp.status_code)
            data = resp.json()
            return CRMAuthResult(access_token=data["access_token"], refresh_token=refresh_token,
                                 expires_at=datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 3600)),
                                 external_account_id=None, external_account_name="Google Sheets")

    async def disconnect(self, *, access_token: str) -> None:
        return None

    def _row_to_lead(self, row: list[str], columns: list[str], row_index: int) -> NormalizedLead:
        values = dict(zip(columns, row + [""] * (len(columns) - len(row))))
        custom_fields = {
            key.removeprefix("custom:"): value for key, value in values.items()
            if key.startswith("custom:") and value
        }
        return NormalizedLead(
            provider_lead_id=f"row-{row_index}",
            name=values.get("name"),
            phone=values.get("phone"),
            email=values.get("email"),
            company=values.get("company"),
            crm_status=values.get("status"),
            crm_updated_at=datetime.now(timezone.utc),
            raw={**values, "custom_fields": custom_fields},
        )

    async def initial_sync(self, *, access_token: str, page_size: int = 100, **kwargs) -> SyncPage:
        return await self.fetch_leads(access_token=access_token, cursor=None, **kwargs)

    async def fetch_leads(
        self, *, access_token: str, cursor: Optional[str], updated_since: Optional[datetime] = None,
        spreadsheet_id: str = "", sheet_range: str = "Leads!A2:F",
        columns: Optional[list[str]] = None,
    ) -> SyncPage:
        from app.core.logging import get_logger
        logger = get_logger("providers.crm.google_sheets")
        columns = columns or ["name", "phone", "email", "company", "status"]
        if not spreadsheet_id:
            raise CRMProviderError("spreadsheet_id is required for Google Sheets sync", retryable=False)
        logger.info("google_sheets_fetch_leads", spreadsheet_id=spreadsheet_id, sheet_range=sheet_range,
                    has_cursor=cursor is not None, updated_since=updated_since.isoformat() if updated_since else None)
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{SHEETS_API_URL}/{quote(spreadsheet_id, safe='')}/values/{quote(sheet_range, safe='')}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code >= 400:
                raise CRMProviderError(f"Google Sheets fetch failed: {resp.text}", status_code=resp.status_code)
            rows = resp.json().get("values", [])
            leads = [self._row_to_lead(row, columns, i) for i, row in enumerate(rows)]
            logger.info("google_sheets_fetch_leads_complete", rows_fetched=len(rows), leads_normalized=len(leads))
            return SyncPage(leads=leads, next_cursor=None, has_more=False)

    async def fetch_lead(self, *, access_token: str, provider_lead_id: str) -> Optional[NormalizedLead]:
        page = await self.fetch_leads(access_token=access_token, cursor=None)
        for lead in page.leads:
            if lead.provider_lead_id == provider_lead_id:
                return lead
        return None

    async def register_webhooks(self, *, access_token: str, callback_url: str) -> str:
        # Sheets has no native webhooks; continuous sync relies entirely on
        # the polling/reconciliation path for this provider.
        return "polling-only"

    async def unregister_webhooks(self, *, access_token: str, subscription_id: str) -> None:
        return None

    def verify_webhook_signature(self, *, headers: dict, body: bytes, secret: str) -> bool:
        return False

    def extract_event_id(self, *, payload: dict) -> str:
        return "n/a"

    async def update_lead(self, *, access_token: str, provider_lead_id: str, fields: dict) -> None:
        raise CRMProviderError("Google Sheets cell write-back is not supported by this connection.", retryable=False)

    async def create_note(self, *, access_token: str, provider_lead_id: str, note: str) -> None:
        raise CRMProviderError("Google Sheets note write-back is not supported.", retryable=False)

    async def create_activity(self, *, access_token: str, provider_lead_id: str, activity: dict) -> None:
        raise CRMProviderError("Google Sheets activity write-back is not supported.", retryable=False)

    async def fetch_activities(self, *, access_token: str, provider_lead_id: str) -> list[dict]:
        return []


class CustomAPICRMProvider(CRMProvider):
    """
    Generic adapter for a tenant-hosted REST API or CSV-import service.
    Connection config (field_mapping_json) shape:
      {"base_url": "...", "auth_header": "Authorization", "auth_value": "Bearer ...",
       "list_path": "/leads", "field_map": {"id": "id", "name": "full_name", ...}}
    """
    provider_name = "custom_api"

    def get_authorization_url(self, *, state: str) -> str:
        return ""  # Custom API uses a static API key, configured directly in Settings

    async def exchange_code_for_token(self, *, code: str) -> CRMAuthResult:
        raise CRMProviderError("Custom API provider does not use OAuth.", retryable=False)

    async def refresh_access_token(self, *, refresh_token: str) -> CRMAuthResult:
        raise CRMProviderError("Custom API provider does not use OAuth.", retryable=False)

    async def disconnect(self, *, access_token: str) -> None:
        return None

    def _map(self, raw: dict, field_map: dict) -> NormalizedLead:
        def g(key: str) -> Optional[str]:
            path = field_map.get(key)
            return raw.get(path) if path else None

        return NormalizedLead(
            provider_lead_id=str(g("id") or raw.get("id")),
            name=g("name"), phone=g("phone"), email=g("email"), company=g("company"),
            crm_status=g("status"), crm_updated_at=datetime.now(timezone.utc),
            raw={**raw, "custom_fields": {k: v for k, v in raw.items() if k not in set(field_map.values())}},
        )

    async def _endpoint(self, base_url: str, path: str) -> str:
        base = urlsplit(base_url)
        if base.scheme.lower() != "https" or not base.hostname or base.username or base.password or base.query or base.fragment:
            raise CRMProviderError("Custom API base_url must be a clean HTTPS URL.", retryable=False)
        if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
            raise CRMProviderError("Custom API paths must be absolute paths on the configured host.", retryable=False)
        endpoint = f"{base_url.rstrip('/')}{path}"
        try:
            return await validate_public_url(endpoint, require_https=True)
        except UnsafeOutboundURL as exc:
            raise CRMProviderError(str(exc), retryable=False) from exc

    def _auth_headers(self, header_name: str, access_token: str) -> dict[str, str]:
        if not access_token or not header_name:
            return {}
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", header_name) or header_name.lower() in {
            "host", "connection", "content-length", "transfer-encoding", "proxy-authorization",
        }:
            raise CRMProviderError("Custom API auth header name is invalid or reserved.", retryable=False)
        return {header_name: access_token}

    async def initial_sync(self, *, access_token: str, page_size: int = 100, **kwargs) -> SyncPage:
        return await self.fetch_leads(access_token=access_token, cursor=None, **kwargs)

    async def fetch_leads(
        self, *, access_token: str, cursor: Optional[str], updated_since: Optional[datetime] = None,
        base_url: str = "", list_path: str = "/leads", auth_header: str = "Authorization",
        field_map: Optional[dict] = None,
    ) -> SyncPage:
        field_map = field_map or {"id": "id", "name": "name", "phone": "phone", "email": "email",
                                   "company": "company", "status": "status"}
        if not base_url:
            raise CRMProviderError("base_url is required for the Custom API provider", retryable=False)
        params = {"cursor": cursor} if cursor else {}
        headers = self._auth_headers(auth_header, access_token)
        endpoint = await self._endpoint(base_url, list_path)
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False, trust_env=False) as client:
            resp = await client.get(endpoint, params=params,
                                     headers=headers)
            if resp.status_code >= 400:
                raise CRMProviderError(f"Custom API fetch_leads failed: {resp.text}", status_code=resp.status_code)
            data = resp.json()
            items = data.get("items", data if isinstance(data, list) else [])
            leads = [self._map(r, field_map) for r in items]
            next_cursor = data.get("next_cursor") if isinstance(data, dict) else None
            return SyncPage(leads=leads, next_cursor=next_cursor, has_more=bool(next_cursor))

    async def fetch_lead(self, *, access_token: str, provider_lead_id: str,
                          base_url: str = "", auth_header: str = "Authorization",
                          field_map: Optional[dict] = None) -> Optional[NormalizedLead]:
        field_map = field_map or {"id": "id", "name": "name", "phone": "phone", "email": "email",
                                   "company": "company", "status": "status"}
        headers = self._auth_headers(auth_header, access_token)
        endpoint = await self._endpoint(base_url, f"/leads/{quote(str(provider_lead_id), safe='')}")
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False, trust_env=False) as client:
            resp = await client.get(endpoint,
                                     headers=headers)
            if resp.status_code == 404:
                return None
            if resp.status_code >= 400:
                raise CRMProviderError(f"Custom API fetch_lead failed: {resp.text}", status_code=resp.status_code)
            return self._map(resp.json(), field_map)

    async def register_webhooks(self, *, access_token: str, callback_url: str) -> str:
        return "manual-configuration-required"

    async def unregister_webhooks(self, *, access_token: str, subscription_id: str) -> None:
        return None

    def verify_webhook_signature(self, *, headers: dict, body: bytes, secret: str) -> bool:
        provided = headers.get("x-webhook-signature", "")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(provided, expected)

    def extract_event_id(self, *, payload: dict) -> str:
        return str(payload.get("event_id") or payload.get("id") or "")

    async def update_lead(self, *, access_token: str, provider_lead_id: str, fields: dict,
                           base_url: str = "", auth_header: str = "Authorization") -> None:
        endpoint = await self._endpoint(base_url, f"/leads/{quote(str(provider_lead_id), safe='')}")
        headers = self._auth_headers(auth_header, access_token)
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False, trust_env=False) as client:
            resp = await client.patch(endpoint, json=fields,
                                       headers=headers)
            if resp.status_code >= 400:
                raise CRMProviderError(f"Custom API update_lead failed: {resp.text}", status_code=resp.status_code)

    async def create_note(self, *, access_token: str, provider_lead_id: str, note: str,
                          base_url: str = "", auth_header: str = "Authorization", note_path: str = "") -> None:
        if not note_path:
            raise CRMProviderError("Custom API note write-back is not configured.", retryable=False)
        path = note_path.replace("{lead_id}", quote(str(provider_lead_id), safe=""))
        endpoint = await self._endpoint(base_url, path)
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False, trust_env=False) as client:
            resp = await client.post(endpoint, json={"lead_id": provider_lead_id, "note": note},
                                     headers=self._auth_headers(auth_header, access_token))
            if resp.status_code >= 400:
                raise CRMProviderError(f"Custom API create_note failed: {resp.text}", status_code=resp.status_code)

    async def create_activity(self, *, access_token: str, provider_lead_id: str, activity: dict,
                              base_url: str = "", auth_header: str = "Authorization", activity_path: str = "") -> None:
        if not activity_path:
            raise CRMProviderError("Custom API activity write-back is not configured.", retryable=False)
        path = activity_path.replace("{lead_id}", quote(str(provider_lead_id), safe=""))
        endpoint = await self._endpoint(base_url, path)
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False, trust_env=False) as client:
            resp = await client.post(endpoint, json={"lead_id": provider_lead_id, **activity},
                                     headers=self._auth_headers(auth_header, access_token))
            if resp.status_code >= 400:
                raise CRMProviderError(f"Custom API create_activity failed: {resp.text}", status_code=resp.status_code)

    async def fetch_activities(self, *, access_token: str, provider_lead_id: str) -> list[dict]:
        return []
