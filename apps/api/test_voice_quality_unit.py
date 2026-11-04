from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.config import settings
from app.providers.voice.tts.kokoro import KokoroTTSProvider
from app.voice_pipeline.orchestrator import VoiceCallOrchestrator
from app.providers.voice.llm.base import LLMResponse, LLMMetrics, ToolCall
from app.providers.voice.stt.base import GroqWhisperSTTProvider, TranscriptionResult
from app.voice_pipeline.vad import SileroVADDetector
import asyncio


def test_hindi_and_english_speed_settings_are_independent():
    provider = KokoroTTSProvider()
    assert provider.language_speeds == {"hi": settings.KOKORO_HINDI_SPEED, "en": settings.KOKORO_ENGLISH_SPEED}
    assert provider.language_speeds == {"hi": 1.15, "en": 1.0}


def test_voice_and_language_mapping():
    provider = KokoroTTSProvider()
    assert provider._resolve_lang_and_voice("hi", None) == ("hi", "hf_alpha")
    assert provider._resolve_lang_and_voice("en", None) == ("en-us", "af_heart")
    assert GroqWhisperSTTProvider(language="hi").language == "hi"
    assert GroqWhisperSTTProvider(language="en").language == "en"
    assert KokoroTTSProvider._clause_language("We provide API integration.") == "en"
    assert KokoroTTSProvider._clause_language("हम API integration करते हैं।") == "hi"
    assert KokoroTTSProvider._clause_language("aap kaise hain") == "hi"


def test_auto_language_switching_and_ambiguous_acknowledgements():
    agent = _orchestrator("auto", _FakeSTT("unused"), _FakeLLM("ok"), [])
    assert agent._apply_turn_language("नमस्ते, आप कैसे हैं?", "hi") == "hi"
    assert agent._apply_turn_language("yes") == "hi"
    assert agent._apply_turn_language("ठीक है") == "hi"
    assert agent._apply_turn_language("Can you explain the API integration?", "en") == "en"
    assert agent._apply_turn_language("haan") == "en"
    assert agent._apply_turn_language("हाँ") == "en"
    assert agent._apply_turn_language("AI ke baare mein batao", "en") == "hinglish"
    assert agent._apply_turn_language("हम automation पर बात कर सकते हैं?", "hi") == "hinglish"


def test_action_turns_keep_tools_while_regular_questions_can_stream():
    requires_tools = VoiceCallOrchestrator._requires_action_tools
    assert not requires_tools("Can you explain the API integration?")
    assert not requires_tools("AI ke baare mein batao")
    assert requires_tools("Please schedule a demo for tomorrow")
    assert requires_tools("Please send it to me on WhatsApp")
    assert requires_tools("Meeting book kar do")


def test_hinglish_tts_routes_whole_clauses_in_order():
    class Engine:
        def __init__(self):
            self.calls = []

        def synthesize_clause(self, clause, voice, lang, speed):
            self.calls.append((clause, voice, lang, speed))
            return bytes([len(self.calls), 0])

    async def run():
        provider = KokoroTTSProvider()
        engine = Engine()
        provider._get_engine = lambda: engine
        chunks = [chunk async for chunk in provider.synthesize_stream(
            text="नमस्ते। We provide AI automation.", language="hinglish"
        )]
        assert [call[2] for call in engine.calls] == ["hi", "en-us"]
        assert [call[1] for call in engine.calls] == ["hf_alpha", "af_heart"]
        assert len(chunks) == 2
        assert chunks == [b"\x01\x00", b"\x02\x00"]

    asyncio.run(run())


def test_llm_phrase_stream_starts_audio_before_response_finishes():
    async def run():
        audio_sent = asyncio.Event()
        sent_chunks = []
        events = []

        class StreamingLLM(_FakeLLM):
            supports_incremental_text = True

            async def chat_stream_response(self, *, messages, tools=None, temperature=0.35, on_text_delta=None):
                on_text_delta("First phrase. ")
                await asyncio.wait_for(audio_sent.wait(), timeout=1.0)
                on_text_delta("Second phrase.")
                return LLMResponse("First phrase. Second phrase.", metrics=LLMMetrics(ttft_ms=1.0))

        class OrderedTTS(_FakeTTS):
            def __init__(self):
                self.calls = []

            async def synthesize_stream(self, *, text, **kwargs):
                self.calls.append(text)
                yield bytes([len(self.calls), 0])

        async def emit(name, payload):
            events.append((name, payload))

        tts = OrderedTTS()

        async def send_audio(chunk):
            sent_chunks.append(chunk)
            audio_sent.set()

        agent = VoiceCallOrchestrator(
            stt=_FakeSTT("unused"), llm=StreamingLLM("unused"), tts=tts,
            tool_executor=object(), context={"company": {"name": "VoxAI"}, "lead": {}, "knowledge": [], "previous_calls": []},
            on_event=emit, on_audio_chunk=send_audio, language="en",
        )
        result = await agent.handle_user_text("Can you explain the API?")
        assert tts.calls == ["First phrase.", "Second phrase."]
        assert sent_chunks == [b"\x01\x00", b"\x02\x00"]
        assert result.ai_text == "First phrase. Second phrase."
        assert [name for name, _ in events].count("AI_RESPONSE_STARTED") == 1
        assert [name for name, _ in events].count("AI_TEXT_DELTA") == 1
        metrics = next(payload["metrics"] for name, payload in events if name == "AI_RESPONSE_COMPLETED")
        assert metrics["first_text_chunk_ms"] is not None
        assert metrics["first_audio_sent_ms"] is not None

    asyncio.run(run())


def test_incremental_stream_cancels_generation_and_does_not_send_later_phrases():
    async def run():
        first_audio_sent = asyncio.Event()
        sent = []
        cancelled = []

        class WaitingLLM(_FakeLLM):
            supports_incremental_text = True

            async def chat_stream_response(self, *, messages, tools=None, temperature=0.35, on_text_delta=None):
                on_text_delta("First phrase. ")
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.append(True)
                return LLMResponse("unreachable")

        async def send_audio(chunk):
            sent.append(chunk)
            first_audio_sent.set()

        agent = VoiceCallOrchestrator(
            stt=_FakeSTT("unused"), llm=WaitingLLM("unused"), tts=_FakeTTS(),
            tool_executor=object(), context={"company": {"name": "VoxAI"}},
            on_audio_chunk=send_audio, language="en",
        )
        task = asyncio.create_task(agent._stream_llm_response_to_tts(
            messages=[], tools=None, speech_end_at=asyncio.get_running_loop().time()
        ))
        await asyncio.wait_for(first_audio_sent.wait(), timeout=1.0)
        agent.interrupt()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert cancelled == [True]
        assert len(sent) == 1

    asyncio.run(run())


def test_tool_call_response_is_not_spoken_as_customer_text():
    async def run():
        sent = []

        class ToolLLM(_FakeLLM):
            supports_incremental_text = True

            async def chat_stream_response(self, *, messages, tools=None, temperature=0.35, on_text_delta=None):
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCall("tool-1", "check_calendar", {})],
                    metrics=LLMMetrics(ttft_ms=2.0),
                )

        agent = VoiceCallOrchestrator(
            stt=_FakeSTT("unused"), llm=ToolLLM("unused"), tts=_FakeTTS(),
            tool_executor=object(), context={"company": {"name": "VoxAI"}},
            on_audio_chunk=lambda chunk: sent.append(chunk), language="en",
        )
        response, stats = await agent._stream_llm_response_to_tts(
            messages=[], tools=[], speech_end_at=None
        )
        assert response.tool_calls and not stats.chunks and not sent

    asyncio.run(run())


def test_invalid_transcripts_are_rejected_but_short_answers_are_kept():
    for text in ("", ".", "<|hi|>", "<|hi|><|hi|>"):
        assert not VoiceCallOrchestrator._is_meaningful_transcript(text)
    for text in ("हाँ", "haan", "no", "yes", "okay", "Thank you."):
        assert VoiceCallOrchestrator._is_meaningful_transcript(text)


class _FakeSTT:
    def __init__(self, text):
        self.text = text

    async def transcribe_chunk(self, audio_bytes, sample_rate=16000):
        return TranscriptionResult(self.text, None, True, "hi", 2.0)


class _FakeLLM:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        return LLMResponse(content=self.answer, metrics=LLMMetrics())


class _FakeTTS:
    async def synthesize_stream(self, **kwargs):
        yield b"\x00\x00" * 160


def _orchestrator(language, stt, llm, events):
    async def emit(name, payload):
        events.append((name, payload))
    return VoiceCallOrchestrator(
        stt=stt, llm=llm, tts=_FakeTTS(), tool_executor=object(),
        context={"company": {"name": "VoxAI"}, "lead": {}, "knowledge": [], "previous_calls": []},
        on_event=emit, on_audio_chunk=None, language=language,
    )


def test_invalid_stt_asks_for_repeat_without_calling_llm():
    events = []
    llm = _FakeLLM("should not be used")
    agent = _orchestrator("hi", _FakeSTT("<|hi|> ."), llm, events)
    result = asyncio.run(agent.handle_user_audio_chunk(b"\x00\x00" * 1000))
    assert result.ai_text and "फिर से" in result.ai_text
    assert llm.calls == 0
    assert [name for name, _ in events].count("AI_RESPONSE_STARTED") == 1


def test_greeting_once_and_single_response_with_clean_history():
    async def run():
        events = []
        llm = _FakeLLM("हाँ, मैं उपलब्ध हूँ। आप किस बारे में बात करना चाहेंगे?")
        agent = _orchestrator("hi", _FakeSTT("unused"), llm, events)
        await agent.opening_greeting()
        result = await agent.handle_user_text("हाँ, बात कर सकते हैं।")
        assistant_turns = [item["content"] for item in agent.conversation if item["role"] == "assistant"]
        assert len(assistant_turns) == 2
        assert assistant_turns[0].startswith("नमस्ते")
        assert result.ai_text == assistant_turns[1]
        assert llm.calls == 1
        assert [name for name, _ in events].count("AI_RESPONSE_STARTED") == 2
    asyncio.run(run())


def test_vad_enforces_maximum_utterance_duration():
    vad = SileroVADDetector(threshold=0.5, min_speech_duration_ms=64,
                            min_silence_duration_ms=64, max_utterance_duration_ms=128)
    vad._estimate_probability = lambda frame: 0.9
    speech = (b"\x01\x00" * 512) * 5
    events = vad.process_chunk(speech)
    assert sum(event.event_type == "SPEECH_STARTED" for event in events) == 1
    assert sum(event.event_type == "SPEECH_ENDED" for event in events) == 1
