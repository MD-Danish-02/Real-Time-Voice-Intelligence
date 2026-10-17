"""
Real Kokoro Neural TTS Inference Test Suite.
Tests exact Hindi, Hinglish, and Indian English prompts with real ONNX inference on CPU.
Measures: model loading time, synthesis latency, audio duration, sample rate, channels,
PCM format, RAM before/after, and saves valid 16kHz mono WAV files.
"""
import os
import sys
import time
import json
import wave
import struct
import numpy as np

# Ensure UTF-8 stdout
sys.stdout.reconfigure(encoding="utf-8")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_PATH = os.path.join(ROOT_DIR, "models", "kokoro", "kokoro-v1.0.onnx")
VOICES_PATH = os.path.join(ROOT_DIR, "models", "kokoro", "voices-v1.0.bin")
OUT_DIR = os.path.join(ROOT_DIR, "artifacts", "kokoro_audio")
os.makedirs(OUT_DIR, exist_ok=True)

def get_ram_mb() -> float:
    try:
        import psutil
        return psutil.virtual_memory().used / (1024 * 1024)
    except Exception:
        # Fallback using Windows CIM/WMI
        import subprocess
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_OperatingSystem | Select-Object -ExpandProperty FreePhysicalMemory"],
            text=True
        ).strip()
        free_kb = float(out)
        total_out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_OperatingSystem | Select-Object -ExpandProperty TotalVisibleMemorySize"],
            text=True
        ).strip()
        total_kb = float(total_out)
        return (total_kb - free_kb) / 1024.0

def resample_to_16k_pcm16_mono(samples_24k: np.ndarray) -> bytes:
    """Converts 24kHz float32 audio to 16kHz mono signed 16-bit PCM bytes."""
    if len(samples_24k) == 0:
        return b""
    target_len = int(len(samples_24k) * 16000 / 24000)
    if target_len == 0:
        return b""
    indices = np.linspace(0, len(samples_24k) - 1, target_len)
    resampled = np.interp(indices, np.arange(len(samples_24k)), samples_24k)
    clamped = np.clip(resampled, -1.0, 1.0)
    pcm16 = (clamped * 32767.0).astype(np.int16).tobytes()
    return pcm16

def save_wav(pcm16_bytes: bytes, out_path: str, sample_rate: int = 16000):
    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(1)  # Mono
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16_bytes)

def run_tests():
    print("=" * 60)
    print("PHASE 5: REAL KOKORO INFERENCE BENCHMARK")
    print("=" * 60)

    ram_start_mb = get_ram_mb()
    print(f"Host Used RAM before model load: {ram_start_mb:.1f} MB")

    t_load_start = time.perf_counter()
    from kokoro_onnx import Kokoro
    kokoro = Kokoro(MODEL_PATH, VOICES_PATH)
    t_load_ms = (time.perf_counter() - t_load_start) * 1000.0
    ram_after_load_mb = get_ram_mb()
    print(f"Model Load Time: {t_load_ms:.1f} ms")
    print(f"Host Used RAM after model load: {ram_after_load_mb:.1f} MB (Delta: +{ram_after_load_mb - ram_start_mb:.1f} MB)")

    test_cases = [
        {
            "id": "TEST 1 — HINDI",
            "file": "test1_hindi.wav",
            "text": "नमस्ते, मैं रिया हूँ। मैं आपको हमारे AI calling solution के बारे में जानकारी देने के लिए कॉल कर रही हूँ।",
            "voice": "hf_alpha",
            "lang": "hi",
        },
        {
            "id": "TEST 2 — HINGLISH",
            "file": "test2_hinglish.wav",
            "text": "Namaste, main Riya bol rahi hoon. Aapne AI automation mein interest dikhaya tha. Kya main aapko product ke baare mein short mein explain kar sakti hoon?",
            "voice": "hf_alpha",
            "lang": "hi",
        },
        {
            "id": "TEST 3 — INDIAN ENGLISH",
            "file": "test3_english.wav",
            "text": "Hello, I'm Riya from VVnT SeQuor. I'm calling to briefly explain our AI automation solution.",
            "voice": "hf_alpha",  # Or af_sarah / hf_alpha for Indian English accent
            "lang": "en-us",
        },
    ]

    results = []

    for tc in test_cases:
        print("\n" + "-" * 50)
        print(f"Running {tc['id']}")
        print(f"Input: {tc['text']}")
        print(f"Voice: {tc['voice']}, Lang: {tc['lang']}")

        ram_pre = get_ram_mb()
        t0 = time.perf_counter()
        
        # Real ONNX neural forward pass
        samples_24k, native_sr = kokoro.create(
            tc["text"],
            voice=tc["voice"],
            lang=tc["lang"],
            speed=1.0,
        )
        synthesis_latency_ms = (time.perf_counter() - t0) * 1000.0
        ram_post = get_ram_mb()

        # Resample to telephony standard 16kHz mono signed PCM16
        pcm16_bytes = resample_to_16k_pcm16_mono(samples_24k)
        audio_duration_sec = len(pcm16_bytes) / (16000 * 2)
        rtf = (synthesis_latency_ms / 1000.0) / audio_duration_sec if audio_duration_sec > 0 else 0.0

        out_wav = os.path.join(OUT_DIR, tc["file"])
        save_wav(pcm16_bytes, out_wav, 16000)

        record = {
            "test_id": tc["id"],
            "text": tc["text"],
            "voice": tc["voice"],
            "lang": tc["lang"],
            "model_loading_time_ms": round(t_load_ms, 1),
            "synthesis_latency_ms": round(synthesis_latency_ms, 1),
            "audio_duration_sec": round(audio_duration_sec, 2),
            "audio_duration_ms": round(audio_duration_sec * 1000, 1),
            "real_time_factor": round(rtf, 3),
            "native_sample_rate": native_sr,
            "output_sample_rate": 16000,
            "channels": 1,
            "pcm_format": "signed 16-bit little-endian (slin16)",
            "output_bytes": len(pcm16_bytes),
            "ram_before_mb": round(ram_pre, 1),
            "ram_after_mb": round(ram_post, 1),
            "ram_delta_mb": round(ram_post - ram_pre, 1),
            "success": True,
            "wav_path": out_wav,
        }
        results.append(record)

        print(f"  Synthesis Latency: {synthesis_latency_ms:.1f} ms")
        print(f"  Audio Duration: {audio_duration_sec:.2f} s")
        print(f"  Real Time Factor (RTF): {rtf:.3f} (Values < 1.0 mean faster than real-time)")
        print(f"  Output Format: 16000 Hz, 1 channel (mono), 16-bit PCM ({len(pcm16_bytes)} bytes)")
        print(f"  RAM Before: {ram_pre:.1f} MB | RAM After: {ram_post:.1f} MB")
        print(f"  WAV saved: {out_wav} ({os.path.getsize(out_wav)} bytes)")
        print(f"  Status: SUCCESS")

    # Save summary JSON for reporting
    json_path = os.path.join(OUT_DIR, "kokoro_benchmark_results.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nAll results saved to {json_path}")

if __name__ == "__main__":
    run_tests()
