"""
LLM provider abstraction with first-class support for tool calling and streaming.
Groq provides low-latency inference for real-time conversational telephony.
"""
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncGenerator, Optional

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("voice.llm")


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMMetrics:
    ttft_ms: float = 0.0  # time to first token
    total_latency_ms: float = 0.0
    token_count: int = 0


@dataclass
class LLMResponse:
    content: Optional[str]
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    raw: dict = field(default_factory=dict)
    metrics: LLMMetrics = field(default_factory=LLMMetrics)


class LLMProvider(ABC):
    provider_name: str = "base"

    @abstractmethod
    async def chat(
        self,
        *,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.4,
        max_tokens: int = 500,
    ) -> LLMResponse:
        ...

    @abstractmethod
    async def chat_stream(
        self,
        *,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.4,
        max_tokens: int = 500,
    ) -> AsyncGenerator[str, None]:
        ...

    @abstractmethod
    async def structured_completion(self, *, messages: list[dict], json_schema: dict) -> dict:
        ...


class MockLLMProvider(LLMProvider):
    """Deterministic canned responses for local dev/tests."""

    provider_name = "mock"

    async def chat(
        self,
        *,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.4,
        max_tokens: int = 500,
    ) -> LLMResponse:
        t0 = time.perf_counter()
        if messages and messages[-1].get("role") == "tool":
            return LLMResponse(
                content="I checked the calendar - tomorrow at 10 AM works. Shall I book it?",
                metrics=LLMMetrics(ttft_ms=1.0, total_latency_ms=1.5, token_count=15),
            )
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        text = str(last_user).lower()
        if "meeting" in text or "demo" in text:
            if tools:
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCall(id="call_1", name="check_calendar", arguments={"duration_minutes": 30})],
                    metrics=LLMMetrics(ttft_ms=1.0, total_latency_ms=1.5),
                )
            return LLMResponse(
                content="Sure, I can set up a quick demo call for you - what day works best?",
                metrics=LLMMetrics(ttft_ms=1.0, total_latency_ms=1.5, token_count=16),
            )
        if "not interested" in text or "no thanks" in text:
            return LLMResponse(
                content="No problem at all, thanks for your time. Have a great day!",
                metrics=LLMMetrics(ttft_ms=1.0, total_latency_ms=1.5, token_count=13),
            )
        content = "Namaste! Main VoxAI se bol rahi hoon. Hum aapke business ke liye AI voice automation provide karte hain. Pricing ₹5000 se shuru hoti hai."
        elapsed = (time.perf_counter() - t0) * 1000.0
        return LLMResponse(
            content=content,
            metrics=LLMMetrics(ttft_ms=elapsed * 0.5, total_latency_ms=elapsed, token_count=25),
        )

    async def chat_stream(
        self,
        *,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.4,
        max_tokens: int = 500,
    ) -> AsyncGenerator[str, None]:
        resp = await self.chat(messages=messages, tools=tools, temperature=temperature, max_tokens=max_tokens)
        if resp.content:
            words = resp.content.split()
            for w in words:
                yield f"{w} "

    async def structured_completion(self, *, messages: list[dict], json_schema: dict) -> dict:
        return {
            "summary": "Lead expressed interest and asked about pricing and product capabilities.",
            "intent": "pricing_inquiry",
            "lead_status": "interested",
            "interest_level": "high",
            "sentiment": "positive",
            "requirements": ["AI voice calling", "CRM integration"],
            "objections": [],
            "questions": ["What is the pricing?"],
            "action_items": ["Send product pricing deck"],
            "meeting_requested": False,
            "follow_up_required": True,
            "next_action": "follow_up_call",
            "confidence": 0.85,
        }


class GroqLLMProvider(LLMProvider):
    """Real Groq adapter with streaming and tool call parsing."""

    provider_name = "groq"

    def __init__(self) -> None:
        from groq import AsyncGroq  # imported lazily

        self._client = AsyncGroq(api_key=settings.GROQ_API_KEY)

    def _to_groq_tools(self, tools: Optional[list[dict]]) -> Optional[list[dict]]:
        if not tools:
            return None
        return [{"type": "function", "function": t} for t in tools]

    async def chat(
        self,
        *,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.4,
        max_tokens: int = 500,
    ) -> LLMResponse:
        t0 = time.perf_counter()
        resp = await self._client.chat.completions.create(
            model=settings.GROQ_MODEL,
            messages=messages,
            tools=self._to_groq_tools(tools),
            temperature=temperature,
            max_tokens=max_tokens,
        )
        total_latency = (time.perf_counter() - t0) * 1000.0

        choice = resp.choices[0]
        tool_calls = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=json.loads(tc.function.arguments or "{}"),
                    )
                )

        tokens = resp.usage.total_tokens if resp.usage else 0
        metrics = LLMMetrics(
            ttft_ms=total_latency * 0.4,  # conservative estimate for non-streaming
            total_latency_ms=total_latency,
            token_count=tokens,
        )

        return LLMResponse(
            content=choice.message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            raw=resp.model_dump(),
            metrics=metrics,
        )

    async def chat_stream(
        self,
        *,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.4,
        max_tokens: int = 500,
    ) -> AsyncGenerator[str, None]:
        stream = await self._client.chat.completions.create(
            model=settings.GROQ_MODEL,
            messages=messages,
            tools=self._to_groq_tools(tools),
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield delta.content

    async def structured_completion(self, *, messages: list[dict], json_schema: dict) -> dict:
        system = {
            "role": "system",
            "content": (
                "Respond with ONLY a single JSON object matching this schema, no prose, "
                f"no markdown fences: {json.dumps(json_schema)}"
            ),
        }
        resp = await self._client.chat.completions.create(
            model=settings.GROQ_MODEL,
            messages=[system] + messages,
            temperature=0.1,
            max_tokens=800,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or "{}"
        return json.loads(content)


def get_llm_provider(mode: Optional[str] = None) -> LLMProvider:
    selected = mode or settings.LLM_PROVIDER_MODE
    if selected == "groq" and settings.GROQ_API_KEY:
        return GroqLLMProvider()
    return MockLLMProvider()
