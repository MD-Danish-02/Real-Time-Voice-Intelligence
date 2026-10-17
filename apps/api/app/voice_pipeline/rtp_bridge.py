"""
Real Asterisk External Media ↔ Voice Pipeline RTP Media Bridge.

Handles:
- Dynamic UDP port allocation per call session
- RTP packet unpacking (receiving caller audio from Asterisk External Media)
- RTP packetization (sending AI speech audio back to Asterisk External Media)
- Codec: Signed Linear 16-bit, 16000 Hz, mono (slin16)
- 20ms packet pacing (320 samples = 640 bytes per packet)
- Interruption / barge-in playback clearing
- Complete lifecycle cleanup per call
"""
import asyncio
import os
import random
import socket
import struct
import time
from typing import Callable, Optional

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("voice.rtp_bridge")

RTP_HEADER_SIZE = 12
DEFAULT_PAYLOAD_TYPE = 96  # Dynamic payload type for Asterisk slin16
SAMPLES_PER_FRAME_20MS = 320  # 20ms at 16000 Hz
BYTES_PER_FRAME_20MS = 640  # 320 samples * 2 bytes (int16)
SAMPLE_RATE = 16000


class RTPPacketizer:
    """RTP packet serialization and deserialization for telephony audio."""

    def __init__(self, payload_type: int = DEFAULT_PAYLOAD_TYPE, ssrc: Optional[int] = None) -> None:
        self.payload_type = payload_type
        self.ssrc = ssrc if ssrc is not None else random.randint(10000, 999999)
        self.sequence_number = random.randint(0, 10000)
        self.timestamp = random.randint(0, 100000)

    def pack(self, pcm_bytes: bytes) -> bytes:
        """Packs raw PCM16 bytes into an RTP packet (RFC 3550)."""
        header = struct.pack(
            "!BBHII",
            0x80,  # V=2, P=0, X=0, CC=0
            self.payload_type & 0x7F,
            self.sequence_number & 0xFFFF,
            self.timestamp & 0xFFFFFFFF,
            self.ssrc & 0xFFFFFFFF,
        )
        self.sequence_number = (self.sequence_number + 1) & 0xFFFF
        num_samples = len(pcm_bytes) // 2
        self.timestamp = (self.timestamp + num_samples) & 0xFFFFFFFF
        return header + pcm_bytes

    @staticmethod
    def unpack(packet_bytes: bytes) -> tuple[int, int, int, bytes]:
        """
        Parses an RTP packet.
        Returns: (payload_type, sequence_number, timestamp, pcm_payload)
        """
        if len(packet_bytes) < RTP_HEADER_SIZE:
            return 0, 0, 0, b""

        byte0, byte1, seq, ts, ssrc = struct.unpack("!BBHII", packet_bytes[:RTP_HEADER_SIZE])
        cc = byte0 & 0x0F
        has_extension = bool(byte0 & 0x10)
        payload_type = byte1 & 0x7F

        offset = RTP_HEADER_SIZE + (cc * 4)
        if has_extension and len(packet_bytes) >= offset + 4:
            ext_len = struct.unpack("!H", packet_bytes[offset + 2 : offset + 4])[0]
            offset += 4 + (ext_len * 4)

        pcm_payload = packet_bytes[offset:]
        return payload_type, seq, ts, pcm_payload


class CallMediaSession:
    """
    Isolated media bridge session for one phone call.
    Binds a local UDP socket, receives caller RTP from Asterisk,
    transmits AI synthesized speech back via RTP to Asterisk.
    """

    def __init__(
        self,
        call_id: str,
        local_port: int,
        on_audio_received: Optional[Callable[[bytes], None]] = None,
        on_interrupted: Optional[Callable[[], None]] = None,
    ) -> None:
        self.call_id = call_id
        self.local_port = local_port
        self.on_audio_received = on_audio_received
        self.on_interrupted = on_interrupted

        self.remote_host: Optional[str] = None
        self.remote_port: Optional[int] = None

        self._socket: Optional[socket.socket] = None
        self._packetizer = RTPPacketizer()
        self._outbound_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._running = False
        self._rx_task: Optional[asyncio.Task] = None
        self._tx_task: Optional[asyncio.Task] = None
        self._is_playing = False

    def set_remote_endpoint(self, host: str, port: int) -> None:
        """Sets destination Asterisk IP and port where AI speech RTP must be sent."""
        if host in ("0.0.0.0", "127.0.0.1", "localhost", "asterisk"):
            if self.remote_host and self.remote_host not in ("0.0.0.0", "127.0.0.1", "localhost", "asterisk"):
                self.remote_port = port
                return
        self.remote_host = host
        self.remote_port = port
        logger.info(
            "rtp_remote_endpoint_set",
            call_id=self.call_id,
            host=host,
            port=port,
            local_port=self.local_port,
        )

    def start(self) -> None:
        """Binds socket and launches async RX and TX packet loops."""
        if self._running:
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.local_port))
        sock.setblocking(False)
        self._socket = sock
        self._running = True

        loop = asyncio.get_event_loop()
        self._rx_task = loop.create_task(self._rx_loop())
        self._tx_task = loop.create_task(self._tx_loop())
        logger.info("rtp_media_session_started", call_id=self.call_id, port=self.local_port)

    async def _rx_loop(self) -> None:
        """Continuously reads incoming RTP packets from Asterisk External Media."""
        loop = asyncio.get_event_loop()
        sock = self._socket
        assert sock is not None

        while self._running:
            try:
                data, addr = await loop.sock_recvfrom(sock, 2048)
                if not data:
                    continue

                # Auto-discover remote endpoint or update if remote_host was placeholder
                if (not self.remote_host) or self.remote_host in ("0.0.0.0", "127.0.0.1", "asterisk") or (not self.remote_port):
                    self.remote_host, self.remote_port = addr
                    logger.info("rtp_auto_discovered_remote", call_id=self.call_id, addr=addr)

                _, _, _, pcm_payload = RTPPacketizer.unpack(data)
                if pcm_payload and self.on_audio_received:
                    self.on_audio_received(pcm_payload)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self._running:
                    logger.error("rtp_rx_error", call_id=self.call_id, error=str(exc))
                await asyncio.sleep(0.01)

    async def _tx_loop(self) -> None:
        """Transmits queued AI speech audio to Asterisk in 20ms paced RTP packets."""
        loop = asyncio.get_event_loop()
        sock = self._socket
        assert sock is not None

        while self._running:
            try:
                pcm_data = await self._outbound_queue.get()
                if not self._running or not pcm_data:
                    continue

                self._is_playing = True
                offset = 0
                frame_duration = 0.020  # 20ms

                while offset < len(pcm_data) and self._running and self._is_playing:
                    t_frame_start = time.perf_counter()
                    chunk = pcm_data[offset : offset + BYTES_PER_FRAME_20MS]
                    offset += BYTES_PER_FRAME_20MS

                    if len(chunk) < BYTES_PER_FRAME_20MS:
                        # Pad final frame with silence to keep exact 20ms
                        chunk = chunk + b"\x00\x00" * ((BYTES_PER_FRAME_20MS - len(chunk)) // 2)

                    if self.remote_host and self.remote_port:
                        rtp_packet = self._packetizer.pack(chunk)
                        try:
                            sock.sendto(rtp_packet, (self.remote_host, self.remote_port))
                        except Exception as e:
                            logger.warning("rtp_tx_sendto_error", error=str(e))

                    elapsed = time.perf_counter() - t_frame_start
                    sleep_time = max(0.001, frame_duration - elapsed)
                    await asyncio.sleep(sleep_time)

                self._is_playing = False
                self._outbound_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self._running:
                    logger.error("rtp_tx_error", call_id=self.call_id, error=str(exc))
                await asyncio.sleep(0.01)

    def queue_outbound_audio(self, pcm_bytes: bytes) -> None:
        """Queues synthesized PCM16 audio for transmission to Asterisk."""
        if not self._running:
            return
        self._outbound_queue.put_nowait(pcm_bytes)

    def interrupt(self) -> None:
        """
        Barge-in: clears all pending outbound speech packets immediately
        when user speaks over AI audio playback.
        """
        self._is_playing = False
        # Drain queued audio
        while not self._outbound_queue.empty():
            try:
                self._outbound_queue.get_nowait()
                self._outbound_queue.task_done()
            except Exception:
                break

        if self.on_interrupted:
            self.on_interrupted()
        logger.info("rtp_barge_in_interrupted", call_id=self.call_id)

    def is_playing(self) -> bool:
        return self._is_playing or not self._outbound_queue.empty()

    def close(self) -> None:
        """Closes socket and cancels tasks cleanly."""
        self._running = False
        self._is_playing = False
        if self._rx_task and not self._rx_task.done():
            self._rx_task.cancel()
        if self._tx_task and not self._tx_task.done():
            self._tx_task.cancel()
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None
        logger.info("rtp_media_session_closed", call_id=self.call_id, port=self.local_port)


class MediaSessionManager:
    """Manages active RTP media sessions and UDP port allocation pool."""

    def __init__(self, port_start: int = 10000, port_end: int = 10020) -> None:
        self.port_start = port_start
        self.port_end = port_end
        self._sessions: dict[str, CallMediaSession] = {}
        self._allocated_ports: set[int] = set()

    def allocate_port(self) -> int:
        """Finds an available UDP port in the configured port pool."""
        for port in range(self.port_start, self.port_end + 1):
            if port not in self._allocated_ports:
                # Test if actually bindable
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.bind(("0.0.0.0", port))
                    s.close()
                    self._allocated_ports.add(port)
                    return port
                except OSError:
                    continue
        # Fallback to an ephemeral port
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("0.0.0.0", 0))
        port = s.getsockname()[1]
        s.close()
        self._allocated_ports.add(port)
        return port

    def create_session(
        self,
        call_id: str,
        on_audio_received: Optional[Callable[[bytes], None]] = None,
        on_interrupted: Optional[Callable[[], None]] = None,
    ) -> CallMediaSession:
        """Creates and starts an isolated media session for a call."""
        if call_id in self._sessions:
            return self._sessions[call_id]

        port = self.allocate_port()
        session = CallMediaSession(
            call_id=call_id,
            local_port=port,
            on_audio_received=on_audio_received,
            on_interrupted=on_interrupted,
        )
        session.start()
        self._sessions[call_id] = session
        return session

    def get_session(self, call_id: str) -> Optional[CallMediaSession]:
        return self._sessions.get(call_id)

    def close_session(self, call_id: str) -> None:
        """Closes a media session and releases its UDP port."""
        session = self._sessions.pop(call_id, None)
        if session:
            session.close()
            self._allocated_ports.discard(session.local_port)


# Global media session manager singleton
media_session_manager = MediaSessionManager(
    port_start=settings.VOICE_RTP_PORT_START,
    port_end=settings.VOICE_RTP_PORT_END,
)
