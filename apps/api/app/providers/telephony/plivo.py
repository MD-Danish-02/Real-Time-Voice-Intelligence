"""Real Plivo adapter using their Voice API v1."""
from typing import Optional

import httpx

from app.core.config import settings
from app.providers.telephony.base import CallStatusResult, DialResult, TelephonyProvider, TelephonyProviderError

_STATUS_MAP = {
    "queued": "queued", "ringing": "ringing", "in-progress": "in_progress",
    "completed": "completed", "busy": "busy", "failed": "failed", "no-answer": "no_answer",
}


class PlivoTelephonyProvider(TelephonyProvider):
    provider_name = "plivo"

    def _base_url(self) -> str:
        return f"https://api.plivo.com/v1/Account/{settings.PLIVO_AUTH_ID}"

    def _auth(self) -> tuple[str, str]:
        return settings.PLIVO_AUTH_ID, settings.PLIVO_AUTH_TOKEN

    def _base_webhook_url(self, webhook_url: str) -> str:
        """Extract scheme://netloc from webhook_url, or fallback to settings.BACKEND_URL."""
        from urllib.parse import urlsplit
        if webhook_url:
            parsed = urlsplit(webhook_url)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}"
        return settings.BACKEND_URL.rstrip("/")

    def _answer_url(self, webhook_base_url: str, call_id: str) -> str:
        """Build the answer_url for Plivo - points to our Plivo XML endpoint."""
        from app.core.signing import call_webhook_signature
        from uuid import UUID
        base = self._base_webhook_url(webhook_base_url)
        sig = call_webhook_signature(UUID(call_id))
        return f"{base}/api/v1/plivo/xml/{call_id}?sig={sig}"

    def _hangup_url(self, webhook_base_url: str, call_id: str) -> str:
        """Build the hangup_url for Plivo - points to our status callback endpoint."""
        from app.core.signing import call_webhook_signature
        from uuid import UUID
        base = self._base_webhook_url(webhook_base_url)
        sig = call_webhook_signature(UUID(call_id))
        return f"{base}/api/v1/plivo/status/{call_id}?sig={sig}"

    async def dial(self, *, to_number: str, from_number: str, webhook_url: str,
                    metadata: Optional[dict] = None) -> DialResult:
        # webhook_url may be a base host or full webhook URL; _base_webhook_url normalizes it
        call_id = metadata.get("call_id") if metadata else None
        if not call_id and webhook_url:
            from urllib.parse import urlparse
            import uuid
            path_parts = urlparse(webhook_url).path.rstrip("/").split("/")
            for part in reversed(path_parts):
                try:
                    uuid.UUID(part)
                    call_id = part
                    break
                except ValueError:
                    continue
        if not call_id:
            raise TelephonyProviderError("call_id missing in metadata for Plivo dial")

        answer_url = self._answer_url(webhook_url, call_id)
        hangup_url = self._hangup_url(webhook_url, call_id)

        payload = {
            "from": from_number or settings.PLIVO_CALLER_ID,
            "to": to_number,
            "answer_url": answer_url,
            "answer_method": "POST",
            "hangup_url": hangup_url,
            "hangup_method": "POST",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{self._base_url()}/Call/", json=payload, auth=self._auth())
            if resp.status_code >= 400:
                raise TelephonyProviderError(f"Plivo dial failed: {resp.text}", retryable=resp.status_code >= 500)
            data = resp.json()
            return DialResult(
                provider_call_id=data.get("request_uuid", ""), status="queued", raw=data,
            )

    async def hangup(self, *, provider_call_id: str) -> None:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.delete(f"{self._base_url()}/Call/{provider_call_id}/", auth=self._auth())
            if resp.status_code >= 400 and resp.status_code != 404:
                raise TelephonyProviderError(f"Plivo hangup failed: {resp.text}")

    async def transfer(self, *, provider_call_id: str, destination: str) -> None:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self._base_url()}/Call/{provider_call_id}/",
                json={"legs": "aleg", "aleg_url": destination, "aleg_method": "GET"},
                auth=self._auth(),
            )
            if resp.status_code >= 400:
                raise TelephonyProviderError(f"Plivo transfer failed: {resp.text}")

    async def get_call(self, *, provider_call_id: str) -> CallStatusResult:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{self._base_url()}/Call/{provider_call_id}/", auth=self._auth())
            if resp.status_code >= 400:
                raise TelephonyProviderError(f"Plivo get_call failed: {resp.text}")
            data = resp.json()
            return CallStatusResult(
                provider_call_id=provider_call_id,
                status=_STATUS_MAP.get(data.get("call_state", "").lower(), "unknown"),
                duration_seconds=data.get("call_duration"),
                recording_url=None,
                raw=data,
            )

    def parse_inbound_webhook(self, *, headers: dict, body: dict) -> dict:
        return {
            "provider_call_id": body.get("CallUUID", ""),
            "from_number": body.get("From", ""),
            "to_number": body.get("To", ""),
            "direction": "inbound",
        }

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._base_url()}/", auth=self._auth())
                return resp.status_code < 400
        except httpx.HTTPError:
            return False
