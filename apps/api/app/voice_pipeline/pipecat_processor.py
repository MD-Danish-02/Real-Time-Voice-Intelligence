"""
Pipecat integration layer.
Connects Pipecat frame processor pipeline to:
Transport In -> Silero VAD -> faster-whisper STT -> VoiceCallOrchestrator -> Chatterbox TTS -> Transport Out.
"""
from typing import Optional

from app.core.logging import get_logger
from app.voice_pipeline.orchestrator import VoiceCallOrchestrator
from app.voice_pipeline.vad import SileroVADDetector

logger = get_logger("voice_pipeline.pipecat")

try:
    from pipecat.frames.frames import AudioRawFrame, Frame, TTSAudioRawFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
    _PIPECAT_AVAILABLE = True
except ImportError:
    _PIPECAT_AVAILABLE = False

    class FrameProcessor:  # type: ignore
        async def push_frame(self, frame, direction=None):
            pass

    class Frame:  # type: ignore
        pass

    class AudioRawFrame(Frame):  # type: ignore
        def __init__(self, audio: bytes, sample_rate: int = 16000, num_channels: int = 1):
            self.audio = audio
            self.sample_rate = sample_rate

    class TTSAudioRawFrame(AudioRawFrame):  # type: ignore
        pass

    FrameDirection = None  # type: ignore


class VoxAIVoiceFrameProcessor(FrameProcessor):
    """
    Receives raw inbound audio frames from transport (RTP or WebSocket),
    runs Silero VAD for barge-in detection and speech boundary segmentation,
    passes completed speech to VoiceCallOrchestrator, and outputs synthesized TTS frames.
    """

    def __init__(
        self,
        orchestrator: VoiceCallOrchestrator,
        vad: Optional[SileroVADDetector] = None,
        on_control=None,
    ) -> None:
        super().__init__()
        self.orchestrator = orchestrator
        self.vad = vad or SileroVADDetector()
        self.on_control = on_control
        self._is_ai_speaking = False

    def notify_ai_speech_started(self) -> None:
        self._is_ai_speaking = True

    def notify_ai_speech_completed(self) -> None:
        self._is_ai_speaking = False

    async def process_frame(self, frame: "Frame", direction: "FrameDirection") -> None:
        if isinstance(frame, AudioRawFrame) and not isinstance(frame, TTSAudioRawFrame):
            # Process chunk through Silero VAD
            vad_events = self.vad.process_chunk(frame.audio)
            for event in vad_events:
                if event.event_type == "SPEECH_STARTED":
                    # User barge-in: user spoke while AI is speaking
                    if self._is_ai_speaking:
                        self.orchestrator.interrupt()
                        self._is_ai_speaking = False
                        logger.info("vad_barge_in_triggered")

                elif event.event_type == "SPEECH_ENDED" and event.speech_buffer:
                    # User completed speech turn
                    self._is_ai_speaking = True
                    turn = await self.orchestrator.handle_user_audio_chunk(event.speech_buffer)
                    self._is_ai_speaking = False

                    if turn.audio and turn.audio.audio_bytes:
                        await self.push_frame(
                            TTSAudioRawFrame(
                                audio=turn.audio.audio_bytes,
                                sample_rate=turn.audio.sample_rate,
                            ),
                            direction,
                        )

                    if (turn.should_end_call or turn.should_transfer) and self.on_control:
                        await self.on_control("transfer" if turn.should_transfer else "end")
        else:
            await self.push_frame(frame, direction)
