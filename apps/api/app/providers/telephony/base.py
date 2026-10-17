"""
Telephony provider abstraction. Wraps outbound dialing, hangup, transfer
and inbound-call handling for whichever carrier a tenant uses (Exotel,
Plivo, or a generic SIP trunk fronted by our own Asterisk box).
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class DialResult:
    provider_call_id: str
    status: str  # queued | dialing | failed
    raw: dict


@dataclass
class CallStatusResult:
    provider_call_id: str
    status: str
    duration_seconds: Optional[int]
    recording_url: Optional[str]
    raw: dict


class TelephonyProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class TelephonyProvider(ABC):
    provider_name: str = "base"

    @abstractmethod
    async def dial(self, *, to_number: str, from_number: str, webhook_url: str,
                    metadata: Optional[dict] = None) -> DialResult:
        ...

    @abstractmethod
    async def hangup(self, *, provider_call_id: str) -> None:
        ...

    @abstractmethod
    async def transfer(self, *, provider_call_id: str, destination: str) -> None:
        ...

    @abstractmethod
    async def get_call(self, *, provider_call_id: str) -> CallStatusResult:
        ...

    @abstractmethod
    def parse_inbound_webhook(self, *, headers: dict, body: dict) -> dict:
        """Normalizes an inbound-call webhook payload into a common shape."""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        ...
