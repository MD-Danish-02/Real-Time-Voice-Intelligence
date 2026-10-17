"""Voice worker entrypoint: `python -m app.voice_pipeline.worker` - consumes the Asterisk ARI event stream."""
import asyncio

from app.core.config import settings
from app.core.logging import configure_logging
from app.telephony.asterisk.ari_client import run_event_loop
from app.telephony.asterisk.event_handler import handle_ari_event

if __name__ == "__main__":
    configure_logging(settings.DEBUG)
    asyncio.run(run_event_loop(handle_ari_event))
