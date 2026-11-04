"""
Local Voice Test WebSocket Endpoint and Interactive Browser Test Harness.
Enables end-to-end testing of the REAL voice pipeline (Microphone -> Silero VAD ->
faster-whisper -> Groq -> Kokoro ONNX TTS -> Audio Out) with multi-turn conversation
and real-time barge-in interruption without needing a paid telephony provider.
"""
import asyncio
import json
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import AsyncSessionLocal
from app.models.call import Call
from app.models.tenant import Tenant
from app.services import call_service
from app.voice_pipeline.session import create_live_voice_session

logger = get_logger("voice.test_endpoint")

router = APIRouter(prefix="/voice", tags=["voice_test"])


@router.websocket("/ws/test")
@router.websocket("/ws/test/{tenant_id}")
async def voice_websocket_test(websocket: WebSocket, tenant_id: Optional[str] = None):
    """
    Real-time bidirectional audio streaming WebSocket for local voice testing:
    - Client sends binary 16kHz PCM16 audio from microphone (or JSON control messages).
    - Server streams binary 16kHz PCM16 audio from Kokoro ONNX TTS to client speaker.
    - Emits real-time turn events (VAD, STT, LLM tokens, TTS metrics, barge-in).
    """
    await websocket.accept()
    call_id = str(uuid.uuid4())
    language = websocket.query_params.get("language", "auto").lower()
    if language not in ("auto", "hi", "en"):
        await websocket.send_json({"event": "VOICE_ERROR", "payload": {"error": "Unsupported language. Use auto, hi, or en."}})
        await websocket.close(code=1008)
        return

    db = None
    call = None
    tenant_uuid = uuid.UUID(tenant_id) if tenant_id else uuid.uuid4()
    try:
        async with AsyncSessionLocal() as session_db:
            # Resolve tenant
            tenant = None
            if tenant_id:
                try:
                    tenant = (await session_db.execute(select(Tenant).where(Tenant.id == tenant_uuid))).scalar_one_or_none()
                except Exception:
                    pass
            if tenant is None:
                tenant = (await session_db.execute(select(Tenant).where(Tenant.is_active == True).limit(1))).scalar_one_or_none()
            if tenant is None:
                tenant = Tenant(name="Local Test Workspace", slug="local-test", is_active=True)
                session_db.add(tenant)
                await session_db.commit()
                await session_db.refresh(tenant)
            tenant_uuid = tenant.id

            # Create Call record for tracking
            call = Call(
                id=uuid.UUID(call_id),
                tenant_id=tenant_uuid,
                direction="inbound",
                status="in_progress",
                attempt_number=1,
                telephony_provider="local_websocket",
                from_number="+919876543210",
                to_number="+918000000000",
            )
            session_db.add(call)
            await session_db.commit()
            db = session_db
    except Exception as db_err:
        logger.info("local_test_running_standalone_without_db", detail=str(db_err))

    # Audio chunk callback to stream binary TTS PCM audio back to browser
    async def _on_audio_out(chunk: bytes) -> None:
        try:
            await websocket.send_bytes(chunk)
        except Exception:
            pass

    # Control callback (hangup)
    async def _on_control(action: str) -> None:
        try:
            await websocket.send_json({"event": "CALL_CONTROL", "action": action})
            if action == "end":
                await websocket.close()
        except Exception:
            pass

    try:
        session = await create_live_voice_session(
            db,
            call_id=call_id,
            tenant_id=tenant_uuid,
            on_audio_out=_on_audio_out,
            on_control=_on_control,
            language=language,
        )
    except Exception as provider_error:
        logger.error("voice_session_provider_validation_failed", language=language, error=str(provider_error))
        await websocket.send_json({
            "event": "VOICE_ERROR",
            "payload": {"error": f"Voice provider configuration error: {provider_error}"},
        })
        await websocket.close(code=1011)
        return

    # The local test client consumes events directly on this WebSocket. Do not
    # also await the production Redis event bus here: a Redis outage otherwise
    # blocks the voice turn (including the first TTS request) for its timeout.
    async def _forward_event(ev_type: str, payload: dict) -> None:
        try:
            await websocket.send_json({"event": ev_type, "payload": payload})
        except Exception:
            pass

    session.orchestrator.on_event = _forward_event

    # Active provider metadata for UI display and verification
    active_providers = {
        "vad": session.vad.runtime_name,
        "stt": f"{type(session.orchestrator.stt).__name__} ({getattr(session.orchestrator.stt, 'language', getattr(session.orchestrator.stt, '_language', 'auto'))})",
        "llm": f"{type(session.orchestrator.llm).__name__} ({settings.GROQ_MODEL if type(session.orchestrator.llm).__name__ == 'GroqLLMProvider' else 'mock'}; per-utterance language)",
        "tts": f"{type(session.orchestrator.tts).__name__} (Kokoro ONNX; hf_alpha / hi or af_heart / en-us per utterance)",
    }

    # Send session initialized confirmation
    await websocket.send_json({
        "event": "SESSION_CONNECTED",
        "call_id": call_id,
        "tenant_id": str(tenant_uuid),
        "sample_rate": 16000,
        "format": "pcm16",
        "language": language,
        "providers": active_providers,
    })

    # Start initial greeting safely
    async def _safe_start():
        try:
            await session.start()
        except Exception as start_err:
            logger.error("voice_session_start_failed", error=str(start_err))
            try:
                await websocket.send_json({
                    "event": "VOICE_ERROR",
                    "payload": {"error": f"Initial greeting failed: {str(start_err)}"},
                })
            except Exception:
                pass

    greeting_task = asyncio.create_task(_safe_start())

    try:
        while True:
            message = await websocket.receive()
            if "bytes" in message and message["bytes"]:
                # Binary audio chunk from microphone (16kHz PCM16)
                session.feed_input_audio(message["bytes"])
            elif "text" in message and message["text"]:
                try:
                    data = json.loads(message["text"])
                    msg_type = data.get("type")
                    if msg_type == "interrupt":
                        session.set_browser_playback_active(False)
                        session.orchestrator.interrupt()
                        await session.orchestrator.notify_interruption({"reason": "manual_interrupt"})
                    elif msg_type == "text_input":
                        # Full conversational turn through real LLM and streaming Kokoro TTS
                        sim_text = data.get("text", "").strip()
                        if sim_text:
                            session.schedule_input_text(sim_text)
                    elif msg_type == "playback_started":
                        session.set_browser_playback_active(True)
                        logger.info("browser_audio_playback_started", call_id=call_id,
                                    browser_time_ms=data.get("browser_time_ms"))
                    elif msg_type == "playback_completed":
                        session.set_browser_playback_active(False)
                        logger.info("browser_audio_playback_completed", call_id=call_id,
                                    browser_time_ms=data.get("browser_time_ms"),
                                    playback_duration_ms=data.get("playback_duration_ms"))
                    elif msg_type == "hangup":
                        break
                except Exception as err:
                    logger.warning("invalid_ws_json_message", error=str(err))
    except WebSocketDisconnect:
        logger.info("voice_ws_disconnected", call_id=call_id)
    except Exception as e:
        logger.error("voice_ws_error", call_id=call_id, error=str(e))
    finally:
        if not greeting_task.done():
            greeting_task.cancel()
            await asyncio.gather(greeting_task, return_exceptions=True)
        await session.aclose()
        # Post-call analysis and transcript save
        if db is not None and call is not None:
            try:
                segments = session.orchestrator.transcript_segments
                full_text = session.orchestrator.full_transcript_text()
                await call_service.finalize_call(db, call, status="completed", duration_seconds=30, end_reason="ws_hangup")
                await call_service.execute_post_call_pipeline(db, call, segments, full_text)
                logger.info("local_voice_test_finalized", call_id=call_id, segments=len(segments))
            except Exception as e:
                logger.error("post_call_test_finalize_failed", error=str(e))
        try:
            await websocket.close(code=1000)
        except Exception:
            # The peer may already have disconnected without a close handshake.
            pass


@router.get("/test-page", response_class=HTMLResponse)
async def voice_test_html_page():
    """Interactive browser-based voice testing UI with verified runtime providers and live metrics."""
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>VoxAI Voice Agent - Real-Time Pipeline Test</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; background: #0b1120; color: #f8fafc; margin: 0; padding: 24px 16px; display: flex; flex-direction: column; align-items: center; min-height: 100vh; }
    .container { max-width: 860px; width: 100%; }
    .header-card { background: #1e293b; border: 1px solid #334155; border-radius: 14px; padding: 24px; margin-bottom: 18px; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.3); }
    h1 { margin: 0 0 8px 0; color: #38bdf8; font-size: 22px; font-weight: 700; display: flex; align-items: center; gap: 10px; }
    .pipeline-badge-row { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0 16px 0; }
    .tech-tag { background: #0f172a; border: 1px solid #475569; color: #94a3b8; padding: 4px 10px; border-radius: 6px; font-size: 11px; font-weight: 600; font-family: monospace; }
    .tech-tag.active { border-color: #0284c7; color: #38bdf8; }
    .tech-tag.tts { border-color: #8b5cf6; color: #c084fc; }
    .pipeline-flow { color: #94a3b8; font-size: 13px; line-height: 1.5; margin-bottom: 16px; padding: 10px 14px; background: #0f172a; border-radius: 8px; border-left: 3px solid #38bdf8; }
    .status-row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
    .status-badge { display: inline-flex; align-items: center; gap: 6px; padding: 5px 12px; border-radius: 9999px; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; }
    .badge-idle { background: #334155; color: #94a3b8; }
    .badge-connecting { background: #854d0e; color: #fde047; }
    .badge-live { background: #065f46; color: #34d399; }
    .badge-speaking { background: #1e3a8a; color: #60a5fa; }
    .badge-error { background: #7f1d1d; color: #f87171; }
    .controls { display: flex; flex-wrap: wrap; gap: 10px; margin: 0 0 14px 0; }
    button { padding: 10px 20px; border-radius: 8px; border: none; font-weight: 600; cursor: pointer; font-size: 14px; transition: all 0.2s; display: inline-flex; align-items: center; gap: 6px; }
    button:disabled { opacity: 0.45; cursor: not-allowed; }
    .btn-start { background: #059669; color: white; }
    .btn-start:hover:not(:disabled) { background: #047857; }
    .btn-stop { background: #dc2626; color: white; }
    .btn-stop:hover:not(:disabled) { background: #b91c1c; }
    .btn-interrupt { background: #d97706; color: white; }
    .btn-interrupt:hover:not(:disabled) { background: #b45309; }
    .btn-send { background: #2563eb; color: white; }
    .btn-send:hover:not(:disabled) { background: #1d4ed8; }
    .text-input-row { display: flex; gap: 8px; margin-top: 10px; }
    .text-input { flex: 1; background: #090d16; border: 1px solid #334155; border-radius: 8px; padding: 10px 14px; color: #f8fafc; font-size: 13px; outline: none; }
    .text-input:focus { border-color: #38bdf8; }
    .panel-card { background: #1e293b; border: 1px solid #334155; border-radius: 14px; padding: 20px; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.3); }
    .panel-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
    .panel-header h3 { margin: 0; font-size: 16px; color: #e2e8f0; }
    .btn-clear { background: transparent; border: 1px solid #475569; color: #94a3b8; padding: 4px 10px; font-size: 11px; border-radius: 6px; cursor: pointer; }
    .btn-clear:hover { background: #334155; color: #f8fafc; }
    .log-box { background: #090d16; border: 1px solid #1e293b; border-radius: 10px; padding: 16px; height: 360px; overflow-y: auto; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 13px; line-height: 1.65; }
    .log-line { margin-bottom: 8px; word-break: break-word; }
    .ai-msg { color: #38bdf8; }
    .user-msg { color: #34d399; }
    .metric-msg { color: #fbbf24; background: rgba(251,191,36,0.08); padding: 3px 8px; border-radius: 4px; display: inline-block; font-size: 12px; }
    .event-msg { color: #94a3b8; }
    .err-msg { color: #f87171; background: rgba(248,113,113,0.1); padding: 2px 6px; border-radius: 4px; }
    .sys-msg { color: #c084fc; font-weight: 600; }
    .pulse-dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; display: inline-block; animation: pulse 1.5s infinite; }
    @keyframes pulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.4; transform: scale(0.85); } }
  </style>
</head>
<body>
  <div class="container">
    <div class="header-card">
      <h1>
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color:#38bdf8"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"></path><path d="M19 10v2a7 7 0 0 1-14 0v-2"></path><line x1="12" x2="12" y1="19" y2="22"></line></svg>
        VoxAI Real-Time Voice Intelligence
      </h1>

      <div class="pipeline-flow">
        <strong>Pipeline:</strong> Microphone &rarr; VAD &rarr; Groq Whisper STT &rarr; Groq LLM (Qwen) &rarr; Kokoro ONNX TTS &rarr; Speaker
      </div>

      <div class="pipeline-badge-row">
        <span class="tech-tag active">VAD: runtime reported at session start</span>
        <span class="tech-tag active">STT: Groq Whisper (Turbo)</span>
        <span class="tech-tag active">LLM: Groq Qwen 3.8-27b</span>
        <span class="tech-tag tts">TTS: Kokoro ONNX (language detected per utterance)</span>
        <span class="tech-tag">Audio: 16kHz PCM16</span>
      </div>

      <div class="status-row">
        <div><strong>Status:</strong> <span id="status" class="status-badge badge-idle"><span class="pulse-dot"></span> IDLE</span></div>
        <div id="callIdDisplay" style="font-size: 11px; color: #64748b; font-family: monospace;">Not Connected</div>
      </div>

      <div class="controls">
        <button id="btnStart" class="btn-start" onclick="startCall()">
          <svg width="16" height="16" fill="currentColor" viewBox="0 0 24 24"><path d="M6.6 10.8c1.4 2.8 3.8 5.1 6.6 6.6l2.2-2.2c.3-.3.7-.4 1-.2 1.1.4 2.3.6 3.6.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1-9.4 0-17-7.6-17-17 0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.3.2 2.5.6 3.6.1.3 0 .7-.2 1l-2.3 2.2z"/></svg>
          Start Call
        </button>
        <button id="btnInterrupt" class="btn-interrupt" onclick="triggerInterrupt()" disabled>
          <svg width="16" height="16" fill="currentColor" viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>
          Barge-in / Interrupt
        </button>
        <button id="btnStop" class="btn-stop" onclick="endCall()" disabled>
          <svg width="16" height="16" fill="currentColor" viewBox="0 0 24 24"><path d="M12 9c-1.6 0-3.15.25-4.6.72v3.1c0 .39-.23.74-.56.9-.98.49-1.87 1.12-2.66 1.85-.18.18-.43.28-.7.28-.28 0-.53-.11-.71-.29L.29 13.08c-.18-.17-.29-.42-.29-.7 0-.28.11-.53.29-.71C3.34 8.78 7.46 7 12 7s8.66 1.78 11.71 4.67c.18.18.29.43.29.71 0 .28-.11.53-.29.71l-2.48 2.48c-.18.18-.43.29-.71.29-.27 0-.52-.11-.7-.28-.79-.74-1.69-1.36-2.67-1.85-.33-.16-.56-.5-.56-.9v-3.1C15.15 9.25 13.6 9 12 9z"/></svg>
          End Call
        </button>
      </div>

      <label for="languageSelect" style="display:block;margin:14px 0 6px;color:#cbd5e1;font-size:13px;font-weight:600">Conversation language</label>
      <select id="languageSelect" class="text-input" aria-label="Conversation language">
        <option value="auto">Auto — Hindi / English / Hinglish</option>
      </select>

      <div class="text-input-row">
        <input id="txtInput" class="text-input" type="text" placeholder="Type a test message in Hindi, English, or Hinglish..." onkeydown="if(event.key==='Enter') sendText()" />
        <button id="btnSend" class="btn-send" onclick="sendText()" disabled>Send Text</button>
      </div>
    </div>

    <div class="panel-card">
      <div class="panel-header">
        <h3>Live Conversation & Latency Metrics</h3>
        <button class="btn-clear" onclick="clearLogs()">Clear Logs</button>
      </div>
      <div id="logs" class="log-box"></div>
    </div>
  </div>

  <script>
    let ws = null;
    let audioContext = null;
    let micStream = null;
    let processor = null;
    let nextPlayTime = 0;
    let activeSourceNodes = [];
    let playbackGeneration = 0;
    let playbackStartedAt = null;
    let firstAudioReceivedAt = null;
    let playbackEndedAt = null;
    let playbackNotified = false;
    let responseAudioComplete = false;
    let responseInterrupted = false;
    let resamplePosition = 0;
    let previousInputSample = 0;

    function setStatus(badgeClass, text) {
      const badge = document.getElementById('status');
      badge.className = 'status-badge ' + badgeClass;
      badge.innerHTML = '<span class="pulse-dot"></span> ' + text;
    }

    function log(html) {
      const box = document.getElementById('logs');
      box.innerHTML += '<div class="log-line">' + html + '</div>';
      box.scrollTop = box.scrollHeight;
    }

    function clearLogs() {
      document.getElementById('logs').innerHTML = '';
      initLogs();
    }

    function initLogs() {
      log('<span class="sys-msg">[System Ready]</span> VoxAI Voice Agent test harness initialized.');
      log('<span class="event-msg">&bull; Pipeline: <strong>Microphone &rarr; VAD (runtime reported at session start) &rarr; STT &rarr; LLM &rarr; TTS &rarr; Speaker</strong></span>');
      log('<span class="event-msg">&bull; TTS: Hindi hf_alpha / hi; English af_heart / en-us. Output: 16kHz mono PCM16.</span>');
      log('<span class="event-msg">&bull; Click <strong>[Start Call]</strong> to start the session, or type below and click <strong>[Send Text]</strong>.</span>');
    }

    window.addEventListener('DOMContentLoaded', initLogs);

    function playPcmChunk(arrayBuffer) {
      if (!audioContext) return;
      const pcm16 = new Int16Array(arrayBuffer);
      if (pcm16.length === 0) return;

      const buffer = audioContext.createBuffer(1, pcm16.length, 16000);
      const channel = buffer.getChannelData(0);
      for (let i = 0; i < pcm16.length; i++) {
        channel[i] = pcm16[i] / 32768.0;
      }

      const node = audioContext.createBufferSource();
      node.buffer = buffer;
      node.connect(audioContext.destination);

      const now = audioContext.currentTime;
      if (nextPlayTime < now) {
        nextPlayTime = now;
      }
      const scheduledStart = nextPlayTime;
      node.start(scheduledStart);
      nextPlayTime += buffer.duration;

      activeSourceNodes.push(node);
      node.playbackGeneration = playbackGeneration;
      const reportPlaybackStart = () => {
        if (node.playbackGeneration !== playbackGeneration || !activeSourceNodes.includes(node)) return;
        if (playbackStartedAt !== null) return;
        if (audioContext.currentTime >= scheduledStart) {
          playbackStartedAt = performance.now();
          if (!playbackNotified && ws && ws.readyState === WebSocket.OPEN) {
            playbackNotified = true;
            ws.send(JSON.stringify({ type: 'playback_started', browser_time_ms: playbackStartedAt,
              first_audio_received_ms: firstAudioReceivedAt }));
          }
          return;
        }
        requestAnimationFrame(reportPlaybackStart);
      };
      requestAnimationFrame(reportPlaybackStart);
      node.onended = () => {
        const idx = activeSourceNodes.indexOf(node);
        if (idx !== -1) activeSourceNodes.splice(idx, 1);
        if (node.playbackGeneration !== playbackGeneration) return;
        finishPlaybackIfDrained();
      };
    }

    function finishPlaybackIfDrained() {
      if (responseInterrupted || !responseAudioComplete || activeSourceNodes.length !== 0 || !ws || ws.readyState !== WebSocket.OPEN) return;
      playbackEndedAt = performance.now();
      const playbackDurationMs = playbackStartedAt === null ? null : playbackEndedAt - playbackStartedAt;
      if (playbackStartedAt !== null) {
        log('<span class="metric-msg">Browser clock: first audio received at ' + Math.round(firstAudioReceivedAt || 0) + 'ms; playback began at ' + Math.round(playbackStartedAt) + 'ms and completed at ' + Math.round(playbackEndedAt) + 'ms; playback ' + Math.round(playbackDurationMs) + 'ms.</span>');
        playbackStartedAt = null;
      }
      setStatus('badge-live', 'LIVE - LISTENING');
      ws.send(JSON.stringify({ type: 'playback_completed', browser_time_ms: playbackEndedAt,
        playback_duration_ms: playbackDurationMs }));
    }

    function stopAllAudio() {
      activeSourceNodes.forEach(node => {
        try { node.stop(); } catch(e) {}
      });
      activeSourceNodes = [];
      if (audioContext) nextPlayTime = audioContext.currentTime;
    }

    function resetPlayback(interrupted = false) {
      playbackGeneration += 1;
      stopAllAudio();
      playbackStartedAt = null;
      firstAudioReceivedAt = null;
      playbackEndedAt = null;
      playbackNotified = false;
      responseAudioComplete = false;
      responseInterrupted = interrupted;
    }

    async function startCall() {
      document.getElementById('btnStart').disabled = true;
      document.getElementById('btnStop').disabled = false;
      document.getElementById('btnInterrupt').disabled = false;
      document.getElementById('btnSend').disabled = false;
      setStatus('badge-connecting', 'CONNECTING...');

      try {
        if (!audioContext) {
          // Keep the browser's native device rate (typically 48kHz) so its
          // acoustic echo canceller operates on the original capture/render
          // clock. Downsample only at the WebSocket boundary below.
          audioContext = new (window.AudioContext || window.webkitAudioContext)();
        }
        if (audioContext.state === 'suspended') {
          await audioContext.resume();
        }
      } catch (e) {
        log('<span class="err-msg">[AudioContext Error] ' + e.message + '</span>');
      }

      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const selectedLanguage = document.getElementById('languageSelect').value;
      const wsUrl = proto + '//' + window.location.host + '/api/v1/voice/ws/test?language=' + encodeURIComponent(selectedLanguage);
      log('<span class="event-msg">[Network] Connecting to WebSocket: ' + wsUrl + '...</span>');

      try {
        ws = new WebSocket(wsUrl);
        ws.binaryType = 'arraybuffer';
      } catch (err) {
        log('<span class="err-msg">[WebSocket Init Failed] ' + err.message + '</span>');
        setStatus('badge-error', 'ERROR');
        endCall();
        return;
      }
      const callSocket = ws;

      ws.onopen = async () => {
        log('<span class="sys-msg">[WebSocket Connected]</span> Live bidirectional media channel open.');
        try {
          // Keep capture at the browser/device's native rate so browser AEC can
          // operate on the hardware path. We downsample exactly once below.
          const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
          if (ws !== callSocket) { stream.getTracks().forEach(t => t.stop()); return; }
          micStream = stream;
          const source = audioContext.createMediaStreamSource(micStream);
          processor = audioContext.createScriptProcessor(2048, 1, 1);
          source.connect(processor);
          processor.connect(audioContext.destination);
          resamplePosition = 0;
          previousInputSample = 0;

          processor.onaudioprocess = (e) => {
            // ScriptProcessorNode otherwise passes its input to the output,
            // creating a local mic-monitor loop that can feed Riya's playback
            // back into VAD as a fresh caller turn.
            const monitorOutput = e.outputBuffer.getChannelData(0);
            monitorOutput.fill(0);
            if (ws === callSocket && callSocket.readyState === WebSocket.OPEN) {
              const input = e.inputBuffer.getChannelData(0);
              if (selectedLanguage !== 'en') {
                const step = audioContext.sampleRate / 16000;
                const output = [];
                while (resamplePosition + 1 < input.length) {
                  const left = Math.floor(resamplePosition);
                  const fraction = resamplePosition - left;
                  const a = left < 0 ? previousInputSample : input[left];
                  const b = input[left + 1];
                  const sample = a * (1 - fraction) + b * fraction;
                  output.push(Math.max(-32768, Math.min(32767, Math.round(sample * 32768))));
                  resamplePosition += step;
                }
                resamplePosition -= input.length;
                previousInputSample = input[input.length - 1] || 0;
                if (output.length) callSocket.send(new Int16Array(output).buffer);
              } else {
                const targetLength = Math.floor(input.length * 16000 / audioContext.sampleRate);
                if (targetLength < 1) return;
                const pcm16 = new Int16Array(targetLength);
                for (let i = 0; i < targetLength; i++) {
                  const sourceIndex = i * audioContext.sampleRate / 16000;
                  const left = Math.floor(sourceIndex);
                  const fraction = sourceIndex - left;
                  const sample = input[left] * (1 - fraction) + (input[Math.min(left + 1, input.length - 1)] || 0) * fraction;
                  pcm16[i] = Math.max(-32768, Math.min(32767, Math.round(sample * 32767)));
                }
                callSocket.send(pcm16.buffer);
              }
            }
          };
          const audioTrack = stream.getAudioTracks()[0];
          const trackSettings = audioTrack.getSettings ? audioTrack.getSettings() : {};
          const aecStatus = trackSettings.echoCancellation === true ? 'active' : (trackSettings.echoCancellation === false ? 'unavailable' : 'unknown');
          const nsStatus = trackSettings.noiseSuppression === true ? 'active' : (trackSettings.noiseSuppression === false ? 'unavailable' : 'unknown');
          const agcStatus = trackSettings.autoGainControl === true ? 'active' : (trackSettings.autoGainControl === false ? 'unavailable' : 'unknown');
          log('<span class="sys-msg">[Microphone Ready]</span> AudioContext ' + audioContext.sampleRate + 'Hz; track ' + (trackSettings.sampleRate || 'unknown') + 'Hz / ' + (trackSettings.channelCount || 'unknown') + ' channel(s), continuously converted to 16kHz mono PCM16. Echo cancellation: ' + aecStatus + '; noise suppression: ' + nsStatus + '; auto gain: ' + agcStatus + '. Mic processor output is silent; acoustic speaker echo may still reach the microphone.');
        } catch (err) {
          log('<span class="err-msg">[Microphone Notice] ' + err.message + '. (You can still talk via the Text Input bar below).</span>');
        }
      };

      ws.onmessage = async (event) => {
        if (ws !== callSocket) return;
        if (event.data instanceof ArrayBuffer) {
          // Binary audio chunk from Kokoro ONNX TTS
          setStatus('badge-speaking', 'AI SPEAKING');
          if (firstAudioReceivedAt === null) firstAudioReceivedAt = performance.now();
          playPcmChunk(event.data);
        } else {
          try {
            const data = JSON.parse(event.data);
            if (data.event === 'SESSION_CONNECTED') {
              setStatus('badge-live', 'LIVE - LISTENING');
              document.getElementById('callIdDisplay').innerText = 'Call: ' + data.call_id.substring(0, 8);
              const p = data.providers || {};
              log('<span class="event-msg">&bull; Language mode: <strong>Auto Hindi / English / Hinglish</strong>; audio: <strong>' + data.sample_rate + 'Hz ' + data.format + '</strong>.</span>');
              log('<span class="sys-msg">[Session Active]</span> Call ID: <code>' + data.call_id + '</code>');
              log('<span class="event-msg">&bull; Runtime-reported providers: VAD: <strong>' + (p.vad || 'unreported') + '</strong> | STT: <strong>' + (p.stt || 'unreported') + '</strong> | LLM: <strong>' + (p.llm || 'unreported') + '</strong> | TTS: <strong>' + (p.tts || 'unreported') + '</strong></span>');
            } else if (data.event === 'SPEECH_STARTED') {
              const p = data.payload || {};
              log('<span class="event-msg">&bull; [VAD] Speech start #' + (p.speech_starts || '?') + '; mic frames ' + (p.mic_frames || '?') + '; score ' + (p.speech_probability ?? 'n/a') + '.</span>');
            } else if (data.event === 'STT_SUBMITTED') {
              log('<span class="event-msg">[STT] Submission #' + data.payload.stt_submissions + ': ' + data.payload.audio_duration_ms + 'ms / ' + data.payload.audio_bytes + ' bytes.</span>');
            } else if (data.event === 'VAD_PROVIDER_ACTIVE') {
              log('<span class="sys-msg">[VAD Inference Verified]</span> ' + data.payload.provider);
            } else if (data.event === 'SPEECH_ENDED') {
              const sttMs = data.payload.stt_latency_ms ? ' (' + Math.round(data.payload.stt_latency_ms) + 'ms)' : '';
              const lang = data.payload.response_language ? ' · detected: ' + data.payload.response_language : '';
              log('<span class="user-msg"><strong>Customer:</strong> ' + (data.payload.text || '') + '<small style="color:#64748b">' + sttMs + lang + '</small></span>');
            } else if (data.event === 'TRANSCRIPT_REJECTED') {
              log('<span class="event-msg">[STT] Transcript rejected as empty or invalid; asking the customer to repeat.</span>');
            } else if (data.event === 'UTTERANCE_DROPPED') {
              log('<span class="event-msg">[VAD] Duplicate utterance dropped (count: ' + data.payload.count + ').</span>');
            } else if (data.event === 'AI_RESPONSE_STARTED') {
              resetPlayback(false);
              responseAudioComplete = false;
              setStatus('badge-speaking', 'AI SPEAKING');
              log('<span class="ai-msg"><strong>AI (Riya):</strong> ' + data.payload.text + '</span>');
            } else if (data.event === 'AI_RESPONSE_COMPLETED') {
              responseAudioComplete = true;
              finishPlaybackIfDrained();
              if (data.payload.metrics) {
                const m = data.payload.metrics;
                const fmt = (v) => v == null ? 'n/a' : Math.round(v) + 'ms';
                log('<span class="metric-msg">&rarr; Latency: STT: ' + fmt(m.stt_ms) + ' | LLM first token: ' + fmt(m.llm_first_token_ms || m.llm_ttft_ms) + ' | First phrase ready: ' + fmt(m.first_text_chunk_ms) + ' | TTS first audio: ' + fmt(m.tts_ttfa_ms) + ' | First audio sent: ' + fmt(m.first_audio_sent_ms) + ' | TTS generation total: ' + fmt(m.tts_total_ms) + ' | Audio send: ' + fmt(m.audio_send_ms) + ' | End-of-speech to first generated audio: ' + fmt(m.end_to_first_audio_ms) + ' | Audio duration: ' + fmt(m.audio_duration_ms) + ' | Server turn: ' + fmt(m.total_turn_ms) + '</span>');
              }
            } else if (data.event === 'AI_TEXT_DELTA') {
              log('<span class="ai-msg"><strong>AI (Riya):</strong> ' + data.payload.text + '</span>');
            } else if (data.event === 'TOOL_CALLED') {
              log('<span class="event-msg">&bull; [Tool Action] Executing ' + data.payload.name + '</span>');
            } else if (data.event === 'USER_INTERRUPTED') {
              resetPlayback(true);
              setStatus('badge-live', 'LIVE - LISTENING');
              log('<span class="event-msg" style="color:#fb923c;">[Barge-in] Customer interrupted AI &mdash; speech playback cleared</span>');
            } else if (data.event === 'VOICE_ERROR') {
              log('<span class="err-msg">[Pipeline Error] ' + (data.payload.error || 'Unknown error') + '</span>');
            } else if (data.event === 'CALL_CONTROL' && data.action === 'end') {
              log('<span class="sys-msg">[Call Control] Call terminated by server.</span>');
              endCall();
            }
          } catch(e) {
            log('<span class="err-msg">[JSON Parse Error] ' + e.message + '</span>');
          }
        }
      };

      ws.onerror = (err) => {
        log('<span class="err-msg">[WebSocket Error] Connection issue detected. Check if backend is running on port 8001.</span>');
        setStatus('badge-error', 'ERROR');
      };

      ws.onclose = (ev) => {
        if (ws !== callSocket) return;
        log('<span class="event-msg">[System] Call disconnected (code: ' + ev.code + ').</span>');
        endCall();
      };
    }

    function sendText() {
      const input = document.getElementById('txtInput');
      const text = input.value.trim();
      if (!text) return;
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        log('<span class="err-msg">[Error] Cannot send text: Call not started. Click [Start Call] first.</span>');
        return;
      }
      ws.send(JSON.stringify({ type: 'text_input', text: text }));
      input.value = '';
    }

    function triggerInterrupt() {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'interrupt' }));
        resetPlayback(true);
      }
    }

    function endCall() {
      resetPlayback(true);
      const closingSocket = ws;
      if (closingSocket) {
        if (closingSocket.readyState === WebSocket.OPEN) {
          try { closingSocket.send(JSON.stringify({ type: 'hangup' })); } catch(e) {}
          // Let the server finish session cleanup and reply with a normal close frame.
          setTimeout(() => {
            if (closingSocket.readyState < WebSocket.CLOSING) {
              try { closingSocket.close(1000, 'Call ended'); } catch(e) {}
            }
          }, 10000);
        }
        if (closingSocket.readyState === WebSocket.CONNECTING) {
          try { closingSocket.close(1000, 'Call ended'); } catch(e) {}
        }
        ws = null;
      }
      if (processor) { processor.disconnect(); processor = null; }
      if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
      if (audioContext) { try { audioContext.close(); } catch(e) {} audioContext = null; }
      document.getElementById('btnStart').disabled = false;
      document.getElementById('btnStop').disabled = true;
      document.getElementById('btnInterrupt').disabled = true;
      document.getElementById('btnSend').disabled = true;
      document.getElementById('callIdDisplay').innerText = 'Not Connected';
      setStatus('badge-idle', 'IDLE');
    }
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)
