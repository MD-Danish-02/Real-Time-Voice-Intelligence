"""
Standalone Chatterbox Multilingual TTS Inference Microservice.
Designed to run on a dedicated GPU host (Ubuntu 22.04 LTS / CUDA 12.1 / Python 3.11).

Uses the official Resemble AI Chatterbox Multilingual V3 neural model:
  from chatterbox.mtl_tts import ChatterboxMultilingualTTS
  model = ChatterboxMultilingualTTS.from_pretrained(device="cuda", t3_model="v3")
  wav = model.generate(text, language_id="hi")

API Contract:
- GET  /health      -> Health check, GPU readiness, and loaded model metadata
- POST /synthesize  -> Synthesizes Hindi, Hinglish, or English text into 16kHz PCM16 (slin16)
"""
import io
import math
import os
import struct
import time
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Chatterbox Multilingual TTS Service", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DEVICE = os.getenv("CHATTERBOX_DEVICE", "cuda")
API_KEY = os.getenv("CHATTERBOX_API_KEY", "")
TELEPHONY_SAMPLE_RATE = 16000
VOICES_DIR = os.getenv("VOICES_DIR", "voices")

# Global model handle kept warm in GPU memory
_chatterbox_model = None


class SynthesisRequest(BaseModel):
    text: str
    voice_id: str = "riya"
    language: str = "hi"
    format: str = "pcm16"
    sample_rate: int = 16000


@app.on_event("startup")
async def startup_event():
    """Initializes and pre-warms the Chatterbox Multilingual neural model once on startup."""
    global _chatterbox_model
    print(f"[Chatterbox] Loading ChatterboxMultilingualTTS (v3) on device: {DEVICE}...")
    t0 = time.perf_counter()
    try:
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS  # type: ignore

        _chatterbox_model = ChatterboxMultilingualTTS.from_pretrained(device=DEVICE, t3_model="v3")
        # Warmup pass in Hindi
        _ = _chatterbox_model.generate("Namaste", language_id="hi")
        load_time_ms = (time.perf_counter() - t0) * 1000.0
        print(f"[Chatterbox] ChatterboxMultilingualTTS loaded and pre-warmed in {load_time_ms:.1f}ms.")
    except Exception as exc:
        print(f"[Chatterbox] Error loading ChatterboxMultilingualTTS: {exc}")
        print("[Chatterbox] Verify 'chatterbox-tts', PyTorch CUDA, and HuggingFace weights access.")


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "chatterbox-multilingual",
        "model_class": "ChatterboxMultilingualTTS",
        "t3_model": "v3",
        "model_loaded": _chatterbox_model is not None,
        "device": DEVICE,
        "native_sample_rate": getattr(_chatterbox_model, "sr", 24000) if _chatterbox_model else 24000,
        "output_format": "16kHz 16-bit Mono PCM (slin16)",
        "supported_languages": ["hi", "en", "hinglish", "es", "fr", "de", "ja", "zh"],
    }


@app.post("/synthesize")
def synthesize_speech(req: SynthesisRequest, authorization: Optional[str] = Header(None)):
    """Synthesizes text into 16kHz PCM16 signed linear mono audio."""
    if API_KEY:
        expected = f"Bearer {API_KEY}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="Unauthorized")

    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty")

    global _chatterbox_model
    if _chatterbox_model is None:
        raise HTTPException(
            status_code=503,
            detail="ChatterboxMultilingualTTS neural model not loaded. Check GPU device and model setup.",
        )

    t0 = time.perf_counter()
    try:
        # Map input language to ISO 2-letter language code expected by Chatterbox
        lang = req.language.lower().strip()
        if lang in ("hi", "hindi", "hinglish"):
            language_id = "hi"
        elif lang in ("en", "english"):
            language_id = "en"
        else:
            language_id = lang

        # Prepare kwargs for ChatterboxMultilingualTTS.generate()
        gen_kwargs = {
            "text": req.text,
            "language_id": language_id,
        }

        # Optional zero-shot voice cloning prompt audio if reference file exists
        ref_audio_path = os.path.join(VOICES_DIR, f"{req.voice_id}.wav")
        if os.path.exists(ref_audio_path):
            gen_kwargs["audio_prompt_path"] = ref_audio_path

        # Execute actual neural inference
        wav = _chatterbox_model.generate(**gen_kwargs)

        import numpy as np

        audio_np = wav.cpu().numpy() if hasattr(wav, "cpu") else np.array(wav)
        if audio_np.ndim > 1:
            audio_np = audio_np.squeeze()

        # Resample from model native rate (model.sr, typically 24000Hz) to target telephony rate (16000Hz)
        src_sr = getattr(_chatterbox_model, "sr", 24000)
        pcm_bytes = _to_pcm16_mono(audio_np, src_sr, req.sample_rate)

        gen_time_ms = (time.perf_counter() - t0) * 1000.0
        audio_dur_ms = (len(pcm_bytes) / (req.sample_rate * 2)) * 1000.0
        rtf = gen_time_ms / max(1.0, audio_dur_ms)

        print(f"[Chatterbox] Synthesized ({language_id}): '{req.text[:30]}...' -> {gen_time_ms:.1f}ms (RTF: {rtf:.3f})")

        return Response(
            content=pcm_bytes,
            media_type="audio/pcm",
            headers={
                "X-Audio-Sample-Rate": str(req.sample_rate),
                "X-Audio-Channels": "1",
                "X-Audio-Bits": "16",
                "X-Generation-Time-Ms": f"{gen_time_ms:.1f}",
                "X-Audio-Duration-Ms": f"{audio_dur_ms:.1f}",
                "X-Real-Time-Factor": f"{rtf:.3f}",
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Synthesis failed: {str(exc)}")


def _to_pcm16_mono(audio_np, src_rate: int, dst_rate: int = 16000) -> bytes:
    import numpy as np

    if src_rate != dst_rate:
        # Linear interpolation resample to 16kHz
        num_src = len(audio_np)
        num_dst = int(num_src * dst_rate / src_rate)
        indices = np.linspace(0, num_src - 1, num_dst)
        audio_np = np.interp(indices, np.arange(num_src), audio_np)

    # Normalize float32 (-1.0 to 1.0) to 16-bit signed PCM
    clamped = np.clip(audio_np, -1.0, 1.0)
    int16_samples = (clamped * 32767).astype(np.int16)
    return int16_samples.tobytes()


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8001"))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
