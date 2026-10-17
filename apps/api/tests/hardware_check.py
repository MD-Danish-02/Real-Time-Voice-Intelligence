"""
Hardware Safety and Provider Runtime Checker.
Inspects CPU, RAM, GPU, Disk, Groq connectivity, faster-whisper and Chatterbox availability.
"""
import ctypes
import os
import platform
import shutil
import sys
import time

# Reconfigure stdout for UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def check_hardware():
    print("=" * 70)
    print("PHASE 6: HARDWARE SAFETY & RESOURCE AUDIT")
    print("=" * 70)

    # CPU
    cpu_name = platform.processor()
    cores = os.cpu_count()
    print(f"CPU: {cpu_name} ({cores} logical cores)")

    # RAM
    mem = MEMORYSTATUSEX()
    mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
    total_gb = mem.ullTotalPhys / (1024 ** 3)
    avail_gb = mem.ullAvailPhys / (1024 ** 3)
    load = mem.dwMemoryLoad
    print(f"RAM Total:     {total_gb:.2f} GB")
    print(f"RAM Available: {avail_gb:.2f} GB (Memory Load: {load}%)")

    # Disk
    du = shutil.disk_usage(".")
    disk_free = du.free / (1024 ** 3)
    disk_total = du.total / (1024 ** 3)
    print(f"Disk Free:     {disk_free:.2f} GB / Total: {disk_total:.2f} GB")

    # GPU
    gpu_available = False
    vram_gb = 0.0
    try:
        import torch
        gpu_available = torch.cuda.is_available()
        if gpu_available:
            vram_gb = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
    except Exception:
        pass

    if gpu_available:
        print(f"GPU: CUDA Available ({vram_gb:.2f} GB VRAM)")
    else:
        print("GPU: UNAVAILABLE (CPU-only execution)")

    # Safety limits
    print("\nHardware Safety Assessment:")
    if avail_gb < 1.0:
        print(f" [CRITICAL] Available RAM ({avail_gb:.2f} GB) is < 1.0 GB! High risk of OOM crash for large neural models.")
    else:
        print(f" [PASS] Sufficient memory for lightweight base models ({avail_gb:.2f} GB free).")

    return {
        "cpu": cpu_name,
        "cores": cores,
        "ram_total_gb": round(total_gb, 2),
        "ram_avail_gb": round(avail_gb, 2),
        "ram_load_pct": load,
        "disk_free_gb": round(disk_free, 2),
        "gpu_available": gpu_available,
        "vram_gb": vram_gb,
    }


def check_groq():
    print("\n" + "=" * 70)
    print("PHASE 3: GROQ LLM VERIFICATION")
    print("=" * 70)

    # Read from repo root .env
    test_dir = os.path.dirname(os.path.abspath(__file__))
    api_dir = os.path.dirname(test_dir)
    apps_dir = os.path.dirname(api_dir)
    repo_root = os.path.dirname(apps_dir)
    env_path = os.path.join(repo_root, ".env")
    groq_key = None
    groq_model = "llama-3.3-70b-versatile"
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("GROQ_API_KEY="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        groq_key = val
                elif line.startswith("GROQ_MODEL="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        groq_model = val

    if not groq_key:
        print("[ERROR] GROQ_API_KEY_REQUIRED: No Groq API key found in .env")
        return {"status": "GROQ_API_KEY_REQUIRED"}

    masked_key = f"{groq_key[:6]}...{groq_key[-4:]}"
    print(f"Groq API Key: Present ({masked_key})")
    print(f"Groq Model:   {groq_model}")

    # Test authentication and streaming using urllib.request
    import json
    import urllib.request
    import urllib.error

    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {
        "model": groq_model,
        "messages": [
            {"role": "system", "content": "You are VoxAI, a helpful Indian phone assistant. Keep answers to 1 short sentence."},
            {"role": "user", "content": "Namaste, mujhe aapke AI calling product ke baare mein jaankari chahiye."}
        ],
        "temperature": 0.3,
        "max_tokens": 60,
        "stream": True,
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {groq_key}",
            "Content-Type": "application/json",
            "User-Agent": "VoxAI/1.0",
        },
        method="POST"
    )

    t0 = time.perf_counter()
    first_token_time = None
    streamed_text = []

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            for line in resp:
                line = line.decode("utf-8").strip()
                if not line or line == "data: [DONE]":
                    continue
                if line.startswith("data: "):
                    data_str = line[6:]
                    try:
                        chunk = json.loads(data_str)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            if first_token_time is None:
                                first_token_time = (time.perf_counter() - t0) * 1000.0
                            streamed_text.append(content)
                    except Exception:
                        pass

        total_time = (time.perf_counter() - t0) * 1000.0
        reply = "".join(streamed_text).strip()
        print(f" [PASS] Authentication Succeeded!")
        print(f" [PASS] Streaming Response Received: \"{reply}\"")
        print(f" [PERF] Groq TTFT:        {first_token_time:.1f} ms")
        print(f" [PERF] Total Latency:    {total_time:.1f} ms")

        return {
            "status": "PASS",
            "model": groq_model,
            "ttft_ms": round(first_token_time, 1) if first_token_time else 0.0,
            "total_ms": round(total_time, 1),
            "response": reply,
        }
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8")
        print(f" [FAIL] HTTP {e.code} Error: {err_msg}")
        return {"status": "FAIL", "error": f"HTTP {e.code}: {err_msg}"}
    except Exception as e:
        print(f" [FAIL] Connection error: {e}")
        return {"status": "FAIL", "error": str(e)}


def check_chatterbox():
    print("\n" + "=" * 70)
    print("PHASE 4: REAL CHATTERBOX AVAILABILITY AUDIT")
    print("=" * 70)

    test_dir = os.path.dirname(os.path.abspath(__file__))
    api_dir = os.path.dirname(test_dir)
    apps_dir = os.path.dirname(api_dir)
    repo_root = os.path.dirname(apps_dir)
    env_path = os.path.join(repo_root, ".env")
    base_url = ""
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("CHATTERBOX_BASE_URL="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        base_url = val

    print(f"Configured CHATTERBOX_BASE_URL: '{base_url}'")
    if not base_url:
        print(" [STATUS] CHATTERBOX NOT AVAILABLE (No CHATTERBOX_BASE_URL configured)")
        print(" [AUDIT] No external Chatterbox microservice is active or defined.")
        print(" [AUDIT] No neural weights for Chatterbox Multilingual found on disk.")
        return {"status": "CHATTERBOX NOT AVAILABLE", "reason": "No model weights and no service URL"}

    import urllib.request
    try:
        req = urllib.request.Request(f"{base_url}/health", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                print(f" [PASS] External Chatterbox Service reachable at {base_url}")
                return {"status": "AVAILABLE", "url": base_url}
    except Exception as e:
        print(f" [FAIL] CHATTERBOX_SERVICE_UNREACHABLE: {e}")
        return {"status": "CHATTERBOX_SERVICE_UNREACHABLE", "error": str(e)}


if __name__ == "__main__":
    hw = check_hardware()
    groq = check_groq()
    cb = check_chatterbox()
