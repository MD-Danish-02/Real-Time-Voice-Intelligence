"""
Real Asterisk External Media ↔ Voice Pipeline RTP Media Bridge.

Handles:
- Dynamic UDP port allocation per call session
- RTP packet unpacking (receiving caller audio from Asterisk External Media)
- RTP packetization (sending AI speech audio back to Asterisk External Media)
- Internal audio: PCM16, 16000 Hz, mono. RTP codec: G.711 mu-law, 8000 Hz (static PT 0).
- 20ms packet pacing (320 samples = 640 bytes per packet)
- Interruption / barge-in playback clearing
- Complete lifecycle cleanup per call
"""
import asyncio
import os
import random
import socket
import struct
import threading
import time
from typing import Callable, Optional

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("voice.rtp_bridge")

RTP_HEADER_SIZE = 12
DEFAULT_PAYLOAD_TYPE = 0  # Static RTP payload type for G.711 mu-law (ulaw)
SAMPLES_PER_FRAME_20MS = 320  # 20ms at 16000 Hz
BYTES_PER_FRAME_20MS = 640  # 320 samples * 2 bytes (int16)
SAMPLE_RATE = 16000
ULAW_PAYLOAD_TYPE = 0
ULAW_BIAS = 0x84
ULAW_CLIP = 32635


def _pcm16_sample_to_ulaw(sample: int) -> int:
    """Encode one signed PCM16 sample as a G.711 mu-law octet."""
    sample = max(-32768, min(32767, int(sample)))
    sign = 0x80 if sample < 0 else 0
    magnitude = min(abs(sample), ULAW_CLIP) + ULAW_BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (magnitude & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def _ulaw_octet_to_pcm16_sample(value: int) -> int:
    """Decode one G.711 mu-law octet to signed PCM16."""
    value = (~value) & 0xFF
    magnitude = ((value & 0x0F) << 3) + ULAW_BIAS
    magnitude <<= (value & 0x70) >> 4
    return ULAW_BIAS - magnitude if value & 0x80 else magnitude - ULAW_BIAS


def pcm16le_16k_to_ulaw_8k(pcm_bytes: bytes) -> bytes:
    """Low-pass by pair averaging, decimate 16 kHz PCM16, then G.711 encode."""
    usable_length = len(pcm_bytes) - (len(pcm_bytes) % 4)
    encoded = bytearray(usable_length // 4)
    for out_index, offset in enumerate(range(0, usable_length, 4)):
        first, second = struct.unpack_from("<hh", pcm_bytes, offset)
        encoded[out_index] = _pcm16_sample_to_ulaw((first + second) // 2)
    return bytes(encoded)


def ulaw_8k_to_pcm16le_16k(payload: bytes) -> bytes:
    """Decode G.711 mu-law and linearly upsample 8 kHz samples to 16 kHz PCM16."""
    if not payload:
        return b""
    samples = [_ulaw_octet_to_pcm16_sample(value) for value in payload]
    output = bytearray(len(samples) * 4)
    for index, sample in enumerate(samples):
        next_sample = samples[index + 1] if index + 1 < len(samples) else sample
        struct.pack_into("<hh", output, index * 4, sample, (sample + next_sample) // 2)
    return bytes(output)


class RTPPortPoolExhaustedError(RuntimeError):
    """Raised when all ports in the approved RTP pool are exhausted."""
    pass


class RTPPacketizer:
    """RTP packet serialization and deserialization for telephony audio."""

    def __init__(self, payload_type: int = DEFAULT_PAYLOAD_TYPE, ssrc: Optional[int] = None) -> None:
        self.payload_type = payload_type
        self.ssrc = ssrc if ssrc is not None else random.randint(10000, 999999)
        self.sequence_number = random.randint(0, 10000)
        self.timestamp = random.randint(0, 100000)

    def pack(self, payload_bytes: bytes, *, timestamp_samples: Optional[int] = None) -> bytes:
        """Packs an RTP payload; timestamp_samples is in the payload codec's clock rate."""
        header = struct.pack(
            "!BBHII",
            0x80,  # V=2, P=0, X=0, CC=0
            self.payload_type & 0x7F,
            self.sequence_number & 0xFFFF,
            self.timestamp & 0xFFFFFFFF,
            self.ssrc & 0xFFFFFFFF,
        )
        self.sequence_number = (self.sequence_number + 1) & 0xFFFF
        num_samples = timestamp_samples if timestamp_samples is not None else len(payload_bytes) // 2
        self.timestamp = (self.timestamp + num_samples) & 0xFFFFFFFF
        return header + payload_bytes

    @staticmethod
    def unpack(packet_bytes: bytes) -> tuple[int, int, int, bytes]:
        """
        Parses an RTP packet.
        Returns: (payload_type, sequence_number, timestamp, payload_bytes)
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
        bound_socket: Optional[socket.socket] = None,
        on_audio_received: Optional[Callable[[bytes], None]] = None,
        on_interrupted: Optional[Callable[[], None]] = None,
    ) -> None:
        self.call_id = call_id
        self.local_port = local_port
        self.on_audio_received = on_audio_received
        self.on_interrupted = on_interrupted

        self.remote_host: Optional[str] = None
        self.remote_port: Optional[int] = None

        self._socket: Optional[socket.socket] = bound_socket
        self._packetizer = RTPPacketizer()
        self._outbound_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._running = False
        self._rx_task: Optional[asyncio.Task] = None
        self._tx_task: Optional[asyncio.Task] = None
        self._is_playing = False
        self.received_packet_count = 0
        self.received_byte_count = 0
        self.transmitted_packet_count = 0
        self.transmitted_byte_count = 0

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
        """Binds socket or uses pre-bound socket, launching async RX and TX packet loops."""
        if self._running:
            return

        if self._socket is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", self.local_port))
            sock.setblocking(False)
            self._socket = sock
        else:
            self._socket.setblocking(False)

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
                if len(data) < RTP_HEADER_SIZE:
                    continue

                # Auto-discover remote endpoint or update if remote_host was placeholder
                if (not self.remote_host) or self.remote_host in ("0.0.0.0", "127.0.0.1", "asterisk") or (not self.remote_port):
                    self.remote_host, self.remote_port = addr
                    logger.info("rtp_auto_discovered_remote", call_id=self.call_id, addr=addr)

                payload_type, _, _, ulaw_payload = RTPPacketizer.unpack(data)
                if payload_type != ULAW_PAYLOAD_TYPE:
                    continue
                self.received_packet_count += 1
                self.received_byte_count += len(data)
                pcm_payload = ulaw_8k_to_pcm16le_16k(ulaw_payload)
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
                sent_packets = 0
                sent_bytes = 0
                send_errors = 0
                frame_duration = 0.020  # 20ms
                if not self.remote_host or not self.remote_port:
                    logger.warning(
                        "rtp_tx_no_remote_endpoint",
                        call_id=self.call_id,
                        local_port=self.local_port,
                        audio_bytes=len(pcm_data),
                    )

                while offset < len(pcm_data) and self._running and self._is_playing:
                    t_frame_start = time.perf_counter()
                    chunk = pcm_data[offset : offset + BYTES_PER_FRAME_20MS]
                    offset += BYTES_PER_FRAME_20MS

                    if len(chunk) < BYTES_PER_FRAME_20MS:
                        # Pad final frame with silence to keep exact 20ms
                        chunk = chunk + b"\x00\x00" * ((BYTES_PER_FRAME_20MS - len(chunk)) // 2)

                    if self.remote_host and self.remote_port:
                        ulaw_payload = pcm16le_16k_to_ulaw_8k(chunk)
                        rtp_packet = self._packetizer.pack(
                            ulaw_payload,
                            timestamp_samples=len(ulaw_payload),
                        )
                        try:
                            packet_bytes = sock.sendto(rtp_packet, (self.remote_host, self.remote_port))
                            sent_bytes += packet_bytes
                            sent_packets += 1
                            self.transmitted_byte_count += packet_bytes
                            self.transmitted_packet_count += 1
                        except Exception as e:
                            send_errors += 1
                            logger.warning(
                                "rtp_tx_sendto_error",
                                call_id=self.call_id,
                                destination_host=self.remote_host,
                                destination_port=self.remote_port,
                                error_type=type(e).__name__,
                            )

                    elapsed = time.perf_counter() - t_frame_start
                    sleep_time = max(0.001, frame_duration - elapsed)
                    await asyncio.sleep(sleep_time)

                self._is_playing = False
                self._outbound_queue.task_done()
                logger.info(
                    "rtp_tx_audio_completed",
                    call_id=self.call_id,
                    destination_host=self.remote_host,
                    destination_port=self.remote_port,
                    audio_bytes=len(pcm_data),
                    audio_sample_rate=16000,
                    audio_duration_ms=round(len(pcm_data) / (16000 * 2) * 1000, 1),
                    rtp_packets_sent=sent_packets,
                    rtp_bytes_sent=sent_bytes,
                    send_errors=send_errors,
                )
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
        logger.info(
            "rtp_media_session_closed",
            call_id=self.call_id,
            allocated_rtp_port=self.local_port,
            remote_rtp_host=self.remote_host,
            remote_rtp_port=self.remote_port,
            rtp_packets_received=self.received_packet_count,
            rtp_bytes_received=self.received_byte_count,
            rtp_packets_transmitted=self.transmitted_packet_count,
            rtp_bytes_transmitted=self.transmitted_byte_count,
            socket_closed=self._socket is None,
        )


class MediaSessionManager:
    """Manages active RTP media sessions and UDP port allocation pool."""

    def __init__(self, port_start: int = 10000, port_end: int = 10020) -> None:
        self.port_start = port_start
        self.port_end = port_end
        self._sessions: dict[str, CallMediaSession] = {}
        self._allocated_sockets: dict[int, socket.socket] = {}
        # create_session() holds this across reservation, socket binding and
        # session registration; reentrancy lets it call allocate_port() safely.
        self._lock = threading.RLock()

    def allocate_port(self) -> int:
        """
        Finds and atomically binds an available UDP port in the configured port pool.
        Retains the bound socket open in self._allocated_sockets to prevent race conditions.
        Raises RTPPortPoolExhaustedError when all ports are exhausted (never falls back to ephemeral).
        """
        bind_failures = 0
        with self._lock:
            for port in range(self.port_start, self.port_end + 1):
                if port not in self._allocated_sockets:
                    sock: Optional[socket.socket] = None
                    try:
                        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        # Do not enable SO_REUSEADDR: the reservation must be exclusive
                        # so another process cannot share an allocated RTP port.
                        sock.setblocking(False)
                        sock.bind(("0.0.0.0", port))
                        self._allocated_sockets[port] = sock
                        return port
                    except OSError:
                        bind_failures += 1
                        if sock is not None:
                            try:
                                sock.close()
                            except OSError:
                                pass
                        continue
            logger.error(
                "rtp_port_pool_exhausted",
                port_start=self.port_start,
                port_end=self.port_end,
                allocated_port_count=len(self._allocated_sockets),
                active_session_count=len(self._sessions),
                bind_conflict_count=bind_failures,
            )
            raise RTPPortPoolExhaustedError(
                f"All RTP ports in range {self.port_start}-{self.port_end} are allocated or unavailable."
            )

    def release_port(self, port: int) -> None:
        """Explicitly releases an allocated port and closes its socket."""
        with self._lock:
            sock = self._allocated_sockets.pop(port, None)
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

    def create_session(
        self,
        call_id: str,
        on_audio_received: Optional[Callable[[bytes], None]] = None,
        on_interrupted: Optional[Callable[[], None]] = None,
    ) -> CallMediaSession:
        """Creates and starts an isolated media session for a call with atomic socket ownership.

        If session construction or startup fails the allocated port is released via
        release_port() so the socket is closed and the port is returned to the pool.
        The original exception is always re-raised; cleanup errors are logged but never
        replace it.
        """
        with self._lock:
            existing = self._sessions.get(call_id)
            if existing:
                return existing

            port = self.allocate_port()
            bound_sock = self._allocated_sockets.get(port)
            session: Optional[CallMediaSession] = None
            try:
                session = CallMediaSession(
                    call_id=call_id,
                    local_port=port,
                    bound_socket=bound_sock,
                    on_audio_received=on_audio_received,
                    on_interrupted=on_interrupted,
                )
                session.start()
                self._sessions[call_id] = session
                return session
            except BaseException:
                # Includes asyncio cancellation during synchronous startup; never
                # orphan the socket acquired for this not-yet-registered session.
                try:
                    if session is not None:
                        session.close()
                except BaseException as cleanup_err:
                    logger.warning(
                        "rtp_partial_session_cleanup_failed",
                        call_id=call_id,
                        port=port,
                        error_type=type(cleanup_err).__name__,
                    )
                try:
                    self.release_port(port)
                except BaseException as cleanup_err:  # pragma: no cover
                    logger.warning(
                        "rtp_create_session_cleanup_failed",
                        call_id=call_id,
                        port=port,
                        error_type=type(cleanup_err).__name__,
                    )
                raise

    def get_session(self, call_id: str) -> Optional[CallMediaSession]:
        with self._lock:
            return self._sessions.get(call_id)

    def close_session(self, call_id: str) -> None:
        """Closes a media session and releases its UDP port and socket."""
        with self._lock:
            session = self._sessions.pop(call_id, None)
            if session:
                port = session.local_port
                try:
                    session.close()
                finally:
                    # Close the manager's owning socket even if session teardown
                    # itself raises while cancelling its receive/transmit tasks.
                    sock = self._allocated_sockets.pop(port, None)
                    if sock:
                        try:
                            sock.close()
                        except OSError:
                            pass


# Global media session manager singleton
media_session_manager = MediaSessionManager(
    port_start=settings.VOICE_RTP_PORT_START,
    port_end=settings.VOICE_RTP_PORT_END,
)
