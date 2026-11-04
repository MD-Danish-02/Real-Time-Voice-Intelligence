"""Focused regressions for browser audio framing, VAD and live-turn lifecycle."""
import asyncio
import math
import struct
import sys
import time
import uuid
from types import SimpleNamespace

import pytest

from app.providers.voice.llm.base import LLMResponse, LLMMetrics
from app.providers.voice.stt.base import GroqWhisperSTTProvider, TranscriptionResult
from app.providers.voice.tts.base import MockTTSProvider
from app.voice_pipeline.orchestrator import VoiceCallOrchestrator
from app.voice_pipeline.session import LiveVoiceSession
from app.voice_pipeline.vad import SileroVADDetector


class _MockTransportGroqWhisperProvider(GroqWhisperSTTProvider):
    """Groq adapter used only with the test's fake HTTP client."""

    provider_name = "mock"


def _tone_pcm(duration_ms: int = 100, frequency: int = 220) -> bytes:
    count = 16000 * duration_ms // 1000
    return struct.pack(f"<{count}h", *(int(8000 * math.sin(2 * math.pi * frequency * i / 16000)) for i in range(count)))


def test_vad_preserves_odd_websocket_frame_boundaries_and_ignores_silence():
    vad = SileroVADDetector(min_speech_duration_ms=64, min_silence_duration_ms=64)
    vad._estimate_probability = lambda frame: 0.9 if max(struct.unpack("<512h", frame)) > 1000 else 0.01
    audio = _tone_pcm(160) + b"\0\0" * 16000
    # Exercise byte fragmentation, including one-byte WebSocket frame boundaries.
    all_events = []
    pieces = [audio[i:i + 401] for i in range(0, len(audio), 401)]
    for piece in pieces:
        all_events.extend(vad.process_chunk(piece))
    assert sum(event.event_type == "SPEECH_STARTED" for event in all_events) == 1
    assert sum(event.event_type == "SPEECH_ENDED" for event in all_events) == 1
    assert all(len(event.speech_buffer) % 2 == 0 for event in all_events if event.speech_buffer)


@pytest.mark.asyncio
async def test_groq_stt_passes_hindi_and_keeps_short_valid_speech(monkeypatch):
    request = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"text": "\u0939\u093e\u0901", "language": "hi"}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            request.update(kwargs)
            return FakeResponse()

    from app.providers.voice.stt import base as stt_module
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(AsyncClient=FakeClient))
    provider = _MockTransportGroqWhisperProvider(language="hi")
    result = await provider.transcribe_chunk(_tone_pcm(80), sample_rate=16000)
    assert result.text == "\u0939\u093e\u0901"
    assert request["data"]["language"] == "hi"
    assert request["files"]["file"][1][:4] == b"RIFF"


@pytest.mark.asyncio
async def test_groq_stt_skips_digital_silence_without_duration_floor(monkeypatch):
    provider = _MockTransportGroqWhisperProvider(language="hi")
    result = await provider.transcribe_chunk(b"\0\0" * 800, sample_rate=16000)
    assert result.text == ""
    # An 80ms voiced acknowledgment proceeds to the mocked request in the test above.


class _FakeSTT:
    async def transcribe_chunk(self, audio_bytes, sample_rate=16000):
        return TranscriptionResult("\u0939\u093e\u0901", None, True, "hi", 1)


class _FakeLLM:
    def __init__(self):
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        return LLMResponse("\u091c\u0940, \u092c\u0924\u093e\u0907\u090f\u0964", metrics=LLMMetrics(ttft_ms=3))


class _StartEventVAD:
    def process_chunk(self, pcm):
        return [SimpleNamespace(event_type="SPEECH_STARTED", speech_started_at=time.perf_counter())] * 2

    def reset(self):
        pass


@pytest.mark.asyncio
async def test_duplicate_speech_start_emits_one_interruption_and_one_cancel():
    events = []

    async def emit(name, payload):
        events.append(name)

    agent = VoiceCallOrchestrator(
        stt=_FakeSTT(), llm=_FakeLLM(), tts=MockTTSProvider(), tool_executor=object(),
        context={"company": {"name": "VoxAI"}}, on_event=emit,
    )
    session = LiveVoiceSession("test", "00000000-0000-0000-0000-000000000001", agent,
                               _StartEventVAD(), lambda chunk: None)
    session._is_ai_speaking = True
    session.set_browser_playback_active(True)
    session.feed_input_audio(b"\0\0")
    await asyncio.sleep(0)
    assert agent._interrupted is True
    assert events.count("USER_INTERRUPTED") == 1
    assert session._diagnostics["interruptions"] == 1
    assert session._diagnostics["cancelled_turns"] == 1
    session.close()
    session.close()


@pytest.mark.asyncio
async def test_vad_start_before_browser_playback_does_not_cancel_generation():
    class RecordingOrchestrator:
        _interrupted = False
        _tts_generation_active = False

        def __init__(self):
            self.interruptions = 0

        def interrupt(self):
            self.interruptions += 1
            self._interrupted = True

        async def notify_interruption(self):
            pass

        async def _emit(self, *args):
            pass

    orch = RecordingOrchestrator()
    session = LiveVoiceSession("test", uuid.uuid4(), orch, _StartEventVAD(), lambda chunk: None)
    session._is_ai_speaking = True  # Includes STT/LLM/TTS first-chunk generation.
    orch._tts_generation_active = True
    session.feed_input_audio(b"\0\0")
    await asyncio.sleep(0)
    assert orch.interruptions == 0
    assert session._diagnostics["interruptions"] == 0
    session.close()


@pytest.mark.asyncio
async def test_telephony_vad_interrupts_active_rtp_playback_once_and_drains_output():
    class RecordingOrchestrator:
        _interrupted = False
        _tts_generation_active = False

        def __init__(self):
            self.interruptions = 0

        def interrupt(self):
            self.interruptions += 1
            self._interrupted = True

        async def notify_interruption(self, payload=None):
            pass

        async def _emit(self, *args):
            pass

    orch = RecordingOrchestrator()
    output = {"active": True, "stops": 0}

    def stop_output():
        output["stops"] += 1
        output["active"] = False
        orch.interrupt()

    session = LiveVoiceSession("phone", uuid.uuid4(), orch, _StartEventVAD(), lambda chunk: None)
    session._is_ai_speaking = True
    session.set_transport_playback_controls(lambda: output["active"], stop_output)
    session.feed_input_audio(b"\0\0")
    await asyncio.sleep(0)

    assert output["active"] is False
    assert output["stops"] == 1
    assert orch.interruptions == 1
    assert session._diagnostics["interruptions"] == 1
    session.close()


@pytest.mark.asyncio
async def test_normal_text_turn_completes_without_cancellation_and_has_monotonic_metrics():
    events = []

    async def emit(name, payload):
        events.append((name, payload))

    agent = VoiceCallOrchestrator(
        stt=_FakeSTT(), llm=_FakeLLM(), tts=MockTTSProvider(), tool_executor=object(),
        context={"company": {"name": "VoxAI"}}, on_event=emit,
    )
    result = await agent.handle_user_text("\u0939\u093e\u0901")
    completed = next(payload for name, payload in events if name == "AI_RESPONSE_COMPLETED")
    assert result.ai_text
    assert "USER_INTERRUPTED" not in [name for name, _ in events]
    assert completed["metrics"]["total_turn_ms"] >= completed["metrics"]["tts_ttfa_ms"]
    assert completed["metrics"]["audio_duration_ms"] > 0


def test_vad_provider_status_is_truthful_and_english_kokoro_mapping_is_unchanged():
    vad = SileroVADDetector()
    status = vad.runtime_name
    assert "Silero VAD ONNX" in status or "Energy fallback" in status or "Silero VAD PyTorch" in status
    from app.providers.voice.tts.kokoro import KokoroTTSProvider

    provider = KokoroTTSProvider()
    assert provider._resolve_lang_and_voice("en", None) == ("en-us", "af_heart")
    assert provider._resolve_lang_and_voice("hi", None) == ("hi", "hf_alpha")


def test_auto_language_explicit_preference_persists_until_caller_changes_it():
    agent = VoiceCallOrchestrator(
        stt=_FakeSTT(), llm=_FakeLLM(), tts=MockTTSProvider(), tool_executor=object(),
        context={"company": {"name": "VoxAI"}}, language="auto",
    )

    assert agent._select_turn_language("नमस्ते, कैसे मदद करूँ?", "hi") == "hi"
    # A direct preference request overrides the language Whisper happened to detect.
    assert agent._select_turn_language("Can we talk in English?", "hi") == "en"
    assert agent._select_turn_language("मुझे API के बारे में बताइए", "hi") == "en"
    assert agent._select_turn_language("Can we speak in Hinglish?", "hi") == "hinglish"
    assert agent._select_turn_language("Aapki voice AI kaise kaam karti hai?", "hi") == "hinglish"
    # A later explicit request changes the persistent preference in the same session.
    assert agent._select_turn_language("क्या हम हिंदी में बात कर सकते हैं?", "en") == "hi"
    assert agent._select_turn_language("Can you explain the API integration?", "en") == "hi"


def test_transcript_validation_does_not_use_a_script_whitelist():
    meaningful = VoiceCallOrchestrator._is_meaningful_transcript
    assert meaningful("हां")
    assert meaningful("haan")
    assert meaningful("How does the API work?")
    assert meaningful("ہاں کر سکتے ہیں")
    assert meaningful("Продолжение следует")
    assert meaningful("好")
    assert not meaningful("<|audio|> … !!!")


@pytest.mark.asyncio
async def test_empty_transcription_does_not_change_explicit_language_preference():
    class EmptySTT:
        async def transcribe_chunk(self, audio_bytes, sample_rate=16000):
            return TranscriptionResult("", None, True, "en", 1)

    agent = VoiceCallOrchestrator(
        stt=EmptySTT(), llm=_FakeLLM(), tts=MockTTSProvider(), tool_executor=object(),
        context={"company": {"name": "VoxAI"}}, language="auto",
    )
    await agent.handle_user_audio_chunk(b"\0\0" * 1600)
    assert agent._explicit_language_preference is None


@pytest.mark.asyncio
async def test_session_serializes_stt_turns_and_disconnect_cleanup_is_idempotent():
    class SequencedVAD:
        def __init__(self):
            self.buffers = [b"utterance-one", b"utterance-two"]

        @property
        def runtime_name(self):
            return "test VAD"

        def process_chunk(self, pcm):
            if not self.buffers:
                return []
            return [SimpleNamespace(event_type="SPEECH_ENDED", speech_buffer=self.buffers.pop(0),
                                    speech_started_at=time.perf_counter(), speech_ended_at=time.perf_counter())]

        def reset(self):
            pass

    class SerializedOrchestrator:
        def __init__(self):
            self._interrupted = False
            self.active = 0
            self.max_active = 0
            self.processed = []

        async def _emit(self, *args):
            pass

        async def handle_user_audio_chunk(self, audio, **kwargs):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.processed.append(("start", audio))
            await asyncio.sleep(0.01)
            self.processed.append(("end", audio))
            self.active -= 1
            return type("Turn", (), {"should_end_call": False, "should_transfer": False})()

    orch = SerializedOrchestrator()
    session = LiveVoiceSession("test", uuid.uuid4(), orch, SequencedVAD(), lambda chunk: None)
    session.feed_input_audio(b"pcm-one")
    session.feed_input_audio(b"pcm-two")
    assert session._diagnostics["stt_submissions"] == 0  # queued, not yet at STT boundary
    await asyncio.gather(*tuple(session._turn_tasks))
    assert session._diagnostics["stt_submissions"] == 2
    assert orch.processed == [
        ("start", b"utterance-one"), ("end", b"utterance-one"),
        ("start", b"utterance-two"), ("end", b"utterance-two"),
    ]
    assert orch.max_active == 1
    session.close()
    session.close()
    await session.aclose()
    assert session._active is False
