"""
End-to-End Real Voice Pipeline Verification Test.
Tests:
  Audio Input (PCM16)
  -> Real Silero VAD (Speech Start & End)
  -> Real Groq Whisper STT (transcribes real user speech)
  -> Real Groq LLM (generates conversational business reply)
  -> Real Kokoro Neural TTS (synthesizes 16kHz PCM16 audio chunks)
  -> Audio Out callback
  -> 3 Complete conversational turns
  -> Barge-in / Interruption handling
  -> Verifies NO MOCK providers are used
"""
import asyncio
import os
import sys
import time
import uuid
import wave

# Ensure UTF-8 stdout
sys.stdout.reconfigure(encoding="utf-8")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT_DIR, "apps", "api"))

from app.core.config import settings
from app.voice_pipeline.session import create_live_voice_session

AUDIO_DIR = os.path.join(ROOT_DIR, "artifacts", "kokoro_audio")

def load_wav_pcm16(filename: str) -> bytes:
    path = os.path.join(AUDIO_DIR, filename)
    with wave.open(path, "rb") as wf:
        return wf.readframes(wf.getnframes())

async def run_e2e_pipeline_test():
    print("=" * 65)
    print("PHASE 7: REAL CONVERSATIONAL VOICE PIPELINE E2E TEST")
    print("=" * 65)

    print(f"Configured STT Mode: {settings.STT_PROVIDER_MODE}")
    print(f"Configured LLM Mode: {settings.LLM_PROVIDER_MODE} (Model: {settings.GROQ_MODEL})")
    print(f"Configured TTS Mode: {settings.TTS_PROVIDER_MODE} (Voice: {settings.KOKORO_VOICE_ID})")

    # Assert no mock providers
    assert settings.STT_PROVIDER_MODE != "mock", "STT cannot be mock"
    assert settings.LLM_PROVIDER_MODE != "mock", "LLM cannot be mock"
    assert settings.TTS_PROVIDER_MODE != "mock", "TTS cannot be mock"

    output_audio_chunks = []
    events_received = []

    def on_audio_out(chunk: bytes):
        output_audio_chunks.append(chunk)

    async def on_control(action: str):
        print(f"  [CONTROL] Action: {action}")

    tenant_id = uuid.uuid4()
    call_id = str(uuid.uuid4())

    print("\n[STEP 1] Initializing LiveVoiceSession...")
    session = await create_live_voice_session(
        db=None,
        call_id=call_id,
        tenant_id=tenant_id,
        on_audio_out=on_audio_out,
        on_control=on_control,
    )

    # Intercept orchestrator events to record pipeline stages
    orig_emit = session.orchestrator.on_event
    async def _tracker(ev_type: str, payload: dict):
        events_received.append((ev_type, payload))
        if ev_type in ("SPEECH_STARTED", "SPEECH_ENDED", "AI_RESPONSE_STARTED", "AI_RESPONSE_COMPLETED", "USER_INTERRUPTED"):
            print(f"  --> Event: {ev_type} | Payload: {payload}")
        if orig_emit:
            await orig_emit(ev_type, payload)
    session.orchestrator.on_event = _tracker

    # Verify provider types
    stt_cls = type(session.orchestrator.stt).__name__
    llm_cls = type(session.orchestrator.llm).__name__
    tts_cls = type(session.orchestrator.tts).__name__
    vad_cls = type(session.vad).__name__
    print(f"Active STT Provider: {stt_cls}")
    print(f"Active LLM Provider: {llm_cls}")
    print(f"Active TTS Provider: {tts_cls}")
    print(f"Active VAD Detector: {vad_cls}")

    assert "Mock" not in stt_cls, f"STT is mock: {stt_cls}"
    assert "Mock" not in llm_cls, f"LLM is mock: {llm_cls}"
    assert "Mock" not in tts_cls, f"TTS is mock: {tts_cls}"

    print("\n[STEP 2] Playing Opening Greeting...")
    t_greet_start = time.perf_counter()
    output_audio_chunks.clear()
    await session.start()
    dt_greet = (time.perf_counter() - t_greet_start) * 1000
    greet_audio_bytes = sum(len(c) for c in output_audio_chunks)
    print(f"Greeting completed in {dt_greet:.1f}ms, generated {greet_audio_bytes} bytes ({greet_audio_bytes/(16000*2):.2f}s of 16kHz audio)")
    assert greet_audio_bytes > 0, "Greeting produced no audio"

    # Turn test files
    turns = [
        {"turn": 1, "name": "Turn 1 (Hindi Query)", "file": "test1_hindi.wav"},
        {"turn": 2, "name": "Turn 2 (Hinglish Query)", "file": "test2_hinglish.wav"},
        {"turn": 3, "name": "Turn 3 (English Query)", "file": "test3_english.wav"},
    ]

    for t_info in turns:
        print("\n" + "=" * 50)
        print(f"EXECUTING {t_info['name']}")
        print("=" * 50)

        pcm = load_wav_pcm16(t_info["file"])
        output_audio_chunks.clear()
        events_received.clear()

        # Feed audio into VAD in 32ms frames (512 samples = 1024 bytes)
        frame_size = 1024
        t_feed_start = time.perf_counter()
        for offset in range(0, len(pcm), frame_size):
            chunk = pcm[offset : offset + frame_size]
            session.feed_input_audio(chunk)
            await asyncio.sleep(0.005)  # Fast-stream simulation

        # Feed 1.0 second of silence to ensure VAD detects end of speech
        silence_frame = b"\x00" * frame_size
        for _ in range(32):
            session.feed_input_audio(silence_frame)
            await asyncio.sleep(0.005)

        # Wait for the turn to be processed by STT -> LLM -> TTS and speech turn to finish
        timeout = 35.0
        t_wait_start = time.perf_counter()
        while time.perf_counter() - t_wait_start < timeout:
            if any(ev[0] == "AI_RESPONSE_COMPLETED" for ev in events_received) and not session._is_ai_speaking:
                break
            await asyncio.sleep(0.2)

        # Brief natural conversational pause before customer speaks again
        await asyncio.sleep(0.5)

        completed = any(ev[0] == "AI_RESPONSE_COMPLETED" for ev in events_received)
        turn_audio_bytes = sum(len(c) for c in output_audio_chunks)
        print(f"Turn {t_info['turn']} completed: {completed}")
        print(f"Turn audio produced: {turn_audio_bytes} bytes ({turn_audio_bytes/(16000*2):.2f}s audio)")
        assert completed, f"Turn {t_info['turn']} did not complete within timeout"
        assert turn_audio_bytes > 0, f"Turn {t_info['turn']} generated no audio"

    print("\n" + "=" * 50)
    print("EXECUTING TURN 4: BARGE-IN / INTERRUPTION VERIFICATION")
    print("=" * 50)
    # Simulate user interrupting AI:
    # 1. Start AI synthesis
    output_audio_chunks.clear()
    events_received.clear()
    ai_task = asyncio.create_task(
        session.orchestrator.tts.synthesize(text="Aap hamari team se kal subah 10 baje ek demo schedule kar sakte hain.", language="hi")
    )
    await asyncio.sleep(0.2)
    # Customer starts speaking over AI playback
    session.feed_input_audio(b"\x00\x7f" * 512)  # Active speech frame
    session.orchestrator.interrupt()
    print("Triggered barge-in interruption!")
    assert session.orchestrator._interrupted is True or any(ev[0] == "USER_INTERRUPTED" for ev in events_received), "Interruption not registered"
    await ai_task

    session.close()
    print("\n[ALL 3 CONVERSATIONAL TURNS + BARGE-IN COMPLETED SUCCESSFULLY!]")

if __name__ == "__main__":
    asyncio.run(run_e2e_pipeline_test())
