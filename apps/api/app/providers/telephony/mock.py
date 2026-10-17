"""
In-process mock telephony provider. Simulates a call lifecycle
deterministically so the call-queue, eligibility, event and analytics
paths can be exercised end-to-end without any real PSTN/SIP dependency.
No real audio is ever transmitted by this provider.
"""
import uuid
from typing import Optional

from app.providers.telephony.base import CallStatusResult, DialResult, TelephonyProvider

_MOCK_CALLS: dict[str, dict] = {}


class MockTelephonyProvider(TelephonyProvider):
    provider_name = "mock"

    async def dial(self, *, to_number: str, from_number: str, webhook_url: str,
                    metadata: Optional[dict] = None) -> DialResult:
        call_id = f"mock-call-{uuid.uuid4().hex[:12]}"
        _MOCK_CALLS[call_id] = {"status": "dialing", "to": to_number, "from": from_number, "duration": 0}
        return DialResult(provider_call_id=call_id, status="dialing", raw={"mock": True})

    async def hangup(self, *, provider_call_id: str) -> None:
        if provider_call_id in _MOCK_CALLS:
            _MOCK_CALLS[provider_call_id]["status"] = "completed"

    async def transfer(self, *, provider_call_id: str, destination: str) -> None:
        if provider_call_id in _MOCK_CALLS:
            _MOCK_CALLS[provider_call_id]["status"] = "transferred"

    async def get_call(self, *, provider_call_id: str) -> CallStatusResult:
        record = _MOCK_CALLS.get(provider_call_id, {"status": "unknown", "duration": 0})
        return CallStatusResult(
            provider_call_id=provider_call_id, status=record["status"],
            duration_seconds=record.get("duration", 0), recording_url=None, raw=record,
        )

    def parse_inbound_webhook(self, *, headers: dict, body: dict) -> dict:
        return {
            "provider_call_id": body.get("CallSid", f"mock-inbound-{uuid.uuid4().hex[:8]}"),
            "from_number": body.get("From", "+910000000000"),
            "to_number": body.get("To", "+910000000001"),
            "direction": "inbound",
        }

    async def health_check(self) -> bool:
        return True
