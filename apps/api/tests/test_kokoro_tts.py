"""
Unit tests for Kokoro ONNX TTS provider.

Tests cover:
- Provider initialization with valid model files
- Provider initialization with missing model files (should raise error)
- Real SIP synthesis with Plivo-compatible output format
- Streaming synthesis (synthesize_stream)
- Invalid voice configuration handling
- Missing dependency handling
"""

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ENV", "").strip().lower() == "test",
    reason="Kokoro model diagnostics are not part of the database-backed mock-provider suite.",
)

sys.path.insert(0, "apps/api")

from app.core.config import settings
from app.providers.voice.tts.base import get_tts_provider
from app.providers.voice.tts.kokoro import KokoroTTSProvider

REPO = Path(__file__).resolve().parents[3]
MODEL_PATH = REPO / "models" / "kokoro" / "kokoro-v1.0.onnx"
VOICES_PATH = REPO / "models" / "kokoro" / "voices-v1.0.bin"


# ---------------------------------------------------------------------------
# Provider instantiation tests
# ---------------------------------------------------------------------------


def test_kokoro_tts_provider_creation():
    """KokoroTTSProvider should be instantiable with valid model paths."""
    provider = KokoroTTSProvider(voice_id="hf_alpha")
    assert provider.provider_name == "kokoro"
    assert provider.voice_id == "hf_alpha"
    assert provider.default_lang == "hi"
    assert provider.speed == settings.KOKORO_SPEED


def test_kokoro_tts_provider_from_settings():
    """get_tts_provider() with kokoro_onnx should return KokoroTTSProvider."""
    provider = get_tts_provider("kokoro_onnx")
    assert isinstance(provider, KokoroTTSProvider)
    assert provider.provider_name == "kokoro"


def test_kokoro_tts_provider_from_settings_legacy_kokoro():
    """get_tts_provider() with kokoro should also return KokoroTTSProvider."""
    provider = get_tts_provider("kokoro")
    assert isinstance(provider, KokoroTTSProvider)
    assert provider.provider_name == "kokoro"


# ---------------------------------------------------------------------------
# Model file existence tests
# ---------------------------------------------------------------------------


def test_kokoro_model_files_exist():
    """Kokoro model and voices files must exist at configured paths."""
    assert MODEL_PATH.exists(), f"Model file not found: {MODEL_PATH}"
    assert VOICES_PATH.exists(), f"Voices file not found: {VOICES_PATH}"
    assert MODEL_PATH.stat().st_size > 1_000_000, "Model file too small (<1MB)"
    assert VOICES_PATH.stat().st_size > 1_000, "Voices file too small (<1KB)"


# ---------------------------------------------------------------------------
# Real synthesis tests
# ---------------------------------------------------------------------------


def test_kokoro_synthesize_english():
    """Kokoro should synthesize English text to 16kHz PCM16 audio."""
    provider = KokoroTTSProvider(voice_id="hf_alpha")
    result = asyncio.run(
        provider.synthesize(text="Hello, how are you?", voice_id="hf_alpha", language="en-us")
    )
    assert result is not None
    assert result.audio_bytes is not None
    assert len(result.audio_bytes) > 0
    assert result.sample_rate == 16000
    assert result.format == "pcm16"
    assert result.metrics.time_to_first_audio_ms > 0
    assert result.metrics.total_latency_ms > 0
    print(f"  English TTS: {len(result.audio_bytes)} bytes, {result.metrics.total_latency_ms:.1f}ms total, TTFA={result.metrics.time_to_first_audio_ms:.1f}ms")


def test_kokoro_synthesize_hindi():
    """Kokoro should synthesize Hindi text to 16kHz PCM16 audio."""
    provider = KokoroTTSProvider(voice_id="hf_alpha")
    result = asyncio.run(
        provider.synthesize(text="नमस्ते, आप कैसे हैं?", voice_id="hf_alpha", language="hi")
    )
    assert result is not None
    assert result.audio_bytes is not None
    assert len(result.audio_bytes) > 0
    assert result.sample_rate == 16000
    assert result.format == "pcm16"
    print(f"  Hindi TTS: {len(result.audio_bytes)} bytes, {result.metrics.total_latency_ms:.1f}ms total, TTFA={result.metrics.time_to_first_audio_ms:.1f}ms")


def test_kokoro_language_speeds_are_configured_independently():
    provider = KokoroTTSProvider(voice_id="hf_alpha")
    assert provider.language_speeds == {"hi": settings.KOKORO_HINDI_SPEED, "en": settings.KOKORO_ENGLISH_SPEED}
    assert provider.language_speeds["hi"] == 1.15
    assert provider.language_speeds["en"] == 1.0


def test_kokoro_rejects_unsupported_language_speed(monkeypatch):
    monkeypatch.setattr(settings, "KOKORO_HINDI_SPEED", 2.1)
    with pytest.raises(ValueError, match="between 0.5 and 2.0"):
        KokoroTTSProvider()


# ---------------------------------------------------------------------------
# Streaming synthesis tests
# ---------------------------------------------------------------------------


def test_kokoro_synthesize_stream():
    """Kokoro synthesize_stream should yield audio chunks."""
    provider = KokoroTTSProvider(voice_id="hf_alpha")
    chunks = []
    async def _run():
        async for chunk in provider.synthesize_stream(text="Hello world, this is a test.", voice_id="hf_alpha", language="en"):
            if chunk:
                chunks.append(chunk)
    asyncio.run(_run())
    assert len(chunks) > 0
    total_bytes = sum(len(c) for c in chunks)
    assert total_bytes > 0
    print(f"  Streaming TTS: {len(chunks)} chunks, {total_bytes} total bytes")


def test_kokoro_language_voice_mapping_and_rejects_unknown_language():
    provider = KokoroTTSProvider(voice_id="hf_alpha")
    assert provider._resolve_lang_and_voice("hi", None) == ("hi", "hf_alpha")
    assert provider._resolve_lang_and_voice("en", None) == ("en-us", "af_heart")
    assert provider._resolve_lang_and_voice("en", "hf_alpha") == ("en-us", "af_heart")
    with pytest.raises(ValueError, match="Unsupported Kokoro language"):
        provider._resolve_lang_and_voice("hinglish", None)


def test_english_text_is_not_hindi_normalized():
    from app.providers.voice.tts.chatterbox import split_into_clauses
    assert split_into_clauses("Rs 10 is the price.", language="en") == ["Rs 10 is the price."]




# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


def test_kokoro_missing_model_file():
    """KokoroTTSProvider should raise FileNotFoundError for missing model."""
    from app.providers.voice.tts.kokoro import KokoroEngine
    engine = KokoroEngine(model_path="/nonexistent/model.onnx", voices_path="models/kokoro/voices-v1.0.bin")
    try:
        engine.load_model()
        assert False, "Should have raised FileNotFoundError"
    except FileNotFoundError as e:
        assert "/nonexistent/model.onnx" in str(e)


def test_kokoro_missing_voices_file():
    """KokoroTTSProvider should raise FileNotFoundError for missing voices file."""
    from app.providers.voice.tts.kokoro import KokoroEngine
    engine = KokoroEngine(model_path="models/kokoro/kokoro-v1.0.onnx", voices_path="/nonexistent/voices.bin")
    try:
        engine.load_model()
        assert False, "Should have raised FileNotFoundError"
    except FileNotFoundError as e:
        assert "/nonexistent/voices.bin" in str(e)


# ---------------------------------------------------------------------------
# Performance benchmarks
# ---------------------------------------------------------------------------


def test_kokoro_synthesis_performance():
    """Kokoro synthesis should complete in reasonable time (<5s for short text)."""
    provider = KokoroTTSProvider(voice_id="hf_alpha")
    result = asyncio.run(
        provider.synthesize(text="Hello, how are you today?", voice_id="hf_alpha", language="en-us")
    )
    assert result.metrics.total_latency_ms < 5000, f"TTS too slow: {result.metrics.total_latency_ms:.1f}ms"
    assert result.metrics.time_to_first_audio_ms < 5000, f"TTS TTFA too slow: {result.metrics.time_to_first_audio_ms:.1f}ms"
    print(f"  Performance: TTFA={result.metrics.time_to_first_audio_ms:.1f}ms, total={result.metrics.total_latency_ms:.1f}ms, RTF={result.metrics.real_time_factor:.2f}")


if __name__ == "__main__":
    test_kokoro_tts_provider_creation()
    test_kokoro_tts_provider_from_settings()
    test_kokoro_tts_provider_from_settings_legacy_kokoro()
    test_kokoro_model_files_exist()
    test_kokoro_synthesize_english()
    test_kokoro_synthesize_hindi()
    test_kokoro_synthesize_hinglish()
    test_kokoro_synthesize_stream()
    test_kokoro_missing_model_file()
    test_kokoro_missing_voices_file()
    test_kokoro_synthesis_performance()
    print("\nAll Kokoro TTS tests passed!")
