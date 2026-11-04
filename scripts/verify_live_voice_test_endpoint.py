"""
E2E Live Verification Script for VoxAI Voice Test Endpoint.
Connects via WebSocket to ws://localhost:8001/api/v1/voice/ws/test
Validates:
1. SESSION_CONNECTED event and confirmed runtime providers
2. Initial greeting: AI_RESPONSE_STARTED -> Kokoro ONNX binary audio chunks -> AI_RESPONSE_COMPLETED
3. Conversational text turn: Groq LLM -> Kokoro ONNX streaming audio chunks -> Latency metrics
4. Barge-in / Interrupt handling
"""
import asyncio
import json
import time
import websockets

WS_URL = "ws://localhost:8001/api/v1/voice/ws/test"

async def test_live_voice_pipeline():
    print(f"[TEST] Connecting to {WS_URL}...")
    async with websockets.connect(WS_URL) as ws:
        print("[TEST] WebSocket connected successfully.")

        # --- STEP 1: Verify SESSION_CONNECTED ---
        raw_msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
        session_data = json.loads(raw_msg)
        print(f"[TEST] Received event: {session_data.get('event')}")
        assert session_data.get("event") == "SESSION_CONNECTED", f"Expected SESSION_CONNECTED, got {session_data}"
        call_id = session_data.get("call_id")
        providers = session_data.get("providers", {})
        print(f"[TEST] Call ID: {call_id}")
        print(f"[TEST] Providers verified by backend: {json.dumps(providers, indent=2)}")

        assert "Kokoro" in providers.get("tts", ""), f"TTS is not Kokoro: {providers.get('tts')}"
        assert "Silero" in providers.get("vad", ""), f"VAD is not Silero: {providers.get('vad')}"
        assert "Groq" in providers.get("llm", ""), f"LLM is not Groq: {providers.get('llm')}"

        # --- STEP 2: Verify Initial Greeting Audio Delivery ---
        print("\n[TEST] Waiting for initial greeting from Kokoro ONNX TTS...")
        greeting_started = False
        greeting_text = ""
        greeting_audio_bytes = 0
        greeting_chunks = 0
        greeting_completed = False
        greeting_metrics = None

        while not greeting_completed:
            msg = await asyncio.wait_for(ws.recv(), timeout=25.0)
            if isinstance(msg, bytes):
                greeting_audio_bytes += len(msg)
                greeting_chunks += 1
                # print first few chunk sizes
                if greeting_chunks <= 3:
                    print(f"  [AUDIO CHUNK] Received Kokoro binary PCM chunk #{greeting_chunks}: {len(msg)} bytes")
            else:
                data = json.loads(msg)
                event = data.get("event")
                if event == "AI_RESPONSE_STARTED":
                    greeting_started = True
                    greeting_text = data.get("payload", {}).get("text", "")
                    print(f"  [AI_RESPONSE_STARTED] Text: '{greeting_text}'")
                elif event == "AI_RESPONSE_COMPLETED":
                    greeting_completed = True
                    greeting_metrics = data.get("payload", {}).get("metrics", {})
                    print(f"  [AI_RESPONSE_COMPLETED] Metrics: {greeting_metrics}")
                elif event == "VOICE_ERROR":
                    raise RuntimeError(f"Voice pipeline error: {data.get('payload')}")

        print(f"\n[PASS] Greeting completed successfully!")
        print(f"  - Total Kokoro audio received: {greeting_audio_bytes} bytes across {greeting_chunks} chunks")
        print(f"  - Calculated audio duration: {greeting_audio_bytes / 32000:.2f} seconds of 16kHz audio")
        assert greeting_audio_bytes > 10000, f"Expected >10000 bytes of audio, got {greeting_audio_bytes}"
        assert greeting_started, "Greeting was not started"
        assert greeting_completed, "Greeting was not completed"

        # --- STEP 3: Verify Interactive Conversational Turn (Groq LLM + Kokoro TTS) ---
        user_prompt = "Aapki pricing kya hai aur AI solutions kya provide karte hain?"
        print(f"\n[TEST] Sending user turn: '{user_prompt}'...")
        await ws.send(json.dumps({"type": "text_input", "text": user_prompt}))

        turn_speech_ended = False
        turn_ai_started = False
        turn_ai_text = ""
        turn_audio_bytes = 0
        turn_chunks = 0
        turn_completed = False
        turn_metrics = None

        while not turn_completed:
            msg = await asyncio.wait_for(ws.recv(), timeout=25.0)
            if isinstance(msg, bytes):
                turn_audio_bytes += len(msg)
                turn_chunks += 1
                if turn_chunks <= 3:
                    print(f"  [TURN AUDIO] Received Kokoro PCM chunk #{turn_chunks}: {len(msg)} bytes")
            else:
                data = json.loads(msg)
                event = data.get("event")
                if event == "SPEECH_ENDED":
                    turn_speech_ended = True
                    print(f"  [SPEECH_ENDED] Customer text acknowledged: '{data.get('payload', {}).get('text')}'")
                elif event == "TOOL_CALLED":
                    print(f"  [TOOL_CALLED] Agent tool called: {data.get('payload', {}).get('name')}")
                elif event == "AI_RESPONSE_STARTED":
                    turn_ai_started = True
                    turn_ai_text = data.get("payload", {}).get("text", "")
                    print(f"  [AI_RESPONSE_STARTED] Riya response: '{turn_ai_text}'")
                elif event == "AI_RESPONSE_COMPLETED":
                    turn_completed = True
                    turn_metrics = data.get("payload", {}).get("metrics", {})
                    print(f"  [AI_RESPONSE_COMPLETED] Latency metrics: {turn_metrics}")
                elif event == "VOICE_ERROR":
                    raise RuntimeError(f"Voice pipeline error during turn: {data.get('payload')}")

        print(f"\n[PASS] Conversational turn completed successfully!")
        print(f"  - Groq LLM generated: '{turn_ai_text}'")
        print(f"  - Real Kokoro ONNX synthesized: {turn_audio_bytes} bytes across {turn_chunks} chunks")
        assert turn_audio_bytes > 5000, f"Expected >5000 bytes audio, got {turn_audio_bytes}"
        assert turn_ai_started, "Turn AI response was not started"
        assert turn_completed, "Turn AI response was not completed"

        # --- STEP 4: Test Barge-in Interruption ---
        print("\n[TEST] Testing Barge-in Interrupt...")
        await ws.send(json.dumps({"type": "interrupt"}))
        raw_msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
        interrupt_data = json.loads(raw_msg)
        print(f"  [INTERRUPT EVENT] Received: {interrupt_data.get('event')}")
        assert interrupt_data.get("event") == "USER_INTERRUPTED", f"Expected USER_INTERRUPTED, got {interrupt_data}"
        print("[PASS] Barge-in interrupt acknowledged!")

        # --- STEP 5: Clean Hangup ---
        print("\n[TEST] Sending hangup...")
        await ws.send(json.dumps({"type": "hangup"}))
        print("[PASS] Test finished successfully.")

if __name__ == "__main__":
    asyncio.run(test_live_voice_pipeline())
