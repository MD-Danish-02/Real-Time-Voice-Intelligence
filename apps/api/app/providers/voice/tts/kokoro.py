"""
Kokoro Neural TTS Provider (ONNX CPU Runtime).
Ultra-fast, lightweight 82M-parameter StyleTTS2 model optimized for CPU inference.

Features:
- Full CPU compatibility (no CUDA / NVIDIA GPU required)
- Native Indian Hindi phonetics and voice support (hf_alpha, hf_beta, hm_omega)
- Native Indian English and international English support
- Resampled to 16kHz 16-bit mono PCM (slin16) for real-time telephony and browser audio
- Low latency conversational streaming via clause-level incremental synthesis
"""
import asyncio
import os
import time
from typing import AsyncGenerator, Optional
import numpy as np

from app.core.config import settings
from app.core.logging import get_logger
from app.providers.voice.tts.chatterbox import (
    TELEPHONY_SAMPLE_RATE,
    SynthesisMetrics,
    SynthesisResult,
    normalize_indian_text,
    split_into_clauses,
)

logger = get_logger("voice.tts.kokoro")

# Global singleton engine instance to avoid loading 325MB model weights repeatedly
_global_kokoro_instance = None


def resample_to_16k_pcm16_mono(samples_24k: np.ndarray) -> bytes:
    """
    Converts 24kHz float32 audio to 16kHz mono signed 16-bit PCM bytes (slin16).
    Vectorized NumPy linear interpolation for sub-4ms latency on CPU.
    """
    if len(samples_24k) == 0:
        return b""
    target_len = int(len(samples_24k) * TELEPHONY_SAMPLE_RATE / 24000)
    if target_len == 0:
        return b""
    indices = np.linspace(0, len(samples_24k) - 1, target_len)
    resampled = np.interp(indices, np.arange(len(samples_24k)), samples_24k)
    clamped = np.clip(resampled, -1.0, 1.0)
    return (clamped * 32767.0).astype(np.int16).tobytes()


def _resolve_asset_path(path_str: str) -> str:
    """Finds asset path checking absolute, cwd, and repository root paths."""
    if os.path.isabs(path_str) and os.path.exists(path_str):
        return path_str
    if os.path.exists(path_str):
        return os.path.abspath(path_str)
    curr = os.path.abspath(__file__)
    for _ in range(7):
        curr = os.path.dirname(curr)
        candidate = os.path.join(curr, path_str)
        if os.path.exists(candidate):
            return candidate
    return path_str


class KokoroEngine:
    """
    In-process ONNX Runtime Kokoro TTS engine.
    Maintains loaded ONNX session and voices embeddings.
    """

    def __init__(
        self,
        model_path: Optional[str] = None,
        voices_path: Optional[str] = None,
    ) -> None:
        raw_model = model_path or settings.KOKORO_MODEL_PATH
        raw_voices = voices_path or settings.KOKORO_VOICES_PATH
        self.model_path = _resolve_asset_path(raw_model)
        self.voices_path = _resolve_asset_path(raw_voices)
        self._kokoro = None
        self._loaded = False

    def load_model(self) -> None:
        if self._loaded:
            return
        t0 = time.perf_counter()
        from kokoro_onnx import Kokoro

        if not os.path.exists(self.model_path):
            raise FileNotFoundError(f"Kokoro model file not found at {self.model_path}")
        if not os.path.exists(self.voices_path):
            raise FileNotFoundError(f"Kokoro voices file not found at {self.voices_path}")

        self._kokoro = Kokoro(self.model_path, self.voices_path)
        self._loaded = True
        logger.info(
            "kokoro_onnx_model_loaded",
            elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
            model_path=self.model_path,
        )

    def synthesize_clause(
        self,
        clause: str,
        voice: str = "hf_alpha",
        lang: str = "hi",
        speed: float = 1.0,
    ) -> bytes:
        if not self._loaded:
            self.load_model()
        samples_24k, _ = self._kokoro.create(
            clause,
            voice=voice,
            lang=lang,
            speed=speed,
        )
        return resample_to_16k_pcm16_mono(samples_24k)


class KokoroTTSProvider:
    """
    Kokoro Neural TTS Provider for CPU-friendly conversational voice calling.
    Implements standard TTSProvider interface.
    """

    provider_name: str = "kokoro"

    def __init__(
        self,
        voice_id: Optional[str] = None,
        default_lang: Optional[str] = None,
        speed: Optional[float] = None,
    ) -> None:
        self.voice_id = voice_id or settings.KOKORO_VOICE_ID or "hf_alpha"
        self.default_lang = default_lang or settings.KOKORO_DEFAULT_LANG or "hi"
        self.speed = speed if speed is not None else settings.KOKORO_SPEED
        self._engine: Optional[KokoroEngine] = None

    def _get_engine(self) -> KokoroEngine:
        global _global_kokoro_instance
        if _global_kokoro_instance is None:
            _global_kokoro_instance = KokoroEngine()
            _global_kokoro_instance.load_model()
        return _global_kokoro_instance

    def _resolve_lang_and_voice(
        self, language: Optional[str], voice_id: Optional[str]
    ) -> tuple[str, str]:
        voice = voice_id or self.voice_id
        lang_input = (language or self.default_lang).lower()
        if lang_input in ("hi", "hindi", "hinglish"):
            lang_code = "hi"
            if voice in ("default", "riya"):
                voice = "hf_alpha"
        elif lang_input in ("en", "english", "en-us", "en-in"):
            lang_code = "en-us"
            if voice in ("default", "riya"):
                voice = "hf_alpha"
        else:
            lang_code = "hi"
            if voice in ("default", "riya"):
                voice = "hf_alpha"
        return lang_code, voice

    async def synthesize(
        self,
        *,
        text: str,
        voice_id: Optional[str] = None,
        language: str = "hi",
    ) -> SynthesisResult:
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
        samples_per_sec = (
            (len(combined_audio) / 2) / (total_latency_ms / 1000.0) if total_latency_ms > 0 else 0.0
        )

        metrics = SynthesisMetrics(
            time_to_first_audio_ms=ttfa_ms or total_latency_ms,
            total_latency_ms=total_latency_ms,
            audio_duration_ms=audio_duration_ms,
            real_time_factor=rtf,
            samples_per_second=samples_per_sec,
            chunk_count=len(audio_chunks),
        )

        return SynthesisResult(
            audio_bytes=combined_audio,
            sample_rate=TELEPHONY_SAMPLE_RATE,
            format="pcm16",
            metrics=metrics,
        )

    async def synthesize_stream(
        self,
        *,
        text: str,
        voice_id: Optional[str] = None,
        language: str = "hi",
    ) -> AsyncGenerator[bytes, None]:
        lang_code, voice = self._resolve_lang_and_voice(language, voice_id)
        clauses = split_into_clauses(text)
        engine = self._get_engine()
        loop = asyncio.get_event_loop()

        for clause in clauses:
            if not clause.strip():
                continue
            pcm_chunk = await loop.run_in_executor(
                None, engine.synthesize_clause, clause, voice, lang_code, self.speed
            )
            if pcm_chunk:
                yield pcm_chunk
