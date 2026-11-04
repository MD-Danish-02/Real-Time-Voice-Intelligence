"""
The core conversation loop shared by inbound, outbound, and local voice sessions:
audio in -> Silero VAD -> faster-whisper STT -> context + tool-augmented Groq LLM -> Chatterbox TTS -> audio out.

Optimized for:
- language-specific Hindi and English
- Per-turn latency measurement (VAD, STT, LLM TTFT, TTS TTFA, End-to-End)
- Streaming audio delivery clause-by-clause
- Real-time barge-in / interruption
- Turn-by-turn transcript segment tracking
- Tool-calling loop with safety constraints
"""
import asyncio
import json
import re
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
    llm_ttft_ms: Optional[float] = None
    llm_total_ms: float = 0.0
    tts_ttfa_ms: float = 0.0
    tts_total_ms: float = 0.0
    end_to_end_ms: float = 0.0  # speech_end -> first_audio_out
    audio_send_ms: float = 0.0


@dataclass
class AudioStreamStats:
    chunks: list[bytes] = field(default_factory=list)
    ttfa_ms: Optional[float] = None
    generation_ms: float = 0.0
    send_ms: float = 0.0
    end_to_first_audio_ms: Optional[float] = None
    first_audio_sent_ms: Optional[float] = None
    first_text_chunk_ms: Optional[float] = None
    llm_first_token_ms: Optional[float] = None
    interrupted: bool = False


@dataclass
class TurnResult:
    user_text: str
    ai_text: Optional[str]
    audio: Optional[SynthesisResult]
    tool_calls_made: list[str]
    should_transfer: bool
    should_end_call: bool
    metrics: TurnLatencyMetrics = field(default_factory=TurnLatencyMetrics)


SYSTEM_PROMPT_TEMPLATE = """You are Riya from {company_name}, a helpful voice assistant.

LANGUAGE: {language_instruction}

STYLE AND TONE:
- Speak naturally and neutrally. Keep responses concise and appropriate for a spoken conversation.
- Use everyday spoken Hindi, not formal or literary Hindi. For Hinglish, keep common technical terms in English and use natural Hindi around them.
- Match the caller's latest clear language choice. Do not switch languages because of a single ambiguous word such as "haan", "yes", "okay", or "theek hai".
- Keep spoken sentences concise (1 to 2 sentences per turn), conversational, and polite. Avoid long essays or email-style bulleted lists.
- Be respectful and answer only what the customer actually said or asked. Do not assume product interest, business needs, consent, or commitments. Do not pitch unless asked about the service.
- Answer the specific question using verified context. Do not repeat a generic follow-up question after every answer. If the transcript or facts are unclear, ask one short clarification or say the information is not available. Do not repeat the opening greeting.
- Handle objections gracefully. If the customer is busy or not interested, politely thank them and offer a scheduled callback.

RULES:
1. Rely strictly on provided lead details, business knowledge, and available tools. Never invent false pricing or dates.
2. To book a meeting, check calendar slots and confirm before booking.
3. Available actions must only be called via the provided tools. When calling a tool, return only the tool call and no customer-facing prose in that same response.

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
        language: str = "hi",
    ) -> None:
        self.stt = stt
        self.llm = llm
        self.tts = tts
        self.tool_executor = tool_executor
        self.context = context
        self.on_event = on_event
        self.on_audio_chunk = on_audio_chunk
        if language not in ("auto", "hi", "en"):
            raise ValueError("Unsupported session language. Use auto, hi, or en.")
        self.session_language = language
        # Auto sessions begin in Hindi for the existing local test greeting, then
        # follow Whisper's per-utterance language result with short-turn hysteresis.
        self.language = "hi" if language == "auto" else language
        language_instruction = self._language_instruction(self.language)

        company_name = context.get("company", {}).get("name", "VoxAI")
        self.conversation: list[dict] = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT_TEMPLATE.format(
                    company_name=company_name,
                    language_instruction=language_instruction,
                    lead_json=json.dumps(context.get("lead", {})),
                    knowledge_json=json.dumps(context.get("knowledge", [])),
                    previous_calls_json=json.dumps(context.get("previous_calls", [])),
                ),
            }
        ]

        self._interrupted = False
        self._interruption_notified = False
        self._greeting_started = False
        self.transcript_segments: list[dict] = []
        self._call_start_time = time.perf_counter()
        self._last_clear_language = self.language
        self._explicit_language_preference: Optional[str] = None
        self._active_generation_task: Optional[asyncio.Task] = None
        self._tts_generation_active = False

    @staticmethod
    def _language_instruction(language: str) -> str:
        if language == "en":
            return "Respond naturally in English."
        if language == "hinglish":
            return "Respond in natural conversational Hinglish: Hindi in Devanagari with English terms or phrases where the caller used them."
        return "Respond in simple, everyday Hindi written in Devanagari; retain familiar English technical terms and names naturally."

    def _select_turn_language(self, text: str, detected: Optional[str] = None) -> str:
        """Choose response language per utterance; preserve context for ambiguous words."""
        if self.session_language in ("hi", "en"):
            return self.session_language

        # Explicit requests are more reliable than Whisper's per-utterance
        # language label (which can flip on technical terms or short answers).
        normalized_text = re.sub(r"[^a-z\u0900-\u097f]+", " ", text.lower()).strip()
        hinglish_request = re.search(
            r"(?:\b(?:can|could|may)\s+we\s+(?:talk|speak|chat)\s+(?:in\s+)?hinglish\b|"
            r"\b(?:talk|speak|chat|reply|respond)\b.{0,35}\b(?:in\s+)?hinglish\b|"
            r"\bhinglish\s+(?:please|mein|me)\b)",
            normalized_text,
        )
        hindi_request = re.search(
            r"(?:\b(?:can|could|may)\s+we\s+(?:talk|speak|chat)\s+(?:in\s+)?hindi\b|"
            r"\b(?:talk|speak|chat|reply|respond)\b.{0,35}\b(?:in\s+)?hindi\b|"
            r"\b(?:hindi\s+(?:please|mein|me)|in\s+hindi)\b|"
            r"(?:हिंदी|हिन्दी).{0,24}(?:बात|बोल|जवाब)|"
            r"(?:हिंदी|हिन्दी) में(?: ही)?(?: बात| बोल| जवाब))",
            normalized_text,
        )
        english_request = re.search(
            r"(?:\b(?:can|could|may)\s+we\s+(?:talk|speak|chat)\s+(?:in\s+)?english\b|"
            r"\b(?:talk|speak|chat|reply|respond)\b.{0,35}\b(?:in\s+)?english\b|"
            r"\b(?:english\s+(?:please|mein|me)|in\s+english)\b|"
            r"अंग्रेज़ी.{0,24}(?:बात|बोल|जवाब)|अंग्रेजी.{0,24}(?:बात|बोल|जवाब)|"
            r"(?:अंग्रेज़ी|अंग्रेजी) में(?: ही)?(?: बात| बोल| जवाब))",
            normalized_text,
        )
        if hinglish_request:
            self._explicit_language_preference = "hinglish"
        elif hindi_request:
            self._explicit_language_preference = "hi"
        elif english_request:
            self._explicit_language_preference = "en"
        if self._explicit_language_preference in ("hi", "en", "hinglish"):
            self._last_clear_language = self._explicit_language_preference
            return self._explicit_language_preference

        normalized = (detected or "").lower().replace("_", "-")
        if normalized in ("hindi", "hi", "hi-in"):
            detected_language = "hi"
        elif normalized in ("english", "en", "en-us", "en-in"):
            detected_language = "en"
        else:
            detected_language = ""
        devanagari = len(re.findall(r"[\u0900-\u097f]", text))
        latin_words = re.findall(r"[A-Za-z]+", text.lower())
        compact = normalized_text
        if compact in {"haan", "han", "yes", "okay", "ok", "theek hai", "theek", "right", "sure", "हाँ", "हाँ जी", "ठीक", "ठीक है", "जी"}:
            return self._last_clear_language
        hindi_roman = {"aap", "kaise", "ho", "hai", "hain", "kya", "kyu", "kyun", "ka", "ki", "ke", "ko", "mein", "baare", "batao", "kar", "sakte", "chahiye", "theek", "nahi", "nahin", "mera", "mujhe", "hum", "aur", "acha", "accha"}
        hindi_roman_count = sum(word in hindi_roman for word in latin_words)
        ambiguous = {"haan", "han", "yes", "okay", "ok", "theek", "right", "sure"}
        meaningful = [word for word in latin_words if word not in ambiguous]
        if devanagari and meaningful:
            selected = "hinglish" if latin_words else "hi"
        elif devanagari:
            selected = "hi"
        elif hindi_roman_count >= 2:
            selected = "hinglish" if len(latin_words) > hindi_roman_count else "hi"
        elif detected_language and meaningful:
            selected = detected_language
        elif not meaningful:
            return self._last_clear_language
        else:
            selected = detected_language or self._last_clear_language
        self._last_clear_language = selected
        return selected

    def _apply_turn_language(self, text: str, detected: Optional[str] = None) -> str:
        selected = self._select_turn_language(text, detected)
        self.language = selected
        system = self.conversation[0]
        system["content"] = re.sub(
            r"LANGUAGE: .*", f"LANGUAGE: {self._language_instruction(selected)}", system["content"], count=1
        )
        return selected

    async def _emit(self, event_type: str, payload: dict) -> None:
        if self.on_event:
            try:
                await self.on_event(event_type, payload)
            except Exception as e:
                logger.warning("event_emission_failed", event=event_type, error=str(e))

    def interrupt(self) -> None:
        """Called immediately by VAD/transport when customer starts speaking over AI playback."""
        if not self._interrupted:
            self._interrupted = True
            self._interruption_notified = False
            task = self._active_generation_task
            if task is not None and not task.done():
                try:
                    if task is not asyncio.current_task():
                        task.cancel()
                except RuntimeError:
                    task.cancel()

    async def notify_interruption(self, payload: Optional[dict] = None) -> None:
        if not self._interruption_notified:
            self._interruption_notified = True
            await self._emit("USER_INTERRUPTED", payload or {})

    async def _consume_interruption(self) -> None:
        await self.notify_interruption()
        self._interrupted = False

    async def _stream_tts(self, text: str, speech_end_at: Optional[float] = None) -> AudioStreamStats:
        """Measure model generation separately from awaited WebSocket/audio sends."""
        stats = AudioStreamStats()
        request_started = time.perf_counter()
        tts_language = self.language if self.language in ("hi", "en", "hinglish") else "hi"
        iterator = self.tts.synthesize_stream(text=text, language=tts_language).__aiter__()
        self._tts_generation_active = True
        try:
            while True:
                generation_started = time.perf_counter()
                try:
                    chunk = await iterator.__anext__()
                except StopAsyncIteration:
                    break
                generated_at = time.perf_counter()
                stats.generation_ms += (generated_at - generation_started) * 1000.0
                if stats.ttfa_ms is None:
                    stats.ttfa_ms = (generated_at - request_started) * 1000.0
                    if speech_end_at is not None:
                        stats.end_to_first_audio_ms = (generated_at - speech_end_at) * 1000.0
                if self._interrupted:
                    stats.interrupted = True
                    await self._consume_interruption()
                    break
                stats.chunks.append(chunk)
                if self.on_audio_chunk:
                    send_started = time.perf_counter()
                    await self.on_audio_chunk(chunk)
                    if stats.first_audio_sent_ms is None and speech_end_at is not None:
                        stats.first_audio_sent_ms = (time.perf_counter() - speech_end_at) * 1000.0
                    stats.send_ms += (time.perf_counter() - send_started) * 1000.0
        finally:
            self._tts_generation_active = False
            close = getattr(iterator, "aclose", None)
            if close:
                await close()
        return stats

    async def _stream_llm_response_to_tts(
        self, *, messages: list[dict], tools: Optional[list[dict]], speech_end_at: Optional[float]
    ) -> tuple[LLMResponse, AudioStreamStats]:
        """Consume provider text deltas while serially synthesizing complete phrases."""
        if tools:
            raise ValueError("Incremental speech requires a tool-free completion to avoid speaking before a tool call.")
        queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        stats = AudioStreamStats()
        llm_started = time.perf_counter()
        first_token_at: Optional[float] = None
        phrase_ready_at: Optional[float] = None
        response_started = False
        buffer = ""

        def on_text_delta(delta: str) -> None:
            nonlocal first_token_at
            if first_token_at is None and delta.strip():
                first_token_at = time.perf_counter()
            queue.put_nowait(delta)

        async def synthesize_phrase(phrase: str) -> None:
            nonlocal phrase_ready_at, response_started
            phrase = phrase.strip()
            if not phrase or self._interrupted:
                return
            if phrase_ready_at is None:
                phrase_ready_at = time.perf_counter()
                stats.first_text_chunk_ms = (
                    (phrase_ready_at - speech_end_at) * 1000.0 if speech_end_at is not None else None
                )
            await self._emit(
                "AI_RESPONSE_STARTED" if not response_started else "AI_TEXT_DELTA",
                {"text": phrase, "streaming": True},
            )
            response_started = True
            phrase_stats = await self._stream_tts(phrase, speech_end_at=speech_end_at)
            if stats.ttfa_ms is None:
                stats.ttfa_ms = phrase_stats.ttfa_ms
                stats.end_to_first_audio_ms = phrase_stats.end_to_first_audio_ms
                stats.first_audio_sent_ms = phrase_stats.first_audio_sent_ms
            stats.generation_ms += phrase_stats.generation_ms
            stats.send_ms += phrase_stats.send_ms
            stats.chunks.extend(phrase_stats.chunks)
            stats.interrupted = stats.interrupted or phrase_stats.interrupted

        async def consume_phrases() -> None:
            nonlocal buffer
            punctuation = re.compile(r"[.!?\u0964\u0965\n][\"'\u2019\u201d)]*(?:\s+|$)")
            while True:
                delta = await queue.get()
                if delta is None:
                    break
                buffer += delta
                while buffer:
                    boundary = punctuation.search(buffer)
                    if boundary:
                        split_at = boundary.end()
                    elif len(buffer) >= 150:
                        split_at = buffer.rfind(" ", 95, 150)
                        if split_at < 1:
                            split_at = 150
                    else:
                        break
                    phrase, buffer = buffer[:split_at], buffer[split_at:]
                    await synthesize_phrase(phrase)
                    if self._interrupted:
                        return
            if buffer.strip() and not self._interrupted:
                await synthesize_phrase(buffer)

        consumer = asyncio.create_task(consume_phrases())
        active_task = asyncio.current_task()
        self._active_generation_task = active_task
        try:
            response: LLMResponse = await self.llm.chat_stream_response(
                messages=messages,
                tools=tools,
                temperature=0.35,
                on_text_delta=on_text_delta,
            )
            # Tool calls take precedence. The system instruction asks the model
            # not to mix speech with a tool call; any queued prose is discarded.
            if response.tool_calls:
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
                return response, stats
            queue.put_nowait(None)
            await consumer
            if response.metrics.ttft_ms is not None:
                stats.llm_first_token_ms = response.metrics.ttft_ms
            if phrase_ready_at is not None and first_token_at is not None and speech_end_at is not None:
                stats.first_text_chunk_ms = (phrase_ready_at - speech_end_at) * 1000.0
            return response, stats
        except BaseException:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            raise
        finally:
            if self._active_generation_task is active_task:
                self._active_generation_task = None

    @staticmethod
    def _requires_action_tools(text: str) -> bool:
        """Keep actions on the established tool path; ordinary Q&A can stream safely."""
        action = re.compile(
            r"\b(book(?:\s+it|\s+me)?|schedule|set\s+up|arrange|reschedule|cancel|"
            r"transfer|speak\s+to|talk\s+to|connect\s+me|send\s+(?:me|it)|whats?app|"
            r"text\s+me|call\s+me\s+back|callback|hang\s+up|end\s+(?:the\s+)?call|"
            r"update\s+(?:my|the)|add\s+(?:a\s+)?note|log\s+(?:an\s+)?activity)\b",
            re.IGNORECASE,
        )
        hindi_action = re.compile(
            r"बुक\s+कर|शेड्यूल\s+कर|मीटिंग\s+(?:रख|बुक)|भेज\s+(?:दो|दीजिए)|"
            r"एजेंट\s+से\s+बात|कॉल\s+बैक|अपडेट\s+कर|नोट\s+जोड़|कॉल\s+खत्म|"
            r"book\s+kar|schedule\s+kar|bhej\s+do|transfer\s+kar|agent\s+se\s+baat",
            re.IGNORECASE,
        )
        return bool(action.search(text) or hindi_action.search(text))

    async def opening_greeting(self) -> SynthesisResult:
        """Generates natural opening greeting customized for Indian business context."""
        if self._greeting_started:
            return SynthesisResult(audio_bytes=b"", sample_rate=16000, format="pcm16")
        self._greeting_started = True
        company = self.context.get("company", {}).get("name", "VoxAI")
        greeting = (
            f"नमस्ते, मैं {company} से रिया बोल रही हूँ। क्या अभी बात करना सुविधाजनक है?"
            if self.language == "hi" else
            f"Hello! This is Riya from {company}. Is now a good time to speak for a minute?"
        )

        self.conversation.append({"role": "assistant", "content": greeting})
        now_ms = int((time.perf_counter() - self._call_start_time) * 1000)
        self.transcript_segments.append({
            "speaker": "ai",
            "text": greeting,
            "started_at_ms": now_ms,
            "confidence": 1.0,
        })

        await self._emit("AI_RESPONSE_STARTED", {"text": greeting})
        stream_stats = await self._stream_tts(greeting)
        combined = b"".join(stream_stats.chunks)
        audio = SynthesisResult(audio_bytes=combined, sample_rate=16000, format="pcm16")
        await self._emit("AI_RESPONSE_COMPLETED", {
            "text": greeting,
            "end_to_end_ms": None,
            "metrics": {
                "stt_ms": None,
                "llm_ttft_ms": None,
                "llm_ms": None,
                "tts_ttfa_ms": stream_stats.ttfa_ms,
                "tts_total_ms": round(stream_stats.generation_ms, 1),
                "audio_send_ms": round(stream_stats.send_ms, 1),
                "audio_duration_ms": len(combined) / 32.0,
                "end_to_first_audio_ms": None,
                "total_turn_ms": round(stream_stats.generation_ms + stream_stats.send_ms, 1),
            },
        })
        return audio

    async def handle_user_audio_chunk(
        self, audio_bytes: bytes, *, speech_started_at: Optional[float] = None,
        speech_ended_at: Optional[float] = None,
    ) -> TurnResult:
        """Processes a completed user speech turn from VAD buffer through STT, LLM, and TTS."""
        t_speech_end = speech_ended_at or time.perf_counter()
        metrics = TurnLatencyMetrics()

        t_stt_start = time.perf_counter()
        transcription = await self.stt.transcribe_chunk(audio_bytes)
        metrics.stt_latency_ms = (time.perf_counter() - t_stt_start) * 1000.0

        user_text = transcription.text.strip()
        transcript_is_meaningful = self._is_meaningful_transcript(user_text)
        # Do not let an empty/invalid STT result change the session language
        # preference or its last-clear-language hysteresis.
        response_language = (
            self._apply_turn_language(user_text, transcription.language)
            if transcript_is_meaningful else self.language
        )
        await self._emit("SPEECH_ENDED", {
            "text": user_text,
            "confidence": transcription.confidence,
            "language": transcription.language,
            "response_language": response_language,
            "stt_latency_ms": metrics.stt_latency_ms,
            "audio_duration_ms": len(audio_bytes) / 32.0,
        })

        now_ms = int(((speech_started_at or t_speech_end) - self._call_start_time) * 1000)
        self.transcript_segments.append({
            "speaker": "customer",
            "text": user_text,
            "started_at_ms": now_ms,
            "confidence": transcription.confidence,
        })

        if not transcript_is_meaningful:
            repeat_text = "क्षमा कीजिए, आपकी बात स्पष्ट नहीं सुनाई दी। कृपया फिर से कहें।" if self.language == "hi" else "Sorry, I didn't catch that. Could you please repeat it?"
            await self._emit("TRANSCRIPT_REJECTED", {"reason": "empty_or_invalid_transcript"})
            await self._speak_fixed_reprompt(repeat_text, metrics, t_speech_end)
            return TurnResult(
                user_text=user_text,
                ai_text=repeat_text,
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
        incremental_stats: Optional[AudioStreamStats] = None

        t_llm_start = time.perf_counter()
        for _ in range(MAX_TOOL_LOOP_ITERATIONS):
            if self._interrupted:
                await self._consume_interruption()
                was_interrupted = True
                break

            tools = TOOL_SCHEMAS if self._requires_action_tools(user_text) else None
            if getattr(self.llm, "supports_incremental_text", False) and tools is None:
                response, incremental_stats = await self._stream_llm_response_to_tts(
                    messages=self.conversation, tools=None, speech_end_at=t_speech_end
                )
            else:
                response = await self.llm.chat(
                    messages=self.conversation, tools=tools, temperature=0.35
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
            ai_text = ("माफ़ कीजिए, मैं अभी इसका उत्तर नहीं दे पाई। क्या आप अपना प्रश्न थोड़ा स्पष्ट करेंगे?"
                       if self.language == "hi" else
                       "Sorry, I couldn't answer that just now. Could you clarify your question?")
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

            if incremental_stats is not None and incremental_stats.chunks:
                stream_stats = incremental_stats
            else:
                await self._emit("AI_RESPONSE_STARTED", {"text": ai_text})
                stream_stats = await self._stream_tts(ai_text, speech_end_at=t_speech_end)
            metrics.tts_ttfa_ms = stream_stats.ttfa_ms or 0.0
            metrics.end_to_end_ms = stream_stats.end_to_first_audio_ms or 0.0
            metrics.tts_total_ms = stream_stats.generation_ms
            metrics.audio_send_ms = stream_stats.send_ms
            was_interrupted = was_interrupted or stream_stats.interrupted
            chunks = stream_stats.chunks
            combined = b"".join(chunks)
            audio = SynthesisResult(audio_bytes=combined, sample_rate=16000, format="pcm16")
            await self._emit("AI_RESPONSE_COMPLETED", {
                "text": ai_text,
                "end_to_end_ms": metrics.end_to_end_ms,
                "metrics": {
                    "stt_ms": round(metrics.stt_latency_ms, 1),
                    "llm_ms": round(metrics.llm_total_ms, 1),
                    "llm_ttft_ms": round(metrics.llm_ttft_ms, 1) if metrics.llm_ttft_ms is not None else None,
                    "llm_first_token_ms": round(incremental_stats.llm_first_token_ms, 1) if incremental_stats and incremental_stats.llm_first_token_ms is not None else None,
                    "first_text_chunk_ms": round(incremental_stats.first_text_chunk_ms, 1) if incremental_stats and incremental_stats.first_text_chunk_ms is not None else None,
                    "tts_ttfa_ms": round(metrics.tts_ttfa_ms, 1) if stream_stats.ttfa_ms is not None else None,
                    "first_audio_sent_ms": round(stream_stats.first_audio_sent_ms, 1) if stream_stats.first_audio_sent_ms is not None else None,
                    "tts_total_ms": round(metrics.tts_total_ms, 1),
                    "audio_send_ms": round(metrics.audio_send_ms, 1),
                    "audio_duration_ms": len(combined) / 32.0,
                    "end_to_first_audio_ms": round(metrics.end_to_end_ms, 1) if stream_stats.end_to_first_audio_ms is not None else None,
                    "total_turn_ms": round((time.perf_counter() - t_speech_end) * 1000.0, 1),
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

    @staticmethod
    def _is_meaningful_transcript(text: str) -> bool:
        import re
        cleaned = re.sub(r"<\|[^|]*\|>", "", text).strip()
        # Recognize letters and numbers from any Unicode script. Script alone
        # cannot tell us whether Whisper was right, and must not discard
        # legitimate Urdu or other multilingual speech.
        return bool(re.search(r"[^\W_]", cleaned, flags=re.UNICODE))

    async def _speak_fixed_reprompt(self, text: str, metrics: TurnLatencyMetrics, speech_end: float) -> None:
        await self._emit("AI_RESPONSE_STARTED", {"text": text})
        stream_stats = await self._stream_tts(text, speech_end_at=speech_end)
        chunks = stream_stats.chunks
        metrics.tts_ttfa_ms = stream_stats.ttfa_ms or 0.0
        metrics.end_to_end_ms = stream_stats.end_to_first_audio_ms or 0.0
        metrics.tts_total_ms = stream_stats.generation_ms
        metrics.audio_send_ms = stream_stats.send_ms
        await self._emit("AI_RESPONSE_COMPLETED", {"text": text, "metrics": {
            "stt_ms": round(metrics.stt_latency_ms, 1), "llm_ms": None,
            "llm_ttft_ms": None, "tts_ttfa_ms": round(metrics.tts_ttfa_ms, 1) if stream_stats.ttfa_ms is not None else None,
            "tts_total_ms": round(metrics.tts_total_ms, 1), "audio_send_ms": round(metrics.audio_send_ms, 1),
            "audio_duration_ms": sum(len(chunk) for chunk in chunks) / 32.0,
            "end_to_first_audio_ms": round(metrics.end_to_end_ms, 1) if stream_stats.end_to_first_audio_ms is not None else None,
            "total_turn_ms": round((time.perf_counter() - speech_end) * 1000.0, 1),
        }})

    async def handle_user_text(self, text: str) -> TurnResult:
        """Processes a text input turn through LLM and streaming TTS (for testing without microphone)."""
        t_speech_end = time.perf_counter()
        metrics = TurnLatencyMetrics()

        user_text = text.strip()
        response_language = self._apply_turn_language(user_text)
        await self._emit("SPEECH_ENDED", {
            "text": user_text,
            "confidence": 1.0,
            "language": self.language,
            "response_language": response_language,
            "stt_latency_ms": 0.0,
        })

        now_ms = int((t_speech_end - self._call_start_time) * 1000)
        self.transcript_segments.append({
            "speaker": "customer",
            "text": user_text,
            "started_at_ms": now_ms,
            "confidence": 1.0,
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
        incremental_stats: Optional[AudioStreamStats] = None

        t_llm_start = time.perf_counter()
        for _ in range(MAX_TOOL_LOOP_ITERATIONS):
            if self._interrupted:
                await self._consume_interruption()
                was_interrupted = True
                break

            tools = TOOL_SCHEMAS if self._requires_action_tools(user_text) else None
            if getattr(self.llm, "supports_incremental_text", False) and tools is None:
                response, incremental_stats = await self._stream_llm_response_to_tts(
                    messages=self.conversation, tools=None, speech_end_at=t_speech_end
                )
            else:
                response = await self.llm.chat(
                    messages=self.conversation, tools=tools, temperature=0.35
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
            ai_text = ("माफ़ कीजिए, मैं अभी इसका उत्तर नहीं दे पाई। क्या आप अपना प्रश्न थोड़ा स्पष्ट करेंगे?"
                       if self.language == "hi" else
                       "Sorry, I couldn't answer that just now. Could you clarify your question?")
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

            if incremental_stats is not None and incremental_stats.chunks:
                stream_stats = incremental_stats
            else:
                await self._emit("AI_RESPONSE_STARTED", {"text": ai_text})
                stream_stats = await self._stream_tts(ai_text, speech_end_at=t_speech_end)
            metrics.tts_ttfa_ms = stream_stats.ttfa_ms or 0.0
            metrics.end_to_end_ms = stream_stats.end_to_first_audio_ms or 0.0
            metrics.tts_total_ms = stream_stats.generation_ms
            metrics.audio_send_ms = stream_stats.send_ms
            was_interrupted = was_interrupted or stream_stats.interrupted
            chunks = stream_stats.chunks
            combined = b"".join(chunks)
            audio = SynthesisResult(audio_bytes=combined, sample_rate=16000, format="pcm16")
            await self._emit("AI_RESPONSE_COMPLETED", {
                "text": ai_text,
                "end_to_end_ms": metrics.end_to_end_ms,
                "metrics": {
                    "stt_ms": None,
                    "llm_ms": round(metrics.llm_total_ms, 1),
                    "llm_ttft_ms": round(metrics.llm_ttft_ms, 1) if metrics.llm_ttft_ms is not None else None,
                    "llm_first_token_ms": round(incremental_stats.llm_first_token_ms, 1) if incremental_stats and incremental_stats.llm_first_token_ms is not None else None,
                    "first_text_chunk_ms": round(incremental_stats.first_text_chunk_ms, 1) if incremental_stats and incremental_stats.first_text_chunk_ms is not None else None,
                    "tts_ttfa_ms": round(metrics.tts_ttfa_ms, 1) if stream_stats.ttfa_ms is not None else None,
                    "first_audio_sent_ms": round(stream_stats.first_audio_sent_ms, 1) if stream_stats.first_audio_sent_ms is not None else None,
                    "tts_total_ms": round(metrics.tts_total_ms, 1),
                    "audio_send_ms": round(metrics.audio_send_ms, 1),
                    "audio_duration_ms": len(combined) / 32.0,
                    "end_to_first_audio_ms": round(metrics.end_to_end_ms, 1) if stream_stats.end_to_first_audio_ms is not None else None,
                    "total_turn_ms": round((time.perf_counter() - t_speech_end) * 1000.0, 1),
                },
            })

        if should_transfer:
            await self._emit("CALL_TRANSFERRED", {})
        if should_end:
            await self._emit("CALL_ENDED", {"reason": "ai_ended"})

        logger.info(
            "text_voice_turn_completed",
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
