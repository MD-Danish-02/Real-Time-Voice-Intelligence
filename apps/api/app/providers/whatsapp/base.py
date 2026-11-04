"""
WhatsApp provider abstraction. Meta's WhatsApp Cloud API is the real
adapter; sending messages always costs money per Meta's own pricing once
a tenant goes live - this is never presented as free anywhere in the UI.
"""
import hashlib
import hmac
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import httpx

from app.core.config import settings
from app.providers.provider_safety import require_mock_provider


@dataclass
class WhatsAppSendResult:
    provider_message_id: str
    status: str


class WhatsAppProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class WhatsAppProvider(ABC):
    provider_name: str = "base"

    def __new__(cls, *args, **kwargs):
        require_mock_provider(cls.provider_name, "WhatsApp")
        return super().__new__(cls)

    @abstractmethod
    async def send_message(self, *, to_number: str, body: str) -> WhatsAppSendResult:
        ...

    @abstractmethod
    async def send_template(self, *, to_number: str, template_name: str, language: str,
                             parameters: Optional[list[str]] = None) -> WhatsAppSendResult:
        ...

    @abstractmethod
    async def get_status(self, *, provider_message_id: str) -> str:
        ...

    @abstractmethod
    def verify_webhook_signature(self, *, headers: dict, body: bytes) -> bool:
        ...


class MockWhatsAppProvider(WhatsAppProvider):
    provider_name = "mock"

    async def send_message(self, *, to_number: str, body: str) -> WhatsAppSendResult:
        return WhatsAppSendResult(provider_message_id=f"mock-wa-{uuid.uuid4().hex[:10]}", status="sent")

    async def send_template(self, *, to_number: str, template_name: str, language: str,
                             parameters: Optional[list[str]] = None) -> WhatsAppSendResult:
        return WhatsAppSendResult(provider_message_id=f"mock-wa-{uuid.uuid4().hex[:10]}", status="sent")

    async def get_status(self, *, provider_message_id: str) -> str:
        return "delivered"

    def verify_webhook_signature(self, *, headers: dict, body: bytes) -> bool:
        return True


class MetaCloudAPIWhatsAppProvider(WhatsAppProvider):
    """Real Meta (Facebook) WhatsApp Cloud API adapter."""
    provider_name = "meta_cloud_api"

    def _base_url(self) -> str:
        return f"https://graph.facebook.com/v20.0/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"

    async def send_message(self, *, to_number: str, body: str) -> WhatsAppSendResult:
        payload = {"messaging_product": "whatsapp", "to": to_number, "type": "text", "text": {"body": body}}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(self._base_url(), json=payload,
                                      headers={"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"})
            if resp.status_code >= 400:
                raise WhatsAppProviderError(f"WhatsApp send_message failed: {resp.text}",
                                             retryable=resp.status_code >= 500)
            data = resp.json()
            return WhatsAppSendResult(provider_message_id=data["messages"][0]["id"], status="sent")

    async def send_template(self, *, to_number: str, template_name: str, language: str,
                             parameters: Optional[list[str]] = None) -> WhatsAppSendResult:
        components = []
        if parameters:
            components.append({"type": "body", "parameters": [{"type": "text", "text": p} for p in parameters]})
        payload = {
            "messaging_product": "whatsapp", "to": to_number, "type": "template",
            "template": {"name": template_name, "language": {"code": language}, "components": components},
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(self._base_url(), json=payload,
                                      headers={"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"})
            if resp.status_code >= 400:
                raise WhatsAppProviderError(f"WhatsApp send_template failed: {resp.text}",
                                             retryable=resp.status_code >= 500)
            data = resp.json()
            return WhatsAppSendResult(provider_message_id=data["messages"][0]["id"], status="sent")

    async def get_status(self, *, provider_message_id: str) -> str:
        # Delivery/read status for WhatsApp arrives via webhook events, not
        # a pollable per-message endpoint - status is tracked from those
        # webhooks in the service layer, not fetched here.
        return "unknown"

    def verify_webhook_signature(self, *, headers: dict, body: bytes) -> bool:
        signature = headers.get("x-hub-signature-256", "")
        if not signature.startswith("sha256="):
            return False
        expected = "sha256=" + hmac.new(settings.WHATSAPP_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)


def get_whatsapp_provider(provider_name: str) -> WhatsAppProvider:
    require_mock_provider(provider_name, "WhatsApp")
    if provider_name == "meta_cloud_api":
        return MetaCloudAPIWhatsAppProvider()
    return MockWhatsAppProvider()
