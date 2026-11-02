"""LLM client behind a tiny interface, so the loop can run against Claude, a stub (CI and
smoke tests without an API key) or a scripted fake (unit tests).

Claude call settings (Claude Sonnet 5.5):
  * thinking: omitted -> adaptive thinking (the model's default); its raw reasoning is not
    returned, and we append response content unchanged so thinking blocks stay valid.
  * effort "medium": the documented starting point for multi-step tool use; cheaper and
    faster than the default "high".
  * cache_control (automatic prompt caching): every loop iteration re-sends the same
    system prompt + tools + growing history, so the prefix is served from cache.
  * fallbacks "default": if Claude declines a request, the API re-runs it on a fallback
    model. The model that actually answered is recorded per call.
  * tool_choice "none" is used for the final wrap-up call (forced tool choice is not
    supported on this model; "none" is).
"""

import re
import time
from dataclasses import dataclass, field
from typing import Protocol

import anthropic

from app.config import get_settings

# USD per million tokens. Cache writes (5-min TTL) bill at 1.25x input, reads at 0.1x.
PRICES = {
    "claude-sonnet-5-5": {"in": 2.00, "out": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-haiku-5-5": {"in": 0.10, "out": 0.50, "cache_read": 0.01, "cache_write": 0.125},
    "claude-opus-5-5": {"in": 4.00, "out": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    "stub": {"in": 0.0, "out": 0.0, "cache_read": 0.0, "cache_write": 0.0},
}


class LLMError(Exception):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens

    def cost_usd(self, model: str) -> float:
        p = PRICES.get(model, PRICES["claude-sonnet-5-5"])
        return round(
            (
                self.input_tokens * p["in"]
                + self.output_tokens * p["out"]
                + self.cache_read_tokens * p["cache_read"]
                + self.cache_write_tokens * p["cache_write"]
            )
            / 1_000_000,
            6,
        )

    def as_dict(self) -> dict:
        return {
            "input": self.input_tokens,
            "output": self.output_tokens,
            "cache_read": self.cache_read_tokens,
            "cache_write": self.cache_write_tokens,
            "total": self.total,
        }


@dataclass
class ToolUse:
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    content: list[dict]  # raw assistant content blocks, appended to history unchanged
    text: str
    tool_uses: list[ToolUse]
    stop_reason: str
    model: str
    usage: Usage
    ms: int = 0


class LLM(Protocol):
    model: str

    def create(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        *,
        allow_tools: bool = True,
        timeout_s: float = 60.0,
    ) -> LLMResponse: ...


class AnthropicLLM:
    def __init__(self, model: str | None = None, effort: str = "medium"):
        s = get_settings()
        self.model = model or s.anthropic_model
        self.effort = effort
        # SDK retries 429/5xx/connection errors twice with backoff: bounded by design.
        self.client = anthropic.Anthropic(api_key=s.anthropic_api_key or None, max_retries=2)

    def create(self, system, messages, tools, *, allow_tools=True, timeout_s=60.0):
        t0 = time.monotonic()
        try:
            resp = self.client.with_options(timeout=max(timeout_s, 1.0)).beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=messages,
                tools=tools,
                tool_choice={"type": "auto" if allow_tools else "none"},
                output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.APIError as e:
            raise LLMError(f"{type(e).__name__}: {getattr(e, 'message', e)}") from e

        u = resp.usage
        return LLMResponse(
            content=[b.to_dict() for b in resp.content],
            text="\n".join(b.text for b in resp.content if b.type == "text").strip(),
            tool_uses=[
                ToolUse(b.id, b.name, dict(b.input)) for b in resp.content if b.type == "tool_use"
            ],
            stop_reason=resp.stop_reason or "",
            model=resp.model,
            usage=Usage(
                input_tokens=u.input_tokens or 0,
                output_tokens=u.output_tokens or 0,
                cache_read_tokens=u.cache_read_input_tokens or 0,
                cache_write_tokens=u.cache_creation_input_tokens or 0,
            ),
            ms=int((time.monotonic() - t0) * 1000),
        )


@dataclass
class StubLLM:
    """Deterministic stand-in for Claude, used by CI and the compose smoke test.

    First turn: searches for the question. Next turn: answers by quoting the first
    retrieved source. It exercises the real loop, tools, DB and citation checks without
    an API key; it is not meant to give good answers.
    """

    model: str = "stub"
    calls: int = field(default=0)

    def create(self, system, messages, tools, *, allow_tools=True, timeout_s=60.0):
        self.calls += 1
        last = messages[-1]["content"]
        tool_text = (
            " ".join(
                b["content"] for b in last if isinstance(b, dict) and b.get("type") == "tool_result"
            )
            if isinstance(last, list)
            else ""
        )
        if allow_tools and not tool_text:
            question = last if isinstance(last, str) else ""
            query = question.split("Question:")[-1].strip()[:300] or "overview"
            content = [
                {
                    "type": "tool_use",
                    "id": f"stub_{self.calls}",
                    "name": "search_documents",
                    "input": {"query": query},
                }
            ]
            return LLMResponse(
                content,
                "",
                [ToolUse(f"stub_{self.calls}", "search_documents", {"query": query})],
                "tool_use",
                self.model,
                Usage(input_tokens=1, output_tokens=1),
            )
        m = re.search(r'source="([^"]+)"', tool_text)
        text = (
            f"Stub answer: the most relevant passage found is [{m.group(1)}]."
            if m
            else "Stub answer: the documents do not contain relevant evidence."
        )
        return LLMResponse(
            [{"type": "text", "text": text}],
            text,
            [],
            "end_turn",
            self.model,
            Usage(input_tokens=1, output_tokens=1),
        )


def make_llm() -> LLM:
    s = get_settings()
    if s.llm_provider == "stub":
        return StubLLM()
    if not s.anthropic_api_key:
        raise LLMError("ANTHROPIC_API_KEY is not set (or set LLM_PROVIDER=stub)")
    return AnthropicLLM(effort=s.llm_effort)
