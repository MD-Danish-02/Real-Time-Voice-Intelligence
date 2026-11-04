"""Focused, non-database regression tests for RTP and ARI failure handling."""
import asyncio
import socket
import threading
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import Text
from sqlalchemy.exc import SQLAlchemyError

from app.models.call import Call
from app.telephony.asterisk import ari_client, event_handler
from app.voice_pipeline.rtp_bridge import CallMediaSession, MediaSessionManager, RTPPacketizer


def test_duplicate_concurrent_call_session_uses_one_port_and_socket():
    manager = MediaSessionManager(port_start=12000, port_end=12001)
    barrier = threading.Barrier(2)
    sessions = []
    errors = []

    def create():
        try:
            barrier.wait()
            sessions.append(manager.create_session("same-call"))
        except BaseException as exc:  # capture thread failures for assertion
            errors.append(exc)

    try:
        with patch.object(CallMediaSession, "start"):
            threads = [threading.Thread(target=create) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

        assert not errors
        assert all(not thread.is_alive() for thread in threads)
        assert len(sessions) == 2
        assert sessions[0] is sessions[1]
        assert len(manager._allocated_sockets) == 1
        assert manager._sessions == {"same-call": sessions[0]}
    finally:
        manager.close_session("same-call")


def test_cancelled_session_start_releases_reserved_socket():
    manager = MediaSessionManager(port_start=12002, port_end=12002)

    with patch.object(CallMediaSession, "start", side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            manager.create_session("cancelled-call")

    assert manager._allocated_sockets == {}
    assert manager.get_session("cancelled-call") is None


def test_session_close_failure_still_closes_reserved_socket():
    manager = MediaSessionManager(port_start=12003, port_end=12003)

    with patch.object(CallMediaSession, "start"):
        session = manager.create_session("close-fails")
    reserved_socket = manager._allocated_sockets[session.local_port]

    with patch.object(session, "close", side_effect=RuntimeError("simulated close failure")):
        with pytest.raises(RuntimeError, match="simulated close failure"):
            manager.close_session("close-fails")

    assert manager._allocated_sockets == {}
    assert reserved_socket.fileno() == -1


def test_ten_sequential_calls_release_and_reuse_rtp_port():
    manager = MediaSessionManager(port_start=12010, port_end=12010)

    with patch.object(CallMediaSession, "start"):
        for index in range(10):
            call_id = f"sequential-call-{index}"
            session = manager.create_session(call_id)
            reserved_socket = manager._allocated_sockets[session.local_port]
            manager.close_session(call_id)

            assert manager.get_session(call_id) is None
            assert manager._allocated_sockets == {}
            assert reserved_socket.fileno() == -1


def test_concurrent_distinct_calls_receive_distinct_rtp_ports():
    manager = MediaSessionManager(port_start=12020, port_end=12029)
    barrier = threading.Barrier(10)
    sessions = []
    errors = []

    def create(index):
        try:
            barrier.wait()
            sessions.append(manager.create_session(f"concurrent-call-{index}"))
        except BaseException as exc:
            errors.append(exc)

    try:
        with patch.object(CallMediaSession, "start"):
            threads = [threading.Thread(target=create, args=(index,)) for index in range(10)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

        assert not errors
        assert all(not thread.is_alive() for thread in threads)
        assert len(sessions) == 10
        assert len({session.local_port for session in sessions}) == 10
        assert len(manager._allocated_sockets) == 10
    finally:
        for index in range(10):
            manager.close_session(f"concurrent-call-{index}")


@pytest.mark.asyncio
async def test_rtp_loopback_counts_received_and_transmitted_packets():
    """Exercise actual UDP receive/transmit loops and their G.711 RTP counters."""
    manager = MediaSessionManager(port_start=15000, port_end=15020)
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("127.0.0.1", 0))
    peer.setblocking(False)
    received_audio = []
    session = None

    try:
        session = manager.create_session(
            "loopback-packet-counter-call",
            on_audio_received=received_audio.append,
        )
        peer_host, peer_port = peer.getsockname()
        session.set_remote_endpoint(peer_host, peer_port)
        packet = RTPPacketizer().pack(b"\xff" * 160, timestamp_samples=160)
        await asyncio.get_running_loop().sock_sendto(
            peer,
            packet,
            ("127.0.0.1", session.local_port),
        )

        async def wait_for_received_packet():
            while not received_audio:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait_for_received_packet(), timeout=1)
        assert session.received_packet_count == 1
        assert session.received_byte_count == len(packet)

        session.queue_outbound_audio(b"\x00\x00" * 320)
        response, _ = await asyncio.wait_for(
            asyncio.get_running_loop().sock_recvfrom(peer, 2048),
            timeout=1,
        )
        payload_type, _, _, payload = RTPPacketizer.unpack(response)
        assert payload_type == 0
        assert len(payload) == 160
        assert session.transmitted_packet_count == 1
        assert session.transmitted_byte_count == len(response)
    finally:
        if session is not None:
            manager.close_session("loopback-packet-counter-call")
            await asyncio.sleep(0)
        peer.close()


def test_call_end_reason_uses_unbounded_text_column():
    column_type = Call.__table__.c.end_reason.type
    assert isinstance(column_type, Text)
    call = Call(
        direction="outbound",
        status="failed",
        telephony_provider="plivo",
        end_reason="media_setup_failed: " + ("x" * 500),
    )
    assert len(call.end_reason) > 64


class _FailingCommitSession:
    def __init__(self):
        self.rollback_count = 0

    async def commit(self):
        raise SQLAlchemyError("simulated database update failure")

    async def rollback(self):
        self.rollback_count += 1


class _AsyncSessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_database_failure_rolls_back_and_ari_loop_handles_next_event(monkeypatch):
    failing_session = _FailingCommitSession()
    seen_events = []

    class FakeARIClient:
        def event_stream(self):
            async def stream():
                yield {"type": "DatabaseUpdateFails"}
                yield {"type": "NextEvent"}
                raise RuntimeError("end synthetic stream")

            return stream()

    async def handler(event):
        seen_events.append(event["type"])
        async with event_handler._db_session_with_rollback() as db:
            await db.commit()

    async def stop_after_stream(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(ari_client.settings, "ENV", "development")
    monkeypatch.setattr(ari_client, "AsteriskARIClient", FakeARIClient)
    monkeypatch.setattr(ari_client.asyncio, "sleep", stop_after_stream)
    monkeypatch.setattr(
        event_handler,
        "AsyncSessionLocal",
        lambda: _AsyncSessionContext(failing_session),
    )

    with pytest.raises(asyncio.CancelledError):
        await ari_client.run_event_loop(handler)

    assert failing_session.rollback_count == 2
    assert seen_events == ["DatabaseUpdateFails", "NextEvent"]


@pytest.mark.asyncio
async def test_hangup_attempts_media_cleanup_when_voice_session_close_raises(monkeypatch):
    channel_id = f"channel-{uuid.uuid4()}"
    call_id = uuid.uuid4()

    class FailingVoiceSession:
        def __init__(self):
            self.call_id = str(call_id)

        def close(self):
            raise RuntimeError("simulated voice-session close failure")

    monkeypatch.setitem(event_handler._active_voice_sessions, channel_id, FailingVoiceSession())
    monkeypatch.setitem(event_handler._channel_to_call_id, channel_id, call_id)
    monkeypatch.setitem(event_handler._active_bridges, channel_id, "bridge-for-cleanup")
    monkeypatch.setitem(event_handler._active_media_channels, channel_id, "external-media-for-cleanup")
    mock_ari = AsyncMock()
    close_session = patch.object(
        event_handler.media_session_manager,
        "close_session",
        side_effect=RuntimeError("simulated media cleanup failure"),
    )

    mock_db = MagicMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=mock_result)
    session_local = _AsyncSessionContext(mock_db)

    with close_session as close_session_mock, patch.object(
        event_handler, "AsyncSessionLocal", lambda: session_local
    ):
        await event_handler._handle_channel_hangup(
            ari=mock_ari,
            event={},
            channel_id=channel_id,
            kind="hangup",
        )

    close_session_mock.assert_called_once_with(str(call_id))
    mock_ari.hangup_channel.assert_awaited_once_with("external-media-for-cleanup")
    mock_ari.destroy_bridge.assert_awaited_once_with("bridge-for-cleanup")
    # Failed voice close remains owned for a later terminal event; successfully
    # destroyed ARI resources are removed independently.
    assert channel_id not in event_handler._active_bridges
    assert channel_id not in event_handler._active_media_channels
    assert channel_id in event_handler._active_voice_sessions
    event_handler._channel_to_call_id.pop(channel_id, None)
    event_handler._active_voice_sessions.pop(channel_id, None)


@pytest.mark.asyncio
async def test_concurrent_duplicate_terminal_events_share_one_cleanup(monkeypatch):
    channel_id = f"duplicate-terminal-{uuid.uuid4()}"
    call_id = uuid.uuid4()
    call = Call(
        id=call_id,
        direction="inbound",
        status="completed",
        telephony_provider="generic_sip",
        provider_call_id=channel_id,
    )
    query_result = MagicMock()
    query_result.scalar_one_or_none.return_value = call
    db = MagicMock()
    db.execute = AsyncMock(return_value=query_result)
    monkeypatch.setattr(event_handler, "AsyncSessionLocal", lambda: _AsyncSessionContext(db))
    monkeypatch.setitem(event_handler._channel_to_call_id, channel_id, call_id)
    monkeypatch.setitem(event_handler._active_bridges, channel_id, "duplicate-bridge")

    entered = asyncio.Event()
    release = asyncio.Event()
    ari = AsyncMock()

    async def slow_destroy(_bridge_id):
        entered.set()
        await release.wait()

    ari.destroy_bridge.side_effect = slow_destroy
    with patch.object(event_handler.media_session_manager, "close_session") as close_session:
        first = asyncio.create_task(
            event_handler._handle_channel_hangup(ari, {}, channel_id, "StasisEnd")
        )
        await entered.wait()
        second = asyncio.create_task(
            event_handler._handle_channel_hangup(ari, {}, channel_id, "ChannelDestroyed")
        )
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)

    ari.destroy_bridge.assert_awaited_once_with("duplicate-bridge")
    db.execute.assert_awaited_once()
    close_session.assert_called_once_with(str(call_id))
    assert channel_id not in event_handler._active_bridges
    assert channel_id not in event_handler._terminal_cleanup_tasks
    event_handler._channel_to_call_id.pop(channel_id, None)


@pytest.mark.asyncio
async def test_bridge_delete_failure_retains_only_its_mapping_for_retry(monkeypatch):
    channel_id = f"bridge-retry-{uuid.uuid4()}"
    call = Call(
        id=uuid.uuid4(),
        direction="inbound",
        status="completed",
        telephony_provider="generic_sip",
        provider_call_id=channel_id,
    )
    query_result = MagicMock()
    query_result.scalar_one_or_none.return_value = call
    db = MagicMock()
    db.execute = AsyncMock(return_value=query_result)
    monkeypatch.setattr(event_handler, "AsyncSessionLocal", lambda: _AsyncSessionContext(db))
    monkeypatch.setitem(event_handler._active_bridges, channel_id, "retry-bridge")
    ari = AsyncMock()
    ari.destroy_bridge.side_effect = [RuntimeError("temporary ARI failure"), None]

    with patch.object(event_handler.media_session_manager, "close_session"):
        await event_handler._handle_channel_hangup(ari, {}, channel_id, "StasisEnd")
        assert event_handler._active_bridges.get(channel_id) == "retry-bridge"
        await event_handler._handle_channel_hangup(ari, {}, channel_id, "ChannelDestroyed")

    assert ari.destroy_bridge.await_count == 2
    assert channel_id not in event_handler._active_bridges
    event_handler._channel_to_call_id.pop(channel_id, None)


@pytest.mark.asyncio
async def test_external_media_caller_hangup_failure_preserves_retry_mapping(monkeypatch):
    caller_id = f"caller-hangup-retry-{uuid.uuid4()}"
    media_id = f"media-hangup-retry-{uuid.uuid4()}"
    call = Call(
        id=uuid.uuid4(),
        direction="inbound",
        status="completed",
        telephony_provider="generic_sip",
        provider_call_id=caller_id,
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = call
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    monkeypatch.setattr(event_handler, "AsyncSessionLocal", lambda: _AsyncSessionContext(db))
    monkeypatch.setitem(event_handler._active_media_channels, caller_id, media_id)
    monkeypatch.setitem(event_handler._media_channel_to_caller, media_id, caller_id)
    ari = AsyncMock()
    # First teardown ends ExternalMedia but cannot hang up its caller. A duplicate
    # terminal event should use the preserved mapping and retry the caller hangup.
    ari.hangup_channel.side_effect = [None, RuntimeError("temporary caller hangup failure"), None, None]

    with patch.object(event_handler.media_session_manager, "close_session"):
        await event_handler._handle_channel_hangup(
            ari,
            {},
            caller_id,
            "ChannelDestroyed",
            hangup_caller=True,
            ended_media_channel_id=media_id,
        )
        assert event_handler._media_channel_to_caller.get(media_id) == caller_id
        assert event_handler._active_media_channels.get(caller_id) == media_id
        await event_handler._handle_channel_hangup(
            ari,
            {},
            caller_id,
            "ChannelDestroyed",
            hangup_caller=True,
            ended_media_channel_id=media_id,
        )

    assert ari.hangup_channel.await_count == 4
    assert media_id not in event_handler._media_channel_to_caller
    assert caller_id not in event_handler._active_media_channels
    event_handler._channel_to_call_id.pop(caller_id, None)


@pytest.mark.asyncio
async def test_concurrent_cleanup_keeps_call_resources_isolated(monkeypatch):
    channel_ids = [f"isolated-terminal-{uuid.uuid4()}" for _ in range(2)]
    calls = {
        channel_id: Call(
            id=uuid.uuid4(),
            direction="inbound",
            status="completed",
            telephony_provider="generic_sip",
            provider_call_id=channel_id,
        )
        for channel_id in channel_ids
    }

    class PerCallDB(_AsyncSessionContext):
        pass

    contexts = []
    for channel_id, call in calls.items():
        db = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = call
        db.execute = AsyncMock(return_value=result)
        contexts.append(PerCallDB(db))
        monkeypatch.setitem(event_handler._active_bridges, channel_id, f"bridge-{channel_id}")
        monkeypatch.setitem(event_handler._channel_to_call_id, channel_id, call.id)

    context_iter = iter(contexts)
    monkeypatch.setattr(event_handler, "AsyncSessionLocal", lambda: next(context_iter))
    ari = AsyncMock()

    async def fail_one_bridge(bridge_id):
        if bridge_id == f"bridge-{channel_ids[0]}":
            raise RuntimeError("simulated failure isolated to first call")

    ari.destroy_bridge.side_effect = fail_one_bridge

    with patch.object(event_handler.media_session_manager, "close_session") as close_session:
        await asyncio.gather(*(
            event_handler._handle_channel_hangup(ari, {}, channel_id, "ChannelDestroyed")
            for channel_id in channel_ids
        ))

    assert {call.args[0] for call in ari.destroy_bridge.await_args_list} == {
        f"bridge-{channel_id}" for channel_id in channel_ids
    }
    assert {call.args[0] for call in close_session.call_args_list} == {
        str(call.id) for call in calls.values()
    }
    assert event_handler._active_bridges.get(channel_ids[0]) == f"bridge-{channel_ids[0]}"
    assert channel_ids[1] not in event_handler._active_bridges
    for channel_id in channel_ids:
        event_handler._channel_to_call_id.pop(channel_id, None)
        event_handler._active_bridges.pop(channel_id, None)


@pytest.mark.asyncio
async def test_database_failure_after_hangup_does_not_skip_media_cleanup(monkeypatch):
    channel_id = f"db-failure-cleanup-{uuid.uuid4()}"
    call_id = uuid.uuid4()
    call = Call(
        id=call_id,
        direction="inbound",
        status="in_progress",
        telephony_provider="generic_sip",
        provider_call_id=channel_id,
        answered_at=None,
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = call
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.rollback = AsyncMock()
    monkeypatch.setattr(event_handler, "AsyncSessionLocal", lambda: _AsyncSessionContext(db))
    monkeypatch.setitem(event_handler._channel_to_call_id, channel_id, call_id)
    monkeypatch.setitem(event_handler._active_bridges, channel_id, "db-failure-bridge")
    ari = AsyncMock()

    with patch.object(event_handler.media_session_manager, "close_session") as close_session, \
         patch("app.services.call_service.finalize_call", new_callable=AsyncMock,
               side_effect=SQLAlchemyError("simulated end_reason persistence failure")):
        with pytest.raises(SQLAlchemyError, match="simulated end_reason persistence failure"):
            await event_handler._handle_channel_hangup(ari, {}, channel_id, "ChannelDestroyed")

    close_session.assert_called_once_with(str(call_id))
    ari.destroy_bridge.assert_awaited_once_with("db-failure-bridge")
    db.rollback.assert_awaited_once()
    assert channel_id not in event_handler._active_bridges
    # Keep DB correlation after a failed status update so a later terminal event
    # can retry the update for outbound calls whose provider UUID differs.
    assert event_handler._channel_to_call_id[channel_id] == call_id
    event_handler._channel_to_call_id.pop(channel_id, None)
