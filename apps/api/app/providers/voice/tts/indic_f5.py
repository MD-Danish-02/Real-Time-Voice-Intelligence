"""
IndicF5 TTS Provider (Optional / Experimental).
Intended for Indian-language phonetic quality comparison.
Note: Evaluated for latency before any real-time telephone deployment.
"""
import asyncio
import time
from typing import AsyncGenerator, Optional

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.providers.voice.tts.chatterbox import (
    TELEPHONY_SAMPLE_RATE,
    SynthesisMetrics,
    SynthesisResult,
    convert_to_pcm16_mono,
    normalize_indian_text,
    split_into_clauses,
)

logger = get_logger("voice.tts.indic_f5")


class IndicF5TTSProvider:
    """
    Experimental IndicF5 TTS Provider for Hindi and Indic language comparison.
    Logs latency warnings if RTF is unsuitable for real-time telephone calls.
    """

    provider_name: str = "indic_f5"

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.base_url = (base_url or settings.INDICF5_BASE_URL).rstrip("/")

    async def synthesize(self, *, text: str, voice_id: str = "default", language: str = "hi") -> SynthesisResult:
        t_start = time.perf_counter()
        audio_chunks: list[bytes] = []
        ttfa_ms = 0.0

        async for chunk in self.synthesize_stream(text=text, voice_id=voice_id, language=language):
            if not audio_chunks:
                ttfa_ms = (time.perf_counter() - t_start) * 1000.0
            audio_chunks.append(chunk)

        total_latency_ms = (time.perf_counter() - t_start) * 1000.0
        combined_audio = b"".join(audio_chunks)
        audio_duration_ms = (len(combined_audio) / (TELEPHONY_SAMPLE_RATE * 2)) * 1000.0
        rtf = (total_latency_ms / max(1.0, audio_duration_ms)) if audio_duration_ms > 0 else 0.0

        if rtf > 0.8:
            logger.warning(
                "indic_f5_high_latency_warning",
                rtf=round(rtf, 2),
                total_latency_ms=round(total_latency_ms, 1),
                detail="IndicF5 measured latency exceeds real-time threshold; Chatterbox is recommended for telephony.",
            )

        metrics = SynthesisMetrics(
            time_to_first_audio_ms=ttfa_ms or total_latency_ms,
            total_latency_ms=total_latency_ms,
            audio_duration_ms=audio_duration_ms,
            real_time_factor=rtf,
            samples_per_second=(len(combined_audio) / 2) / max(0.001, total_latency_ms / 1000.0),
            chunk_count=len(audio_chunks),
        )

        return SynthesisResult(
            audio_bytes=combined_audio,
            sample_rate=TELEPHONY_SAMPLE_RATE,
            format="pcm16",
            metrics=metrics,
        )

    async def synthesize_stream(
        self, *, text: str, voice_id: str = "default", language: str = "hi"
    ) -> AsyncGenerator[bytes, None]:
        clauses = split_into_clauses(text)

        if self.base_url:
            async with httpx.AsyncClient(timeout=30.0) as client:
                for clause in clauses:
                    resp = await client.post(
                        f"{self.base_url}/synthesize",
                        json={
                            "text": clause,
                            "voice_id": voice_id,
                            "language": language,
                            "format": "pcm16",
                            "sample_rate": TELEPHONY_SAMPLE_RATE,
                        },
                    )
                    resp.raise_for_status()
                    yield convert_to_pcm16_mono(resp.content, TELEPHONY_SAMPLE_RATE)
        else:
            # Fallback to local synthesis when no IndicF5 inference server is connected
            from app.providers.voice.tts.chatterbox import ChatterboxEngine

            engine = ChatterboxEngine()
            loop = asyncio.get_event_loop()
            for clause in clauses:
                pcm_chunk = await loop.run_in_executor(None, engine.synthesize_clause, clause, voice_id)
                yield pcm_chunk
