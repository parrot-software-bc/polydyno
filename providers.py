"""LLM and simulated runners for polycracker-dyno benchmark turns."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field

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
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self.provider == "anthropic":
            import anthropic

            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise RuntimeError("ANTHROPIC_API_KEY is required for live Anthropic runs.")
            self._client = anthropic.Anthropic(api_key=api_key)
        else:
            import openai

            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY is required for live OpenAI-compatible runs.")
            kwargs: dict[str, Any] = {"api_key": api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = openai.OpenAI(**kwargs)
        return self._client

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

    @staticmethod
    def _cached_tokens_openai(usage: Any) -> int:
        if usage is None:
            return 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cached = getattr(details, "cached_tokens", None)
            if cached is not None:
                return int(cached)
        return int(getattr(usage, "cached_tokens", 0) or 0)

    @staticmethod
    def _cached_tokens_anthropic(usage: Any) -> int:
        if usage is None:
            return 0
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        if cache_read:
            return cache_read
        return int(getattr(usage, "cache_creation_input_tokens", 0) or 0)

    def _call_anthropic(self) -> tuple[str, int, int, int]:
        client = self._ensure_client()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "messages": self.messages,
        }
        if self.system_prompt:
            kwargs["system"] = self.system_prompt
        response = client.messages.create(**kwargs)
        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        usage = response.usage
        cached = self._cached_tokens_anthropic(usage)
        return text, usage.input_tokens, usage.output_tokens, cached

    def _call_openai(self) -> tuple[str, int, int, int]:
        client = self._ensure_client()
        messages: list[dict[str, str]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.extend(self.messages)
        response = client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=self.max_output_tokens,
        )
        choice = response.choices[0].message.content or ""
        usage = response.usage
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
        cached = self._cached_tokens_openai(usage)
        return choice, input_tokens, output_tokens, cached

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
