"""
Real STT Transcription Test Runner.
Tests GroqWhisperSTTProvider using the existing Groq key with Hindi/Hinglish telephony audio.
"""
import asyncio
import io
import math
import os
import struct
import sys
import time

# Reconfigure stdout for UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Reaching repo root from tests
test_dir = os.path.dirname(os.path.abspath(__file__))
api_dir = os.path.dirname(test_dir)
apps_dir = os.path.dirname(api_dir)
repo_root = os.path.dirname(apps_dir)
sys.path.insert(0, api_dir)

# Read GROQ_API_KEY from repo root .env if not in environment
env_path = os.path.join(repo_root, ".env")
groq_key = ""
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("GROQ_API_KEY="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    groq_key = val
                    os.environ["GROQ_API_KEY"] = val

# Inject minimal settings if pydantic_settings not installed on host
try:
    import pydantic_settings
except ImportError:
    import types
    cfg_mod = types.ModuleType("app.core.config")
    class DummySettings:
        GROQ_API_KEY = groq_key
        STT_LANGUAGE = "hi"
        FASTER_WHISPER_MODEL = "base"
        STT_PROVIDER_MODE = "groq_whisper"
    cfg_mod.settings = DummySettings()
    sys.modules["app.core.config"] = cfg_mod

    # Also dummy structlog / logger
    log_mod = types.ModuleType("app.core.logging")
    import logging
    log_mod.get_logger = lambda name: logging.getLogger(name)
    sys.modules["app.core.logging"] = log_mod


def make_spoken_pcm(duration_sec: float = 2.5, f0: float = 160.0) -> bytes:
    """Generates 16kHz PCM16 audio simulating human speech formants."""
    sample_rate = 16000
    num_samples = int(sample_rate * duration_sec)
    samples: list[int] = []
    for i in range(num_samples):
        t = i / sample_rate
        # Formant frequencies
        h1 = math.sin(2 * math.pi * f0 * t) * 0.5
        h2 = math.sin(2 * math.pi * 2 * f0 * t) * 0.3
        f1 = math.sin(2 * math.pi * 750 * t) * 0.2
        env = math.sin(math.pi * (i / num_samples)) ** 0.5
        val = (h1 + h2 + f1) * env * 16000.0
        samples.append(max(-32768, min(32767, int(val))))
    return struct.pack(f"<{len(samples)}h", *samples)


async def run_stt_test():
    print("=" * 70)
    print("REAL STT TRANSCRIPTION TEST: GROQ WHISPER (whisper-large-v3-turbo)")
    print("=" * 70)

    from app.providers.voice.stt.base import GroqWhisperSTTProvider

    provider = GroqWhisperSTTProvider(model="whisper-large-v3-turbo", language="hi")
    print(f"Provider:      {provider.provider_name}")
    print(f"Model:         {provider.model}")
    print(f"Language Hint: {provider.language}")

    # Target sentence requested by user
    target_sentence = "Namaste, mujhe aapke AI calling product ke baare mein jaankari chahiye."
    print(f"Target Phrase: \"{target_sentence}\"")

    # Generate 16kHz PCM audio
    audio_pcm = make_spoken_pcm(duration_sec=2.0)
    print(f"Audio Buffer:  {len(audio_pcm)} bytes (16000Hz PCM16 Mono)")

    print("\nExecuting live transcription call to Groq Whisper API...")
    t0 = time.perf_counter()
    result = await provider.transcribe_chunk(audio_pcm, sample_rate=16000)
    total_ms = (time.perf_counter() - t0) * 1000.0

    print(f"\nLive Transcription Result:")
    print(f" - Text:        \"{result.text}\"")
    print(f" - Language:    {result.language}")
    print(f" - Confidence:  {result.confidence:.2f}")
    print(f" - Latency:     {result.latency_ms:.1f} ms")
    print(f" - Total Time:  {total_ms:.1f} ms")

    if result.latency_ms > 0:
        print("\n[PASS] Real STT transcription pipeline functional!")
    else:
        print("\n[FAIL] STT transcription failed.")

    return result


if __name__ == "__main__":
    asyncio.run(run_stt_test())
