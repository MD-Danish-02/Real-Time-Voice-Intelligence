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
import math
import os
import threading
import time
from typing import AsyncGenerator, Optional
import numpy as np

from app.core.config import settings
from app.core.logging import get_logger
from app.providers.provider_safety import require_mock_provider
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
_kokoro_lock = threading.RLock()


def resample_to_16k_pcm16_mono(samples_24k: np.ndarray, source_rate: int = 24000) -> bytes:
    """
    Converts 24kHz float32 audio to 16kHz mono signed 16-bit PCM bytes (slin16).
    Polyphase resampling when SciPy is present; a linear interpolation fallback keeps
    the provider usable in minimal installations.
    """
    if len(samples_24k) == 0:
        return b""
    if source_rate <= 0:
        raise ValueError("Kokoro returned an invalid sample rate.")
    divisor = math.gcd(TELEPHONY_SAMPLE_RATE, source_rate)
    up, down = TELEPHONY_SAMPLE_RATE // divisor, source_rate // divisor
    try:
        from scipy.signal import resample_poly
        resampled = resample_poly(samples_24k, up, down)
    except ImportError:
        target_len = int(round(len(samples_24k) * TELEPHONY_SAMPLE_RATE / source_rate))
        if target_len == 0:
            return b""
        indices = np.linspace(0, len(samples_24k) - 1, target_len)
        resampled = np.interp(indices, np.arange(len(samples_24k)), samples_24k)
    if len(resampled) == 0:
        return b""
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
        logger.info("kokoro_model_load_started")
        try:
            if not os.path.exists(self.model_path):
                raise FileNotFoundError(f"Kokoro model file not found at {self.model_path}")
            if not os.path.exists(self.voices_path):
                raise FileNotFoundError(f"Kokoro voices file not found at {self.voices_path}")

            from kokoro_onnx import Kokoro

            self._kokoro = Kokoro(self.model_path, self.voices_path)
            self._loaded = True
        except Exception as exc:
            logger.error(
                "kokoro_model_load_failed",
                error_type=type(exc).__name__,
                elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
            )
            raise

        logger.info(
            "kokoro_model_load_completed",
            elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
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
        samples_24k, sample_rate = self._kokoro.create(
            clause,
            voice=voice,
            lang=lang,
            speed=speed,
        )
        return resample_to_16k_pcm16_mono(samples_24k, sample_rate)


class KokoroTTSProvider:
    """
    Kokoro Neural TTS Provider for CPU-friendly conversational voice calling.
    Implements standard TTSProvider interface.
    """

    provider_name: str = "kokoro"

    # kokoro-onnx 0.4.x expects espeak language names, not single-letter IDs.
    LANGUAGE_CONFIG = {
        "hi": ("hi", "hf_alpha"),       # Hindi phonemizer and Hindi female voice
        "en": ("en-us", "af_heart"),    # US English phonemizer and English female voice
    }

    def __init__(
        self,
        voice_id: Optional[str] = None,
        default_lang: Optional[str] = None,
        speed: Optional[float] = None,
    ) -> None:
        require_mock_provider(self.provider_name, "TTS")
        self.voice_id = voice_id or settings.KOKORO_VOICE_ID or "hf_alpha"
        self.default_lang = default_lang or settings.KOKORO_DEFAULT_LANG or "hi"
        self.speed = speed if speed is not None else settings.KOKORO_SPEED
        self.language_speeds = {
            "hi": float(settings.KOKORO_HINDI_SPEED),
            "en": float(settings.KOKORO_ENGLISH_SPEED),
        }
        if speed is not None:
            self.language_speeds = {"hi": float(speed), "en": float(speed)}
        for lang, value in self.language_speeds.items():
            if not 0.5 <= value <= 2.0:
                raise ValueError(f"Kokoro {lang} speed must be between 0.5 and 2.0; got {value}.")
        self.call_id: Optional[str] = None
        self._engine: Optional[KokoroEngine] = None

    def _get_engine(self) -> KokoroEngine:
        global _global_kokoro_instance
        with _kokoro_lock:
            if _global_kokoro_instance is None:
                _global_kokoro_instance = KokoroEngine()
                _global_kokoro_instance.load_model()
            return _global_kokoro_instance

    async def warmup(self) -> None:
        """Load the process-wide Kokoro model before the worker accepts ARI events."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._get_engine)

    @staticmethod
    def _synthesize_clause(engine: KokoroEngine, clause: str, voice: str, lang_code: str, speed: float) -> bytes:
        # Protect the shared ONNX instance across simultaneous sessions.
        with _kokoro_lock:
            return engine.synthesize_clause(clause, voice, lang_code, speed)

    def _resolve_lang_and_voice(
        self, language: Optional[str], voice_id: Optional[str]
    ) -> tuple[str, str]:
        voice = voice_id or self.voice_id
        lang_input = (language or self.default_lang).lower()
        if lang_input in ("hindi",):
            lang_input = "hi"
        elif lang_input in ("english", "en-us", "en-in"):
            lang_input = "en"
        if lang_input not in self.LANGUAGE_CONFIG:
            raise ValueError(f"Unsupported Kokoro language '{language}'. Supported: Hindi (hi), English (en).")
        lang_code, configured_voice = self.LANGUAGE_CONFIG[lang_input]
        # Language-specific voice selection is deliberate: hf_alpha is Hindi-only.
        if voice in ("default", "riya", "hf_alpha", "af_heart"):
            voice = configured_voice
        elif lang_input == "hi" and not voice.startswith("h"):
            raise ValueError(f"Voice '{voice}' is not configured as a Hindi Kokoro voice.")
        elif lang_input == "en" and not voice.startswith("a"):
            raise ValueError(f"Voice '{voice}' is not configured as an English Kokoro voice.")
        return lang_code, voice

    @staticmethod
    def _clause_language(clause: str) -> str:
        """Route whole Hinglish clauses by script, preserving clause order and text."""
        import re

        if re.search(r"[\u0900-\u097f]", clause):
            return "hi"
        words = re.findall(r"[A-Za-z]+", clause.lower())
        roman_hindi = {"aap", "kaise", "hai", "hain", "kya", "ke", "ki", "ka", "ko", "mein", "batao", "kar", "sakte", "ho", "nahi", "aur", "mujhe", "hum"}
        if sum(word in roman_hindi for word in words) >= 2:
            return "hi"
        return "en" if words else "hi"

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
        started_at = time.perf_counter()
        generated_bytes = 0
        chunk_count = 0
        logger.info(
            "kokoro_synthesis_started",
            provider=self.provider_name,
            call_id=self.call_id,
            sample_rate=TELEPHONY_SAMPLE_RATE,
        )
        mixed = language.lower() == "hinglish"
        try:
            if mixed:
                clauses = split_into_clauses(text, language="en")
                route = [(clause, self._clause_language(clause), None) for clause in clauses]
            else:
                lang_code, voice = self._resolve_lang_and_voice(language, voice_id)
                route = [(clause, "hi" if lang_code == "hi" else "en", voice)
                         for clause in split_into_clauses(text, language="hi" if lang_code == "hi" else "en")]
            loop = asyncio.get_running_loop()
            # Startup warmup loads this same process-wide engine; retain lazy fallback
            # for non-worker consumers such as local voice preview.
            engine = await loop.run_in_executor(None, self._get_engine)

            for clause, clause_language, configured_voice in route:
                if not clause.strip():
                    continue
                lang_code, voice = self.LANGUAGE_CONFIG[clause_language]
                if configured_voice:
                    voice = configured_voice
                elif voice_id and voice_id not in ("default", "riya", "hf_alpha", "af_heart"):
                    voice = voice_id
                speed = self.language_speeds[clause_language]
                pcm_chunk = await loop.run_in_executor(
                    None, self._synthesize_clause, engine, clause, voice, lang_code, speed
                )
                if pcm_chunk:
                    generated_bytes += len(pcm_chunk)
                    chunk_count += 1
                    yield pcm_chunk
        except asyncio.CancelledError:
            logger.warning(
                "kokoro_synthesis_cancelled",
                call_id=self.call_id,
                elapsed_ms=round((time.perf_counter() - started_at) * 1000, 1),
                generated_audio_bytes=generated_bytes,
            )
            raise
        except Exception as exc:
            logger.error(
                "kokoro_synthesis_failed",
                call_id=self.call_id,
                error_type=type(exc).__name__,
                elapsed_ms=round((time.perf_counter() - started_at) * 1000, 1),
                generated_audio_bytes=generated_bytes,
            )
            raise
        else:
            logger.info(
                "kokoro_synthesis_completed",
                call_id=self.call_id,
                generated_audio_bytes=generated_bytes,
                sample_rate=TELEPHONY_SAMPLE_RATE,
                duration_ms=round(generated_bytes / (TELEPHONY_SAMPLE_RATE * 2) * 1000, 1),
                chunk_count=chunk_count,
                elapsed_ms=round((time.perf_counter() - started_at) * 1000, 1),
            )
