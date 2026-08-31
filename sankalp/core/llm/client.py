"""
Provider-agnostic LLM client with an on-disk response cache.

Never called from business logic directly — core/obligation/compiler.py is
the only caller in the project, and it receives a client by injection so
tests can supply a recorded one.

DETERMINISM: THE CACHE IS THE MECHANISM, NOT `temperature=0`
--------------------------------------------------------------
The Stage 4 brief asked for a "deterministic seed". That is not achievable
via request parameters on current Claude models: `temperature`, `top_p` and
`top_k` were REMOVED on Claude Opus 5 / Sonnet 5 / Opus 4.6+ and a request
carrying them is rejected with a 400. There is no seed parameter either.

So determinism is provided structurally instead, which is what the brief
actually needs ("on-disk response cache keyed by prompt hash for
reproducible evals"):

  * Every request is keyed by sha256 over (provider, model, system, prompt,
    max_tokens, effort, prompt_version). Identical inputs => identical key.
  * A cache hit returns the recorded response and makes ZERO network calls.
  * The cache directory is COMMITTED to the repo (eval/llm_cache/). Once one
    machine has run the eval with an API key, every later run — CI, a
    reviewer's laptop, the demo machine — reproduces the exact same
    compiler output and therefore the exact same metrics, with no API key
    and no spend.
  * CacheOnlyProvider makes that guarantee enforceable: it raises on a cache
    miss instead of silently falling back to a live call, so a CI run can
    prove it reproduced recorded results rather than generating new ones.

This is a stronger reproducibility guarantee than a seed would have been:
a seed reproduces a sample from one model version, whereas the cache
reproduces the exact bytes the metrics were computed from, across model
deprecations.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import time
from decimal import Decimal
from pathlib import Path
from typing import Protocol

DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[2] / "eval" / "llm_cache"

# Model IDs and per-1M-token USD pricing, verified against the Anthropic
# model/pricing reference on 2026-08-29. If a run reports a cost that looks
# wrong, re-verify here first — this table is the only place cost is defined.
MODEL_PRICING_USD_PER_MTOK: dict[str, tuple[Decimal, Decimal]] = {
    #  model id            (input, output)
    "claude-opus-5":    (Decimal("5.00"), Decimal("25.00")),
    "claude-sonnet-5":  (Decimal("2.00"), Decimal("10.00")),
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
}

DEFAULT_MODEL = "claude-opus-5"

# USD -> INR. A floating market rate frozen to a constant so that a reported
# rupee figure is reproducible rather than drifting between runs. Update
# deliberately and re-run; never read a live rate inside an eval.
USD_TO_INR = Decimal("88.0")


class LLMError(Exception):
    """Any failure reaching or parsing a provider response."""


class CacheMiss(LLMError):
    """Raised by CacheOnlyProvider when a request is not in the cache."""


@dataclasses.dataclass(frozen=True)
class LLMResponse:
    text:            str
    input_tokens:     int
    output_tokens:     int
    model:              str
    from_cache:          bool
    latency_seconds:      float

    def cost_usd(self) -> Decimal:
        if self.model not in MODEL_PRICING_USD_PER_MTOK:
            raise LLMError(
                f"No pricing entry for model {self.model!r}. Add it to "
                f"MODEL_PRICING_USD_PER_MTOK rather than guessing a cost."
            )
        in_rate, out_rate = MODEL_PRICING_USD_PER_MTOK[self.model]
        million = Decimal("1000000")
        return (Decimal(self.input_tokens) / million) * in_rate + (
            Decimal(self.output_tokens) / million
        ) * out_rate

    def cost_inr(self) -> Decimal:
        return self.cost_usd() * USD_TO_INR


@dataclasses.dataclass(frozen=True)
class LLMRequest:
    system:          str
    prompt:           str
    max_tokens:        int = 4096
    effort:             str = "high"
    prompt_version:      str = "unversioned"
    model:                str = DEFAULT_MODEL


class LLMProvider(Protocol):
    name: str

    def complete(self, request: LLMRequest) -> LLMResponse: ...


# ── Cache key ─────────────────────────────────────────────────────────────

def cache_key(provider_name: str, request: LLMRequest) -> str:
    """
    Stable sha256 over every input that can change the response. Uses
    sort_keys so dict ordering can never alter the key — the same silent
    invalidator class that breaks prompt caching would otherwise break
    eval reproducibility here.
    """
    payload = {
        "provider": provider_name,
        "model": request.model,
        "system": request.system,
        "prompt": request.prompt,
        "max_tokens": request.max_tokens,
        "effort": request.effort,
        "prompt_version": request.prompt_version,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ── Providers ─────────────────────────────────────────────────────────────

class AnthropicProvider:
    """
    Live Anthropic provider.

    Note what is deliberately NOT sent: no `temperature`, `top_p`, `top_k`
    (removed on current models — a 400), and no `budget_tokens` (removed on
    Opus 5; adaptive thinking replaces it). Depth is controlled by
    `output_config.effort`.
    """

    name = "anthropic"

    def __init__(self, api_key: str | None = None) -> None:
        try:
            import anthropic
        except ImportError as exc:   # pragma: no cover - depends on install
            raise LLMError(
                "The `anthropic` package is not installed. Install it with "
                "`pip install anthropic`, or use CacheOnlyProvider to replay a "
                "committed cache without any provider SDK."
            ) from exc
        self._anthropic = anthropic
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        # A bare client also resolves an `ant auth login` profile, so an unset
        # env var is not necessarily "no credentials" — let the SDK decide.
        self._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()

    def complete(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        try:
            response = self._client.messages.create(
                model=request.model,
                max_tokens=request.max_tokens,
                system=request.system,
                output_config={"effort": request.effort},
                messages=[{"role": "user", "content": request.prompt}],
            )
        except self._anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error ({exc.status_code}): {exc}") from exc
        except self._anthropic.APIConnectionError as exc:
            raise LLMError(f"Could not reach the Anthropic API: {exc}") from exc

        if response.stop_reason == "refusal":
            raise LLMError(
                f"Model refused the request (stop_reason=refusal). "
                f"stop_details={getattr(response, 'stop_details', None)!r}"
            )

        text = "".join(block.text for block in response.content if block.type == "text")
        if not text.strip():
            raise LLMError(
                f"Model returned no text content (stop_reason={response.stop_reason!r}). "
                f"If stop_reason is 'max_tokens', raise max_tokens."
            )

        return LLMResponse(
            text=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=response.model,
            from_cache=False,
            latency_seconds=time.perf_counter() - started,
        )


class CacheOnlyProvider:
    """
    Replays a committed cache and refuses to make network calls.

    This is what makes "reproducible eval" checkable rather than aspirational:
    a run configured with this provider either reproduces the recorded
    responses exactly or fails loudly on the first miss. It never silently
    generates fresh output that would change the metrics.
    """

    name = "anthropic"   # shares the cache namespace with AnthropicProvider

    def complete(self, request: LLMRequest) -> LLMResponse:
        raise CacheMiss(
            "CacheOnlyProvider: request is not in the committed cache "
            f"(model={request.model!r}, prompt_version={request.prompt_version!r}). "
            "Either the prompt/model changed since the cache was recorded, or the "
            "cache was not committed. Re-record with a live provider, or restore "
            "eval/llm_cache/."
        )


# ── Client ────────────────────────────────────────────────────────────────

class LLMClient:
    """Wraps a provider with an on-disk, content-addressed response cache."""

    def __init__(
        self,
        provider: LLMProvider,
        cache_dir: Path | None = None,
        *,
        read_cache: bool = True,
        write_cache: bool = True,
    ) -> None:
        self.provider = provider
        self.cache_dir = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
        self.read_cache = read_cache
        self.write_cache = write_cache
        self.hits = 0
        self.misses = 0

    def _path_for(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def _load(self, key: str) -> LLMResponse | None:
        path = self._path_for(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise LLMError(
                f"Corrupt LLM cache entry at {path}. Delete it and re-record."
            ) from exc
        return LLMResponse(
            text=data["text"],
            input_tokens=data["input_tokens"],
            output_tokens=data["output_tokens"],
            model=data["model"],
            from_cache=True,
            latency_seconds=data.get("latency_seconds", 0.0),
        )

    def _store(self, key: str, request: LLMRequest, response: LLMResponse) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "text": response.text,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "model": response.model,
            "latency_seconds": response.latency_seconds,
            # Recorded for auditability — a reviewer can see exactly which
            # prompt produced a cached response without re-running anything.
            "_request": {
                "model": request.model,
                "system": request.system,
                "prompt": request.prompt,
                "max_tokens": request.max_tokens,
                "effort": request.effort,
                "prompt_version": request.prompt_version,
            },
        }
        self._path_for(key).write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
        )

    def complete(self, request: LLMRequest) -> LLMResponse:
        key = cache_key(self.provider.name, request)

        if self.read_cache:
            cached = self._load(key)
            if cached is not None:
                self.hits += 1
                return cached

        self.misses += 1
        response = self.provider.complete(request)
        if self.write_cache:
            self._store(key, request, response)
        return response


def default_client(*, cache_only: bool = False) -> LLMClient:
    """
    Build the project's standard client. The model is a per-request field
    (LLMRequest.model), not a client-level setting, so that the cache key
    always reflects the model that actually produced a response.

    cache_only=True replays the committed cache and never calls a provider —
    use it in CI and anywhere reproducibility matters more than freshness.
    """
    provider: LLMProvider = CacheOnlyProvider() if cache_only else AnthropicProvider()
    return LLMClient(provider)
