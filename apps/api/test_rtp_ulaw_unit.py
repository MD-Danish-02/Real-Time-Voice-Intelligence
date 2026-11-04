"""Offline checks for the Asterisk static-payload RTP audio adapter."""
import math
import struct

from app.voice_pipeline.rtp_bridge import (
    RTPPacketizer,
    _pcm16_sample_to_ulaw,
    pcm16le_16k_to_ulaw_8k,
    ulaw_8k_to_pcm16le_16k,
)


def test_g711_ulaw_standard_codewords():
    assert _pcm16_sample_to_ulaw(0) == 0xFF
    assert _pcm16_sample_to_ulaw(32124) == 0x80
    assert _pcm16_sample_to_ulaw(-32124) == 0x00


def test_16khz_pcm_to_static_payload_and_back():
    samples = [int(9000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(320)]
    pcm = struct.pack("<320h", *samples)
    payload = pcm16le_16k_to_ulaw_8k(pcm)
    restored = ulaw_8k_to_pcm16le_16k(payload)

    assert len(payload) == 160  # 20 ms at 8 kHz, 8-bit mono
    assert len(restored) == 640  # 20 ms at 16 kHz, PCM16 mono
    assert restored != pcm  # The bridge performs real narrowband codec conversion.
    assert any(restored)


def test_rtp_uses_ulaw_static_payload_and_8khz_timestamp_clock():
    packetizer = RTPPacketizer(ssrc=12345)
    payload = bytes([0xFF]) * 160
    packet = packetizer.pack(payload, timestamp_samples=len(payload))

    payload_type, _, timestamp, unpacked = RTPPacketizer.unpack(packet)
    assert payload_type == 0
    assert unpacked == payload
    assert (packetizer.timestamp - timestamp) & 0xFFFFFFFF == 160

    second_packet = packetizer.pack(payload, timestamp_samples=160)
    _, second_sequence, second_timestamp, _ = RTPPacketizer.unpack(second_packet)
    _, first_sequence, _, _ = RTPPacketizer.unpack(packet)
    assert second_sequence == (first_sequence + 1) & 0xFFFF
    assert (second_timestamp - timestamp) & 0xFFFFFFFF == 160
