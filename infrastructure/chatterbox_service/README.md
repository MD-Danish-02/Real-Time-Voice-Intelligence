# Standalone Chatterbox Multilingual TTS Service Deployment Guide

This standalone microservice runs the actual Chatterbox Multilingual neural TTS model on a separate GPU server and exposes the exact API consumed by VoxAI's `ChatterboxTTSProvider`.

## Recommended Hardware
- **GPU**: NVIDIA GPU with 8GB–16GB VRAM (e.g., RTX 3060, T4, A10G, RTX 4090)
- **RAM**: 16 GB RAM minimum (32 GB recommended)
- **OS**: Ubuntu 22.04 LTS (Linux x86_64)
- **Python**: Python 3.10 or 3.11

## Deployment Steps on GPU Server

1. **Clone or copy this folder to the GPU host**:
   ```bash
   git clone <repo_url>
   cd AI-Final-Voice-Agent/infrastructure/chatterbox_service
   ```

2. **Create Python Virtual Environment (Python 3.10/3.11)**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. **Install PyTorch with CUDA**:
   ```bash
   pip install --upgrade pip
   pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
   pip install -r requirements.txt
   ```

4. **Launch the Service**:
   ```bash
   export CHATTERBOX_DEVICE=cuda
   export PORT=8001
   uvicorn server:app --host 0.0.0.0 --port 8001 --workers 1
   ```

5. **Verify the Health Check**:
   ```bash
   curl http://localhost:8001/health
   ```
   Expected response:
   ```json
   {
     "status": "ok",
     "service": "chatterbox-multilingual",
     "model_loaded": true,
     "device": "cuda",
     "supported_languages": ["hi", "en", "hinglish"],
     "telephony_format": "16kHz 16-bit Mono PCM (slin16)"
   }
   ```

6. **Connect VoxAI to this GPU Server**:
   In VoxAI's `.env` on the main application machine:
   ```ini
   CHATTERBOX_BASE_URL=http://<GPU_SERVER_IP>:8001
   TTS_PROVIDER_MODE=chatterbox
   TTS_PROVIDER_NAME=chatterbox
   ```
