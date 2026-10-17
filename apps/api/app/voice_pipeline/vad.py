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


class SileroVADDetector:
    """
    Silero VAD implementation tuned for telephony.
    Processes 16kHz 16-bit signed PCM chunks.
    """

    def __init__(
        self,
        threshold: float = 0.5,
        min_speech_duration_ms: int = 200,
        min_silence_duration_ms: int = 550,
        speech_pad_ms: int = 64,
    ) -> None:
        self.threshold = threshold
        self.min_speech_frames = int(min_speech_duration_ms / 32)
        self.min_silence_frames = int(min_silence_duration_ms / 32)
        self.pad_frames = int(speech_pad_ms / 32)

        self._model = None
        self._onnx_session = None
        self._init_attempted = False

        # State tracking
        self.is_speaking = False
        self._speech_frames_count = 0
        self._silence_frames_count = 0
        self._ring_buffer: list[bytes] = []
        self._speech_buffer: list[bytes] = []
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
                self._onnx_session = ort.InferenceSession(model_path, sess_options=opts)
                logger.info("silero_vad_onnx_initialized")
                return
        except Exception:
            pass

        try:
            import torch  # type: ignore

            model, _ = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                trust_repo=True,
            )
            self._model = model
            logger.info("silero_vad_torch_initialized")
        except Exception as exc:
            logger.info("silero_vad_using_calibrated_telephony_estimator", detail=str(exc))

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

        # Robust telephone RMS energy + zero-crossing rate estimator
        rms = math.sqrt(sum(s * s for s in samples) / max(1, num_samples))
        # Update background noise floor slowly during silence
        if not self.is_speaking and rms < self._noise_floor * 1.5:
            self._noise_floor = 0.95 * self._noise_floor + 0.05 * rms

        zcr = sum(1 for i in range(1, num_samples) if (samples[i] >= 0) != (samples[i - 1] >= 0))
        zcr_rate = zcr / num_samples

        snr = rms / max(10.0, self._noise_floor)
        # Speech typically has higher SNR and moderate ZCR (vowels have low ZCR, fricatives high)
        if snr > 2.8:
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
                        # Prepend padding frames
                        self._speech_buffer.extend(self._ring_buffer)
                else:
                    self._speech_buffer.append(frame)
            else:
                self._silence_frames_count += 1
                self._speech_frames_count = 0

                if self.is_speaking:
                    self._speech_buffer.append(frame)
                    if self._silence_frames_count >= self.min_silence_frames:
                        self.is_speaking = False
                        event_type = "SPEECH_ENDED"
                        finished_buffer = b"".join(self._speech_buffer)
                        self._speech_buffer.clear()

            events.append(
                VADEvent(
                    is_speech=self.is_speaking,
                    event_type=event_type,
                    speech_probability=prob,
                    speech_buffer=finished_buffer,
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
        self._speech_buffer.clear()
        self._remainder = b""
