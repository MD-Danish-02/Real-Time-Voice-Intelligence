"""Offline unit checks for Kokoro startup warmup and synthesized PCM metadata."""
import asyncio
import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))


def test_kokoro_warmup_loads_shared_engine_once_and_synthesis_returns_16k_pcm(monkeypatch):
    from app.providers.voice.tts import kokoro as kokoro_module

    class FakeEngine:
        def __init__(self):
            self.load_calls = 0
            self.synthesis_calls = 0

        def load_model(self):
            self.load_calls += 1

        def synthesize_clause(self, clause, voice, lang, speed):
            self.synthesis_calls += 1
            return b"\x01\x00" * 320

    fake_engine = FakeEngine()
    monkeypatch.setattr(kokoro_module, "_global_kokoro_instance", None)
    monkeypatch.setattr(kokoro_module, "KokoroEngine", lambda: fake_engine)
    # Test-safety mode intentionally blocks live providers. This test injects a
    # deterministic in-memory engine, so bypass only the constructor guard.
    monkeypatch.setattr(kokoro_module, "require_mock_provider", lambda *args: None)
    provider = kokoro_module.KokoroTTSProvider()

    async def exercise():
        await provider.warmup()
        await provider.warmup()
        return await provider.synthesize(text="Hello Riya.", language="en")

    result = asyncio.run(exercise())
    assert fake_engine.load_calls == 1
    assert fake_engine.synthesis_calls == 1
    assert result.sample_rate == 16000
    assert result.format == "pcm16"
    assert result.audio_bytes == b"\x01\x00" * 320


def test_kokoro_engine_load_logs_without_provider_attribute(monkeypatch):
    from types import SimpleNamespace

    from app.providers.voice.tts import kokoro as kokoro_module

    log_events = []

    class FakeLogger:
        def info(self, event, **fields):
            log_events.append((event, fields))

        def error(self, event, **fields):
            log_events.append((event, fields))

    class FakeKokoro:
        def __init__(self, model_path, voices_path):
            self.model_path = model_path
            self.voices_path = voices_path

    monkeypatch.setattr(kokoro_module, "logger", FakeLogger())
    monkeypatch.setattr(kokoro_module.os.path, "exists", lambda _path: True)
    monkeypatch.setitem(sys.modules, "kokoro_onnx", SimpleNamespace(Kokoro=FakeKokoro))

    engine = kokoro_module.KokoroEngine(model_path="mock-model", voices_path="mock-voices")
    assert not hasattr(engine, "provider_name")
    engine.load_model()

    assert engine._loaded
    assert [event for event, _ in log_events] == [
        "kokoro_model_load_started",
        "kokoro_model_load_completed",
    ]
    assert all("provider" not in fields for _, fields in log_events)


def test_voice_worker_completes_tts_warmup_before_starting_ari(monkeypatch):
    from app.voice_pipeline import worker

    events = []

    class FakeTTS:
        provider_name = "kokoro"

        async def warmup(self):
            events.append("warmup")

    async def fake_run_event_loop(handler):
        events.append("ari")

    monkeypatch.setattr(worker, "get_tts_provider", lambda: FakeTTS())
    monkeypatch.setattr(worker, "run_event_loop", fake_run_event_loop)
    asyncio.run(worker.run_worker())
    assert events == ["warmup", "ari"]


def test_voice_worker_refuses_unapproved_tts_provider_before_ari(monkeypatch):
    import pytest
    from app.voice_pipeline import worker

    events = []

    class FakeTTS:
        provider_name = "not-kokoro"

    async def fake_run_event_loop(handler):
        events.append("ari")

    monkeypatch.setattr(worker, "get_tts_provider", lambda: FakeTTS())
    monkeypatch.setattr(worker, "run_event_loop", fake_run_event_loop)
    with pytest.raises(RuntimeError, match="requires the approved Kokoro"):
        asyncio.run(worker.run_worker())
    assert events == []


def test_hangup_cancels_pending_opening_greeting_tts():
    import uuid
    from types import SimpleNamespace

    from app.voice_pipeline.session import LiveVoiceSession

    async def exercise():
        greeting_started = asyncio.Event()

        async def blocking_greeting():
            greeting_started.set()
            await asyncio.Future()

        session = LiveVoiceSession(
            call_id="unit-call",
            tenant_id=uuid.uuid4(),
            orchestrator=SimpleNamespace(opening_greeting=blocking_greeting),
            vad=SimpleNamespace(reset=lambda: None),
            on_audio_out=lambda _chunk: None,
        )
        task = session.schedule_start()
        await greeting_started.wait()
        session.close()
        await asyncio.gather(task, return_exceptions=True)
        return task, session

    task, session = asyncio.run(exercise())
    assert task.cancelled()
    assert not session._turn_tasks
