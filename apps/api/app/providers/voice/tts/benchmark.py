"""
TTS Latency and Performance Benchmark Utility.
Measures model loading time, generation latency, TTFA, RTF, and throughput
across required Indian Hindi, Hinglish, Indian English, and mixed business phrases.
"""
import asyncio
import json
import time
from typing import Any

from app.core.logging import get_logger
from app.providers.voice.tts.base import get_tts_provider

logger = get_logger("voice.tts.benchmark")

BENCHMARK_PHRASES = [
    {
        "category": "Hindi",
        "text": "Namaste, main Riya bol rahi hoon.",
        "language": "hi",
    },
    {
        "category": "Hinglish",
        "text": "Hi, main Riya bol rahi hoon. Aapko product ke baare mein ek quick information share karni thi.",
        "language": "hi",
    },
    {
        "category": "Indian English",
        "text": "Could you please tell me a convenient time for a quick discussion?",
        "language": "en",
    },
    {
        "category": "Mixed",
        "text": "Actually, hum aapke existing CRM ko AI calling ke saath automate kar sakte hain.",
        "language": "hi",
    },
    {
        "category": "Target Conversational Code-Switching",
        "text": "Namaste, main Riya bol rahi hoon. Aapke business ke liye AI automation ke regarding thodi information share karni thi. Kya abhi baat karna convenient hai?",
        "language": "hi",
    },
    {
        "category": "Numbers, Currency & Dates",
        "text": "Price starting at ₹5000 per month with 99.9% uptime, schedule on 15th October.",
        "language": "en",
    },
    {
        "category": "Indian Names & Terminology",
        "text": "Sunil Sharma ji from Delhi, aapka VoxAI demo account ready hai.",
        "language": "hi",
    },
]


async def run_tts_benchmark(provider_name: str = "chatterbox") -> dict[str, Any]:
    """Runs full benchmark suite across all target phrases and computes aggregate metrics."""
    t_load_start = time.perf_counter()
    provider = get_tts_provider(provider_name)
    load_time_ms = (time.perf_counter() - t_load_start) * 1000.0

    phrase_results = []
    total_latency_sum = 0.0
    total_audio_duration_sum = 0.0
    ttfa_sum = 0.0

    for item in BENCHMARK_PHRASES:
        t0 = time.perf_counter()
        result = await provider.synthesize(text=item["text"], language=item["language"])
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        m = result.metrics
        phrase_results.append({
            "category": item["category"],
            "text": item["text"],
            "language": item["language"],
            "ttfa_ms": round(m.time_to_first_audio_ms, 2),
            "generation_latency_ms": round(m.total_latency_ms, 2),
            "audio_duration_ms": round(m.audio_duration_ms, 2),
            "real_time_factor": round(m.real_time_factor, 3),
            "samples_per_second": round(m.samples_per_second, 1),
            "audio_bytes": len(result.audio_bytes),
            "sample_rate": result.sample_rate,
        })

        total_latency_sum += m.total_latency_ms
        total_audio_duration_sum += m.audio_duration_ms
        ttfa_sum += m.time_to_first_audio_ms

    avg_ttfa = ttfa_sum / max(1, len(phrase_results))
    avg_latency = total_latency_sum / max(1, len(phrase_results))
    overall_rtf = (total_latency_sum / max(1.0, total_audio_duration_sum))

    report = {
        "provider": provider.provider_name,
        "model_loading_time_ms": round(load_time_ms, 2),
        "phrases_tested": len(phrase_results),
        "average_time_to_first_audio_ms": round(avg_ttfa, 2),
        "average_generation_latency_ms": round(avg_latency, 2),
        "total_audio_duration_seconds": round(total_audio_duration_sum / 1000.0, 2),
        "overall_real_time_factor": round(overall_rtf, 3),
        "real_time_capable": overall_rtf < 1.0,
        "results": phrase_results,
    }

    return report


if __name__ == "__main__":
    import sys

    prov = sys.argv[1] if len(sys.argv) > 1 else "chatterbox"
    res = asyncio.run(run_tts_benchmark(prov))
    print(json.dumps(res, indent=2))
