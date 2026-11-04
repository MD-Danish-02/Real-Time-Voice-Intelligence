"""
Unified Voice Call Session.
Drives the real-time AI conversation path:
Audio In -> Silero VAD -> faster-whisper STT -> Groq LLM -> Chatterbox TTS -> Audio Out.

Shared identically by:
1. Asterisk ARI phone calls (via RTP media bridge)
2. Local Voice Test Mode (via WebSocket / local audio)
"""
import asyncio
import hashlib
import time
from typing import Awaitable, Callable, Optional
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.events.bus import publish_event
from app.models.lead import Lead, LeadCallState
from app.models.tenant import Tenant, TenantBusinessRules
from app.providers.voice.llm.base import get_llm_provider
from app.providers.voice.stt.base import get_stt_provider
from app.providers.voice.tts.base import get_tts_provider
from app.voice_pipeline.agent_tools import AgentToolExecutor
from app.voice_pipeline.context_engine import build_call_context
from app.voice_pipeline.orchestrator import VoiceCallOrchestrator
from app.voice_pipeline.vad import SileroVADDetector

logger = get_logger("voice.session")


class LiveVoiceSession:
    """
    Manages an active real-time conversational voice turn session.
    Provides streaming audio I/O, barge-in, tool calls, and event publishing.
    """

    def __init__(
        self,
        call_id: str,
        tenant_id: uuid.UUID,
        orchestrator: VoiceCallOrchestrator,
        vad: SileroVADDetector,
        on_audio_out: Callable[[bytes], None],
        on_control: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        self.call_id = call_id
        self.tenant_id = tenant_id
        self.orchestrator = orchestrator
        self.vad = vad
        self.on_audio_out = on_audio_out
        self.on_control = on_control

        self._is_ai_speaking = False
        self._browser_playback_active = False
        self._transport_playback_is_active: Optional[Callable[[], bool]] = None
        self._interrupt_transport_playback: Optional[Callable[[], None]] = None
        self._active = True
        self._closed = False
        self._turn_lock = asyncio.Lock()
        self._turn_tasks: set[asyncio.Task] = set()
        self._last_utterance_digest: Optional[str] = None
        self._last_utterance_at = 0.0
        self.rejected_utterance_count = 0
        self._speech_started_at: Optional[float] = None
        self._vad_active_status_sent = False
        self._diagnostics = {
            "mic_frames": 0,
            "speech_starts": 0,
            "speech_ends": 0,
            "interruptions": 0,
            "cancelled_turns": 0,
            "stt_submissions": 0,
        }

    def feed_input_audio(self, pcm_bytes: bytes) -> None:
        """
        Receives incoming 16kHz PCM16 audio (from RTP or WebSocket).
        Processes through Silero VAD. Handles barge-in and turn dispatch.
        """
        if not self._active:
            return
        self._diagnostics["mic_frames"] += 1
        # A PCM16 stream may split a 16-bit sample across WebSocket frames. The
        # VAD retains its byte remainder, so don't assume each message is even.

        events = self.vad.process_chunk(pcm_bytes)
        vad_status = getattr(self.vad, "runtime_name", "")
        if "inference verified" in vad_status.lower() and not self._vad_active_status_sent:
            self._vad_active_status_sent = True
            asyncio.create_task(self.orchestrator._emit("VAD_PROVIDER_ACTIVE", {"provider": vad_status}))
        for ev in events:
            if ev.event_type == "SPEECH_STARTED":
                self._speech_started_at = ev.speech_started_at
                self._diagnostics["speech_starts"] += 1
                asyncio.create_task(self.orchestrator._emit("SPEECH_STARTED", {
                    **self._diagnostics,
                    "speech_probability": round(getattr(ev, "speech_probability", 0.0), 3),
                }))
                # Customer interrupted AI playback (Barge-in)
                # _is_ai_speaking covers the full turn, including STT. Only
                # treat VAD onset as barge-in while audio is being generated or
                # the browser has confirmed audible playback.
                # TTS may spend several seconds generating its first chunk. At
                # that point nothing is audible to interrupt; treat the onset
                # as a buffered caller turn and wait until playback is real.
                output_active = self._browser_playback_active
                if self._transport_playback_is_active is not None:
                    try:
                        output_active = output_active or self._transport_playback_is_active()
                    except Exception as exc:
                        logger.warning("transport_playback_state_check_failed", call_id=self.call_id, error=str(exc))
                if self._is_ai_speaking and output_active:
                    if not self.orchestrator._interrupted:
                        logger.info("barge_in_detected", call_id=self.call_id)
                        if self._interrupt_transport_playback is not None:
                            try:
                                # The RTP transport callback also invalidates the
                                # orchestrator generation and drains queued audio.
                                self._interrupt_transport_playback()
                            except Exception as exc:
                                logger.warning("transport_playback_interrupt_failed", call_id=self.call_id, error=str(exc))
                                self.orchestrator.interrupt()
                        else:
                            self.orchestrator.interrupt()
                        self._diagnostics["interruptions"] += 1
                        self._diagnostics["cancelled_turns"] += 1
                        self._browser_playback_active = False
                        asyncio.create_task(self.orchestrator.notify_interruption({
                            "reason": "vad_during_output_playback",
                            "counters": dict(self._diagnostics),
                        }))
                    self._is_ai_speaking = False

            elif ev.event_type == "SPEECH_ENDED" and ev.speech_buffer:
                self._diagnostics["speech_ends"] += 1
                # Customer completed speaking; dispatch to STT -> LLM -> TTS pipeline
                digest = hashlib.sha256(ev.speech_buffer).hexdigest()
                now = time.monotonic()
                if digest == self._last_utterance_digest and now - self._last_utterance_at < 2.0:
                    self.rejected_utterance_count += 1
                    logger.info("duplicate_or_busy_utterance_rejected", call_id=self.call_id)
                    asyncio.create_task(self.orchestrator._emit("UTTERANCE_DROPPED", {
                        "reason": "duplicate_audio_buffer", "count": self.rejected_utterance_count,
                    }))
                    continue
                self._last_utterance_digest = digest
                self._last_utterance_at = now
                task = asyncio.create_task(self._process_speech_turn(
                    ev.speech_buffer, self._speech_started_at, ev.speech_ended_at
                ))
                self._turn_tasks.add(task)
                task.add_done_callback(self._turn_tasks.discard)

    def set_browser_playback_active(self, active: bool) -> None:
        """Track the browser's actual output state separately from STT/LLM work."""
        self._browser_playback_active = active

    def set_transport_playback_controls(
        self,
        is_active: Callable[[], bool],
        interrupt: Callable[[], None],
    ) -> None:
        """Connect non-browser transports to their real playback state and stop control."""
        self._transport_playback_is_active = is_active
        self._interrupt_transport_playback = interrupt

    async def _process_speech_turn(
        self, speech_buffer: bytes, speech_started_at: Optional[float], speech_ended_at: Optional[float]
    ) -> None:
        async with self._turn_lock:
            if not self._active:
                return
            # Count and report the request when it actually reaches the STT
            # boundary, after any prior turn has released the lock.
            self._diagnostics["stt_submissions"] += 1
            await self.orchestrator._emit("STT_SUBMITTED", {
                **self._diagnostics,
                "audio_bytes": len(speech_buffer),
                "audio_duration_ms": round(len(speech_buffer) / 32.0, 1),
                "queue_wait_ms": round(
                    max(0.0, (time.perf_counter() - speech_ended_at) * 1000.0)
                    if speech_ended_at is not None else 0.0, 1
                ),
            })
            self._is_ai_speaking = True
            try:
                turn = await self.orchestrator.handle_user_audio_chunk(
                    speech_buffer, speech_started_at=speech_started_at, speech_ended_at=speech_ended_at
                )
                if (turn.should_end_call or turn.should_transfer) and self.on_control:
                    await self.on_control("transfer" if turn.should_transfer else "end")
            except Exception as e:
                logger.error("speech_turn_processing_error", call_id=self.call_id, error=str(e))
            finally:
                self._is_ai_speaking = False

    async def feed_input_text(self, text: str) -> None:
        """Processes typed text through LLM and TTS pipeline."""
        async with self._turn_lock:
            if not self._active:
                return
            self._is_ai_speaking = True
            try:
                turn = await self.orchestrator.handle_user_text(text)
                if (turn.should_end_call or turn.should_transfer) and self.on_control:
                    await self.on_control("transfer" if turn.should_transfer else "end")
            except Exception as e:
                logger.error("text_turn_processing_error", call_id=self.call_id, error=str(e))
                await self.orchestrator._emit("VOICE_ERROR", {"error": str(e)})
            finally:
                self._is_ai_speaking = False

    def schedule_input_text(self, text: str) -> None:
        task = asyncio.create_task(self.feed_input_text(text))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    async def start(self) -> None:
        """Plays initial conversational greeting."""
        async with self._turn_lock:
            if not self._active:
                return
            self._is_ai_speaking = True
            try:
                await self.orchestrator.opening_greeting()
            except Exception as e:
                logger.error("opening_greeting_error", call_id=self.call_id, error=str(e))
                await self.orchestrator._emit("VOICE_ERROR", {"error": f"Initial greeting error: {str(e)}"})
            finally:
                self._is_ai_speaking = False

    def schedule_start(self) -> asyncio.Task:
        """Schedule and track the greeting so hangup cancels pending TTS work."""
        task = asyncio.create_task(self.start(), name=f"voice-greeting-{self.call_id}")
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)
        return task

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._active = False
        self._browser_playback_active = False
        self.vad.reset()
        for task in tuple(self._turn_tasks):
            if not task.done():
                task.cancel()

    async def aclose(self) -> None:
        """Idempotently stop audio tasks before disconnect persistence begins."""
        self.close()
        tasks = tuple(self._turn_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def create_live_voice_session(
    db: Optional[AsyncSession] = None,
    *,
    call_id: str,
    tenant_id: uuid.UUID,
    lead_id: Optional[uuid.UUID] = None,
    on_audio_out: Callable[[bytes], None],
    on_control: Optional[Callable[[str], Awaitable[None]]] = None,
    language: str = "auto",
) -> LiveVoiceSession:
    """Builds and wires a complete voice session with tenant context, VAD, STT, LLM, and TTS."""
    if db is not None:
        tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
        rules = (
            await db.execute(select(TenantBusinessRules).where(TenantBusinessRules.tenant_id == tenant_id))
        ).scalar_one_or_none()
        if rules is None:
            rules = TenantBusinessRules(tenant_id=tenant_id)
            db.add(rules)
            await db.flush()

        lead = None
        call_state = None
        if lead_id:
            lead = (await db.execute(select(Lead).where(Lead.id == lead_id))).scalar_one_or_none()
            if lead:
                call_state = (
                    await db.execute(select(LeadCallState).where(LeadCallState.lead_id == lead.id))
                ).scalar_one_or_none()

        if lead is None:
            lead = Lead(
                tenant_id=tenant_id,
                name="Valued Customer",
                phone="+910000000000",
                crm_provider="voice_inbound",
                provider_lead_id=f"inbound_{uuid.uuid4().hex[:8]}",
            )
            db.add(lead)
            await db.flush()
            call_state = LeadCallState(tenant_id=tenant_id, lead_id=lead.id, calling_status="in_call")
            db.add(call_state)
            await db.flush()

        if call_state is None:
            call_state = LeadCallState(tenant_id=tenant_id, lead_id=lead.id, calling_status="in_call")
            db.add(call_state)
            await db.flush()

        context = await build_call_context(db, tenant, rules, lead, call_state)
        context_dict = context.to_dict()
        tool_executor = AgentToolExecutor(db, tenant_id, lead, call_state, rules, uuid.UUID(call_id))
    else:
        lead = Lead(
            tenant_id=tenant_id,
            name="Valued Customer",
            phone="+919876543210",
            crm_provider="voice_inbound",
            provider_lead_id=f"inbound_{uuid.uuid4().hex[:8]}",
        )
        rules = TenantBusinessRules(tenant_id=tenant_id)
        call_state = LeadCallState(tenant_id=tenant_id, lead_id=lead.id, calling_status="in_call")
        context_dict = {
            "company": {
                "name": "VoxAI",
                "industry": "AI Voice Calling",
                "value_proposition": "Automated voice agent for inbound and outbound calls",
            },
            "lead": {"name": "Valued Customer", "phone": "+919876543210"},
            "rules": {"language_primary": "hi", "language_secondary": "en"},
            "knowledge": [],
            "previous_calls": [],
        }
        tool_executor = AgentToolExecutor(None, tenant_id, lead, call_state, rules, uuid.UUID(call_id))

    language = language.lower()
    if language not in ("auto", "hi", "en"):
        raise ValueError("Unsupported session language. Use auto, hi, or en.")
    # Auto mode lets Whisper detect each utterance independently. Legacy clients
    # can still pin hi/en via the existing query parameter.
    stt = get_stt_provider(language="auto" if language == "auto" else language)
    llm = get_llm_provider()
    tts = get_tts_provider()
    if hasattr(tts, "call_id"):
        tts.call_id = call_id
    if hasattr(tts, "_resolve_lang_and_voice"):
        tts._resolve_lang_and_voice("hi" if language == "auto" else language, None)
    vad = SileroVADDetector()

    async def _emit_event(ev_type: str, payload: dict) -> None:
        payload["call_id"] = call_id
        await publish_event(tenant_id, ev_type, payload)

    async def _on_audio_chunk(chunk: bytes) -> None:
        if on_audio_out and chunk:
            if asyncio.iscoroutinefunction(on_audio_out):
                await on_audio_out(chunk)
            else:
                on_audio_out(chunk)

    orchestrator = VoiceCallOrchestrator(
        stt=stt,
        llm=llm,
        tts=tts,
        tool_executor=tool_executor,
        context=context_dict,
        language=language,
        on_event=_emit_event,
        on_audio_chunk=_on_audio_chunk,
    )

    return LiveVoiceSession(
        call_id=call_id,
        tenant_id=tenant_id,
        orchestrator=orchestrator,
        vad=vad,
        on_audio_out=on_audio_out,
        on_control=on_control,
    )
