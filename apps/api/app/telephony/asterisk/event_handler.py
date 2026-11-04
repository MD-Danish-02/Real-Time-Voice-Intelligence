"""
Asterisk ARI Event Handler.
Handles complete inbound and outbound call lifecycles, bridging externalMedia RTP
into the real-time AI voice session, and triggering post-call transcription and analysis.
"""
import asyncio
from contextlib import asynccontextmanager
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
from app.telephony.asterisk.ari_client import AsteriskARIClient, sanitize_ari_diagnostic
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
_media_channel_to_caller: dict[str, str] = {}
_active_voice_sessions: dict[str, LiveVoiceSession] = {}
_channel_to_call_id: dict[str, uuid.UUID] = {}
_stasis_start_in_progress: set[str] = set()
_stasis_start_processed: set[str] = set()
_stasis_key_by_channel: dict[str, str] = {}
_terminal_cleanup_tasks: dict[str, asyncio.Task] = {}


@asynccontextmanager
async def _db_session_with_rollback():
    """Roll back failed/cancelled ARI database work before closing its session."""
    async with AsyncSessionLocal() as db:
        try:
            yield db
        except BaseException:
            try:
                await db.rollback()
            except BaseException as rollback_error:
                logger.error(
                    "ari_db_rollback_failed",
                    error_type=type(rollback_error).__name__,
                )
            raise


def _log_ari_operation_failure(
    *,
    call_id: str,
    channel_id: str,
    bridge_id: Optional[str],
    operation: str,
    exc: Exception,
) -> None:
    response = getattr(exc, "response", None)
    logger.error(
        "ari_channel_operation_failed",
        call_id=call_id,
        channel_id=channel_id,
        bridge_id=bridge_id,
        operation=operation,
        exception_type=type(exc).__name__,
        exception_message=sanitize_ari_diagnostic(str(exc)),
        http_status=getattr(response, "status_code", None),
        response_body=(
            AsteriskARIClient.sanitize_response_body(response.text)
            if response is not None
            else None
        ),
    )


def _ari_resource_is_gone(exc: Exception) -> bool:
    """A 404 during teardown means the remote resource is already cleaned up."""
    return getattr(getattr(exc, "response", None), "status_code", None) == 404


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

    if kind == "StasisStart":
        # ExternalMedia channels are delivered to the same Stasis application as
        # caller channels. Only caller-side PJSIP channels may enter call setup;
        # in particular, never recursively process UnicastRTP StasisStart events.
        channel_name = channel.get("name") or ""
        channel_technology = channel_name.partition("/")[0]
        if channel_technology != "PJSIP":
            logger.info(
                "ari_non_pjsip_stasis_start_ignored",
                channel_id=channel_id,
                channel_technology=channel_technology or "unknown",
            )
            return

        args = event.get("args") or []
        if not args or args[0] not in {"inbound", "outbound", "plivo_outbound"}:
            logger.info(
                "ari_pjsip_stasis_start_with_unrecognized_route_ignored",
                channel_id=channel_id,
            )
            return
        if args[0] == "plivo_outbound" and len(args) < 2:
            logger.warning("ari_plivo_stasis_start_missing_call_id", channel_id=channel_id)
            return
        dedupe_key = (
            f"call:{args[1]}"
            if len(args) > 1 and args[0] == "plivo_outbound"
            else f"channel:{channel_id}"
        )
        if (
            dedupe_key in _stasis_start_in_progress
            or dedupe_key in _stasis_start_processed
            or channel_id in _active_bridges
            or channel_id in _active_voice_sessions
        ):
            logger.info(
                "ari_duplicate_stasis_start_ignored",
                call_id=args[1] if len(args) > 1 and args[0] == "plivo_outbound" else None,
                channel_id=channel_id,
            )
            return

        # The check and claim happen before the first await, making duplicate
        # events on this worker's event loop mutually exclusive during setup.
        _stasis_start_in_progress.add(dedupe_key)
        try:
            await _handle_stasis_start(AsteriskARIClient(), event, channel, channel_id)
        except BaseException:
            raise
        else:
            # _handle_stasis_start handles media setup failures itself after
            # cleanup; retaining the key until the terminal channel event keeps
            # a repeated StasisStart from allocating a second bridge/session.
            _stasis_start_processed.add(dedupe_key)
            _stasis_key_by_channel[channel_id] = dedupe_key
        finally:
            _stasis_start_in_progress.discard(dedupe_key)
    elif kind == "ChannelStateChange":
        await _handle_channel_state_change(event, channel, channel_id)
    elif kind in {"StasisEnd", "ChannelDestroyed", "ChannelHangupRequest"}:
        caller_channel_id = _media_channel_to_caller.get(channel_id)
        ended_media_channel_id: Optional[str] = None
        if caller_channel_id:
            if _active_media_channels.get(caller_channel_id) != channel_id:
                # A late event for an old media channel must never tear down a
                # newer session that happens to belong to the same caller.
                if _media_channel_to_caller.get(channel_id) == caller_channel_id:
                    _media_channel_to_caller.pop(channel_id, None)
                logger.info(
                    "ari_stale_external_media_event_ignored",
                    external_media_channel_id=channel_id,
                    caller_channel_id=caller_channel_id,
                )
                return
            # An ExternalMedia channel ending before its caller must release the
            # call's media resources and terminate the now-unusable caller leg.
            ended_media_channel_id = channel_id
            channel_id = caller_channel_id
        elif (channel.get("name") or "").partition("/")[0] == "UnicastRTP":
            # Unknown or already-cleaned ExternalMedia channels are not call
            # identities and must not be used for DB lookup or caller cleanup.
            logger.info("ari_unknown_external_media_terminal_event_ignored", channel_id=channel_id)
            return
        try:
            await _handle_channel_hangup(
                AsteriskARIClient(),
                event,
                channel_id,
                kind,
                hangup_caller=ended_media_channel_id is not None,
                ended_media_channel_id=ended_media_channel_id,
            )
        finally:
            if kind in {"StasisEnd", "ChannelDestroyed"}:
                dedupe_key = _stasis_key_by_channel.pop(channel_id, None)
                if dedupe_key:
                    _stasis_start_processed.discard(dedupe_key)


async def _handle_stasis_start(ari: AsteriskARIClient, event: dict, channel: dict, channel_id: str) -> None:
    args = event.get("args") or []
    caller_info = channel.get("caller") or {}
    dialplan_info = channel.get("dialplan") or {}

    from_number = caller_info.get("number") or ""
    to_number = args[1] if len(args) > 1 else (dialplan_info.get("exten") or "")
    is_plivo_outbound = bool(args and args[0] == "plivo_outbound" and len(args) > 1)
    route = args[0] if args else None
    is_generic_outbound = route == "outbound"
    is_inbound = route == "inbound" or (
        route not in {"inbound", "outbound", "plivo_outbound"}
        and not bool(event.get("channel", {}).get("connected", {}).get("number"))
    )

    async with _db_session_with_rollback() as db:
        call = None
        if is_plivo_outbound:
            try:
                target_call_id = uuid.UUID(args[1])
                call = (await db.execute(select(Call).where(Call.id == target_call_id))).scalar_one_or_none()
                if call:
                    # Maintain Asterisk channel_id correlation without overwriting Plivo request_uuid
                    _channel_to_call_id[channel_id] = call.id
                    call.asterisk_channel_id = channel_id
                    await db.commit()
                    await db.refresh(call)
                    logger.info(
                        "plivo_outbound_call_correlated",
                        call_id=str(call.id),
                        channel_id=channel_id,
                        plivo_request_uuid=call.provider_call_id,
                    )
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
            if call is None and is_generic_outbound:
                logger.error("outbound_ari_call_not_found", channel_id=channel_id)
                await ari.hangup_channel(channel_id)
                return

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

        # Answer channel. A 404 is logged with context, then we inspect the channel
        # before deciding whether bridge attachment can proceed.
        bridge_id: Optional[str] = None
        media_chan_id: Optional[str] = None
        voice_session: Optional[LiveVoiceSession] = None
        try:
            await ari.answer_channel(channel_id)
        except Exception as exc:
            _log_ari_operation_failure(
                call_id=str(call.id),
                channel_id=channel_id,
                bridge_id=bridge_id,
                operation="answer_channel",
                exc=exc,
            )
            response = getattr(exc, "response", None)
            if getattr(response, "status_code", None) == 404:
                logger.warning(
                    "ari_answer_404_continuing_to_read_only_channel_inspection",
                    call_id=str(call.id),
                    channel_id=channel_id,
                    bridge_id=bridge_id,
                )
            else:
                logger.warning(
                    "ari_answer_failed_continuing_to_read_only_channel_inspection",
                    call_id=str(call.id),
                    channel_id=channel_id,
                    bridge_id=bridge_id,
                    exception_type=type(exc).__name__,
                )

        await call_service.record_call_event(db, call, "CALL_ANSWERED", {"channel": channel_id})
        await publish_event(call.tenant_id, "CALL_ANSWERED", {"call_id": str(call.id)})

        # Create ARI mixing bridge
        operation = "create_bridge"
        try:
            bridge = await ari.create_bridge(bridge_type="mixing", name=f"bridge_{call.id}")
            bridge_id = bridge["id"]
            _active_bridges[channel_id] = bridge_id

            operation = "inspect_caller_channel_before_bridge_add"
            try:
                caller_channel = await ari.get_channel(channel_id)
            except Exception as inspect_exc:
                _log_ari_operation_failure(
                    call_id=str(call.id),
                    channel_id=channel_id,
                    bridge_id=bridge_id,
                    operation=operation,
                    exc=inspect_exc,
                )
                raise
            logger.info(
                "ari_caller_channel_inspected_before_bridge_add",
                call_id=str(call.id),
                channel_id=channel_id,
                bridge_id=bridge_id,
                channel_exists=caller_channel is not None,
                channel_state=(caller_channel or {}).get("state"),
            )

            operation = "add_caller_channel_to_bridge"
            try:
                await ari.add_channel_to_bridge(bridge_id, channel_id)
            except Exception as add_exc:
                _log_ari_operation_failure(
                    call_id=str(call.id),
                    channel_id=channel_id,
                    bridge_id=bridge_id,
                    operation=operation,
                    exc=add_exc,
                )
                raise

            # Create media session for RTP exchange
            operation = "create_rtp_media_session"
            media_session = media_session_manager.create_session(call_id=str(call.id))

            # Create Asterisk externalMedia channel
            media_host = settings.VOICE_MEDIA_HOST_EXTERNAL or settings.VOICE_MEDIA_HOST
            ext_host = f"{media_host}:{media_session.local_port}"
            operation = "create_external_media_channel"
            media_chan = await ari.create_external_media_channel(
                external_host=ext_host,
                encapsulation="rtp",
                transport="udp",
                format_="ulaw",
            )
            media_chan_id = media_chan["id"]
            _active_media_channels[channel_id] = media_chan_id
            _media_channel_to_caller[media_chan_id] = channel_id
            operation = "add_external_media_channel_to_bridge"
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
            logger.info(
                "ari_external_media_rtp_endpoint_configured",
                call_id=str(call.id),
                caller_channel_id=channel_id,
                external_media_channel_id=media_chan_id,
                bridge_id=bridge_id,
                allocated_rtp_port=media_session.local_port,
                remote_rtp_host=media_session.remote_host,
                remote_rtp_port=media_session.remote_port,
                endpoint_configured=bool(media_session.remote_host and media_session.remote_port),
                format="ulaw",
            )

            # Instantiate LiveVoiceSession
            operation = "create_live_voice_session"
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
            voice_session.set_transport_playback_controls(
                media_session.is_playing,
                media_session.interrupt,
            )
            _active_voice_sessions[channel_id] = voice_session

            operation = "record_call_started_event"
            await call_service.record_call_event(db, call, "CALL_STARTED", {"bridge": bridge_id})
            await publish_event(call.tenant_id, "CALL_STARTED", {"call_id": str(call.id)})

            # Start greeting in background
            voice_session.schedule_start()
            logger.info("voice_session_connected_to_ari_bridge", call_id=str(call.id), channel=channel_id)

        except asyncio.CancelledError:
            # A cancelled setup must still release the RTP socket and partial ARI
            # resources. The surrounding DB context performs transaction rollback.
            try:
                await asyncio.shield(
                    _cleanup_failed_stasis_start(
                        ari=ari,
                        channel_id=channel_id,
                        call_id=str(call.id),
                        bridge_id=bridge_id,
                        media_chan_id=media_chan_id,
                        voice_session=voice_session,
                    )
                )
            except BaseException as cleanup_error:
                logger.error(
                    "cancelled_stasis_cleanup_failed",
                    call_id=str(call.id),
                    error_type=type(cleanup_error).__name__,
                )
            raise
        except Exception as exc:
            logger.error(
                "ari_media_bridge_setup_failed",
                call_id=str(call.id),
                channel_id=channel_id,
                bridge_id=bridge_id,
                operation=operation,
                exception_type=type(exc).__name__,
                exception_message=sanitize_ari_diagnostic(str(exc)),
                http_status=getattr(getattr(exc, "response", None), "status_code", None),
                response_body=(
                    AsteriskARIClient.sanitize_response_body(exc.response.text)
                    if getattr(exc, "response", None) is not None
                    else None
                ),
            )
            # Idempotently clean up partially allocated resources without masking original exception
            await _cleanup_failed_stasis_start(
                ari=ari,
                channel_id=channel_id,
                call_id=str(call.id),
                bridge_id=bridge_id,
                media_chan_id=media_chan_id,
                voice_session=voice_session,
            )
            try:
                call.status = "failed"
                call.ended_at = datetime.now(timezone.utc)
                call.end_reason = f"media_setup_failed: {exc}"
                await db.commit()
            except Exception as db_err:
                try:
                    await db.rollback()
                except Exception as rollback_err:
                    logger.error(
                        "failed_call_status_rollback_failed",
                        error_type=type(rollback_err).__name__,
                    )
                logger.warning(
                    "failed_updating_call_status_after_stasis_failure",
                    error_type=type(db_err).__name__,
                )
            await call_service.record_call_event(db, call, "CALL_FAILED", {"error": str(exc)})
            await publish_event(call.tenant_id, "CALL_FAILED", {"call_id": str(call.id)})


async def _cleanup_failed_stasis_start(
    ari: AsteriskARIClient,
    channel_id: str,
    call_id: Optional[str] = None,
    bridge_id: Optional[str] = None,
    media_chan_id: Optional[str] = None,
    voice_session: Optional[LiveVoiceSession] = None,
) -> None:
    """
    Idempotently cleans up partially initialized resources on StasisStart media setup failure.
    Catches and logs individual cleanup errors to guarantee full teardown attempt
    without masking the root cause exception.
    """
    cleanup_errors = 0
    media_session = media_session_manager.get_session(call_id) if call_id else None
    allocated_rtp_port = getattr(media_session, "local_port", None)
    vs = _active_voice_sessions.get(channel_id) or voice_session
    if vs:
        try:
            vs.close()
            if _active_voice_sessions.get(channel_id) is vs:
                _active_voice_sessions.pop(channel_id, None)
        except Exception as err:
            cleanup_errors += 1
            logger.warning("cleanup_voice_session_failed", channel_id=channel_id, error=str(err))

    if call_id:
        try:
            media_session_manager.close_session(call_id)
        except Exception as err:
            cleanup_errors += 1
            logger.warning("cleanup_media_session_failed", call_id=call_id, error=str(err))

    chan_id = media_chan_id or _active_media_channels.get(channel_id)
    if chan_id:
        try:
            await ari.hangup_channel(chan_id)
            if _active_media_channels.get(channel_id) == chan_id:
                _active_media_channels.pop(channel_id, None)
            if _media_channel_to_caller.get(chan_id) == channel_id:
                _media_channel_to_caller.pop(chan_id, None)
        except Exception as err:
            if _ari_resource_is_gone(err):
                if _active_media_channels.get(channel_id) == chan_id:
                    _active_media_channels.pop(channel_id, None)
                if _media_channel_to_caller.get(chan_id) == channel_id:
                    _media_channel_to_caller.pop(chan_id, None)
            else:
                cleanup_errors += 1
            logger.warning("cleanup_external_media_channel_failed", media_chan_id=chan_id, error=str(err))

    b_id = bridge_id or _active_bridges.get(channel_id)
    if b_id:
        try:
            await ari.destroy_bridge(b_id)
            if _active_bridges.get(channel_id) == b_id:
                _active_bridges.pop(channel_id, None)
        except Exception as err:
            if _ari_resource_is_gone(err):
                if _active_bridges.get(channel_id) == b_id:
                    _active_bridges.pop(channel_id, None)
            else:
                cleanup_errors += 1
            logger.warning("cleanup_bridge_failed", bridge_id=b_id, error=str(err))

    try:
        await ari.hangup_channel(channel_id)
        _channel_to_call_id.pop(channel_id, None)
    except Exception as err:
        if _ari_resource_is_gone(err):
            _channel_to_call_id.pop(channel_id, None)
        else:
            cleanup_errors += 1
        logger.warning("cleanup_caller_channel_failed", channel_id=channel_id, error=str(err))

    logger.info(
        "ari_media_cleanup_completed",
        call_id=call_id,
        caller_channel_id=channel_id,
        external_media_channel_id=chan_id,
        bridge_id=b_id,
        allocated_rtp_port=allocated_rtp_port,
        rtp_session_released=(
            media_session_manager.get_session(call_id) is None if call_id else None
        ),
        cleanup_success=cleanup_errors == 0,
        cleanup_error_count=cleanup_errors,
    )


async def _handle_channel_state_change(event: dict, channel: dict, channel_id: str) -> None:
    state = channel.get("state")
    if state not in _STATE_TO_EVENT:
        return

    status, ev = _STATE_TO_EVENT[state]
    async with _db_session_with_rollback() as db:
        call = None
        call_id = _channel_to_call_id.get(channel_id)
        if call_id:
            call = (await db.execute(select(Call).where(Call.id == call_id))).scalar_one_or_none()
        if call is None:
            call = (await db.execute(select(Call).where(Call.provider_call_id == channel_id))).scalar_one_or_none()
        if call is None:
            return

        call.status = status
        now = datetime.now(timezone.utc)
        if status == "in_progress" and call.answered_at is None:
            call.answered_at = now
        await call_service.record_call_event(db, call, ev, {"channel": channel_id})
        await publish_event(call.tenant_id, ev, {"call_id": str(call.id)})


async def _handle_channel_hangup(
    ari: AsteriskARIClient,
    event: dict,
    channel_id: str,
    kind: str,
    *,
    hangup_caller: bool = False,
    ended_media_channel_id: Optional[str] = None,
) -> None:
    """Coalesce concurrent terminal events for one caller into one teardown."""
    task = _terminal_cleanup_tasks.get(channel_id)
    if task is None:
        task = asyncio.create_task(
            _perform_channel_hangup(
                ari,
                event,
                channel_id,
                kind,
                hangup_caller=hangup_caller,
                ended_media_channel_id=ended_media_channel_id,
            )
        )
        _terminal_cleanup_tasks[channel_id] = task

        def clear_completed_task(done_task: asyncio.Task) -> None:
            if _terminal_cleanup_tasks.get(channel_id) is done_task:
                _terminal_cleanup_tasks.pop(channel_id, None)

        task.add_done_callback(clear_completed_task)
    await asyncio.shield(task)


async def _perform_channel_hangup(
    ari: AsteriskARIClient,
    event: dict,
    channel_id: str,
    kind: str,
    *,
    hangup_caller: bool = False,
    ended_media_channel_id: Optional[str] = None,
) -> None:
    voice_session = _active_voice_sessions.get(channel_id)
    cleanup_error: Optional[Exception] = None
    if voice_session:
        try:
            voice_session.close()
            if _active_voice_sessions.get(channel_id) is voice_session:
                _active_voice_sessions.pop(channel_id, None)
        except Exception as exc:
            # Preserve the first teardown error, but continue releasing the RTP
            # socket and ARI resources before surfacing it to the event-loop guard.
            cleanup_error = exc
            logger.warning(
                "hangup_voice_session_cleanup_failed",
                channel_id=channel_id,
                error_type=type(exc).__name__,
            )

    bridge_id = _active_bridges.get(channel_id)
    media_chan_id = _active_media_channels.get(channel_id)

    if media_chan_id:
        try:
            await ari.hangup_channel(media_chan_id)
            if _active_media_channels.get(channel_id) == media_chan_id:
                _active_media_channels.pop(channel_id, None)
            if _media_channel_to_caller.get(media_chan_id) == channel_id:
                _media_channel_to_caller.pop(media_chan_id, None)
        except Exception as exc:
            if _ari_resource_is_gone(exc):
                if _active_media_channels.get(channel_id) == media_chan_id:
                    _active_media_channels.pop(channel_id, None)
                if _media_channel_to_caller.get(media_chan_id) == channel_id:
                    _media_channel_to_caller.pop(media_chan_id, None)
            else:
                logger.warning("hangup_external_media_cleanup_failed", channel_id=channel_id, error_type=type(exc).__name__)
                cleanup_error = cleanup_error or exc

    if hangup_caller:
        try:
            await ari.hangup_channel(channel_id)
        except Exception as exc:
            if not _ari_resource_is_gone(exc):
                logger.warning(
                    "hangup_caller_after_external_media_end_failed",
                    caller_channel_id=channel_id,
                    external_media_channel_id=ended_media_channel_id,
                    error_type=type(exc).__name__,
                )
                cleanup_error = cleanup_error or exc
                if ended_media_channel_id:
                    # Preserve the association as a retry key for a repeated
                    # terminal event; do not let a failed caller hangup strand it.
                    _active_media_channels[channel_id] = ended_media_channel_id
                    _media_channel_to_caller[ended_media_channel_id] = channel_id

    if bridge_id:
        try:
            await ari.destroy_bridge(bridge_id)
            if _active_bridges.get(channel_id) == bridge_id:
                _active_bridges.pop(channel_id, None)
        except Exception as exc:
            if _ari_resource_is_gone(exc):
                if _active_bridges.get(channel_id) == bridge_id:
                    _active_bridges.pop(channel_id, None)
            else:
                logger.warning("hangup_bridge_cleanup_failed", channel_id=channel_id, bridge_id=bridge_id, error_type=type(exc).__name__)
                cleanup_error = cleanup_error or exc

    call_id = _channel_to_call_id.get(channel_id) or getattr(voice_session, "call_id", None)
    if call_id:
        try:
            media_session_manager.close_session(str(call_id))
        except Exception as exc:
            logger.warning(
                "hangup_media_session_cleanup_failed",
                call_id=str(call_id),
                error_type=type(exc).__name__,
            )
            if cleanup_error is None:
                cleanup_error = exc

    logger.info(
        "ari_call_media_cleanup_completed",
        call_id=str(call_id) if call_id else None,
        caller_channel_id=channel_id,
        external_media_channel_id=media_chan_id,
        bridge_id=bridge_id,
        rtp_session_released=(media_session_manager.get_session(str(call_id)) is None if call_id else None),
        cleanup_error_type=type(cleanup_error).__name__ if cleanup_error else None,
    )
    async with _db_session_with_rollback() as db:
        call = None
        call_id = _channel_to_call_id.get(channel_id)

        # Release the media session as soon as the call UUID is known, before any DB
        # lookup that could return None and cause an early return.  close_session() is
        # idempotent: a second call for the same call_id is a safe no-op.
        if call_id:
            call = (await db.execute(select(Call).where(Call.id == call_id))).scalar_one_or_none()
        if call is None:
            call = (await db.execute(select(Call).where(Call.provider_call_id == channel_id))).scalar_one_or_none()
        if call is None:
            _channel_to_call_id.pop(channel_id, None)
            return

        # Session already closed above for the call_id path; this handles the fallback
        # path where call was found via provider_call_id instead of _channel_to_call_id.
        if call_id is None:
            media_session_manager.close_session(str(call.id))

        if call.status in {"completed", "failed", "no_answer", "busy"}:
            _channel_to_call_id.pop(channel_id, None)
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
        _channel_to_call_id.pop(channel_id, None)
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
