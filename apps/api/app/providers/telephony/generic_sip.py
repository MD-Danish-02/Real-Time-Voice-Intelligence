"""
Generic SIP adapter. Used when a tenant brings their own SIP trunk rather
than a managed provider like Exotel/Plivo - calls are originated through
our own Asterisk instance via ARI, with the trunk configured in
infrastructure/asterisk/pjsip.conf.
"""
from typing import Optional

from app.core.config import settings
from app.providers.telephony.base import CallStatusResult, DialResult, TelephonyProvider, TelephonyProviderError
from app.telephony.asterisk.ari_client import AsteriskARIClient


class GenericSIPTelephonyProvider(TelephonyProvider):
    provider_name = "generic_sip"

    def __init__(self) -> None:
        self._ari = AsteriskARIClient()

    async def dial(self, *, to_number: str, from_number: str, webhook_url: str,
                    metadata: Optional[dict] = None) -> DialResult:
        if not settings.GENERIC_SIP_HOST:
            raise TelephonyProviderError(
                "GENERIC_SIP_HOST is not configured - set it in .env before using the generic SIP provider.",
                retryable=False,
            )
        endpoint = f"PJSIP/{to_number}@{settings.GENERIC_SIP_HOST}"
        channel = await self._ari.originate(
            endpoint=endpoint, app=settings.ARI_APP_NAME, caller_id=from_number,
            variables={"WEBHOOK_URL": webhook_url, **(metadata or {})},
        )
        return DialResult(provider_call_id=channel["id"], status="dialing", raw=channel)

    async def hangup(self, *, provider_call_id: str) -> None:
        await self._ari.hangup_channel(provider_call_id)

    async def transfer(self, *, provider_call_id: str, destination: str) -> None:
        await self._ari.redirect_channel(provider_call_id, destination)

    async def get_call(self, *, provider_call_id: str) -> CallStatusResult:
        channel = await self._ari.get_channel(provider_call_id)
        state = (channel or {}).get("state", "Unknown").lower()
        status_map = {"up": "in_progress", "ring": "ringing", "ringing": "ringing", "down": "completed"}
        return CallStatusResult(
            provider_call_id=provider_call_id,
            status=status_map.get(state, "unknown"),
            duration_seconds=None,
            recording_url=None,
            raw=channel or {},
        )

    def parse_inbound_webhook(self, *, headers: dict, body: dict) -> dict:
        # Inbound generic-SIP calls arrive as ARI StasisStart events, not
        # HTTP webhooks - handled in app.telephony.asterisk.ari_client's
        # event loop instead. This method exists to satisfy the common
        # interface for symmetry with the HTTP-webhook-based providers.
        return body

    async def health_check(self) -> bool:
        return await self._ari.ping()
