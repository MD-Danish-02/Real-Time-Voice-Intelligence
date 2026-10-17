"""
Unified Voice Call Session.
Drives the real-time AI conversation path:
Audio In -> Silero VAD -> faster-whisper STT -> Groq LLM -> Chatterbox TTS -> Audio Out.

Shared identically by:
1. Asterisk ARI phone calls (via RTP media bridge)
2. Local Voice Test Mode (via WebSocket / local audio)
"""
import asyncio
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
        self._active = True
        self._turn_lock = asyncio.Lock()

    def feed_input_audio(self, pcm_bytes: bytes) -> None:
        """
        Receives incoming 16kHz PCM16 audio (from RTP or WebSocket).
        Processes through Silero VAD. Handles barge-in and turn dispatch.
        """
        if not self._active:
            return

        events = self.vad.process_chunk(pcm_bytes)
        for ev in events:
            if ev.event_type == "SPEECH_STARTED":
                # Customer interrupted AI playback (Barge-in)
                if self._is_ai_speaking:
                    logger.info("barge_in_detected", call_id=self.call_id)
                    self.orchestrator.interrupt()
                    self._is_ai_speaking = False

            elif ev.event_type == "SPEECH_ENDED" and ev.speech_buffer:
                # Customer completed speaking; dispatch to STT -> LLM -> TTS pipeline
                asyncio.create_task(self._process_speech_turn(ev.speech_buffer))

    async def _process_speech_turn(self, speech_buffer: bytes) -> None:
        async with self._turn_lock:
            if not self._active:
                return
            self._is_ai_speaking = True
            try:
                turn = await self.orchestrator.handle_user_audio_chunk(speech_buffer)
                if (turn.should_end_call or turn.should_transfer) and self.on_control:
                    await self.on_control("transfer" if turn.should_transfer else "end")
            except Exception as e:
                logger.error("speech_turn_processing_error", call_id=self.call_id, error=str(e))
            finally:
                self._is_ai_speaking = False

    async def start(self) -> None:
        """Plays initial conversational greeting."""
        self._is_ai_speaking = True
        try:
            await self.orchestrator.opening_greeting()
        except Exception as e:
            logger.error("opening_greeting_error", call_id=self.call_id, error=str(e))
        finally:
            self._is_ai_speaking = False

    def close(self) -> None:
        self._active = False
        self.vad.reset()


async def create_live_voice_session(
    db: Optional[AsyncSession] = None,
    *,
    call_id: str,
    tenant_id: uuid.UUID,
    lead_id: Optional[uuid.UUID] = None,
    on_audio_out: Callable[[bytes], None],
    on_control: Optional[Callable[[str], Awaitable[None]]] = None,
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

    stt = get_stt_provider()
    llm = get_llm_provider()
    tts = get_tts_provider()
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
