"""LLM gateway used by every agent: `live` | `mock` | `replay`.

Agents ask for a logical model role (`fast`, `reason`, `guard`); the provider's model ids live only in
configuration (MODEL_FAST, MODEL_REASON, MODEL_GUARD) and are used at the HTTP call.

- mock:   deterministic fixtures supplied by the calling agent. No network.
- replay: responses recorded from earlier live runs (packs/<pack>/data/replay_cache.json).
          On a cache miss it falls back to the mock fixture and marks the result `replay_miss`.
- live:   OpenAI SDK pointed at an OpenAI-compatible endpoint (LLM_BASE_URL), with a per-role token
          bucket, a SQLite response cache keyed by sha256(role + messages), and retry-after on 429.

Every agent answer is JSON validated by a pydantic model: one retry on failure, then LLMError,
which the orchestrator turns into "route to a human with the reason".
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from core.settings import Settings
from core.store import Store

MockFn = Callable[[list[dict]], dict]
ToolHook = Callable[[str, dict, Any, int], None]


class LLMError(Exception):
    """The model could not produce a valid answer; the case must go to a human."""


@dataclass
class Tool:
    """A plain Python function exposed to the model through OpenAI-style tool calling."""

    name: str
    description: str
    parameters: dict
    fn: Callable[..., Any]

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


@dataclass
class Completion:
    content: str | None
    tool_calls: list[dict]
    model: str
    tokens: int
    latency_ms: int
    source: str  # live | cache | mock | replay | replay_miss


@dataclass
class AgentResult:
    data: Any
    model: str
    tokens: int
    latency_ms: int
    sources: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    degraded: str | None = None  # set when tools were run locally after the model's tool call was rejected

    @property
    def replay_miss(self) -> bool:
        return "replay_miss" in self.sources

    @property
    def source(self) -> str:
        return "replay_miss" if self.replay_miss else (self.sources[-1] if self.sources else "mock")


def cache_key(model: str, messages: list[dict]) -> str:
    blob = model + json.dumps(messages, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def estimate_tokens(messages: list[dict]) -> int:
    return sum(len(str(m.get("content") or "")) for m in messages) // 4 + 60


def parse_json(text: str | None) -> Any:
    if not text or not text.strip():
        raise ValueError("empty reply")
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in reply")
    return json.loads(body[start : end + 1])


def _short(err: Exception, limit: int = 220) -> str:
    return str(err).replace("\n", " ")[:limit]


class TokenBucket:
    """Requests-per-minute and tokens-per-minute bucket for one model."""

    def __init__(self, rpm: int, tpm: int):
        self.rpm, self.tpm = max(rpm, 1), max(tpm, 1)
        self._req, self._tok = float(self.rpm), float(self.tpm)
        self._t = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        dt, self._t = now - self._t, now
        self._req = min(self.rpm, self._req + dt * self.rpm / 60)
        self._tok = min(self.tpm, self._tok + dt * self.tpm / 60)

    def acquire(self, tokens: int, max_wait_s: float = 90) -> float:
        tokens = min(tokens, self.tpm)
        waited = 0.0
        while True:
            with self._lock:
                self._refill()
                if self._req >= 1 and self._tok >= tokens:
                    self._req -= 1
                    self._tok -= tokens
                    return waited
                need = max((1 - self._req) * 60 / self.rpm, (tokens - self._tok) * 60 / self.tpm, 0.05)
            if waited >= max_wait_s:
                raise LLMError("local throttle wait exceeded")
            step = min(need, 2.0)
            time.sleep(step)
            waited += step


class ReplayCache:
    """Committed file of redacted, synthetic responses recorded from earlier live runs."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self._data = {"version": 1, "entries": {}}
        self._data.setdefault("entries", {})

    def get(self, key: str) -> dict | None:
        return self._data["entries"].get(key)

    def put(self, key: str, entry: dict) -> None:
        with self._lock:
            self._data["entries"][key] = entry
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=1, sort_keys=True, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)

    def __len__(self) -> int:
        return len(self._data["entries"])


class LLMClient:
    def __init__(self, settings: Settings, store: Store, replay_path: Path):
        self.settings = settings
        self.mode = settings.llm_mode
        self.store = store
        self.replay = ReplayCache(replay_path)
        self._buckets: dict[str, TokenBucket] = {}
        self._sdk_client = None
        self._lock = threading.Lock()

    # ---------- low level ----------
    def _bucket(self, role: str) -> TokenBucket:
        with self._lock:
            if role not in self._buckets:
                tpm = 15000 if role == "guard" else self.settings.llm_tpm
                self._buckets[role] = TokenBucket(self.settings.llm_rpm, tpm)
            return self._buckets[role]

    def _sdk(self):
        if self._sdk_client is None:
            from openai import OpenAI

            # Behind an authenticating proxy the real key is injected in transit; a placeholder is enough.
            self._sdk_client = OpenAI(
                base_url=self.settings.llm_base_url,
                api_key=self.settings.llm_api_key or "proxy-injected-key",
                max_retries=0,
                timeout=self.settings.llm_timeout_s,
            )
        return self._sdk_client

    def _mock(self, model: str, messages: list[dict], mock: MockFn, source: str) -> Completion:
        t0 = time.perf_counter()
        out = mock(messages) or {}
        calls = [
            {"id": f"call_mock_{i}_{c['name']}", "name": c["name"], "arguments": json.dumps(c.get("arguments", {}), sort_keys=True)}
            for i, c in enumerate(out.get("tool_calls", []))
        ]
        content = out.get("content")
        if isinstance(content, (dict, list)):
            content = json.dumps(content, ensure_ascii=False)
        return Completion(content, calls, model, estimate_tokens(messages), int((time.perf_counter() - t0) * 1000), source)

    def complete(
        self,
        *,
        model: str,
        messages: list[dict],
        mock: MockFn,
        tools: list[dict] | None = None,
        max_tokens: int = 1200,
        json_mode: bool = False,
        reasoning_effort: str | None = None,
        force_mock: bool = False,
    ) -> Completion:
        mode = "mock" if force_mock else self.mode
        if mode == "mock":
            return self._mock(model, messages, mock, "mock")
        key = cache_key(model, messages)
        if mode == "replay":
            hit = self.replay.get(key)
            if hit and hit.get("error"):
                raise LLMError(hit["error"])
            if hit:
                return Completion(hit.get("content"), hit.get("tool_calls", []), model, hit.get("tokens", 0), 0, "replay")
            return self._mock(model, messages, mock, "replay_miss")
        cached = self.store.cache_get(key)
        if cached:
            comp = Completion(cached.get("content"), cached.get("tool_calls", []), model, cached.get("tokens", 0), 0, "cache")
        else:
            try:
                comp = self._call_live(model, messages, tools, max_tokens, json_mode, reasoning_effort)
            except LLMError as err:
                if self.settings.replay_record and "rejected the call (400)" in str(err):
                    self.replay.put(key, {"model": model, "error": str(err)})  # so replay takes the same degrade path
                raise
            self.store.cache_put(key, model, {"content": comp.content, "tool_calls": comp.tool_calls, "tokens": comp.tokens})
        if self.settings.replay_record:
            self.replay.put(key, {"model": model, "content": comp.content, "tool_calls": comp.tool_calls, "tokens": comp.tokens})
        return comp

    def _call_live(self, role, messages, tools, max_tokens, json_mode, reasoning_effort) -> Completion:
        from openai import APIConnectionError, APIStatusError, InternalServerError, RateLimitError

        kwargs: dict[str, Any] = {"model": self.settings.model_id(role), "messages": messages, "max_tokens": max_tokens}
        if tools:
            kwargs["tools"] = tools
        elif json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if reasoning_effort and self.settings.reasoning_effort:  # LLM_REASONING_EFFORT= (empty) for models without it
            kwargs["reasoning_effort"] = self.settings.reasoning_effort
        self._bucket(role).acquire(estimate_tokens(messages) + 300)
        attempt = 0
        while True:
            attempt += 1
            t0 = time.perf_counter()
            try:
                resp = self._sdk().chat.completions.create(**kwargs)
            except RateLimitError as err:
                if attempt > 3:
                    raise LLMError(f"LLM provider rate limit (429) persisted after {attempt} attempts") from err
                time.sleep(_retry_after(err, attempt))
                continue
            except (APIConnectionError, InternalServerError) as err:
                if attempt > 2:
                    raise LLMError(f"LLM provider unavailable: {type(err).__name__}") from err
                time.sleep(1.5 * attempt)
                continue
            except APIStatusError as err:
                # Models occasionally emit a malformed tool call (the provider answers 400 "tool call
                # validation failed"); the same request usually succeeds on a fresh sample, so allow two retries.
                if attempt > (3 if err.status_code == 400 else 1):
                    raise LLMError(f"LLM provider rejected the call ({err.status_code}): {_short(err)}") from err
                time.sleep(0.5 * attempt)
                continue
            msg = resp.choices[0].message
            calls = [
                {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments or "{}"}
                for tc in (msg.tool_calls or [])
            ]
            tokens = resp.usage.total_tokens if resp.usage else 0
            return Completion(msg.content, calls, role, tokens or 0, int((time.perf_counter() - t0) * 1000), "live")

    # ---------- agent loop ----------
    def run_agent(self, *, fallback_calls: list[tuple[str, dict]] | None = None, **kwargs) -> AgentResult:
        """Run one agent turn. If the provider rejects the model's tool call (HTTP 400), run `fallback_calls`
        locally with the same allow-listed tools and ask once more without tools."""
        try:
            return self._run_agent(**kwargs)
        except LLMError as err:
            tools = kwargs.get("tools")
            if not (tools and fallback_calls) or "rejected the call (400)" not in str(err):
                raise
            by_name = {t.name: t for t in tools}
            outputs = {}
            for i, (name, args) in enumerate(fallback_calls):
                call = {"id": f"local_{i}", "name": name, "arguments": json.dumps(args, sort_keys=True)}
                outputs[name] = self._run_tool(call, by_name, kwargs.get("on_tool"))
            retry = dict(kwargs, tools=None, user=kwargs["user"] + "\n\nTOOL RESULTS (the tools were run for you; do not call tools):\n"
                         + json.dumps(outputs, ensure_ascii=False, sort_keys=True))
            result = self._run_agent(**retry)
            result.degraded = f"tool call rejected by the provider; {len(fallback_calls)} allow-listed tool(s) run locally"
            return result

    def _run_agent(
        self,
        *,
        agent: str,
        model: str,
        system: str,
        user: str,
        schema: type[BaseModel],
        mock: MockFn,
        tools: list[Tool] | None = None,
        on_tool: ToolHook | None = None,
        max_tool_rounds: int = 3,
        max_tokens: int = 1200,
        reasoning_effort: str | None = "low",
        force_mock: bool = False,
    ) -> AgentResult:
        """Run one agent turn: optional tool-calling rounds, then a JSON answer validated by `schema`."""
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        by_name = {t.name: t for t in (tools or [])}
        tool_schemas = [t.schema() for t in tools] if tools else None
        result = AgentResult(data=None, model=model, tokens=0, latency_ms=0)
        retries_left, rounds = 1, 0
        while True:
            comp = self.complete(
                model=model, messages=messages, mock=mock, tools=tool_schemas, max_tokens=max_tokens,
                json_mode=not tools, reasoning_effort=reasoning_effort, force_mock=force_mock,
            )
            result.tokens += comp.tokens
            result.latency_ms += comp.latency_ms
            result.sources.append(comp.source)
            if comp.tool_calls and tools and rounds < max_tool_rounds:
                rounds += 1
                messages.append({
                    "role": "assistant",
                    "content": comp.content or None,
                    "tool_calls": [
                        {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                        for c in comp.tool_calls
                    ],
                })
                for call in comp.tool_calls:
                    output = self._run_tool(call, by_name, on_tool)
                    result.tool_calls.append({"name": call["name"], "arguments": call["arguments"]})
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(output, ensure_ascii=False)})
                continue
            try:
                result.data = schema.model_validate(parse_json(comp.content))
                return result
            except (ValueError, ValidationError) as err:
                if retries_left == 0:
                    raise LLMError(f"{agent}: reply failed schema validation after one retry ({_short(err, 160)})") from err
                retries_left -= 1
                messages.append({"role": "assistant", "content": comp.content or ""})
                messages.append({
                    "role": "user",
                    "content": f"That reply was not valid JSON for the required schema ({_short(err, 160)}). "
                    "Reply again with only the JSON object.",
                })

    @staticmethod
    def _run_tool(call: dict, by_name: dict[str, Tool], on_tool: ToolHook | None) -> Any:
        t0 = time.perf_counter()
        tool = by_name.get(call["name"])
        try:
            args = json.loads(call["arguments"] or "{}")
        except json.JSONDecodeError:
            args = {}
        if tool is None:
            output: Any = {"error": f"tool '{call['name']}' is not on this agent's allow-list"}
        else:
            try:
                output = tool.fn(**args)
            except Exception as err:  # tools are deterministic; surface errors to the model, never crash
                output = {"error": f"{type(err).__name__}: {_short(err, 120)}"}
        if on_tool:
            on_tool(call["name"], args, output, int((time.perf_counter() - t0) * 1000))
        return output


def _retry_after(err: Exception, attempt: int) -> float:
    headers = getattr(getattr(err, "response", None), "headers", {}) or {}
    raw = headers.get("retry-after")
    try:
        return min(max(float(raw), 0.5), 30.0)
    except (TypeError, ValueError):
        return min(2.0 ** attempt, 30.0)
