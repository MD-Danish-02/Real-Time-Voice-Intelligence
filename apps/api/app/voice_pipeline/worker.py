"""Voice worker entrypoint: `python -m app.voice_pipeline.worker` - consumes the Asterisk ARI event stream."""
import asyncio

from app.core.config import settings
from app.core.logging import configure_logging
from app.core.logging import get_logger
from app.providers.voice.tts.base import get_tts_provider
from app.telephony.asterisk.ari_client import run_event_loop
from app.telephony.asterisk.event_handler import handle_ari_event

logger = get_logger("voice.worker")


async def run_worker() -> None:
    """Warm the configured TTS provider before accepting ARI call events."""
    try:
        tts_provider = get_tts_provider()
        if tts_provider.provider_name != "kokoro":
            raise RuntimeError("Voice worker requires the approved Kokoro TTS provider.")
        warmup = getattr(tts_provider, "warmup", None)
        if callable(warmup):
            logger.info("voice_worker_tts_warmup_started", provider=tts_provider.provider_name)
            await warmup()
            logger.info("voice_worker_tts_warmup_completed", provider=tts_provider.provider_name)
        else:
            logger.info("voice_worker_tts_warmup_not_required", provider=tts_provider.provider_name)
    except Exception as exc:
        # Fail startup closed: do not subscribe to ARI and accept calls without
        # the configured TTS provider being usable.
        logger.error("voice_worker_tts_startup_failed", error_type=type(exc).__name__)
        raise

    logger.info("voice_worker_ari_event_loop_starting")
    await run_event_loop(handle_ari_event)


if __name__ == "__main__":
    configure_logging(settings.DEBUG)
    asyncio.run(run_worker())
