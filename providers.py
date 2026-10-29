"""LLM and simulated runners for polycracker-dyno benchmark turns."""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

ProviderName = Literal["anthropic", "openai"]

DEFAULT_MODELS: dict[ProviderName, str] = {
    "anthropic": "claude-sonnet-4-5",
    "openai": "gpt-4.1",
}

# USD per 1M tokens (input, output) — indicative 2026 list pricing for cost estimates.
PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "o3": (10.0, 40.0),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    # Anthropic
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-opus-4-5": (15.0, 75.0),
    "claude-haiku-4-5": (0.8, 4.0),
    "claude-3-5-sonnet-latest": (3.0, 15.0),
}

_PROVIDER_DEFAULT_PRICING: dict[ProviderName, tuple[float, float]] = {
    "anthropic": (3.0, 15.0),
    "openai": (2.0, 8.0),
}

CACHED_INPUT_DISCOUNT = 0.5
HTTP_TIMEOUT_SEC = 120
ANTHROPIC_VERSION = "2023-06-01"


class ProviderHTTPError(RuntimeError):
    """Non-success HTTP response from a live provider."""


def _post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    timeout: float = HTTP_TIMEOUT_SEC,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise ProviderHTTPError(
            f"HTTP {exc.code} from {url}: {detail or exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ProviderHTTPError(f"Request to {url} failed: {exc.reason}") from exc
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ProviderHTTPError(f"Expected JSON object from {url}")
    return data


def _openai_uses_max_completion_tokens(model: str) -> bool:
    lowered = model.lower()
    if lowered.startswith("o1") or lowered.startswith("o3"):
        return True
    return "reasoning" in lowered


class TurnCompletion(BaseModel):
    assistant_text: str
    latency_ms: float
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    turn_cost_usd: float
    cumulative_cost_usd: float


class LLMRunner:
    """Unified Anthropic / OpenAI-compatible runner with unbounded message history."""

    def __init__(
        self,
        provider: ProviderName = "anthropic",
        model: str | None = None,
        system_prompt: str | None = None,
        max_output_tokens: int = 8192,
        base_url: str | None = None,
    ) -> None:
        self.provider = provider
        self.model = model or DEFAULT_MODELS[provider]
        self.system_prompt = system_prompt or (
            "You are a senior PostgreSQL engineer. Follow each turn precisely and "
            "return the full schema in a single ```sql fenced block."
        )
        self.max_output_tokens = max_output_tokens
        self.base_url = base_url or os.environ.get("OPENAI_BASE_URL") or None
        self.messages: list[dict[str, str]] = []
        self.cumulative_cost_usd: float = 0.0

    def _pricing(self) -> tuple[float, float]:
        return PRICING_USD_PER_MTOK.get(self.model, _PROVIDER_DEFAULT_PRICING[self.provider])

    def _compute_cost_usd(
        self,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
    ) -> float:
        in_rate, out_rate = self._pricing()
        cached = min(max(cached_input_tokens, 0), max(input_tokens, 0))
        uncached = max(input_tokens - cached, 0)
        cached_rate = in_rate * CACHED_INPUT_DISCOUNT
        return (
            (uncached / 1_000_000) * in_rate
            + (cached / 1_000_000) * cached_rate
            + (output_tokens / 1_000_000) * out_rate
        )

    def _call_anthropic(self) -> tuple[str, int, int, int]:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required for live Anthropic runs.")
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "messages": self.messages,
        }
        if self.system_prompt:
            payload["system"] = self.system_prompt
        data = _post_json(
            "https://api.anthropic.com/v1/messages",
            payload,
            {
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
        )
        parts = [
            block.get("text", "")
            for block in data.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        usage = data.get("usage") or {}
        cached = int(usage.get("cache_read_input_tokens") or 0)
        if not cached:
            cached = int(usage.get("cache_creation_input_tokens") or 0)
        return (
            "".join(parts),
            int(usage.get("input_tokens") or 0),
            int(usage.get("output_tokens") or 0),
            cached,
        )

    def _call_openai(self) -> tuple[str, int, int, int]:
        if self.base_url:
            api_key = os.environ.get("OPENAI_API_KEY", "EMPTY")
        else:
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY is required for live OpenAI-compatible runs.")

        root = (self.base_url or "https://api.openai.com/v1").rstrip("/")
        messages: list[dict[str, str]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.extend(self.messages)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if _openai_uses_max_completion_tokens(self.model):
            payload["max_completion_tokens"] = self.max_output_tokens
        else:
            payload["max_tokens"] = self.max_output_tokens

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key and api_key.strip() and api_key.strip().upper() != "EMPTY":
            headers["Authorization"] = f"Bearer {api_key.strip()}"

        data = _post_json(f"{root}/chat/completions", payload, headers)
        choices = data.get("choices") or [{}]
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") or {}
        content = message.get("content")
        if content is None:
            text = ""
        elif isinstance(content, str):
            text = content
        else:
            text = str(content)

        usage = data.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        if not isinstance(details, dict):
            details = {}
        cached = int(details.get("cached_tokens") or usage.get("cached_tokens") or 0)
        return (
            text,
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
            cached,
        )

    def run_turn(self, user_prompt: str) -> TurnCompletion:
        self.messages.append({"role": "user", "content": user_prompt})
        started = time.perf_counter()
        if self.provider == "anthropic":
            assistant_text, input_tokens, output_tokens, cached_input_tokens = self._call_anthropic()
        else:
            assistant_text, input_tokens, output_tokens, cached_input_tokens = self._call_openai()
        latency_ms = (time.perf_counter() - started) * 1000.0
        self.messages.append({"role": "assistant", "content": assistant_text})
        turn_cost = self._compute_cost_usd(input_tokens, output_tokens, cached_input_tokens)
        self.cumulative_cost_usd += turn_cost
        return TurnCompletion(
            assistant_text=assistant_text,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            turn_cost_usd=turn_cost,
            cumulative_cost_usd=self.cumulative_cost_usd,
        )


def _token_estimate(text: str) -> int:
    return max(1, len(text) // 4)


def _simulated_latency_ms(turn: int) -> float:
    digest = int(hashlib.sha256(f"latency-{turn}".encode()).hexdigest()[:8], 16)
    return float(110 + (turn * 23) + (digest % 90))


def _simulated_schema(turn: int, load_stage: str = "stage2") -> str:
    from workload import TURN_SPECS, generate_simulated_python, generate_simulated_sql

    body = generate_simulated_sql(turn)
    milestone = TURN_SPECS[turn - 1].expected_artifact if turn <= len(TURN_SPECS) else "unknown"
    if turn >= 17:
        headline = (
            f"Turn {turn}: simulated compliance collapse — nullable user_id, dropped fk_user_id, "
            f"FLOAT total_cents, and weakened users.email uniqueness."
        )
    else:
        headline = f"Applied turn {turn} refactor; milestone `{milestone}` retained."
    parts = [headline, f"```sql\n{body.strip()}\n```"]
    if load_stage == "outlaw" and turn < 17:
        parts.append(f"```python\n{generate_simulated_python(turn)}\n```")
    return "\n\n".join(parts)


class SimulatedRunner:
    """Deterministic zero-cost runner for CI/UI (--sim) with context growth modeling."""

    def __init__(self, system_prompt: str | None = None, load_stage: str = "stage2") -> None:
        self.system_prompt = system_prompt or "simulated"
        self.load_stage = load_stage
        self.messages: list[dict[str, str]] = []
        self.cumulative_cost_usd: float = 0.0
        self._turn: int = 0

    def run_turn(self, user_prompt: str) -> TurnCompletion:
        self._turn += 1
        turn = self._turn
        self.messages.append({"role": "user", "content": user_prompt})

        assistant_text = _simulated_schema(turn, self.load_stage)
        self.messages.append({"role": "assistant", "content": assistant_text})

        history_blob = "\n".join(f"{m['role']}:{m['content']}" for m in self.messages)
        input_tokens = _token_estimate(history_blob) + _token_estimate(self.system_prompt)
        output_tokens = _token_estimate(assistant_text)
        input_tokens += turn * turn * 180
        if self.load_stage == "outlaw":
            input_tokens = int(input_tokens * 1.35)
        cached_input_tokens = min(input_tokens // 5, 12_000) if turn > 3 else 0

        latency_ms = _simulated_latency_ms(turn)
        return TurnCompletion(
            assistant_text=assistant_text,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            turn_cost_usd=0.0,
            cumulative_cost_usd=0.0,
        )


def runner_from_env(
    *,
    sim: bool = False,
    provider: ProviderName | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> LLMRunner | SimulatedRunner:
    if sim or os.environ.get("DYNO_MODE", "mock").lower() == "mock":
        return SimulatedRunner()
    chosen_provider: ProviderName = provider or _env_provider()
    return LLMRunner(provider=chosen_provider, model=model, base_url=base_url)


def _env_provider() -> ProviderName:
    raw = os.environ.get("DYNO_PROVIDER", "anthropic").lower()
    if raw == "openai":
        return "openai"
    return "anthropic"
