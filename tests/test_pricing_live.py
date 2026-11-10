"""Tests for live LiteLLM rate loading (cache, offline fallback, provenance)."""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

from asm import models
from asm.services import pricing

_MILLION_INPUT = {"input_tokens": 1_000_000}


def _reset(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(models, "APP_DATA_DIR", tmp_path / ".asm")
    monkeypatch.setattr(pricing, "_live_db", None)
    monkeypatch.setattr(pricing, "_rates_source", "bundled table")
    return tmp_path / ".asm" / "pricing-cache.json"


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _fake_fetch(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=0: _FakeResponse(payload))


def _broken_fetch(monkeypatch) -> None:
    def _raise(url, timeout=0):
        raise OSError("offline")
    monkeypatch.setattr(urllib.request, "urlopen", _raise)


def test_live_rates_override_bundled_table(monkeypatch, tmp_path: Path):
    cache = _reset(monkeypatch, tmp_path)
    _fake_fetch(monkeypatch, {
        "claude-fable-5": {
            "input_cost_per_token": 2e-05,
            "output_cost_per_token": 1e-04,
            "cache_read_input_token_cost": 2e-06,
            "cache_creation_input_token_cost": 2.5e-05,
        },
        "gpt-5.5": {
            "input_cost_per_token": 7e-06,
            "output_cost_per_token": 4.2e-05,
            "cache_read_input_token_cost": 7e-07,
        },
        "irrelevant-model": {"input_cost_per_token": 1.0, "output_cost_per_token": 1.0},
    })
    assert pricing.load_live_rates() == "LiteLLM (live)"
    assert pricing.calc_cost(_MILLION_INPUT, "claude-fable-5") == 20.0
    u = {"input_tokens": 1_000_000, "cached_input_tokens": 0, "output_tokens": 0}
    assert pricing.calc_openai_cost(u, "gpt-5.5") == 7.0
    # Cache written for offline reuse, filtered to claude/gpt entries only.
    saved = json.loads(cache.read_text())
    assert "claude-fable-5" in saved and "irrelevant-model" not in saved


def test_prefixed_litellm_keys_are_found(monkeypatch, tmp_path: Path):
    _reset(monkeypatch, tmp_path)
    _fake_fetch(monkeypatch, {
        "anthropic.claude-mythos-5": {
            "input_cost_per_token": 1e-05,
            "output_cost_per_token": 5e-05,
        },
    })
    pricing.load_live_rates()
    # cache_read/create fall back to Anthropic's standard multipliers.
    assert pricing.calc_cost(_MILLION_INPUT, "claude-mythos-5") == 10.0
    assert pricing.calc_cost({"cache_creation_input_tokens": 1_000_000}, "claude-mythos-5") == 12.5


def test_offline_without_cache_uses_bundled_table(monkeypatch, tmp_path: Path):
    _reset(monkeypatch, tmp_path)
    _broken_fetch(monkeypatch)
    assert pricing.load_live_rates() == "bundled table (LiteLLM unreachable)"
    assert pricing.calc_cost(_MILLION_INPUT, "claude-fable-5") == 10.0


def test_fresh_cache_skips_fetch(monkeypatch, tmp_path: Path):
    cache = _reset(monkeypatch, tmp_path)
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({
        "claude-fable-5": {"input_cost_per_token": 3e-05, "output_cost_per_token": 1e-04},
    }))
    _broken_fetch(monkeypatch)  # would fail if the loader tried the network
    assert pricing.load_live_rates().startswith("LiteLLM (cached")
    assert pricing.calc_cost(_MILLION_INPUT, "claude-fable-5") == 30.0


def test_stale_cache_beats_bundled_when_offline(monkeypatch, tmp_path: Path):
    cache = _reset(monkeypatch, tmp_path)
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({
        "claude-fable-5": {"input_cost_per_token": 4e-05, "output_cost_per_token": 1e-04},
    }))
    stale = time.time() - pricing.CACHE_MAX_AGE_SECONDS - 3600
    os.utime(cache, (stale, stale))
    _broken_fetch(monkeypatch)
    assert pricing.load_live_rates() == "LiteLLM (stale cache, offline)"
    assert pricing.calc_cost(_MILLION_INPUT, "claude-fable-5") == 40.0


def test_one_invalid_live_entry_keeps_the_rest(monkeypatch, tmp_path: Path):
    _reset(monkeypatch, tmp_path)
    _fake_fetch(monkeypatch, {
        "claude-fable-5": {"input_cost_per_token": 2e-05, "output_cost_per_token": 1e-04},
        "bedrock/claude-fable-latest": {"input_cost_per_token": None, "output_cost_per_token": 1e-04},
    })
    assert pricing.load_live_rates() == "LiteLLM (live)"
    assert pricing.calc_cost(_MILLION_INPUT, "claude-fable-5") == 20.0


def test_unlisted_sonnet_uses_family_rate(monkeypatch, tmp_path: Path):
    _reset(monkeypatch, tmp_path)
    monkeypatch.setattr(pricing, "_live_db", {})
    assert pricing.calc_cost(_MILLION_INPUT, "claude-sonnet-5-5") == pricing.calc_cost(_MILLION_INPUT, "claude-sonnet-5")
    assert pricing.calc_cost(_MILLION_INPUT, "claude-sonnet-4-9") == 3.0
