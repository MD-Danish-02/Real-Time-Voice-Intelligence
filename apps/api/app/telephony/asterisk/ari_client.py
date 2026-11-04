"""
Asterisk ARI (Asterisk REST Interface) client.

This talks to a real Asterisk instance's ARI HTTP API (channels,
bridges, playback, recording) and its WebSocket event stream. It is
implemented against Asterisk's documented ARI contract; there is no
Asterisk server reachable from this sandbox, so channel origination has
not been exercised against a live PBX here. `infrastructure/asterisk/`
contains a development Asterisk container config to test this against
locally (see docs/asterisk.md).
"""
import asyncio
import json
import re
from typing import Optional

import httpx
import websockets

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("asterisk.ari")

_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)(authorization|password|passwd|token|secret|api[_-]?key|signature|credential)"
    r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)
_PHONE_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\+?\d[\d ().-]{5,}\d(?![A-Za-z0-9])")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_URL_RE = re.compile(r"(?i)\b(?:https?|wss?)://[^\s\"'<>]+")


def sanitize_ari_diagnostic(value: str, *, limit: int = 512) -> str:
    """Bound and redact untrusted ARI response/error text before logging it."""
    text = str(value or "")[:2048]
    text = _URL_RE.sub("[URL REDACTED]", text)
    text = re.sub(r"(?i)\b(?:basic|bearer)\s+[^\s,;]+", "[AUTH REDACTED]", text)
    text = _SENSITIVE_ASSIGNMENT_RE.sub(r"\1\2[REDACTED]", text)
    text = _PHONE_NUMBER_RE.sub("[NUMBER REDACTED]", text)
    text = _EMAIL_RE.sub("[EMAIL REDACTED]", text)
    return text[:limit]


class AsteriskARIClient:
    def __init__(self) -> None:
        self.base_url = settings.ARI_BASE_URL
        self.auth = (settings.ARI_USERNAME, settings.ARI_PASSWORD)
        self.app_name = settings.ARI_APP_NAME

    @staticmethod
    def _require_network_enabled() -> None:
        if settings.ENV.strip().lower() == "test":
            raise RuntimeError("ARI network access is disabled by test safety mode.")

    @staticmethod
    def sanitize_response_body(body: str) -> str:
        """Keep only useful ARI error fields and redact possible sensitive values."""
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            return sanitize_ari_diagnostic(body)

        if isinstance(payload, dict):
            payload = {
                key: payload[key]
                for key in ("message", "error", "code")
                if key in payload and isinstance(payload[key], (str, int, float, bool))
            }
        return sanitize_ari_diagnostic(json.dumps(payload, ensure_ascii=False))

    @classmethod
    def _log_response_failure(cls, response: httpx.Response, *, operation: str) -> None:
        logger.error(
            "ari_http_request_failed",
            operation=operation,
            http_status=response.status_code,
            response_body=cls.sanitize_response_body(response.text),
        )

    @classmethod
    def _raise_for_status(cls, response: httpx.Response, *, operation: str) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            cls._log_response_failure(response, operation=operation)
            raise

    async def ping(self) -> bool:
        if settings.ENV.strip().lower() == "test":
            return False
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.base_url}/asterisk/info", auth=self.auth)
                if resp.status_code >= 400:
                    self._log_response_failure(resp, operation="ping")
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def originate(self, *, endpoint: str, app: str, caller_id: Optional[str] = None,
                         variables: Optional[dict] = None) -> dict:
        self._require_network_enabled()
        params = {"endpoint": endpoint, "app": app}
        if caller_id:
            params["callerId"] = caller_id
        payload = {"variables": variables or {}}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{self.base_url}/channels", params=params, json=payload, auth=self.auth)
            self._raise_for_status(resp, operation="originate")
            return resp.json()

    async def answer_channel(self, channel_id: str) -> None:
        self._require_network_enabled()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{self.base_url}/channels/{channel_id}/answer", auth=self.auth)
            self._raise_for_status(resp, operation="answer_channel")

    async def hangup_channel(self, channel_id: str) -> None:
        self._require_network_enabled()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(f"{self.base_url}/channels/{channel_id}", auth=self.auth)
            if resp.status_code == 404:
                self._log_response_failure(resp, operation="hangup_channel")
            if resp.status_code not in (204, 404):
                self._raise_for_status(resp, operation="hangup_channel")

    async def redirect_channel(self, channel_id: str, destination_endpoint: str) -> None:
        self._require_network_enabled()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self.base_url}/channels/{channel_id}/redirect",
                params={"endpoint": destination_endpoint}, auth=self.auth,
            )
            self._raise_for_status(resp, operation="redirect_channel")

    async def get_channel(self, channel_id: str) -> Optional[dict]:
        self._require_network_enabled()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{self.base_url}/channels/{channel_id}", auth=self.auth)
            if resp.status_code == 404:
                self._log_response_failure(resp, operation="get_channel")
                return None
            self._raise_for_status(resp, operation="get_channel")
            return resp.json()

    async def start_recording(self, channel_id: str, name: str) -> dict:
        self._require_network_enabled()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self.base_url}/channels/{channel_id}/record",
                params={"name": name, "format": "wav", "ifExists": "overwrite"}, auth=self.auth,
            )
            self._raise_for_status(resp, operation="start_recording")
            return resp.json()

    async def create_external_media_channel(self, *, external_host: str, encapsulation: str = "rtp",
                                              transport: str = "udp", format_: str = "ulaw") -> dict:
        """
        Bridges Asterisk audio to an external media endpoint (our voice
        worker running Pipecat) via ARI externalMedia - this is how
        raw audio reaches the STT/TTS pipeline.
        """
        self._require_network_enabled()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self.base_url}/channels/externalMedia",
                params={
                    "app": self.app_name, "external_host": external_host,
                    "encapsulation": encapsulation, "transport": transport, "format": format_,
                },
                auth=self.auth,
            )
            self._raise_for_status(resp, operation="create_external_media_channel")
            return resp.json()

    async def create_bridge(self, *, bridge_type: str = "mixing", name: Optional[str] = None) -> dict:
        """Creates an ARI bridge (typically 'mixing' for multi-channel audio)."""
        self._require_network_enabled()
        params = {"type": bridge_type}
        if name:
            params["name"] = name
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{self.base_url}/bridges", params=params, auth=self.auth)
            self._raise_for_status(resp, operation="create_bridge")
            return resp.json()

    async def add_channel_to_bridge(self, bridge_id: str, channel_id: str) -> None:
        """Adds a channel (caller channel or externalMedia channel) into the bridge."""
        self._require_network_enabled()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self.base_url}/bridges/{bridge_id}/addChannel",
                params={"channel": channel_id},
                auth=self.auth,
            )
            self._raise_for_status(resp, operation="add_channel_to_bridge")

    async def remove_channel_from_bridge(self, bridge_id: str, channel_id: str) -> None:
        self._require_network_enabled()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self.base_url}/bridges/{bridge_id}/removeChannel",
                params={"channel": channel_id},
                auth=self.auth,
            )
            if resp.status_code not in (200, 204, 404):
                self._raise_for_status(resp, operation="remove_channel_from_bridge")
            elif resp.status_code == 404:
                self._log_response_failure(resp, operation="remove_channel_from_bridge")

    async def destroy_bridge(self, bridge_id: str) -> None:
        """Destroys an ARI bridge."""
        self._require_network_enabled()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(f"{self.base_url}/bridges/{bridge_id}", auth=self.auth)
            if resp.status_code not in (204, 404):
                self._raise_for_status(resp, operation="destroy_bridge")
            elif resp.status_code == 404:
                self._log_response_failure(resp, operation="destroy_bridge")

    async def get_channel_variable(self, channel_id: str, variable: str) -> Optional[str]:
        """Gets a channel variable (e.g. UNICASTRTP_LOCAL_PORT, UNICASTRTP_LOCAL_ADDRESS)."""
        self._require_network_enabled()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{self.base_url}/channels/{channel_id}/variable",
                params={"variable": variable},
                auth=self.auth,
            )
            if resp.status_code == 200:
                return resp.json().get("value")
            self._log_response_failure(resp, operation="get_channel_variable")
            return None

    async def event_stream(self):
        """
        Async generator yielding parsed ARI events from the WebSocket
        stream (StasisStart, StasisEnd, ChannelStateChange, etc). The
        voice worker consumes this to drive call lifecycle + hand audio
        off to the Pipecat pipeline.
        """
        self._require_network_enabled()
        ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        uri = (f"{ws_url}/events?app={self.app_name}"
               f"&api_key={settings.ARI_USERNAME}:{settings.ARI_PASSWORD}&subscribeAll=true")
        async with websockets.connect(uri) as ws:
            async for raw_message in ws:
                try:
                    yield json.loads(raw_message)
                except json.JSONDecodeError:
                    logger.warning("ari_event_parse_failed", raw=raw_message[:200])


async def run_event_loop(handler) -> None:
    """Reconnecting wrapper: keeps the ARI event stream alive across drops."""
    if settings.ENV.strip().lower() == "test":
        raise RuntimeError("ARI event loop is disabled by test safety mode.")
    client = AsteriskARIClient()
    while True:
        try:
            async for event in client.event_stream():
                try:
                    await handler(event)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # isolate a failed call event from the ARI socket
                    logger.error(
                        "ari_event_handler_failed",
                        event_type=event.get("type") if isinstance(event, dict) else None,
                        error_type=type(exc).__name__,
                    )
        except Exception as exc:  # noqa: BLE001 - deliberate broad catch to keep the worker alive
            logger.error("ari_event_loop_error", error=str(exc))
            await asyncio.sleep(3)
