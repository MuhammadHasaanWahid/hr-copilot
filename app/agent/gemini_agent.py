"""Gemini-powered planner: a manual function-calling loop (google-genai SDK).

Same contract as the Claude planner: every function call the model makes is
executed by OUR ToolGateway (authorisation, risk tiers, audit) - automatic
function calling is disabled on purpose, so the SDK never runs anything itself.

Free-tier aware: the free Gemini API has low per-model rate limits, so the
agent walks a list of models (HR_GEMINI_MODEL + HR_GEMINI_FALLBACK_MODELS).
A model that returns 429 (quota) or 404 (not available) is skipped for a
cool-down period; if every model is exhausted the orchestrator degrades to the
deterministic offline planner.

Data note: under Google's terms, prompts sent on the *unpaid* tier may be used
to improve Google products. This demo only sends synthetic data. Real HR data
requires a paid (billing-enabled) project or Vertex AI - see docs/PRIVACY.md.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable

import httpx
from google import genai
from google.genai import errors, types

from ..config import Settings
from ..security import Principal
from .llm_common import CircuitBreaker, LLMUnavailable
from .prompts import STATIC_SYSTEM, user_context

log = logging.getLogger("hrcopilot.gemini")
BLOCKED = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "MODEL_ARMOR"}


def make_gemini_client() -> genai.Client:
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    retry = types.HttpRetryOptions(attempts=2, http_status_codes=[500, 502, 503, 504])
    return genai.Client(api_key=key, http_options=types.HttpOptions(timeout=120_000, retry_options=retry))


def _to_content(item) -> types.Content:
    return item if isinstance(item, types.Content) else types.Content.model_validate(item)


def _dump(contents: list) -> list:
    return [c.model_dump(mode="json", exclude_none=True) if hasattr(c, "model_dump") else c for c in contents]


class GeminiAgent:
    name = "gemini"

    def __init__(self, settings: Settings, tool_defs: list[dict], client=None) -> None:
        self.settings = settings
        self.models = settings.gemini_models
        self.model = self.models[0]
        self.client = client or make_gemini_client()
        self.breaker = CircuitBreaker()
        self.decls = [types.FunctionDeclaration(name=t["name"], description=t["description"],
                                                parameters_json_schema=t["input_schema"]) for t in tool_defs]
        self._cooldown: dict[str, float] = {}   # model -> monotonic time until it may be tried again
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ calls
    def _available_models(self) -> list[str]:
        now = time.monotonic()
        return [m for m in self.models if self._cooldown.get(m, 0) <= now]

    def _skip(self, model: str, seconds: float, why: str) -> None:
        with self._lock:
            self._cooldown[model] = time.monotonic() + seconds
        log.warning("gemini_model_skipped", extra={"model": model, "reason": why, "seconds": seconds})

    def _generate(self, contents: list, config: types.GenerateContentConfig):
        if not self.breaker.allow():
            raise LLMUnavailable("AI provider temporarily disabled after repeated errors (circuit breaker open)")
        candidates = self._available_models()
        if not candidates:
            raise LLMUnavailable("all configured Gemini models are rate-limited (free-tier quota)")
        last = "no model available"
        for model in candidates:
            try:
                resp = self.client.models.generate_content(model=model, contents=contents, config=config)
                self.breaker.success()
                self.model = model
                return resp
            except errors.ClientError as exc:
                code = getattr(exc, "code", None)
                if code == 429:
                    daily = "day" in str(exc).lower()
                    self._skip(model, 3600 if daily else 60, "quota")
                    last = f"rate limit / quota on {model}"
                    continue
                if code == 404:
                    self._skip(model, 86400, "model not found")
                    last = f"model {model} not available"
                    continue
                if code in (401, 403):
                    self.breaker.failure()
                    raise LLMUnavailable("Gemini API key rejected or not permitted") from exc
                raise  # 400 etc. - handled by run()
            except errors.ServerError as exc:
                self.breaker.failure()
                last = f"server error {getattr(exc, 'code', '')} on {model}"
                continue
            except (httpx.HTTPError, OSError) as exc:
                self.breaker.failure()
                raise LLMUnavailable("Cannot reach the Gemini API") from exc
        raise LLMUnavailable(last)

    # -------------------------------------------------------------------- loop
    def run(self, principal: Principal, history: list, user_text: str,
            call_tool: Callable[[str, dict], dict]) -> tuple[str, dict, list]:
        """Returns (reply_text, usage, new_history). `history` is not mutated."""
        config = types.GenerateContentConfig(
            system_instruction=STATIC_SYSTEM + "\n\n" + user_context(principal, self.settings.today, self.settings.autonomy),
            tools=[types.Tool(function_declarations=self.decls)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            max_output_tokens=8192,
        )
        user_msg = types.Content(role="user", parts=[types.Part.from_text(text=user_text)])
        contents = [_to_content(c) for c in history] + [user_msg]
        try:
            return self._loop(contents, config, call_tool)
        except errors.ClientError as exc:
            # A history produced by another model (after a quota fallback) can be rejected
            # (e.g. thought-signature validation). Retry once with a fresh context.
            if history and getattr(exc, "code", None) == 400:
                log.warning("gemini_history_rejected_retrying_fresh")
                return self._loop([user_msg], config, call_tool)
            self.breaker.failure()
            raise LLMUnavailable(f"Gemini rejected the request ({getattr(exc, 'code', '?')})") from exc

    def _loop(self, contents: list, config, call_tool) -> tuple[str, dict, list]:
        usage = {"input_tokens": 0, "output_tokens": 0}
        text, malformed_retries = "", 0
        for _ in range(self.settings.max_agent_steps):
            resp = self._generate(contents, config)
            um = resp.usage_metadata
            if um:
                usage["input_tokens"] += um.prompt_token_count or 0
                usage["output_tokens"] += (um.candidates_token_count or 0) + (um.thoughts_token_count or 0)
            cand = resp.candidates[0] if resp.candidates else None
            reason = getattr(getattr(cand, "finish_reason", None), "name", None) if cand else None
            if not cand or not cand.content or not cand.content.parts:
                if reason == "MALFORMED_FUNCTION_CALL" and malformed_retries < 1:
                    malformed_retries += 1
                    contents.append(types.Content(role="user", parts=[types.Part.from_text(
                        text="[System] Your last function call was malformed. Call the tool again with valid arguments.")]))
                    continue
                if reason in BLOCKED or (resp.prompt_feedback and resp.prompt_feedback.block_reason):
                    text = "I can't help with that request. If you think this is a mistake, please contact HR directly."
                else:
                    text = "I couldn't produce an answer this time - please rephrase or try again."
                contents.append(types.Content(role="model", parts=[types.Part.from_text(text=text)]))
                break
            contents.append(cand.content)  # unchanged: keeps thought signatures for the next call
            calls = [p.function_call for p in cand.content.parts if p.function_call]
            if calls:
                parts = []
                for fc in calls:
                    out = call_tool(fc.name, dict(fc.args or {}))
                    payload = json.loads(json.dumps(out, default=str))
                    parts.append(types.Part(function_response=types.FunctionResponse(
                        id=fc.id, name=fc.name, response={"result": payload})))
                contents.append(types.Content(role="user", parts=parts))  # all results in ONE turn
                continue
            text = "".join(p.text for p in cand.content.parts if p.text and not p.thought).strip()
            if reason == "MAX_TOKENS":
                text += "\n\n_(Response truncated.)_"
            break
        else:
            text = ("I could not finish this request within the allowed number of steps. What I found so far is "
                    "in the reasoning trace; please narrow the question.")
            contents.append(types.Content(role="model", parts=[types.Part.from_text(text=text)]))
        return text or "(no answer)", usage, _dump(contents)
