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
from typing import Optional

import httpx
import websockets

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("asterisk.ari")


class AsteriskARIClient:
    def __init__(self) -> None:
        self.base_url = settings.ARI_BASE_URL
        self.auth = (settings.ARI_USERNAME, settings.ARI_PASSWORD)
        self.app_name = settings.ARI_APP_NAME

    async def ping(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.base_url}/asterisk/info", auth=self.auth)
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def originate(self, *, endpoint: str, app: str, caller_id: Optional[str] = None,
                         variables: Optional[dict] = None) -> dict:
        params = {"endpoint": endpoint, "app": app}
        if caller_id:
            params["callerId"] = caller_id
        payload = {"variables": variables or {}}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{self.base_url}/channels", params=params, json=payload, auth=self.auth)
            resp.raise_for_status()
            return resp.json()

    async def answer_channel(self, channel_id: str) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{self.base_url}/channels/{channel_id}/answer", auth=self.auth)
            resp.raise_for_status()

    async def hangup_channel(self, channel_id: str) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(f"{self.base_url}/channels/{channel_id}", auth=self.auth)
            if resp.status_code not in (204, 404):
                resp.raise_for_status()

    async def redirect_channel(self, channel_id: str, destination_endpoint: str) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self.base_url}/channels/{channel_id}/redirect",
                params={"endpoint": destination_endpoint}, auth=self.auth,
            )
            resp.raise_for_status()

    async def get_channel(self, channel_id: str) -> Optional[dict]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{self.base_url}/channels/{channel_id}", auth=self.auth)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()

    async def start_recording(self, channel_id: str, name: str) -> dict:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self.base_url}/channels/{channel_id}/record",
                params={"name": name, "format": "wav", "ifExists": "overwrite"}, auth=self.auth,
            )
            resp.raise_for_status()
            return resp.json()

    async def create_external_media_channel(self, *, external_host: str, encapsulation: str = "rtp",
                                              transport: str = "udp", format_: str = "slin16") -> dict:
        """
        Bridges Asterisk audio to an external media endpoint (our voice
        worker running Pipecat) via ARI externalMedia - this is how
        raw audio reaches the STT/TTS pipeline.
        """
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self.base_url}/channels/externalMedia",
                params={
                    "app": self.app_name, "external_host": external_host,
                    "encapsulation": encapsulation, "transport": transport, "format": format_,
                },
                auth=self.auth,
            )
            resp.raise_for_status()
            return resp.json()

    async def create_bridge(self, *, bridge_type: str = "mixing", name: Optional[str] = None) -> dict:
        """Creates an ARI bridge (typically 'mixing' for multi-channel audio)."""
        params = {"type": bridge_type}
        if name:
            params["name"] = name
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{self.base_url}/bridges", params=params, auth=self.auth)
            resp.raise_for_status()
            return resp.json()

    async def add_channel_to_bridge(self, bridge_id: str, channel_id: str) -> None:
        """Adds a channel (caller channel or externalMedia channel) into the bridge."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self.base_url}/bridges/{bridge_id}/addChannel",
                params={"channel": channel_id},
                auth=self.auth,
            )
            resp.raise_for_status()

    async def remove_channel_from_bridge(self, bridge_id: str, channel_id: str) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self.base_url}/bridges/{bridge_id}/removeChannel",
                params={"channel": channel_id},
                auth=self.auth,
            )
            if resp.status_code not in (200, 204, 404):
                resp.raise_for_status()

    async def destroy_bridge(self, bridge_id: str) -> None:
        """Destroys an ARI bridge."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(f"{self.base_url}/bridges/{bridge_id}", auth=self.auth)
            if resp.status_code not in (204, 404):
                resp.raise_for_status()

    async def get_channel_variable(self, channel_id: str, variable: str) -> Optional[str]:
        """Gets a channel variable (e.g. UNICASTRTP_LOCAL_PORT, UNICASTRTP_LOCAL_ADDRESS)."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{self.base_url}/channels/{channel_id}/variable",
                params={"variable": variable},
                auth=self.auth,
            )
            if resp.status_code == 200:
                return resp.json().get("value")
            return None

    async def event_stream(self):
        """
        Async generator yielding parsed ARI events from the WebSocket
        stream (StasisStart, StasisEnd, ChannelStateChange, etc). The
        voice worker consumes this to drive call lifecycle + hand audio
        off to the Pipecat pipeline.
        """
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
    client = AsteriskARIClient()
    while True:
        try:
            async for event in client.event_stream():
                await handler(event)
        except Exception as exc:  # noqa: BLE001 - deliberate broad catch to keep the worker alive
            logger.error("ari_event_loop_error", error=str(exc))
            await asyncio.sleep(3)
