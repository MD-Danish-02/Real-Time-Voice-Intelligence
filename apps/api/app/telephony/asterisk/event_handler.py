"""
Asterisk ARI Event Handler.
Handles complete inbound and outbound call lifecycles, bridging externalMedia RTP
into the real-time AI voice session, and triggering post-call transcription and analysis.
"""
import asyncio
from datetime import datetime, timezone
import json
from typing import Optional
import uuid

from sqlalchemy import select

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import AsyncSessionLocal
from app.events.bus import publish_event
from app.models.call import Call
from app.models.lead import Lead, LeadCallState
from app.models.tenant import Tenant
from app.services import call_service
from app.telephony.asterisk.ari_client import AsteriskARIClient
from app.voice_pipeline.rtp_bridge import media_session_manager
from app.voice_pipeline.session import LiveVoiceSession, create_live_voice_session

logger = get_logger("asterisk.events")

_STATE_TO_EVENT = {
    "Ringing": ("ringing", "CALL_RINGING"),
    "Up": ("in_progress", "CALL_ANSWERED"),
}

# In-memory tracking of active ARI bridges and voice sessions per channel
_active_bridges: dict[str, str] = {}
_active_media_channels: dict[str, str] = {}
_active_voice_sessions: dict[str, LiveVoiceSession] = {}


async def _resolve_inbound_tenant(db, destination_number: str) -> Tenant:
    """
    Deterministically resolves tenant for an inbound phone call.
    Uses first active tenant as default for single-tenant/demo deployment.
    """
    tenant = (await db.execute(select(Tenant).where(Tenant.is_active == True).limit(1))).scalar_one_or_none()
    if tenant is None:
        tenant = Tenant(name="Default Tenant", slug="default", is_active=True)
        db.add(tenant)
        await db.flush()
    return tenant


async def handle_ari_event(event: dict) -> None:
    channel = event.get("channel") or {}
    channel_id = channel.get("id")
    kind = event.get("type")
    if not channel_id:
        return

    ari = AsteriskARIClient()

    if kind == "StasisStart":
        await _handle_stasis_start(ari, event, channel, channel_id)
    elif kind == "ChannelStateChange":
        await _handle_channel_state_change(event, channel, channel_id)
    elif kind in {"StasisEnd", "ChannelDestroyed", "ChannelHangupRequest"}:
        await _handle_channel_hangup(ari, event, channel_id, kind)


async def _handle_stasis_start(ari: AsteriskARIClient, event: dict, channel: dict, channel_id: str) -> None:
    args = event.get("args") or []
    caller_info = channel.get("caller") or {}
    dialplan_info = channel.get("dialplan") or {}

    from_number = caller_info.get("number") or ""
    to_number = args[1] if len(args) > 1 else (dialplan_info.get("exten") or "")
    is_plivo_outbound = bool(args and args[0] == "plivo_outbound" and len(args) > 1)
    is_inbound = (args and args[0] == "inbound") or not bool(event.get("channel", {}).get("connected", {}).get("number"))
    if is_plivo_outbound:
        is_inbound = False

    async with AsyncSessionLocal() as db:
        call = None
        if is_plivo_outbound:
            try:
                target_call_id = uuid.UUID(args[1])
                call = (await db.execute(select(Call).where(Call.id == target_call_id))).scalar_one_or_none()
                if call:
                    # Correlate Asterisk channel_id to this active Call record
                    call.provider_call_id = channel_id
                    await db.commit()
                    await db.refresh(call)
                    logger.info("plivo_outbound_call_correlated", call_id=str(call.id), channel_id=channel_id)
                else:
                    logger.error("plivo_outbound_call_not_found", call_id=args[1], channel_id=channel_id)
                    await ari.hangup_channel(channel_id)
                    return
            except (ValueError, TypeError) as exc:
                logger.error("invalid_plivo_outbound_call_id", arg=args[1], error=str(exc))
                await ari.hangup_channel(channel_id)
                return
        elif not is_inbound:
            call = (await db.execute(select(Call).where(Call.provider_call_id == channel_id))).scalar_one_or_none()

        if call is None:
            # Inbound call routing
            tenant = await _resolve_inbound_tenant(db, to_number)
            # Find or create lead
            lead = (
                await db.execute(
                    select(Lead).where(Lead.tenant_id == tenant.id, Lead.phone == from_number)
                )
            ).scalar_one_or_none()
            if lead is None:
                lead = Lead(
                    tenant_id=tenant.id,
                    name=f"Inbound Caller {from_number[-4:] if len(from_number) >= 4 else from_number}",
                    phone=from_number or "+910000000000",
                    crm_provider="voice_inbound",
                    provider_lead_id=f"inbound_{channel_id}",
                )
                db.add(lead)
                await db.flush()
                call_state = LeadCallState(tenant_id=tenant.id, lead_id=lead.id, calling_status="in_call")
                db.add(call_state)
                await db.flush()

            call = Call(
                tenant_id=tenant.id,
                lead_id=lead.id,
                direction="inbound",
                status="in_progress",
                attempt_number=1,
                telephony_provider="generic_sip",
                provider_call_id=channel_id,
                from_number=from_number,
                to_number=to_number,
                started_at=datetime.now(timezone.utc),
                answered_at=datetime.now(timezone.utc),
            )
            db.add(call)
            await db.commit()
            await db.refresh(call)

        now = datetime.now(timezone.utc)
        call.status = "in_progress"
        if call.answered_at is None:
            call.answered_at = now
        await db.commit()

        # Answer channel
        try:
            await ari.answer_channel(channel_id)
        except Exception as e:
            logger.warning("ari_answer_channel_ignored", error=str(e))

        await call_service.record_call_event(db, call, "CALL_ANSWERED", {"channel": channel_id})
        await publish_event(call.tenant_id, "CALL_ANSWERED", {"call_id": str(call.id)})

        # Create ARI mixing bridge
        try:
            bridge = await ari.create_bridge(bridge_type="mixing", name=f"bridge_{call.id}")
            bridge_id = bridge["id"]
            _active_bridges[channel_id] = bridge_id
            await ari.add_channel_to_bridge(bridge_id, channel_id)

            # Create media session for RTP exchange
            media_session = media_session_manager.create_session(call_id=str(call.id))

            # Create Asterisk externalMedia channel
            media_host = settings.VOICE_MEDIA_HOST_EXTERNAL or settings.VOICE_MEDIA_HOST
            ext_host = f"{media_host}:{media_session.local_port}"
            media_chan = await ari.create_external_media_channel(
                external_host=ext_host,
                encapsulation="rtp",
                transport="udp",
                format_="slin16",
            )
            media_chan_id = media_chan["id"]
            _active_media_channels[channel_id] = media_chan_id
            await ari.add_channel_to_bridge(bridge_id, media_chan_id)

            # Discover remote Asterisk RTP destination port
            rtp_port = await ari.get_channel_variable(media_chan_id, "UNICASTRTP_LOCAL_PORT")
            rtp_host = await ari.get_channel_variable(media_chan_id, "UNICASTRTP_LOCAL_ADDRESS")
            
            # Resolve actual Asterisk host from ARI_BASE_URL if Asterisk reported a local/dummy address
            from urllib.parse import urlparse
            ari_host = urlparse(settings.ARI_BASE_URL).hostname or "127.0.0.1"
            if not rtp_host or rtp_host in ("0.0.0.0", "127.0.0.1", "localhost", "asterisk"):
                rtp_host = ari_host

            if rtp_port and rtp_port.isdigit():
                media_session.set_remote_endpoint(rtp_host, int(rtp_port))

            # Instantiate LiveVoiceSession
            voice_session = await create_live_voice_session(
                db,
                call_id=str(call.id),
                tenant_id=call.tenant_id,
                lead_id=call.lead_id,
                on_audio_out=lambda chunk: media_session.queue_outbound_audio(chunk),
                on_control=lambda act: ari.hangup_channel(channel_id),
            )
            media_session.on_audio_received = lambda chunk: voice_session.feed_input_audio(chunk)
            media_session.on_interrupted = lambda: voice_session.orchestrator.interrupt()
            _active_voice_sessions[channel_id] = voice_session

            await call_service.record_call_event(db, call, "CALL_STARTED", {"bridge": bridge_id})
            await publish_event(call.tenant_id, "CALL_STARTED", {"call_id": str(call.id)})

            # Start greeting in background
            asyncio.create_task(voice_session.start())
            logger.info("voice_session_connected_to_ari_bridge", call_id=str(call.id), channel=channel_id)

        except Exception as exc:
            logger.error("ari_media_bridge_setup_failed", call_id=str(call.id), error=str(exc))
            await call_service.record_call_event(db, call, "CALL_FAILED", {"error": str(exc)})
            await publish_event(call.tenant_id, "CALL_FAILED", {"call_id": str(call.id)})


async def _handle_channel_state_change(event: dict, channel: dict, channel_id: str) -> None:
    state = channel.get("state")
    if state not in _STATE_TO_EVENT:
        return

    status, ev = _STATE_TO_EVENT[state]
    async with AsyncSessionLocal() as db:
        call = (await db.execute(select(Call).where(Call.provider_call_id == channel_id))).scalar_one_or_none()
        if call is None:
            return

        call.status = status
        now = datetime.now(timezone.utc)
        if status == "in_progress" and call.answered_at is None:
            call.answered_at = now
        await call_service.record_call_event(db, call, ev, {"channel": channel_id})
        await publish_event(call.tenant_id, ev, {"call_id": str(call.id)})


async def _handle_channel_hangup(ari: AsteriskARIClient, event: dict, channel_id: str, kind: str) -> None:
    voice_session = _active_voice_sessions.pop(channel_id, None)
    if voice_session:
        voice_session.close()

    bridge_id = _active_bridges.pop(channel_id, None)
    media_chan_id = _active_media_channels.pop(channel_id, None)

    if media_chan_id:
        try:
            await ari.hangup_channel(media_chan_id)
        except Exception:
            pass

    if bridge_id:
        try:
            await ari.destroy_bridge(bridge_id)
        except Exception:
            pass

    async with AsyncSessionLocal() as db:
        call = (await db.execute(select(Call).where(Call.provider_call_id == channel_id))).scalar_one_or_none()
        if call is None:
            return

        media_session_manager.close_session(str(call.id))

        if call.status in {"completed", "failed", "no_answer", "busy"}:
            return

        now = datetime.now(timezone.utc)
        answered = call.answered_at is not None
        duration = int((now - call.answered_at).total_seconds()) if answered else None

        await call_service.finalize_call(
            db,
            call,
            status="completed" if answered else "no_answer",
            duration_seconds=duration,
            end_reason=kind,
        )
        await publish_event(call.tenant_id, "CALL_ENDED", {"call_id": str(call.id)})

        # Post-call transcript & analysis pipeline
        if voice_session:
            segments = voice_session.orchestrator.transcript_segments
            full_text = voice_session.orchestrator.full_transcript_text()
            try:
                await call_service.execute_post_call_pipeline(
                    db,
                    call,
                    segments=segments,
                    full_text=full_text,
                )
                logger.info("post_call_pipeline_completed", call_id=str(call.id))
            except Exception as e:
                logger.error("post_call_pipeline_failed", call_id=str(call.id), error=str(e))
