"""
Speech-to-text provider abstraction.
faster-whisper runs a local Whisper model (CTranslate2) with multilingual
support for Indian Hindi, English, and Hinglish code-switching.
"""
import asyncio
import math
import struct
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.providers.provider_safety import require_mock_provider

logger = get_logger("voice.stt")

# Keep transcription unbiased. A vocabulary prompt caused Whisper to favor business phrases.


@dataclass
class TranscriptionResult:
    text: str
    confidence: Optional[float]
    is_final: bool
    language: Optional[str] = None
    latency_ms: float = 0.0


class STTProvider(ABC):
    provider_name: str = "base"

    def __new__(cls, *args, **kwargs):
        require_mock_provider(cls.provider_name, "STT")
        return super().__new__(cls)

    @abstractmethod
    async def transcribe_chunk(self, audio_bytes: bytes, sample_rate: int = 16000) -> TranscriptionResult:
        ...


class MockSTTProvider(STTProvider):
    """Returns a deterministic canned transcript for tests."""

    provider_name = "mock"

    def __init__(self, canned_text: str = "Yes, I'm interested, please tell me more.") -> None:
        self.canned_text = canned_text

    async def transcribe_chunk(self, audio_bytes: bytes, sample_rate: int = 16000) -> TranscriptionResult:
        return TranscriptionResult(
            text=self.canned_text,
            confidence=0.92,
            is_final=True,
            language="en",
            latency_ms=2.0,
        )


class FasterWhisperSTTProvider(STTProvider):
    """
    Real local STT using faster-whisper.
    Tuned for Indian English, Hindi, and Hinglish code-switching on CPU.
    """

    provider_name = "faster_whisper"

    def __init__(self, model_size: Optional[str] = None, language: Optional[str] = None) -> None:
        self._model_size = model_size or settings.FASTER_WHISPER_MODEL or "base"
        self._language = language or settings.STT_LANGUAGE  # "auto" | "hi" | "en"
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel  # type: ignore

                logger.info("loading_faster_whisper_model", model_size=self._model_size)
                self._model = WhisperModel(self._model_size, device="cpu", compute_type="int8")
            except Exception as exc:
                logger.warning("faster_whisper_import_or_load_failed", error=str(exc))
                self._model = None
        return self._model

    async def transcribe_chunk(self, audio_bytes: bytes, sample_rate: int = 16000) -> TranscriptionResult:
        t0 = time.perf_counter()

        def _run() -> TranscriptionResult:
            import numpy as np

            model = self._ensure_model()
            if model is None:
                # Fallback if faster-whisper is not compiled/available
                return TranscriptionResult(
                    text="",
                    confidence=0.0,
                    is_final=True,
                    language="en",
                    latency_ms=(time.perf_counter() - t0) * 1000.0,
                )

            audio_array = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0

            # Language selection: auto-detect if "auto" or None
            lang = None if self._language in ("auto", "", None) else self._language

            segments, info = model.transcribe(
                audio_array,
                language=lang,
                initial_prompt=None,
                vad_filter=False,  # Silero VAD already handles voice activity upstream
                beam_size=2,
            )

            text_parts = [seg.text.strip() for seg in segments]
            text = " ".join(t for t in text_parts if t)
            detected_lang = getattr(info, "language", lang or "hi")
            prob = float(getattr(info, "language_probability", 0.85))

            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return TranscriptionResult(
                text=text,
                confidence=prob,
                is_final=True,
                language=detected_lang,
                latency_ms=elapsed_ms,
            )

        return await asyncio.get_event_loop().run_in_executor(None, _run)


class GroqWhisperSTTProvider(STTProvider):
    """
    Cloud-based Whisper STT using Groq's high-speed whisper-large-v3-turbo API.
    Used for low-memory environments where local faster-whisper CTranslate2
    is deferred due to host RAM constraints.
    """

    provider_name: str = "groq_whisper"

    def __init__(self, model: str = "whisper-large-v3-turbo", language: Optional[str] = None) -> None:
        self.model = model
        self.language = language or settings.STT_LANGUAGE

    @staticmethod
    def _is_model_marked_silence(result: dict, threshold: float = 0.65) -> bool:
        """Reject only when every returned Whisper segment marks itself as silence."""
        segments = result.get("segments")
        if not isinstance(segments, list) or not segments:
            return False
        probabilities = [
            float(segment["no_speech_prob"])
            for segment in segments
            if isinstance(segment, dict) and segment.get("no_speech_prob") is not None
        ]
        return bool(probabilities) and all(value >= threshold for value in probabilities)

    async def transcribe_chunk(self, audio_bytes: bytes, sample_rate: int = 16000) -> TranscriptionResult:
        t0 = time.perf_counter()
        if not audio_bytes:
            return TranscriptionResult(text="", confidence=1.0, is_final=True, latency_ms=0.0)

        # Ignore measured near-silence in every language mode. No duration or
        # transcript-length floor: quiet, short acknowledgments remain eligible.
        aligned_bytes = audio_bytes[: len(audio_bytes) & ~1]
        if not aligned_bytes or sample_rate <= 0:
            return TranscriptionResult(text="", confidence=1.0, is_final=True,
                                       language=self.language, latency_ms=0.0)
        samples = struct.unpack(f"<{len(aligned_bytes) // 2}h", aligned_bytes)
        peak = max((abs(value) for value in samples), default=0)
        rms = math.sqrt(sum(value * value for value in samples) / max(1, len(samples)))
        duration_ms = len(aligned_bytes) / (sample_rate * 2) * 1000.0
        logger.info(
            "groq_whisper_audio_received",
            sample_rate=sample_rate,
            duration_ms=round(duration_ms, 1),
            byte_count=len(aligned_bytes),
            rms=round(rms, 1),
            peak=peak,
        )
        if peak < 100 and rms < 30:
            logger.info("groq_whisper_silence_skipped", sample_rate=sample_rate,
                        duration_ms=round(duration_ms, 1), rms=round(rms, 1), peak=peak)
            return TranscriptionResult(text="", confidence=1.0, is_final=True,
                                       language=self.language, latency_ms=(time.perf_counter() - t0) * 1000.0)

        wav_bytes = self._pcm_to_wav(aligned_bytes, sample_rate)

        try:
            try:
                import httpx
                use_httpx = True
            except ImportError:
                use_httpx = False

            if use_httpx:
                files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
                data = {
                    "model": self.model,
                    "response_format": "verbose_json",
                }
                if self.language and self.language != "auto":
                    data["language"] = self.language

                headers = {
                    "Authorization": f"Bearer {settings.GROQ_API_KEY}",
                    "User-Agent": "VoxAI/1.0",
                }

                async with httpx.AsyncClient(timeout=15.0) as client:
                    request_started = time.perf_counter()
                    resp = await client.post(
                        "https://api.groq.com/openai/v1/audio/transcriptions",
                        files=files,
                        data=data,
                        headers=headers,
                    )
                    resp.raise_for_status()
                    result = resp.json()
                    request_ms = (time.perf_counter() - request_started) * 1000.0
            else:
                # Standard library urllib multipart POST
                import json
                import urllib.request
                boundary = f"----WebKitFormBoundary{time.time_ns()}"
                body = []
                body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{self.model}\r\n".encode())
                if self.language and self.language != "auto":
                    body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\n{self.language}\r\n".encode())
                body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"response_format\"\r\n\r\nverbose_json\r\n".encode())
                body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\nContent-Type: audio/wav\r\n\r\n".encode() + wav_bytes + b"\r\n")
                body.append(f"--{boundary}--\r\n".encode())
                payload = b"".join(body)

                req = urllib.request.Request(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    data=payload,
                    headers={
                        "Authorization": f"Bearer {settings.GROQ_API_KEY}",
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                        "User-Agent": "VoxAI/1.0",
                    },
                    method="POST",
                )
                loop = asyncio.get_event_loop()
                def _do_req():
                    with urllib.request.urlopen(req, timeout=15) as r:
                        return json.loads(r.read().decode())
                result = await loop.run_in_executor(None, _do_req)
                request_ms = (time.perf_counter() - t0) * 1000.0

            text = result.get("text", "").strip()
            detected_lang = result.get("language", self.language or "hi")
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            if text and self._is_model_marked_silence(result):
                logger.info(
                    "groq_whisper_silence_transcript_rejected",
                    sample_rate=sample_rate,
                    duration_ms=round(duration_ms, 1),
                    byte_count=len(aligned_bytes),
                    language_detected=detected_lang,
                    segment_count=len(result.get("segments", [])),
                    reason="all_segments_above_no_speech_probability_threshold",
                )
                return TranscriptionResult(
                    text="",
                    confidence=None,
                    is_final=True,
                    language=detected_lang,
                    latency_ms=elapsed_ms,
                )
            logger.info(
                "groq_whisper_transcription_completed",
                sample_rate=sample_rate,
                duration_ms=round(duration_ms, 1),
                byte_count=len(aligned_bytes),
                request_ms=round(request_ms, 1),
                response_ms=round(elapsed_ms, 1),
                language_requested=self.language,
                language_detected=detected_lang,
            )

            return TranscriptionResult(
                text=text,
                confidence=None,  # Groq verbose_json does not provide calibrated confidence.
                is_final=True,
                language=detected_lang,
                latency_ms=elapsed_ms,
            )
        except Exception as exc:
            logger.error("groq_whisper_transcription_failed", error_type=type(exc).__name__)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return TranscriptionResult(
                text="",
                confidence=0.0,
                is_final=True,
                language=self.language,
                latency_ms=elapsed_ms,
            )

    @staticmethod
    def _pcm_to_wav(pcm_data: bytes, sample_rate: int = 16000) -> bytes:
        import struct
        channels = 1
        bits_per_sample = 16
        byte_rate = sample_rate * channels * (bits_per_sample // 8)
        block_align = channels * (bits_per_sample // 8)
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            36 + len(pcm_data),
            b"WAVE",
            b"fmt ",
            16,
            1,  # PCM
            channels,
            sample_rate,
            byte_rate,
            block_align,
            bits_per_sample,
            b"data",
            len(pcm_data),
        )
        return header + pcm_data


def get_stt_provider(mode: Optional[str] = None, language: Optional[str] = None) -> STTProvider:
    selected = mode or settings.STT_PROVIDER_MODE
    require_mock_provider(selected, "STT")
    if selected == "faster_whisper":
        return FasterWhisperSTTProvider(language=language)
    elif selected in ("groq_whisper", "groq"):
        return GroqWhisperSTTProvider(language=language)
    return MockSTTProvider()
