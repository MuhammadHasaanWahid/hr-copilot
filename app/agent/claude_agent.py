"""Claude-powered planner: a manual tool-use loop on the Messages API.

Why a hand-written loop (not a hosted agent runtime): every tool call must pass
through our ToolGateway in-process (authorisation, risk tiers, audit), the
enterprise data never leaves our network except as minimised tool results, and
the same loop can be swapped for the offline planner when the LLM is down.

Provider: the Anthropic API directly (default) or Claude on Amazon Bedrock
(HR_LLM_PROVIDER=bedrock) for an enterprise deployment under the company's own
cloud contract and region. Same SDK, different client class.
"""
from __future__ import annotations

import json
from collections.abc import Callable

import anthropic

from ..config import Settings
from ..security import Principal
from .llm_common import CircuitBreaker, LLMUnavailable
from .prompts import STATIC_SYSTEM, user_context

__all__ = ["ClaudeAgent", "LLMUnavailable", "CircuitBreaker", "make_client", "serialize_messages"]


def make_client(settings: Settings):
    if settings.llm_provider == "bedrock":
        return anthropic.AnthropicBedrockMantle(aws_region=settings.aws_region, max_retries=2, timeout=120)
    return anthropic.Anthropic(max_retries=2, timeout=120)


def serialize_messages(messages: list) -> list:
    """Make the conversation JSON-storable. Content blocks (incl. thinking blocks
    with their signatures) are dumped unchanged so they can be replayed."""
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, list):
            content = [b.model_dump(mode="json", exclude_none=True) if hasattr(b, "model_dump") else b for b in content]
        out.append({"role": m["role"], "content": content})
    return out


class ClaudeAgent:
    name = "claude"

    def __init__(self, settings: Settings, tool_defs: list[dict], client=None) -> None:
        self.settings = settings
        self.tool_defs = tool_defs
        self.client = client or make_client(settings)
        self.breaker = CircuitBreaker()
        m = settings.claude_model
        self.model = f"anthropic.{m}" if settings.llm_provider == "bedrock" and not m.startswith("anthropic.") else m

    def _create(self, system: list, messages: list):
        kwargs = dict(model=self.model, max_tokens=16000, system=system, tools=self.tool_defs, messages=messages,
                      thinking={"type": "adaptive"}, output_config={"effort": self.settings.effort})
        if self.settings.llm_provider == "bedrock":
            return self.client.messages.create(**kwargs)
        # Server-side refusal fallback: a false-positive safety refusal is retried on a fallback model.
        return self.client.beta.messages.create(**kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default")

    def run(self, principal: Principal, history: list, user_text: str,
            call_tool: Callable[[str, dict], dict]) -> tuple[str, dict, list]:
        """Returns (reply_text, usage, new_history). `history` is not mutated, so a
        failed turn leaves the stored conversation untouched (append-only)."""
        if not self.breaker.allow():
            raise LLMUnavailable("AI provider temporarily disabled after repeated errors (circuit breaker open)")
        messages = list(history) + [{"role": "user", "content": user_text}]
        system = [
            {"type": "text", "text": STATIC_SYSTEM, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": user_context(principal, self.settings.today, self.settings.autonomy)},
        ]
        usage = {"input_tokens": 0, "output_tokens": 0}
        text = ""
        for _ in range(self.settings.max_agent_steps):
            try:
                resp = self._create(system, messages)
                self.breaker.success()
            except anthropic.AuthenticationError as exc:
                self.breaker.failure()
                raise LLMUnavailable("Anthropic API key rejected") from exc
            except anthropic.RateLimitError as exc:
                self.breaker.failure()
                raise LLMUnavailable("Rate limited by the AI provider") from exc
            except anthropic.APIStatusError as exc:
                self.breaker.failure()
                raise LLMUnavailable(f"AI provider error {exc.status_code}") from exc
            except anthropic.APIConnectionError as exc:
                self.breaker.failure()
                raise LLMUnavailable("Cannot reach the AI provider") from exc

            u = resp.usage
            usage["input_tokens"] += (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) \
                + (getattr(u, "cache_creation_input_tokens", 0) or 0)
            usage["output_tokens"] += u.output_tokens or 0
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "tool_use":
                results = []
                for block in resp.content:
                    if block.type != "tool_use":
                        continue
                    out = call_tool(block.name, dict(block.input or {}))
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": json.dumps(out, default=str),
                                    "is_error": out.get("status") in ("error", "denied")})
                messages.append({"role": "user", "content": results})  # all results in ONE message
                continue
            if resp.stop_reason == "pause_turn":
                continue
            if resp.stop_reason == "refusal":
                text = "I can't help with that request. If you think this is a mistake, please contact HR directly."
                break
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            if resp.stop_reason == "max_tokens":
                text += "\n\n_(Response truncated.)_"
            break
        else:
            text = ("I could not finish this request within the allowed number of steps. What I found so far is "
                    "in the reasoning trace; please narrow the question.")
            messages.append({"role": "assistant", "content": text})
        return text or "(no answer)", usage, serialize_messages(messages)
