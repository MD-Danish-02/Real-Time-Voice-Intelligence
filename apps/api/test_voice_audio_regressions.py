"""Isolated audio/provider regressions that do not need the DB-backed app fixtures."""
import asyncio
import math
import random
import struct
import sys
from types import SimpleNamespace
from types import SimpleNamespace

import pytest

from app.providers.voice.stt.base import GroqWhisperSTTProvider
from app.providers.voice.llm.base import GroqLLMProvider
from app.providers.voice.tts.kokoro import KokoroTTSProvider, resample_to_16k_pcm16_mono
from app.voice_pipeline.vad import SileroVADDetector


def _pcm(duration_ms=160):
    count = duration_ms * 16
    samples = [int(8000 * math.sin(2 * math.pi * 220 * i / 16000)) for i in range(count)]
    return struct.pack(f"<{count}h", *samples)


def test_vad_retains_confirmed_onset_and_orders_fragmented_pcm_frames():
    signal = _pcm(160)
    audio = signal + b"\0\0" * 3200

    def run(parts):
        vad = SileroVADDetector(min_speech_duration_ms=96, min_silence_duration_ms=96)
        vad._estimate_probability = lambda frame: 0.9 if max(struct.unpack("<512h", frame)) > 1000 else 0.01
        events = []
        for part in parts:
            events.extend(vad.process_chunk(part))
        return events

    whole = run([audio])
    fragmented = run([audio[i:i + 401] for i in range(0, len(audio), 401)])
    whole_end = next(e for e in whole if e.event_type == "SPEECH_ENDED")
    fragmented_end = next(e for e in fragmented if e.event_type == "SPEECH_ENDED")
    assert whole_end.speech_buffer[:len(signal)] == signal
    assert fragmented_end.speech_buffer == whole_end.speech_buffer
    assert [e.event_type for e in fragmented if e.event_type] == ["SPEECH_STARTED", "SPEECH_ENDED"]


def test_groq_stt_short_hindi_phrase_language_and_pcm_wav(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"text": "\u0939\u093e\u0901", "language": "hi"}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            captured.update(kwargs)
            return Response()

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(AsyncClient=Client))
    result = asyncio.run(GroqWhisperSTTProvider(language="hi").transcribe_chunk(_pcm(80)))
    assert result.text == "\u0939\u093e\u0901"
    assert captured["data"]["language"] == "hi"
    filename, wav, content_type = captured["files"]["file"]
    assert filename == "audio.wav" and content_type == "audio/wav"
    assert struct.unpack("<I", wav[24:28])[0] == 16000
    assert wav[44:] == _pcm(80)
    captured.clear()
    asyncio.run(GroqWhisperSTTProvider(language="auto").transcribe_chunk(_pcm(80)))
    assert "language" not in captured["data"]


def test_digital_silence_is_skipped_but_no_short_duration_floor():
    provider = GroqWhisperSTTProvider(language="hi")
    result = asyncio.run(provider.transcribe_chunk(b"\0\0" * 160, sample_rate=16000))
    assert result.text == ""
    samples = struct.unpack("<1280h", _pcm(80))
    assert max(map(abs, samples)) > 1000


def test_kokoro_24khz_resampling_count_and_language_specific_voices():
    import numpy as np

    pcm = resample_to_16k_pcm16_mono(np.ones(2400, dtype=np.float32) * 0.25)
    assert len(pcm) == 1600 * 2
    provider = KokoroTTSProvider()
    assert provider._resolve_lang_and_voice("hi", None) == ("hi", "hf_alpha")
    assert provider._resolve_lang_and_voice("en", None) == ("en-us", "af_heart")
    assert provider.language_speeds["hi"] == 1.15
    assert provider.language_speeds["en"] == 1.0


def test_vad_status_names_the_runtime_or_fallback():
    vad = SileroVADDetector()
    status = vad.runtime_name
    events = vad.process_chunk(b"\0\0" * 512)
    assert len(events) == 1
    after_inference = vad.runtime_name
    assert "Silero VAD ONNX" in after_inference or "Energy fallback" in after_inference or "Silero VAD PyTorch" in after_inference
    if "Silero VAD ONNX" in status:
        assert "Silero VAD ONNX" in after_inference and vad._onnx_session is not None


def test_energy_fallback_rejects_broadband_noise_and_joins_a_natural_pause():
    vad = SileroVADDetector(min_silence_duration_ms=800)
    vad._init_attempted = True  # Exercise the energy fallback deterministically.
    rng = random.Random(17)
    noise = struct.pack("<16000h", *(rng.randint(-12000, 12000) for _ in range(16000)))
    assert not any(event.event_type for event in vad.process_chunk(noise))

    vad.reset()
    first = _pcm(320)
    pause = b"\0\0" * (16000 * 600 // 1000)
    second = _pcm(320)
    tail = b"\0\0" * (16000 * 900 // 1000)
    events = vad.process_chunk(first + pause + second + tail)
    assert [event.event_type for event in events if event.event_type] == ["SPEECH_STARTED", "SPEECH_ENDED"]


def test_energy_fallback_rejects_low_level_room_noise_but_keeps_short_voiced_ack():
    vad = SileroVADDetector()
    vad._init_attempted = True
    low_noise = struct.pack("<16000h", *[(-1 if i % 2 else 1) * (200 + (i % 41)) for i in range(16000)])
    assert not any(event.event_type for event in vad.process_chunk(low_noise))

    vad.reset()
    count = 16000 * 240 // 1000
    voice = struct.pack(
        f"<{count}h",
        *(int(1800 * math.sin(2 * math.pi * 220 * i / 16000)) for i in range(count)),
    )
    tail = b"\0\0" * (16000 * 900 // 1000)
    events = vad.process_chunk(voice + tail)
    assert [event.event_type for event in events if event.event_type] == ["SPEECH_STARTED", "SPEECH_ENDED"]


def test_groq_auto_language_skips_measured_silence_but_accepts_short_speech(monkeypatch):
    # Silence is rejected before the API call irrespective of pinned/automatic language.
    provider = GroqWhisperSTTProvider(language="auto")
    result = asyncio.run(provider.transcribe_chunk(b"\0\0" * 800, sample_rate=16000))
    assert result.text == ""


def test_groq_silence_rejection_uses_whisper_no_speech_evidence_not_script_or_length():
    reject = GroqWhisperSTTProvider._is_model_marked_silence
    assert reject({"segments": [{"no_speech_prob": 0.91}, {"no_speech_prob": 0.82}]})
    assert not reject({"segments": [{"no_speech_prob": 0.91}, {"no_speech_prob": 0.12}]})
    assert not reject({"text": "好", "segments": [{"no_speech_prob": 0.15}]})
    assert not reject({"text": "हाँ", "segments": [{"no_speech_prob": 0.15}]})
    assert not reject({"text": "yes"})


def test_vad_onnx_uses_silero_context_and_only_reports_active_after_inference():
    import numpy as np

    class Input:
        def __init__(self, name):
            self.name = name

    class Session:
        def __init__(self):
            self.feeds = []

        def get_inputs(self):
            return [Input("input"), Input("state"), Input("sr")]

        def get_providers(self):
            return ["CPUExecutionProvider"]

        def run(self, outputs, feeds):
            self.feeds.append(feeds)
            return [np.asarray([[0.8]], dtype=np.float32), np.ones((2, 1, 128), dtype=np.float32)]

    vad = SileroVADDetector()
    vad._init_attempted = True
    vad._onnx_session = Session()
    assert "awaiting first inference" in vad.runtime_name
    assert vad._estimate_probability(struct.pack("<512h", *([16384] * 512))) == pytest.approx(0.8)
    assert vad._estimate_probability(b"\0\0" * 512) == pytest.approx(0.8)
    assert "inference verified" in vad.runtime_name
    assert vad._onnx_session.feeds[0]["input"].shape == (1, 576)
    assert np.all(vad._onnx_session.feeds[1]["input"][:, :64] == 0.5)
    assert np.all(vad._onnx_session.feeds[1]["input"][:, 64:] == 0.0)


def test_groq_llm_stream_reports_first_token_and_parses_tool_fragments():
    class Stream:
        def __aiter__(self):
            self._chunks = iter([
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Hi", tool_calls=[]), finish_reason=None)]),
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=[
                    SimpleNamespace(index=0, id="call_1", function=SimpleNamespace(name="check_", arguments="{\"day\":")),
                ]), finish_reason=None)]),
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=[
                    SimpleNamespace(index=0, id=None, function=SimpleNamespace(name="calendar", arguments="\"today\"}")),
                ]), finish_reason="tool_calls")]),
            ])
            return self

        async def __anext__(self):
            try:
                return next(self._chunks)
            except StopIteration:
                raise StopAsyncIteration

    class Completions:
        async def create(self, **kwargs):
            assert kwargs["stream"] is True
            return Stream()

    provider = GroqLLMProvider.__new__(GroqLLMProvider)
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    result = asyncio.run(provider.chat_stream_response(
        messages=[{"role": "user", "content": "check"}], tools=[{"name": "check_calendar"}]
    ))
    assert result.content == "Hi"
    assert result.metrics.ttft_ms is not None
    assert result.tool_calls[0].name == "check_calendar"
    assert result.tool_calls[0].arguments == {"day": "today"}


def test_groq_nonstream_chat_path_remains_unchanged_for_english():
    captured = {}

    class Completions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            message = SimpleNamespace(content="Hello.", tool_calls=None)
            choice = SimpleNamespace(message=message, finish_reason="stop")
            return SimpleNamespace(choices=[choice], usage=SimpleNamespace(total_tokens=2), model_dump=lambda: {})

    provider = GroqLLMProvider.__new__(GroqLLMProvider)
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    result = asyncio.run(provider.chat(messages=[{"role": "user", "content": "Hello"}]))
    assert "stream" not in captured
    assert result.content == "Hello."
    assert result.metrics.ttft_ms is None
