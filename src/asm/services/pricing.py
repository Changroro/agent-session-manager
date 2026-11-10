"""Model pricing for cost estimation.

Rates come from LiteLLM's pricing database (https://github.com/BerriAI/litellm),
fetched live by ``load_live_rates()`` (15-minute cache under ~/.asm) so new
models are priced correctly without a release. The bundled table below is the offline
fallback and the tier-estimate source for models absent from the live DB; the
active source is always reported via ``rates_source()``.

Callers must invoke ``load_live_rates()`` explicitly (CLI/TUI entry points do);
imports never touch the network, so tests and library use stay deterministic.
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Per-1M-token rates: (input, output, cache_read, cache_create) in USD.
_OPUS_LEGACY = (15.0, 75.0, 1.50, 18.75)   # opus 4.0 / 4.1 / opus-3
_OPUS = (5.0, 25.0, 0.50, 6.25)            # opus 4.5+
_SONNET = (3.0, 15.0, 0.30, 3.75)          # sonnet 4.x / 3.x
_SONNET_5 = (2.0, 10.0, 0.20, 2.50)        # sonnet 5
_HAIKU = (1.0, 5.0, 0.10, 1.25)            # haiku 4.x / 3.5
_FABLE = (10.0, 50.0, 1.00, 12.50)         # fable/mythos 5

# Exact per-model rates (keyed by the model id with the date suffix stripped).
_RATES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-4-0": _OPUS_LEGACY,
    "claude-opus-4-1": _OPUS_LEGACY,
    "claude-opus-4-5": _OPUS,
    "claude-opus-4-6": _OPUS,
    "claude-opus-4-7": _OPUS,
    "claude-opus-4-8": _OPUS,
    "claude-sonnet-4-5": _SONNET,
    "claude-sonnet-4-6": _SONNET,
    "claude-sonnet-4": _SONNET,
    "claude-sonnet-5": _SONNET_5,
    "claude-haiku-4-5": _HAIKU,
    "claude-haiku-4": _HAIKU,
    "claude-fable-5": _FABLE,
    "claude-mythos-5": _FABLE,
}

# Opus releases that still carry the old, expensive pricing.
_LEGACY_OPUS_MARKERS = ("opus-4-0", "opus-4-1", "opus-3")


# --- Live rates (LiteLLM) ----------------------------------------------------

_LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
CACHE_MAX_AGE_SECONDS = 15 * 60
_FETCH_TIMEOUT = 3.0

_live_db: dict[str, dict] | None = None
_rates_source = "bundled table"


class PricingDataError(ValueError):
    pass


def _validate_live_db(data) -> dict[str, dict]:
    if not isinstance(data, dict):
        raise PricingDataError("pricing payload must be an object")
    result: dict[str, dict] = {}
    invalid: list[str] = []
    for key, entry in data.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        relevant = _relevant_entry(key)
        if not relevant:
            continue
        fields = ["input_cost_per_token", "output_cost_per_token"] + [
            f for f in ("cache_read_input_token_cost", "cache_creation_input_token_cost") if f in entry
        ]
        if all(
            isinstance(entry.get(f), (int, float)) and math.isfinite(entry[f]) and entry[f] >= 0
            for f in fields
        ):
            result[key] = entry
        else:
            invalid.append(key)
    # One malformed third-party entry must not discard every live rate.
    if invalid:
        logger.info("Ignoring %d invalid pricing entries: %s", len(invalid), ", ".join(invalid[:5]))
    return result


def _cache_path() -> Path:
    from asm import models
    return models.APP_DATA_DIR / "pricing-cache.json"


def rates_source() -> str:
    """Human-readable provenance of the rates currently in use."""
    return _rates_source


def _relevant_entry(key: str) -> bool:
    k = key.lower()
    return "claude" in k or k.startswith("gpt-") or k.startswith("openai/gpt-")


def load_live_rates(force: bool = False) -> str:
    """Load current per-model rates from LiteLLM into module state.

    Uses a 15-minute on-disk cache; when the fetch fails, a stale cache still
    beats the bundled table. Returns (and records) the provenance string shown
    in the dashboard / CLI so the user always sees which source priced their data.
    """
    global _live_db, _rates_source
    if _live_db is not None and not force:
        return _rates_source

    cache = _cache_path()
    try:
        age = time.time() - cache.stat().st_mtime
    except OSError:
        age = None
    if age is not None and age < CACHE_MAX_AGE_SECONDS and not force:
        try:
            _live_db = _validate_live_db(json.loads(cache.read_text()))
            _rates_source = f"LiteLLM (cached {max(int(age // 60), 0)}m ago)"
            return _rates_source
        except (OSError, json.JSONDecodeError, PricingDataError) as exc:
            logger.warning("Unreadable pricing cache %s: %s", cache, exc)

    try:
        import urllib.request
        with urllib.request.urlopen(_LITELLM_URL, timeout=_FETCH_TIMEOUT) as resp:
            full = json.loads(resp.read().decode("utf-8"))
        _live_db = _validate_live_db(full)
        _rates_source = "LiteLLM (live)"
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(_live_db))
        except OSError as exc:
            logger.warning("Failed to write pricing cache %s: %s", cache, exc)
        return _rates_source
    except (OSError, ValueError) as exc:
        logger.warning("Live pricing fetch failed: %s", exc)

    if cache.exists():
        try:
            _live_db = _validate_live_db(json.loads(cache.read_text()))
            _rates_source = "LiteLLM (stale cache, offline)"
            return _rates_source
        except (OSError, json.JSONDecodeError, PricingDataError):
            pass
    _live_db = {}
    _rates_source = "bundled table (LiteLLM unreachable)"
    return _rates_source


def _live_lookup(keys: tuple[str, ...]) -> dict | None:
    if not _live_db:
        return None
    for key in keys:
        entry = _live_db.get(key)
        if (
            isinstance(entry, dict)
            and "input_cost_per_token" in entry
            and "output_cost_per_token" in entry
            and all(
                isinstance(entry[field], (int, float))
                and math.isfinite(entry[field])
                and entry[field] >= 0
                for field in ("input_cost_per_token", "output_cost_per_token")
            )
        ):
            return entry
    return None


def _live_claude_rates(name: str) -> tuple[float, float, float, float] | None:
    entry = _live_lookup((name, f"anthropic/{name}", f"anthropic.{name}"))
    if entry is None:
        return None
    inp = entry["input_cost_per_token"]
    out = entry["output_cost_per_token"]
    # Anthropic's standard multipliers when LiteLLM omits the cache fields.
    cache_read = entry.get("cache_read_input_token_cost", inp * 0.1)
    cache_create = entry.get("cache_creation_input_token_cost", inp * 1.25)
    return (inp * 1e6, out * 1e6, cache_read * 1e6, cache_create * 1e6)


def _live_openai_rates(name: str) -> tuple[float, float, float] | None:
    entry = _live_lookup((name, f"openai/{name}"))
    if entry is None:
        return None
    inp = entry["input_cost_per_token"]
    out = entry["output_cost_per_token"]
    cached = entry.get("cache_read_input_token_cost", inp * 0.1)
    return (inp * 1e6, out * 1e6, cached * 1e6)


def _normalize(model: str) -> str:
    """Strip a trailing date / variant suffix from a model id.

    e.g. ``claude-opus-4-8-20260101`` -> ``claude-opus-4-8``,
    ``claude-opus-4-6[1m]`` -> ``claude-opus-4-6``.
    """
    name = model.split("[")[0]
    for marker in ("-2024", "-2025", "-2026", "-2027"):
        name = name.split(marker)[0]
    if not name.startswith("claude-"):
        name = "claude-" + name.lstrip("-")
    return name


def _rates_for(model: str) -> tuple[float, float, float, float]:
    """Return (input, output, cache_read, cache_create) per-1M rates for a model."""
    if not isinstance(model, str) or not model:
        raise PricingDataError("Claude model id is required")
    name = _normalize(model)
    live = _live_claude_rates(name)
    if live is not None:
        return live
    if name in _RATES:
        return _RATES[name]
    # Fallback by family for unknown / future models.
    if "opus" in name:
        return _OPUS_LEGACY if any(m in name for m in _LEGACY_OPUS_MARKERS) else _OPUS
    if "fable" in name or "mythos" in name:
        return _FABLE
    if "haiku" in name:
        return _HAIKU
    if "sonnet" in name:
        return _SONNET if any(m in name for m in ("sonnet-3", "sonnet-4")) else _SONNET_5
    raise PricingDataError(f"unknown Claude model: {model}")


def display_model(model: str) -> str:
    """Canonical display name for a Claude model id (date suffix stripped)."""
    return _normalize(model)


def calc_cost(usage: dict, model: str) -> float:
    """Estimate USD cost for a single usage record."""
    inp_r, out_r, cr_r, cw_r = _rates_for(model)
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    cache_create = usage.get("cache_creation_input_tokens", 0)
    return (
        inp * inp_r
        + out * out_r
        + cache_read * cr_r
        + cache_create * cw_r
    ) / 1_000_000


# Models that are placeholders, not billable (e.g. Claude Code's synthetic
# "no API call" assistant turns). Skipped entirely in usage aggregation.
NON_BILLABLE_MODELS = frozenset({"<synthetic>", "<synthetic-streaming>", ""})


def is_billable(model: str) -> bool:
    return model not in NON_BILLABLE_MODELS


# --- OpenAI / Codex pricing -------------------------------------------------
# Per-1M-token rates: (input, output, cached_input) in USD. OpenAI bills cached
# input at the cheaper rate and folds cache writes / reasoning tokens into the
# normal input / output rates (no separate cache-creation charge).
_GPT_DEFAULT = (1.25, 10.0, 0.125)  # gpt-5 / 5.1 family
_GPT_RATES: dict[str, tuple[float, float, float]] = {
    "gpt-5": (1.25, 10.0, 0.125),
    "gpt-5-codex": (1.25, 10.0, 0.125),
    "gpt-5-mini": (0.25, 2.0, 0.025),
    "gpt-5-nano": (0.05, 0.4, 0.005),
    "gpt-5.1": (1.25, 10.0, 0.125),
    "gpt-5.1-codex": (1.25, 10.0, 0.125),
    "gpt-5.1-codex-mini": (0.25, 2.0, 0.025),
    "gpt-5.2": (1.75, 14.0, 0.175),
    "gpt-5.2-codex": (1.75, 14.0, 0.175),
    "gpt-5.3-codex": (1.75, 14.0, 0.175),
    "gpt-5.4": (2.5, 15.0, 0.25),
    "gpt-5.4-codex": (2.5, 15.0, 0.25),
    "gpt-5.4-mini": (0.75, 4.5, 0.075),
    "gpt-5.5": (5.0, 30.0, 0.5),
    "gpt-5.5-codex": (5.0, 30.0, 0.5),
    "gpt-4.1": (2.0, 8.0, 0.5),
    "gpt-4.1-mini": (0.4, 1.6, 0.1),
    "gpt-4o": (2.5, 10.0, 1.25),
    "gpt-4o-mini": (0.15, 0.6, 0.075),
}


def _openai_rates_for(model: str) -> tuple[float, float, float]:
    name = model.split("[")[0]
    for marker in ("-2024", "-2025", "-2026", "-2027"):
        name = name.split(marker)[0]
    live = _live_openai_rates(name)
    if live is not None:
        return live
    if name in _GPT_RATES:
        return _GPT_RATES[name]
    # Longest known prefix match (e.g. "gpt-5.5-codex-foo" -> gpt-5.5-codex).
    for key in sorted(_GPT_RATES, key=len, reverse=True):
        if name.startswith(key):
            return _GPT_RATES[key]
    return _GPT_DEFAULT


def calc_openai_cost(usage: dict, model: str) -> float:
    """Estimate USD cost for an OpenAI/Codex ``total_token_usage`` record.

    ``input_tokens`` is the full prompt size and already includes
    ``cached_input_tokens`` (billed cheaper); ``output_tokens`` already includes
    reasoning tokens (billed at the output rate).
    """
    in_r, out_r, cached_r = _openai_rates_for(model)
    inp = usage.get("input_tokens", 0)
    cached = usage.get("cached_input_tokens", 0)
    out = usage.get("output_tokens", 0)
    uncached = max(inp - cached, 0)
    return (uncached * in_r + cached * cached_r + out * out_r) / 1_000_000
