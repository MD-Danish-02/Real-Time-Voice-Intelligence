"""
Text-to-speech provider abstraction.
Chatterbox Multilingual/Hindi is the primary default provider for real-time phone calling.
IndicF5 is an optional provider for Indian language quality comparison.
"""
from abc import ABC, abstractmethod
from typing import AsyncGenerator, Optional

from app.core.config import settings
from app.providers.voice.tts.chatterbox import (
    ChatterboxTTSProvider,
    SynthesisMetrics,
    SynthesisResult,
    TELEPHONY_SAMPLE_RATE,
)
from app.providers.voice.tts.indic_f5 import IndicF5TTSProvider
from app.providers.provider_safety import require_mock_provider


class TTSProvider(ABC):
    provider_name: str = "base"

    def __new__(cls, *args, **kwargs):
        require_mock_provider(cls.provider_name, "TTS")
        return super().__new__(cls)

    @abstractmethod
    async def synthesize(self, *, text: str, voice_id: str = "default", language: str = "hi") -> SynthesisResult:
        ...

    async def synthesize_stream(
        self, *, text: str, voice_id: str = "default", language: str = "hi"
    ) -> AsyncGenerator[bytes, None]:
        res = await self.synthesize(text=text, voice_id=voice_id, language=language)
        yield res.audio_bytes


class MockTTSProvider(TTSProvider):
    """Returns deterministic 16kHz mono PCM16 audio for fast test execution."""

    provider_name = "mock"

    async def synthesize(self, *, text: str, voice_id: str = "default", language: str = "hi") -> SynthesisResult:
        # 0.2s of 16kHz mono audio (6400 bytes) with a light tone so test waveform is non-trivial
        samples = [int(1000 * ((i % 32) / 16.0 - 1.0)) for i in range(3200)]
        import struct

        pcm = struct.pack(f"<{len(samples)}h", *samples)
        metrics = SynthesisMetrics(
            time_to_first_audio_ms=1.5,
            total_latency_ms=2.0,
            audio_duration_ms=200.0,
            real_time_factor=0.01,
            samples_per_second=1600000.0,
            chunk_count=1,
        )
        return SynthesisResult(audio_bytes=pcm, sample_rate=TELEPHONY_SAMPLE_RATE, format="pcm16", metrics=metrics)

    async def synthesize_stream(
        self, *, text: str, voice_id: str = "default", language: str = "hi"
    ) -> AsyncGenerator[bytes, None]:
        res = await self.synthesize(text=text, voice_id=voice_id, language=language)
        yield res.audio_bytes


def get_tts_provider(mode: Optional[str] = None) -> TTSProvider:
    selected = mode or settings.TTS_PROVIDER_NAME or settings.TTS_PROVIDER_MODE
    require_mock_provider(selected, "TTS")
    if selected == "chatterbox":
        return ChatterboxTTSProvider()
    if selected == "kokoro_onnx":
        from app.providers.voice.tts.kokoro import KokoroTTSProvider

        return KokoroTTSProvider()
    if selected == "kokoro":
        from app.providers.voice.tts.kokoro import KokoroTTSProvider

        return KokoroTTSProvider()
    if selected == "indic_f5":
        return IndicF5TTSProvider()
    if selected == "mock":
        return MockTTSProvider()
    # Default to Chatterbox
    return ChatterboxTTSProvider()


__all__ = [
    "TTSProvider",
    "SynthesisResult",
    "SynthesisMetrics",
    "ChatterboxTTSProvider",
    "KokoroTTSProvider",
    "IndicF5TTSProvider",
    "MockTTSProvider",
    "get_tts_provider",
    "TELEPHONY_SAMPLE_RATE",
]
