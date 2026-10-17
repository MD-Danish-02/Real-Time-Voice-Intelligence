"""
The core conversation loop shared by inbound, outbound, and local voice sessions:
audio in -> Silero VAD -> faster-whisper STT -> context + tool-augmented Groq LLM -> Chatterbox TTS -> audio out.

Optimized for:
- Indian Hindi, Hinglish, Indian English natural code-switching
- Per-turn latency measurement (VAD, STT, LLM TTFT, TTS TTFA, End-to-End)
- Streaming audio delivery clause-by-clause
- Real-time barge-in / interruption
- Turn-by-turn transcript segment tracking
- Tool-calling loop with safety constraints
"""
import json
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from app.core.logging import get_logger
from app.providers.voice.llm.base import LLMProvider, LLMResponse
from app.providers.voice.stt.base import STTProvider
from app.providers.voice.tts.base import SynthesisResult, TTSProvider
from app.voice_pipeline.agent_tools import TOOL_SCHEMAS, AgentToolExecutor

logger = get_logger("voice_pipeline.orchestrator")

MAX_TOOL_LOOP_ITERATIONS = 4


@dataclass
class TurnLatencyMetrics:
    vad_latency_ms: float = 0.0
    stt_latency_ms: float = 0.0
    llm_ttft_ms: float = 0.0
    llm_total_ms: float = 0.0
    tts_ttfa_ms: float = 0.0
    tts_total_ms: float = 0.0
    end_to_end_ms: float = 0.0  # speech_end -> first_audio_out


@dataclass
class TurnResult:
    user_text: str
    ai_text: Optional[str]
    audio: Optional[SynthesisResult]
    tool_calls_made: list[str]
    should_transfer: bool
    should_end_call: bool
    metrics: TurnLatencyMetrics = field(default_factory=TurnLatencyMetrics)


SYSTEM_PROMPT_TEMPLATE = """You are Riya, a friendly, knowledgeable, and highly articulate AI sales and customer specialist calling on behalf of {company_name}.

STYLE AND TONE:
- Speak naturally like a modern Indian business professional.
- Code-switch smoothly between Hindi, Hinglish, and Indian English as appropriate for the conversation (e.g., "Namaste! Main Riya bol rahi hoon from {company_name}. Aapke business ke liye ek quick AI automation solution discuss karna tha.").
- Keep spoken sentences concise (1 to 2 sentences per turn), conversational, and polite. Avoid long essays or email-style bulleted lists.
- Be respectful, helpful, and attentive. If the customer speaks Hindi, respond primarily in Hindi/Hinglish. If they speak English, respond in Indian English.
- Handle objections gracefully. If the customer is busy or not interested, politely thank them and offer a scheduled callback.

RULES:
1. Rely strictly on provided lead details, business knowledge, and available tools. Never invent false pricing or dates.
2. To book a meeting, check calendar slots and confirm before booking.
3. Available actions must only be called via the provided tools.

Lead details:
{lead_json}

Company Knowledge:
{knowledge_json}

Previous call history:
{previous_calls_json}
"""


class VoiceCallOrchestrator:
    def __init__(
        self,
        *,
        stt: STTProvider,
        llm: LLMProvider,
        tts: TTSProvider,
        tool_executor: AgentToolExecutor,
        context: dict,
        on_event: Optional[Callable[[str, dict], Awaitable[None]]] = None,
        on_audio_chunk: Optional[Callable[[bytes], Awaitable[None]]] = None,
    ) -> None:
        self.stt = stt
        self.llm = llm
        self.tts = tts
        self.tool_executor = tool_executor
        self.context = context
        self.on_event = on_event
        self.on_audio_chunk = on_audio_chunk

        company_name = context.get("company", {}).get("name", "VoxAI")
        self.conversation: list[dict] = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT_TEMPLATE.format(
                    company_name=company_name,
                    lead_json=json.dumps(context.get("lead", {})),
                    knowledge_json=json.dumps(context.get("knowledge", [])),
                    previous_calls_json=json.dumps(context.get("previous_calls", [])),
                ),
            }
        ]

        self._interrupted = False
        self.transcript_segments: list[dict] = []
        self._call_start_time = time.perf_counter()

    async def _emit(self, event_type: str, payload: dict) -> None:
        if self.on_event:
            try:
                await self.on_event(event_type, payload)
            except Exception as e:
                logger.warning("event_emission_failed", event=event_type, error=str(e))

    def interrupt(self) -> None:
        """Called immediately by VAD/transport when customer starts speaking over AI playback."""
        self._interrupted = True

    async def opening_greeting(self) -> SynthesisResult:
        """Generates natural opening greeting customized for Indian business context."""
        company = self.context.get("company", {}).get("name", "VoxAI")
        lead_name = self.context.get("lead", {}).get("name", "")
        name_greeting = f"{lead_name} ji, " if lead_name else ""
        greeting = f"Namaste! {name_greeting}Main Riya bol rahi hoon from {company}. Kya abhi aapse ek minute baat karna convenient hoga?"

        self.conversation.append({"role": "assistant", "content": greeting})
        now_ms = int((time.perf_counter() - self._call_start_time) * 1000)
        self.transcript_segments.append({
            "speaker": "ai",
            "text": greeting,
            "started_at_ms": now_ms,
            "confidence": 1.0,
        })

        await self._emit("AI_RESPONSE_STARTED", {"text": greeting})
        audio = await self.tts.synthesize(text=greeting, language="hi")
        if self.on_audio_chunk and audio.audio_bytes:
            await self.on_audio_chunk(audio.audio_bytes)
        await self._emit("AI_RESPONSE_COMPLETED", {"text": greeting})
        return audio

    async def handle_user_audio_chunk(self, audio_bytes: bytes) -> TurnResult:
        """Processes a completed user speech turn from VAD buffer through STT, LLM, and TTS."""
        t_speech_end = time.perf_counter()
        metrics = TurnLatencyMetrics()

        await self._emit("SPEECH_STARTED", {})
        t_stt_start = time.perf_counter()
        transcription = await self.stt.transcribe_chunk(audio_bytes)
        metrics.stt_latency_ms = (time.perf_counter() - t_stt_start) * 1000.0

        user_text = transcription.text.strip()
        await self._emit("SPEECH_ENDED", {
            "text": user_text,
            "confidence": transcription.confidence,
            "language": transcription.language,
            "stt_latency_ms": metrics.stt_latency_ms,
        })

        now_ms = int((t_speech_end - self._call_start_time) * 1000)
        self.transcript_segments.append({
            "speaker": "customer",
            "text": user_text,
            "started_at_ms": now_ms,
            "confidence": transcription.confidence,
        })

        if not user_text:
            return TurnResult(
                user_text="",
                ai_text=None,
                audio=None,
                tool_calls_made=[],
                should_transfer=False,
                should_end_call=False,
                metrics=metrics,
            )

        self.conversation.append({"role": "user", "content": user_text})

        tool_calls_made: list[str] = []
        ai_text: Optional[str] = None
        should_transfer = False
        should_end = False
        was_interrupted = False

        t_llm_start = time.perf_counter()
        for _ in range(MAX_TOOL_LOOP_ITERATIONS):
            if self._interrupted:
                await self._emit("USER_INTERRUPTED", {})
                self._interrupted = False
                was_interrupted = True
                break

            response: LLMResponse = await self.llm.chat(
                messages=self.conversation,
                tools=TOOL_SCHEMAS,
                temperature=0.35,
            )

            metrics.llm_ttft_ms = response.metrics.ttft_ms
            metrics.llm_total_ms = (time.perf_counter() - t_llm_start) * 1000.0

            if response.tool_calls:
                self.conversation.append({
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                        }
                        for tc in response.tool_calls
                    ],
                })
                for tool_call in response.tool_calls:
                    await self._emit("TOOL_CALLED", {"name": tool_call.name, "arguments": tool_call.arguments})
                    result = await self.tool_executor.execute(tool_call.name, tool_call.arguments)
                    tool_calls_made.append(tool_call.name)
                    self.conversation.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result),
                    })
                    if tool_call.name == "transfer_to_human":
                        should_transfer = True
                    if tool_call.name == "end_call":
                        should_end = True
                if should_transfer or should_end:
                    break
                continue

            ai_text = response.content
            self.conversation.append({"role": "assistant", "content": ai_text})
            break

        if ai_text is None and not (should_transfer or should_end) and not was_interrupted:
            try:
                final = await self.llm.chat(messages=self.conversation, tools=None, temperature=0.35)
                ai_text = final.content
            except Exception:
                ai_text = None
            ai_text = ai_text or "Sorry, kya aap ek baar dohra sakte hain?"
            self.conversation.append({"role": "assistant", "content": ai_text})

        audio: Optional[SynthesisResult] = None
        if ai_text and not was_interrupted:
            ai_now_ms = int((time.perf_counter() - self._call_start_time) * 1000)
            self.transcript_segments.append({
                "speaker": "ai",
                "text": ai_text,
                "started_at_ms": ai_now_ms,
                "confidence": 1.0,
            })

            await self._emit("AI_RESPONSE_STARTED", {"text": ai_text})
            t_tts_start = time.perf_counter()

            # Synthesize streaming chunks so audio flows immediately
            chunks: list[bytes] = []
            async for chunk in self.tts.synthesize_stream(text=ai_text, language="hi"):
                if self._interrupted:
                    await self._emit("USER_INTERRUPTED", {})
                    self._interrupted = False
                    break
                if not chunks:
                    metrics.tts_ttfa_ms = (time.perf_counter() - t_tts_start) * 1000.0
                    metrics.end_to_end_ms = (time.perf_counter() - t_speech_end) * 1000.0
                chunks.append(chunk)
                if self.on_audio_chunk:
                    await self.on_audio_chunk(chunk)

            metrics.tts_total_ms = (time.perf_counter() - t_tts_start) * 1000.0
            combined = b"".join(chunks)
            audio = SynthesisResult(audio_bytes=combined, sample_rate=16000, format="pcm16")
            await self._emit("AI_RESPONSE_COMPLETED", {
                "text": ai_text,
                "end_to_end_ms": metrics.end_to_end_ms,
                "metrics": {
                    "stt_ms": metrics.stt_latency_ms,
                    "llm_ms": metrics.llm_total_ms,
                    "tts_ttfa_ms": metrics.tts_ttfa_ms,
                    "total_turn_ms": metrics.end_to_end_ms,
                },
            })

        if should_transfer:
            await self._emit("CALL_TRANSFERRED", {})
        if should_end:
            await self._emit("CALL_ENDED", {"reason": "ai_ended"})

        logger.info(
            "voice_turn_completed",
            stt_ms=round(metrics.stt_latency_ms, 1),
            llm_ms=round(metrics.llm_total_ms, 1),
            tts_ttfa_ms=round(metrics.tts_ttfa_ms, 1),
            end_to_end_ms=round(metrics.end_to_end_ms, 1),
            user_text=user_text[:60],
            ai_text=(ai_text or "")[:60],
        )

        return TurnResult(
            user_text=user_text,
            ai_text=ai_text,
            audio=audio,
            tool_calls_made=tool_calls_made,
            should_transfer=should_transfer,
            should_end_call=should_end,
            metrics=metrics,
        )

    def full_transcript_text(self) -> str:
        return "\n".join(
            f"{m['speaker'].title()}: {m['text']}" for m in self.transcript_segments if m.get("text")
        )
