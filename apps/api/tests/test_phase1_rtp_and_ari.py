"""
Isolated, non-destructive unit tests for Phase 1:
- RTP port allocator exhaustion and race-free socket retention
- Concurrency and socket release / re-use
- Plivo request UUID preservation & Asterisk channel ID correlation
- Idempotent ARI setup-failure cleanup
"""
import asyncio
import socket
import threading
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.call import Call
from app.telephony.asterisk import event_handler
from app.voice_pipeline.rtp_bridge import (
    CallMediaSession,
    MediaSessionManager,
    RTPPortPoolExhaustedError,
)


# ==============================================================================
# 1. RTP Port Allocator Tests
# ==============================================================================

def test_rtp_allocator_exhaustion():
    """Verify allocator raises RTPPortPoolExhaustedError instead of falling back to ephemeral port."""
    mgr = MediaSessionManager(port_start=11000, port_end=11002)

    try:
        p1 = mgr.allocate_port()
        p2 = mgr.allocate_port()
        p3 = mgr.allocate_port()
        assert {p1, p2, p3} == {11000, 11001, 11002}

        # 4th allocation must fail safely
        with pytest.raises(RTPPortPoolExhaustedError) as exc_info:
            mgr.allocate_port()

        assert "All RTP ports in range 11000-11002 are allocated or unavailable" in str(exc_info.value)
    finally:
        for p in (11000, 11001, 11002):
            mgr.release_port(p)


def test_rtp_allocator_socket_retention_and_release():
    """Verify allocator retains open socket during session life and releases cleanly on close.

    session.start() calls asyncio.get_event_loop() which requires a running loop.
    We mock start() to isolate the allocator and socket-retention logic, which is
    the actual subject under test.
    """
    mgr = MediaSessionManager(port_start=11005, port_end=11006)

    try:
        with patch.object(CallMediaSession, "start"):
            session = mgr.create_session("call-retention-test")
            port = session.local_port
            assert port in (11005, 11006)

            # Socket must be retained in manager while session is active
            assert port in mgr._allocated_sockets
            sock = mgr._allocated_sockets[port]
            assert sock.fileno() != -1

            # Close session — socket and port must be released
            mgr.close_session("call-retention-test")
            assert port not in mgr._allocated_sockets
            assert mgr.get_session("call-retention-test") is None

            # Re-allocation must succeed on the same port without WinError 10048
            session2 = mgr.create_session("call-retention-test-2")
            assert session2.local_port == port
            mgr.close_session("call-retention-test-2")
    finally:
        for p in (11005, 11006):
            mgr.release_port(p)


def test_rtp_allocator_concurrency():
    """Verify concurrent allocations obtain distinct ports without race conditions."""
    mgr = MediaSessionManager(port_start=11010, port_end=11029)  # 20 ports
    allocated_ports = []
    errors = []

    def _worker():
        try:
            p = mgr.allocate_port()
            allocated_ports.append(p)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=_worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert len(errors) == 0, f"Concurrent allocation errors: {errors}"
        assert len(allocated_ports) == 20
        assert len(set(allocated_ports)) == 20
        assert all(11010 <= p <= 11029 for p in allocated_ports)
    finally:
        for p in range(11010, 11030):
            mgr.release_port(p)


# ==============================================================================
# 2. Call ID Correlation Tests
# ==============================================================================

def test_call_model_asterisk_channel_id_property():
    """Verify Call model property tracks asterisk_channel_id without interfering with mapped attributes."""
    call = Call(
        id=uuid.uuid4(),
        direction="outbound",
        status="queued",
        telephony_provider="plivo",
        provider_call_id="cbe6a894-6b9f-43fb-b5cf-9bb2ee88dbd2",  # Plivo Request UUID
    )
    assert call.provider_call_id == "cbe6a894-6b9f-43fb-b5cf-9bb2ee88dbd2"
    assert call.asterisk_channel_id is None

    # Track Asterisk channel ID
    call.asterisk_channel_id = "1698547281.42"
    assert call.asterisk_channel_id == "1698547281.42"
    # Plivo Request UUID must remain untouched
    assert call.provider_call_id == "cbe6a894-6b9f-43fb-b5cf-9bb2ee88dbd2"


@pytest.mark.asyncio
async def test_stasis_start_preserves_plivo_request_uuid():
    """Verify caller setup is one-shot and its own UnicastRTP event is ignored."""
    call_id = uuid.uuid4()
    mock_call = Call(
        id=call_id,
        direction="outbound",
        status="dialing",
        telephony_provider="plivo",
        provider_call_id="plivo-originate-uuid-12345",
    )

    mock_db = MagicMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_call
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()

    mock_session_local = MagicMock()
    mock_session_local.__aenter__ = AsyncMock(return_value=mock_db)
    mock_session_local.__aexit__ = AsyncMock(return_value=None)

    mock_ari = AsyncMock()
    channel_id = "asterisk-chan-999"
    mock_ari.get_channel.return_value = {"id": channel_id, "state": "Up"}

    # Reset correlation map
    event_handler._channel_to_call_id.clear()

    try:
        with patch("app.telephony.asterisk.event_handler.AsyncSessionLocal", return_value=mock_session_local), \
             patch("app.telephony.asterisk.event_handler.create_live_voice_session", new_callable=AsyncMock) as mock_vs_create, \
             patch("app.telephony.asterisk.event_handler.media_session_manager.create_session") as mock_ms_create, \
             patch("app.services.call_service.record_call_event", new_callable=AsyncMock), \
             patch("app.events.bus.publish_event", new_callable=AsyncMock), \
             patch("app.telephony.asterisk.event_handler.AsteriskARIClient", return_value=mock_ari):

            mock_session = MagicMock()
            mock_session.local_port = 10000
            mock_session.start = AsyncMock()
            mock_vs_create.return_value = mock_session
            mock_ms_create.return_value = mock_session
            mock_ari.create_bridge.return_value = {"id": "bridge_123"}

            async def create_external_media_channel(**_kwargs):
                await event_handler.handle_ari_event({
                    "type": "StasisStart",
                    "channel": {
                        "id": "media_chan_456",
                        "name": "UnicastRTP/127.0.0.1:18000",
                    },
                    "args": [],
                })
                return {"id": "media_chan_456"}

            mock_ari.create_external_media_channel.side_effect = create_external_media_channel
            mock_ari.get_channel_variable.side_effect = ["10000", "100.80.52.68"]

            event = {
                "type": "StasisStart",
                "channel": {
                    "id": channel_id,
                    "name": "PJSIP/test-caller",
                    "caller": {"number": "+15551234567"},
                },
                "args": ["plivo_outbound", str(call_id)],
            }

            await event_handler.handle_ari_event(event)

            # Preserve Plivo correlation and create exactly one caller bridge/media channel.
            assert mock_call.provider_call_id == "plivo-originate-uuid-12345"
            assert mock_call.asterisk_channel_id == channel_id
            assert event_handler._channel_to_call_id.get(channel_id) == call_id
            mock_ari.create_bridge.assert_awaited_once()
            mock_ari.create_external_media_channel.assert_awaited_once()
    finally:
        event_handler._active_bridges.pop(channel_id, None)
        event_handler._active_media_channels.pop(channel_id, None)
        event_handler._media_channel_to_caller.pop("media_chan_456", None)
        event_handler._active_voice_sessions.pop(channel_id, None)
        event_handler._channel_to_call_id.pop(channel_id, None)
        event_handler._stasis_start_processed.discard(f"call:{call_id}")
        event_handler._stasis_key_by_channel.pop(channel_id, None)


@pytest.mark.asyncio
async def test_stasis_event_router_ignores_external_media_and_deduplicates_caller(monkeypatch):
    """Only one PJSIP caller setup is dispatched for a call; UnicastRTP is ignored."""
    call_id = str(uuid.uuid4())
    handled_channel_ids = []

    async def fake_stasis_start(_ari, _event, _channel, channel_id):
        handled_channel_ids.append(channel_id)

    monkeypatch.setattr(event_handler, "AsteriskARIClient", MagicMock)
    monkeypatch.setattr(event_handler, "_handle_stasis_start", fake_stasis_start)
    dedupe_key = f"call:{call_id}"
    event_handler._stasis_start_processed.discard(dedupe_key)
    event_handler._stasis_key_by_channel.pop("caller-channel-1", None)
    event_handler._stasis_key_by_channel.pop("caller-channel-2", None)

    try:
        await event_handler.handle_ari_event({
            "type": "StasisStart",
            "channel": {"id": "external-media-channel", "name": "UnicastRTP/127.0.0.1:18000"},
            "args": [],
        })
        await event_handler.handle_ari_event({
            "type": "StasisStart",
            "channel": {"id": "unsupported-route-channel", "name": "PJSIP/other-route-00001"},
            "args": ["unknown-route", "+15551234567"],
        })
        inbound_event = {
            "type": "StasisStart",
            "channel": {"id": "inbound-channel-1", "name": "PJSIP/inbound-trunk-00001"},
            "args": ["inbound", "1001"],
        }
        await event_handler.handle_ari_event(inbound_event)
        await event_handler.handle_ari_event(inbound_event)
        generic_outbound_event = {
            "type": "StasisStart",
            "channel": {"id": "generic-outbound-channel", "name": "PJSIP/local-00001"},
            "args": ["outbound", "+15551234567"],
        }
        await event_handler.handle_ari_event(generic_outbound_event)
        caller_event = {
            "type": "StasisStart",
            "channel": {"id": "caller-channel-1", "name": "PJSIP/plivo-trunk-00001"},
            "args": ["plivo_outbound", call_id],
        }
        await event_handler.handle_ari_event(caller_event)
        duplicate_channel_event = {
            **caller_event,
            "channel": {"id": "caller-channel-2", "name": "PJSIP/plivo-trunk-00002"},
        }
        await event_handler.handle_ari_event(duplicate_channel_event)
        await event_handler.handle_ari_event(caller_event)

        assert handled_channel_ids == [
            "inbound-channel-1", "generic-outbound-channel", "caller-channel-1"
        ]
    finally:
        event_handler._stasis_start_processed.discard(dedupe_key)
        event_handler._stasis_start_processed.discard("channel:inbound-channel-1")
        event_handler._stasis_key_by_channel.pop("inbound-channel-1", None)
        event_handler._stasis_start_processed.discard("channel:generic-outbound-channel")
        event_handler._stasis_key_by_channel.pop("generic-outbound-channel", None)
        event_handler._stasis_key_by_channel.pop("caller-channel-1", None)


@pytest.mark.asyncio
async def test_generic_outbound_route_never_creates_an_inbound_record(monkeypatch):
    channel_id = "generic-outbound-without-call-record"
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=db)
    session.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(event_handler, "AsyncSessionLocal", lambda: session)
    ari = AsyncMock()

    await event_handler._handle_stasis_start(
        ari,
        {
            "type": "StasisStart",
            "channel": {"id": channel_id, "name": "PJSIP/local-00001"},
            "args": ["outbound", "+15551234567"],
        },
        {"id": channel_id, "name": "PJSIP/local-00001"},
        channel_id,
    )

    db.add.assert_not_called()
    ari.hangup_channel.assert_awaited_once_with(channel_id)


@pytest.mark.asyncio
async def test_external_media_terminal_event_cleans_up_its_caller(monkeypatch):
    mock_ari = AsyncMock()
    cleanup = AsyncMock()
    monkeypatch.setattr(event_handler, "AsteriskARIClient", lambda: mock_ari)
    monkeypatch.setattr(event_handler, "_handle_channel_hangup", cleanup)
    event_handler._active_media_channels["caller-channel-for-media"] = "external-media-channel"
    event_handler._media_channel_to_caller["external-media-channel"] = "caller-channel-for-media"

    await event_handler.handle_ari_event({
        "type": "ChannelDestroyed",
        "channel": {"id": "external-media-channel", "name": "UnicastRTP/127.0.0.1:18000"},
    })

    cleanup.assert_awaited_once_with(
        mock_ari,
        {
            "type": "ChannelDestroyed",
            "channel": {"id": "external-media-channel", "name": "UnicastRTP/127.0.0.1:18000"},
        },
        "caller-channel-for-media",
        "ChannelDestroyed",
        hangup_caller=True,
        ended_media_channel_id="external-media-channel",
    )
    assert event_handler._media_channel_to_caller.get("external-media-channel") == "caller-channel-for-media"
    # Ownership remains until the real cleanup coroutine confirms remote teardown.
    assert event_handler._active_media_channels.get("caller-channel-for-media") == "external-media-channel"
    event_handler._active_media_channels.pop("caller-channel-for-media", None)
    event_handler._media_channel_to_caller.pop("external-media-channel", None)


@pytest.mark.asyncio
async def test_unknown_external_media_terminal_event_cannot_route_to_call(monkeypatch):
    hangup = AsyncMock()
    monkeypatch.setattr(event_handler, "_handle_channel_hangup", hangup)
    ari = AsyncMock()
    monkeypatch.setattr(event_handler, "AsteriskARIClient", lambda: ari)

    await event_handler.handle_ari_event({
        "type": "ChannelDestroyed",
        "channel": {"id": "unknown-external-media", "name": "UnicastRTP/127.0.0.1:19000"},
    })

    hangup.assert_not_awaited()
    ari.hangup_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_external_media_mapping_does_not_terminate_newer_call_media(monkeypatch):
    hangup = AsyncMock()
    monkeypatch.setattr(event_handler, "_handle_channel_hangup", hangup)
    ari = AsyncMock()
    monkeypatch.setattr(event_handler, "AsteriskARIClient", lambda: ari)
    caller_id = "caller-with-new-media"
    old_media_id = "old-external-media"
    event_handler._media_channel_to_caller[old_media_id] = caller_id
    event_handler._active_media_channels[caller_id] = "new-external-media"

    try:
        await event_handler.handle_ari_event({
            "type": "ChannelDestroyed",
            "channel": {"id": old_media_id, "name": "UnicastRTP/127.0.0.1:19000"},
        })
        hangup.assert_not_awaited()
        ari.hangup_channel.assert_not_awaited()
        assert event_handler._active_media_channels[caller_id] == "new-external-media"
        assert old_media_id not in event_handler._media_channel_to_caller
    finally:
        event_handler._active_media_channels.pop(caller_id, None)
        event_handler._media_channel_to_caller.pop(old_media_id, None)


@pytest.mark.asyncio
async def test_external_media_terminal_events_route_to_their_own_call(monkeypatch):
    ari = AsyncMock()
    monkeypatch.setattr(event_handler, "AsteriskARIClient", lambda: ari)
    routed_callers = []

    async def record_cleanup(_ari, _event, caller_id, _kind, **_kwargs):
        routed_callers.append(caller_id)

    monkeypatch.setattr(event_handler, "_handle_channel_hangup", record_cleanup)
    pairs = [("media-a", "caller-a"), ("media-b", "caller-b")]
    for media_id, caller_id in pairs:
        event_handler._active_media_channels[caller_id] = media_id
        event_handler._media_channel_to_caller[media_id] = caller_id

    try:
        await asyncio.gather(*(
            event_handler.handle_ari_event({
                "type": "ChannelDestroyed",
                "channel": {"id": media_id, "name": "UnicastRTP/127.0.0.1:19000"},
            })
            for media_id, _caller_id in pairs
        ))
        assert routed_callers == ["caller-a", "caller-b"]
        assert ari.hangup_channel.await_count == 0
    finally:
        for media_id, caller_id in pairs:
            event_handler._active_media_channels.pop(caller_id, None)
            event_handler._media_channel_to_caller.pop(media_id, None)


# ==============================================================================
# 3. Idempotent ARI Teardown Cleanup Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_cleanup_failed_stasis_start_all_resources():
    """Verify _cleanup_failed_stasis_start destroys bridge, channel, and releases media session."""
    mock_ari = AsyncMock()
    channel_id = "test-chan-101"
    bridge_id = "test-bridge-202"
    media_chan_id = "test-media-303"
    call_id = "test-call-404"

    mock_voice_session = MagicMock()
    event_handler._active_bridges[channel_id] = bridge_id
    event_handler._active_media_channels[channel_id] = media_chan_id
    event_handler._active_voice_sessions[channel_id] = mock_voice_session
    event_handler._channel_to_call_id[channel_id] = uuid.uuid4()

    with patch("app.telephony.asterisk.event_handler.media_session_manager.close_session") as mock_close_ms:
        await event_handler._cleanup_failed_stasis_start(
            ari=mock_ari,
            channel_id=channel_id,
            call_id=call_id,
            bridge_id=bridge_id,
            media_chan_id=media_chan_id,
            voice_session=mock_voice_session,
        )

        mock_voice_session.close.assert_called_once()
        mock_close_ms.assert_called_once_with(call_id)
        mock_ari.hangup_channel.assert_any_call(media_chan_id)
        mock_ari.destroy_bridge.assert_called_once_with(bridge_id)
        mock_ari.hangup_channel.assert_any_call(channel_id)

        # Maps must be clean
        assert channel_id not in event_handler._active_bridges
        assert channel_id not in event_handler._active_media_channels
        assert channel_id not in event_handler._active_voice_sessions
        assert channel_id not in event_handler._channel_to_call_id


# ==============================================================================
# 4. Fix 1 — Socket released on session startup failure
# ==============================================================================

def test_create_session_releases_port_on_init_failure():
    """Fix 1: If CallMediaSession.__init__ raises, the port must be released and re-allocatable.

    session.start() requires a running event loop; we mock it in the recovery step
    so the test exercises only allocator and socket-ownership logic.
    """
    mgr = MediaSessionManager(port_start=11030, port_end=11031)

    call_id = "call-init-fail"
    with patch(
        "app.voice_pipeline.rtp_bridge.CallMediaSession.__init__",
        side_effect=RuntimeError("simulated __init__ failure"),
    ):
        with pytest.raises(RuntimeError, match="simulated __init__ failure"):
            mgr.create_session(call_id)

    # Fix 1: port must be released — _allocated_sockets must be empty
    assert len(mgr._allocated_sockets) == 0, (
        f"Port leaked in _allocated_sockets after __init__ failure: {list(mgr._allocated_sockets)}"
    )
    # The call_id must not be in _sessions
    assert mgr.get_session(call_id) is None

    # The same port must now be re-allocatable without error (mock start to avoid event-loop requirement)
    try:
        with patch.object(CallMediaSession, "start"):
            session2 = mgr.create_session("call-after-recovery")
        assert session2 is not None
        assert session2.local_port in (11030, 11031)
        mgr.close_session("call-after-recovery")
    finally:
        for p in (11030, 11031):
            mgr.release_port(p)


def test_create_session_releases_port_on_start_failure():
    """Fix 1: If session.start() raises, the port must be released and re-allocatable.

    session.start() requires a running event loop; we mock it in the recovery step
    so the test exercises only allocator and socket-ownership logic.
    """
    mgr = MediaSessionManager(port_start=11032, port_end=11033)

    call_id = "call-start-fail"
    with patch.object(CallMediaSession, "start", side_effect=RuntimeError("simulated start failure")):
        with pytest.raises(RuntimeError, match="simulated start failure"):
            mgr.create_session(call_id)

    # Fix 1: port must be released — _allocated_sockets must be empty
    assert len(mgr._allocated_sockets) == 0, (
        f"Port leaked in _allocated_sockets after start() failure: {list(mgr._allocated_sockets)}"
    )
    assert call_id not in [s.call_id for s in mgr._sessions.values()]

    # Re-allocation must succeed (mock start to avoid event-loop requirement)
    try:
        with patch.object(CallMediaSession, "start"):
            session2 = mgr.create_session("call-after-start-recovery")
        assert session2.local_port in (11032, 11033)
        mgr.close_session("call-after-start-recovery")
    finally:
        for p in (11032, 11033):
            mgr.release_port(p)


# ==============================================================================
# 5. Fix 2 — Media session closed even when DB lookup returns None
# ==============================================================================

@pytest.mark.asyncio
async def test_hangup_closes_media_session_when_db_returns_none():
    """Fix 2: media_session_manager.close_session must be called even if the DB
    lookup finds no matching call record (early return path)."""
    import uuid as uuid_mod

    mock_ari = AsyncMock()
    channel_id = "chan-db-miss-test"
    call_id = uuid_mod.uuid4()

    # Seed the channel-to-call mapping so the first DB path is taken
    event_handler._channel_to_call_id[channel_id] = call_id
    # Ensure no stale entries from other tests
    event_handler._active_voice_sessions.pop(channel_id, None)
    event_handler._active_bridges.pop(channel_id, None)
    event_handler._active_media_channels.pop(channel_id, None)

    # DB returns None for both lookups → triggers early return
    mock_db = MagicMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=mock_result)

    mock_session_local = MagicMock()
    mock_session_local.__aenter__ = AsyncMock(return_value=mock_db)
    mock_session_local.__aexit__ = AsyncMock(return_value=None)

    with patch("app.telephony.asterisk.event_handler.AsyncSessionLocal", return_value=mock_session_local), \
         patch("app.telephony.asterisk.event_handler.media_session_manager.close_session") as mock_close:

        await event_handler._handle_channel_hangup(mock_ari, {}, channel_id, "hangup")

        # close_session MUST have been called with the UUID even though DB returned None
        mock_close.assert_called_once_with(str(call_id))

    # Channel mapping must be cleaned up
    assert channel_id not in event_handler._channel_to_call_id


@pytest.mark.asyncio
async def test_hangup_closes_media_session_via_provider_call_id_fallback():
    """Fix 2: When call_id is absent from _channel_to_call_id but the DB fallback
    finds the call via provider_call_id, close_session must still be called."""
    import uuid as uuid_mod

    mock_ari = AsyncMock()
    channel_id = "chan-fallback-test"
    call_uuid = uuid_mod.uuid4()

    # No entry in _channel_to_call_id — force fallback path
    event_handler._channel_to_call_id.pop(channel_id, None)
    event_handler._active_voice_sessions.pop(channel_id, None)
    event_handler._active_bridges.pop(channel_id, None)
    event_handler._active_media_channels.pop(channel_id, None)

    mock_call = Call(
        id=call_uuid,
        direction="inbound",
        status="in_progress",
        telephony_provider="generic_sip",
        provider_call_id=channel_id,
    )

    mock_db = MagicMock()
    mock_result_none = MagicMock()
    mock_result_none.scalar_one_or_none.return_value = None
    mock_result_found = MagicMock()
    mock_result_found.scalar_one_or_none.return_value = mock_call

    # First execute (by call_id) → skipped (call_id is None); second (by provider_call_id) → found
    mock_db.execute = AsyncMock(return_value=mock_result_found)

    mock_session_local = MagicMock()
    mock_session_local.__aenter__ = AsyncMock(return_value=mock_db)
    mock_session_local.__aexit__ = AsyncMock(return_value=None)

    with patch("app.telephony.asterisk.event_handler.AsyncSessionLocal", return_value=mock_session_local), \
         patch("app.telephony.asterisk.event_handler.media_session_manager.close_session") as mock_close, \
         patch("app.services.call_service.finalize_call", new_callable=AsyncMock), \
         patch("app.events.bus.publish_event", new_callable=AsyncMock):

        mock_call.answered_at = None
        await event_handler._handle_channel_hangup(mock_ari, {}, channel_id, "hangup")

        # close_session must be called with the call's UUID from the fallback path
        mock_close.assert_called_once_with(str(call_uuid))


@pytest.mark.asyncio
async def test_cleanup_failed_stasis_start_idempotent_with_exceptions():
    """Verify cleanup continues and does not raise even if Asterisk teardown calls fail."""
    mock_ari = AsyncMock()
    mock_ari.destroy_bridge.side_effect = RuntimeError("Bridge already destroyed in Asterisk")
    mock_ari.hangup_channel.side_effect = RuntimeError("Channel already hung up")

    channel_id = "chan-resilient-1"
    bridge_id = "bridge-resilient-2"

    with patch("app.telephony.asterisk.event_handler.media_session_manager.close_session"):
        # Must not raise despite ARI exceptions
        await event_handler._cleanup_failed_stasis_start(
            ari=mock_ari,
            channel_id=channel_id,
            call_id="call-resilient-3",
            bridge_id=bridge_id,
            media_chan_id=None,
        )

        assert channel_id not in event_handler._active_bridges
