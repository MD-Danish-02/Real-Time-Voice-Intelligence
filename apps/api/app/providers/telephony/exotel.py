"""
Real Exotel adapter using their Voice API (Connect/Calls resource). This
matches Danish's existing production stack, ported behind the common
TelephonyProvider interface. Requires EXOTEL_SID/API_KEY/API_TOKEN from
the Exotel dashboard; not live-tested here (no network path to
exotel.com in this sandbox).
"""
from typing import Optional

import httpx

from app.core.config import settings
from app.providers.telephony.base import CallStatusResult, DialResult, TelephonyProvider, TelephonyProviderError

_STATUS_MAP = {
    "queued": "queued", "in-progress": "in_progress", "completed": "completed",
    "failed": "failed", "busy": "busy", "no-answer": "no_answer", "ringing": "ringing",
}


class ExotelTelephonyProvider(TelephonyProvider):
    provider_name = "exotel"

    def _base_url(self) -> str:
        return f"https://{settings.EXOTEL_SUBDOMAIN}/v1/Accounts/{settings.EXOTEL_SID}"

    def _auth(self) -> tuple[str, str]:
        return settings.EXOTEL_API_KEY, settings.EXOTEL_API_TOKEN

    async def dial(self, *, to_number: str, from_number: str, webhook_url: str,
                    metadata: Optional[dict] = None) -> DialResult:
        payload = {
            "From": from_number or settings.EXOTEL_CALLER_ID,
            "To": to_number,
            "CallerId": settings.EXOTEL_CALLER_ID,
            "StatusCallback": webhook_url,
            "StatusCallbackEvents[0]": "terminal",
            "StatusCallbackEvents[1]": "answered",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{self._base_url()}/Calls/connect.json", data=payload, auth=self._auth())
            if resp.status_code >= 400:
                raise TelephonyProviderError(f"Exotel dial failed: {resp.text}", retryable=resp.status_code >= 500)
            data = resp.json().get("Call", {})
            return DialResult(
                provider_call_id=data.get("Sid", ""),
                status=_STATUS_MAP.get(data.get("Status", "queued"), "queued"),
                raw=data,
            )

    async def hangup(self, *, provider_call_id: str) -> None:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self._base_url()}/Calls/{provider_call_id}.json",
                data={"Status": "completed"}, auth=self._auth(),
            )
            if resp.status_code >= 400:
                raise TelephonyProviderError(f"Exotel hangup failed: {resp.text}")

    async def transfer(self, *, provider_call_id: str, destination: str) -> None:
        raise TelephonyProviderError(
            "Exotel call transfer requires an Exoml applet configured for the flow; "
            "not supported via the plain Calls API.", retryable=False,
        )

    async def get_call(self, *, provider_call_id: str) -> CallStatusResult:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{self._base_url()}/Calls/{provider_call_id}.json", auth=self._auth())
            if resp.status_code >= 400:
                raise TelephonyProviderError(f"Exotel get_call failed: {resp.text}")
            data = resp.json().get("Call", {})
            duration = data.get("Duration")
            return CallStatusResult(
                provider_call_id=provider_call_id,
                status=_STATUS_MAP.get(data.get("Status", "unknown"), "unknown"),
                duration_seconds=int(duration) if duration else None,
                recording_url=data.get("RecordingUrl"),
                raw=data,
            )

    def parse_inbound_webhook(self, *, headers: dict, body: dict) -> dict:
        return {
            "provider_call_id": body.get("CallSid", ""),
            "from_number": body.get("CallFrom", ""),
            "to_number": body.get("CallTo", ""),
            "direction": "inbound",
        }

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._base_url()}.json", auth=self._auth())
                return resp.status_code < 400
        except httpx.HTTPError:
            return False
