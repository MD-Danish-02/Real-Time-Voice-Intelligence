"""
Downloads official Kokoro ONNX model weights and voice embeddings for CPU inference.
Saves to models/kokoro/
"""
import os
import sys
import urllib.request
import time

DEST_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models", "kokoro")
os.makedirs(DEST_DIR, exist_ok=True)

FILES = [
    {
        "name": "voices-v1.0.bin",
        "url": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/voices-v1.0.bin",
        "expected_min_bytes": 20_000_000,
    },
    {
        "name": "kokoro-v1.0.onnx",
        "url": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/kokoro-v1.0.onnx",
        "expected_min_bytes": 300_000_000,
    },
]

def download_with_progress(url: str, dest_path: str, expected_min_bytes: int):
    if os.path.exists(dest_path) and os.path.getsize(dest_path) >= expected_min_bytes:
        print(f"[SKIP] {os.path.basename(dest_path)} already exists ({os.path.getsize(dest_path)} bytes)")
        return

    print(f"[DOWNLOADING] {os.path.basename(dest_path)} from {url}...")
    temp_path = dest_path + ".tmp"
    t0 = time.time()
    
    def reporthook(block_num, block_size, total_size):
        downloaded = block_num * block_size
        if total_size > 0:
            percent = downloaded / total_size * 100
            if block_num % 500 == 0:
                mb = downloaded / (1024 * 1024)
                total_mb = total_size / (1024 * 1024)
                sys.stdout.write(f"\r  -> {mb:.1f}/{total_mb:.1f} MB ({percent:.1f}%)")
                sys.stdout.flush()

    urllib.request.urlretrieve(url, temp_path, reporthook)
    if os.path.exists(dest_path):
        os.remove(dest_path)
    os.rename(temp_path, dest_path)
    elapsed = time.time() - t0
    final_size = os.path.getsize(dest_path)
    print(f"\n[DONE] Saved {os.path.basename(dest_path)} ({final_size / (1024*1024):.1f} MB in {elapsed:.1f}s)")

if __name__ == "__main__":
    for item in FILES:
        target = os.path.join(DEST_DIR, item["name"])
        download_with_progress(item["url"], target, item["expected_min_bytes"])
    print("[ALL_DOWNLOADS_COMPLETED]")
