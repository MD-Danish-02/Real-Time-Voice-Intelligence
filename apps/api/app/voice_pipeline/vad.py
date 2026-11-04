"""
Silero VAD (Voice Activity Detection) Module.
Calibrated specifically for real-time telephone conversations (ARI / SIP / RTP).

Features:
- Real-time speech probability estimation on 16kHz PCM16 audio
- Speech start detection (triggers barge-in / interruption of AI playback)
- Speech end detection (triggers turn handover to faster-whisper STT)
- Dynamic noise floor adaptation for noisy phone lines
- Pre-speech padding to avoid clipping initial consonants
"""
import math
import struct
import time
from dataclasses import dataclass
from typing import Optional

from app.core.logging import get_logger

logger = get_logger("voice.vad")

# Frame size for Silero VAD at 16kHz: 512 samples = 32ms (or 256 for 16ms)
SILERO_FRAME_SIZE = 512
SAMPLE_RATE = 16000


@dataclass
class VADEvent:
    is_speech: bool
    event_type: Optional[str]  # "SPEECH_STARTED" | "SPEECH_ENDED" | None
    speech_probability: float
    speech_buffer: bytes = b""  # populated on SPEECH_ENDED
    speech_started_at: Optional[float] = None
    speech_ended_at: Optional[float] = None


class SileroVADDetector:
    """
    Silero VAD implementation tuned for telephony.
    Processes 16kHz 16-bit signed PCM chunks.
    """

    def __init__(
        self,
        threshold: float = 0.5,
        min_speech_duration_ms: int = 200,
        min_silence_duration_ms: int = 800,
        speech_pad_ms: int = 64,
        max_utterance_duration_ms: int = 20000,
    ) -> None:
        self.threshold = threshold
        self.min_speech_frames = int(min_speech_duration_ms / 32)
        self.min_silence_frames = int(min_silence_duration_ms / 32)
        self.pad_frames = int(speech_pad_ms / 32)
        self.max_speech_frames = max(1, int(max_utterance_duration_ms / 32))

        self._model = None
        self._onnx_session = None
        self._init_attempted = False
        self._init_error: Optional[str] = None
        self._inference_error: Optional[str] = None
        self._onnx_inference_succeeded = False
        self._onnx_context = None

        # State tracking
        self.is_speaking = False
        self._speech_frames_count = 0
        self._silence_frames_count = 0
        self._ring_buffer: list[bytes] = []
        self._candidate_buffer: list[bytes] = []
        self._speech_buffer: list[bytes] = []
        self._active_speech_started_at: Optional[float] = None
        self._active_speech_frames = 0
        self._remainder = b""

        # Adaptive energy baseline for telephone line noise
        self._noise_floor = 150.0

    def _init_model(self) -> None:
        """Initializes Silero VAD (ONNX or PyTorch) if available."""
        if self._init_attempted:
            return
        self._init_attempted = True
        try:
            import onnxruntime as ort  # type: ignore

            # Check if silero-vad onnx model path exists
            import silero_vad  # type: ignore
            import os

            model_path = os.path.join(os.path.dirname(silero_vad.__file__), "data", "silero_vad.onnx")
            if os.path.exists(model_path):
                opts = ort.SessionOptions()
                opts.inter_op_num_threads = 1
                opts.intra_op_num_threads = 1
                self._onnx_session = ort.InferenceSession(
                    model_path, sess_options=opts, providers=["CPUExecutionProvider"]
                )
                logger.info("silero_vad_onnx_initialized")
                return
        except Exception as exc:
            self._init_error = f"{type(exc).__name__}: {exc}"

        logger.warning(
            "silero_vad_unavailable_using_energy_estimator",
            reason=self._init_error or "silero_vad ONNX model file missing",
        )

    @property
    def runtime_name(self) -> str:
        if not self._init_attempted:
            self._init_model()
        if self._onnx_session is not None:
            provider = self._onnx_session.get_providers()[0]
            if self._onnx_inference_succeeded:
                return f"Silero VAD ONNX ({provider}; inference verified)"
            return "Silero VAD ONNX initialized (awaiting first inference)"
        if self._model is not None:
            return "Silero VAD PyTorch"
        return (
            f"Energy fallback (Silero unavailable: {self._inference_error or self._init_error or 'model file missing'})"
            if self._init_attempted else "Silero VAD not initialized"
        )

    def _estimate_probability(self, frame_bytes: bytes) -> float:
        """
        Estimates speech probability for a 512-sample (1024-byte) frame.
        Uses neural Silero VAD if available, or calibrated telephony acoustic estimator.
        """
        if not self._init_attempted:
            self._init_model()

        num_samples = len(frame_bytes) // 2
        if num_samples == 0:
            return 0.0

        # Unpack samples
        samples = struct.unpack(f"<{num_samples}h", frame_bytes)

        if self._model is not None:
            try:
                import torch

                tensor = torch.tensor(samples, dtype=torch.float32) / 32768.0
                prob = float(self._model(tensor, SAMPLE_RATE).item())
                return prob
            except Exception:
                pass

        if self._onnx_session is not None:
            try:
                import numpy as np
                inputs = self._onnx_session.get_inputs()
                state = getattr(self, "_onnx_state", None)
                if state is None:
                    state = np.zeros((2, 1, 128), dtype=np.float32)
                context = self._onnx_context
                if context is None:
                    context = np.zeros((1, 64), dtype=np.float32)
                sr_name = next((item.name for item in inputs if "sr" in item.name.lower()), None)
                normalized_frame = np.asarray(samples, dtype=np.float32)[None, :] / 32768.0
                model_input = np.concatenate((context, normalized_frame), axis=1)
                feeds = {inputs[0].name: model_input}
                state_name = next((item.name for item in inputs if "state" in item.name.lower()), None)
                if state_name:
                    feeds[state_name] = state
                if sr_name:
                    feeds[sr_name] = np.asarray(SAMPLE_RATE, dtype=np.int64)
                outputs = self._onnx_session.run(None, feeds)
                if state_name and len(outputs) > 1:
                    self._onnx_state = outputs[1]
                self._onnx_context = model_input[:, -64:]
                self._onnx_inference_succeeded = True
                return float(np.asarray(outputs[0]).reshape(-1)[0])
            except Exception as exc:
                self._inference_error = f"{type(exc).__name__}: {exc}"
                logger.warning("silero_vad_onnx_inference_failed", reason=self._inference_error)
                self._onnx_session = None

        # Robust telephone RMS energy + zero-crossing rate estimator
        rms = math.sqrt(sum(s * s for s in samples) / max(1, num_samples))
        # Update background noise floor slowly during silence
        if not self.is_speaking and rms < self._noise_floor * 1.5:
            self._noise_floor = 0.95 * self._noise_floor + 0.05 * rms

        zcr = sum(1 for i in range(1, num_samples) if (samples[i] >= 0) != (samples[i - 1] >= 0))
        zcr_rate = zcr / num_samples

        snr = rms / max(10.0, self._noise_floor)
        # Reject broadband/impulsive noise before applying the energy/SNR score.
        # Loudness alone is not speech evidence: keyboard clicks and speaker noise
        # can have a very high SNR against a quiet adaptive floor.
        if zcr_rate < 0.008 or zcr_rate > 0.45:
            return 0.01
        # Low absolute level is silence/noise even when an adaptive floor is near zero.
        # Browser diagnostics showed noise/echo turns averaging RMS 252–619,
        # while the checked Hindi/English speech fixtures average >1,900.
        # Keep this absolute floor so a tiny adaptive noise floor cannot turn
        # quiet room noise into a high-SNR speech score.
        if rms < 1200.0:
            prob = 0.01
        elif snr > 2.8:
            prob = min(0.98, 0.4 + 0.15 * snr)
        elif snr > 1.8:
            prob = 0.55
        elif snr > 1.2 and 0.05 < zcr_rate < 0.65:
            prob = 0.45
        else:
            prob = max(0.01, min(0.25, snr * 0.1))

        return prob

    def process_chunk(self, pcm_bytes: bytes) -> list[VADEvent]:
        """
        Ingests arbitrary-length 16kHz PCM16 bytes, splits into 512-sample frames,
        and outputs VAD events (SPEECH_STARTED, SPEECH_ENDED, or ongoing).
        """
        data = self._remainder + pcm_bytes
        frame_bytes_len = SILERO_FRAME_SIZE * 2  # 1024 bytes per 512 samples
        events: list[VADEvent] = []

        offset = 0
        while offset + frame_bytes_len <= len(data):
            frame = data[offset : offset + frame_bytes_len]
            offset += frame_bytes_len

            prob = self._estimate_probability(frame)
            is_frame_speech = prob >= self.threshold

            # Retain every frame while speech onset is being confirmed. Previously
            # only the tiny pre-roll ring was copied on SPEECH_STARTED, dropping
            # the first ~128ms and often most of short acknowledgments.
            if not self.is_speaking and is_frame_speech:
                self._candidate_buffer.append(frame)

            # Manage pre-speech circular buffer for padding
            self._ring_buffer.append(frame)
            if len(self._ring_buffer) > self.pad_frames:
                self._ring_buffer.pop(0)

            event_type: Optional[str] = None
            finished_buffer = b""

            if is_frame_speech:
                self._speech_frames_count += 1
                self._silence_frames_count = 0

                if not self.is_speaking:
                    if self._speech_frames_count >= self.min_speech_frames:
                        self.is_speaking = True
                        event_type = "SPEECH_STARTED"
                        self._active_speech_started_at = time.perf_counter()
                        self._active_speech_frames = self._speech_frames_count
                        ring_overlap = min(len(self._ring_buffer), len(self._candidate_buffer))
                        pre_speech = self._ring_buffer[:-ring_overlap] if ring_overlap else self._ring_buffer
                        self._speech_buffer.extend(pre_speech)
                        self._speech_buffer.extend(self._candidate_buffer)
                        self._candidate_buffer.clear()
                else:
                    self._speech_buffer.append(frame)
                    self._active_speech_frames += 1
            else:
                self._silence_frames_count += 1
                self._speech_frames_count = 0
                if not self.is_speaking:
                    self._candidate_buffer.clear()

                if self.is_speaking:
                    self._speech_buffer.append(frame)
                    self._active_speech_frames += 1
                    if self._silence_frames_count >= self.min_silence_frames:
                        self.is_speaking = False
                        event_type = "SPEECH_ENDED"
                        finished_buffer = b"".join(self._speech_buffer)
                        self._speech_buffer.clear()
                        self._candidate_buffer.clear()
                        self._active_speech_started_at = None
                        self._active_speech_frames = 0

            if self.is_speaking and self._active_speech_frames >= self.max_speech_frames:
                self.is_speaking = False
                event_type = "SPEECH_ENDED"
                finished_buffer = b"".join(self._speech_buffer)
                self._speech_buffer.clear()
                self._candidate_buffer.clear()
                self._active_speech_started_at = None
                self._active_speech_frames = 0
                self._speech_frames_count = 0
                self._silence_frames_count = 0

            events.append(
                VADEvent(
                    is_speech=self.is_speaking,
                    event_type=event_type,
                    speech_probability=prob,
                    speech_buffer=finished_buffer,
                    speech_started_at=self._active_speech_started_at if event_type == "SPEECH_STARTED" else None,
                    speech_ended_at=time.perf_counter() if event_type == "SPEECH_ENDED" else None,
                )
            )

        self._remainder = data[offset:]
        return events

    def reset(self) -> None:
        """Resets speech detection state for a new conversation turn or call."""
        self.is_speaking = False
        self._speech_frames_count = 0
        self._silence_frames_count = 0
        self._ring_buffer.clear()
        self._candidate_buffer.clear()
        self._speech_buffer.clear()
        self._remainder = b""
        self._onnx_state = None
        self._onnx_context = None
        self._inference_error = None
        self._active_speech_started_at = None
        self._active_speech_frames = 0
