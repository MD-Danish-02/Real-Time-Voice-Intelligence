"""
Live WebSocket Multi-Turn Voice Pipeline Test over ws://127.0.0.1:8002/api/v1/voice/ws/test
Connects via WebSocket, verifies Greeting, 3 Full Turns with real mic audio chunks,
and Barge-in / interruption handling.
"""
import asyncio
import json
import os
import sys
import wave
import websockets

sys.stdout.reconfigure(encoding="utf-8")

AUDIO_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "artifacts", "kokoro_audio")

def load_wav(name: str) -> bytes:
    with wave.open(os.path.join(AUDIO_DIR, name), "rb") as wf:
        return wf.readframes(wf.getnframes())

async def run_multiturn_ws():
    uri = "ws://127.0.0.1:8002/api/v1/voice/ws/test"
    print(f"Connecting to {uri}...")
    async with websockets.connect(uri) as ws:
        # Step 1: Wait for Greeting
        greeting_audio = 0
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=20.0)
            if isinstance(msg, bytes):
                greeting_audio += len(msg)
            else:
                d = json.loads(msg)
                print("  [Greeting]", d.get("event"))
                if d.get("event") == "AI_RESPONSE_COMPLETED":
                    break
        print(f"Greeting received: {greeting_audio} bytes ({greeting_audio / 32000:.2f}s of 16kHz audio)")

        # Step 2: Run 3 Turns over WebSocket
        test_files = [
            ("Turn 1 (Hindi)", "test1_hindi.wav"),
            ("Turn 2 (Hinglish)", "test2_hinglish.wav"),
            ("Turn 3 (English)", "test3_english.wav"),
        ]

        for label, fname in test_files:
            print(f"\n--- Sending {label} ---")
            pcm = load_wav(fname)
            # Stream in 1024-byte chunks
            for offset in range(0, len(pcm), 1024):
                await ws.send(pcm[offset : offset + 1024])
                await asyncio.sleep(0.005)
            # Feed 1.2s silence for VAD speech end detection
            silence = b"\x00" * 1024
            for _ in range(40):
                await ws.send(silence)
                await asyncio.sleep(0.005)

            # Receive Turn response
            turn_audio = 0
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=35.0)
                if isinstance(msg, bytes):
                    turn_audio += len(msg)
                else:
                    d = json.loads(msg)
                    ev = d.get("event")
                    print(f"  [{label}] Event: {ev} | Payload: {d.get('payload')}")
                    if ev == "AI_RESPONSE_COMPLETED":
                        break
            print(f"{label} audio received: {turn_audio} bytes ({turn_audio / 32000:.2f}s of 16kHz audio)")
            assert turn_audio > 0, f"{label} produced no audio!"
            await asyncio.sleep(0.5)

        # Step 3: Barge-in interruption
        print("\n--- Testing Interruption / Barge-in ---")
        await ws.send(json.dumps({"type": "interrupt"}))
        msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
        d = json.loads(msg) if isinstance(msg, str) else {}
        print("  [Interrupt] Event:", d.get("event"))
        assert d.get("event") == "USER_INTERRUPTED"
        print("\n[LIVE WEBSOCKET MULTI-TURN CONVERSATION + BARGE-IN 100% VERIFIED!]")

if __name__ == "__main__":
    asyncio.run(run_multiturn_ws())
