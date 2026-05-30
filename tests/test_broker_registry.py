"""Tests for the single-source-of-truth broker registry (ADR 0004).

These three properties are impossible to assert against the old five-registry
layout — they are the whole point of the registry:

1. consistency  — every spec resolves to real callables; flags are coherent
2. parity       — the registry faithfully mirrors the registries it replaces
3. isolation    — importing the registry imports no broker SDK
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from brokers import registry

SRC = Path(__file__).resolve().parents[1] / "src"


# --- 1. Consistency --------------------------------------------------------


def test_keys_match_spec_names():
    for name, spec in registry.BROKERS.items():
        assert name == spec.name


def test_every_spec_resolves_to_callables():
    """Every lazy ref resolves to a callable. Catches symbol typos in CI."""
    for spec in registry.all_specs():
        assert callable(registry.resolve_trade(spec.name)), spec.name
        assert callable(registry.resolve_holdings(spec.name)), spec.name
        assert callable(registry.resolve_session_getter(spec.name)), spec.name
        v = registry.resolve_validate(spec.name)
        assert v is None or callable(v), spec.name


def test_validate_presence_matches_spec():
    """resolve_validate returns None exactly when the spec declares no validate."""
    for spec in registry.all_specs():
        resolved = registry.resolve_validate(spec.name)
        if spec.validate is None:
            assert resolved is None, spec.name
        else:
            assert callable(resolved), spec.name


def test_enabled_subset_of_all():
    assert set(registry.enabled_names()) <= set(registry.all_names())
    assert registry.enabled_names() == [
        s.name for s in registry.all_specs() if s.enabled
    ]


def test_malformed_ref_raises():
    with pytest.raises(ValueError):
        registry._resolve("not_a_valid_ref")


def test_unknown_broker_raises():
    with pytest.raises(KeyError):
        registry.resolve_trade("Nonexistent")
    assert registry.get("Nonexistent") is None


# --- 2. Parity with the registries the registry replaces -------------------


def test_parity_with_broker_config():
    """Registry mirrors BrokerConfig.BROKERS (name, session_key, env_vars, mfa)."""
    from brokers.base import BrokerConfig

    assert set(registry.all_names()) == set(BrokerConfig.BROKERS.keys())
    for name, cfg in BrokerConfig.BROKERS.items():
        spec = registry.get(name)
        assert spec is not None, name
        assert spec.session_key == cfg["session_key"], name
        assert list(spec.env_vars) == list(cfg["env_vars"]), name
        assert spec.requires_mfa == cfg["requires_mfa"], name
        assert spec.enabled == cfg["enabled"], name


def test_session_getter_binding_convention():
    """Each spec's session_getter points at this broker's own
    `brokers.<session_key>:get_<session_key>_session` — guards against a
    wrong-broker binding (e.g. Tradier pointing at get_schwab_session). Pure
    data check; resolves nothing."""
    for spec in registry.all_specs():
        assert spec.session_getter == (
            f"brokers.{spec.session_key}:get_{spec.session_key}_session"
        ), spec.name


def test_broker_functions_map_shape():
    """broker_functions_map() reproduces the {name: {trade, holdings, validate?}}
    shape the CLI/TUI consume — validate present only for brokers that have one."""
    fmap = registry.broker_functions_map()
    assert set(fmap.keys()) == set(registry.enabled_names())
    for name, fns in fmap.items():
        assert fns["trade"] is registry.resolve_trade(name), name
        assert fns["holdings"] is registry.resolve_holdings(name), name
        spec = registry.get(name)
        assert spec is not None
        if spec.validate is None:
            assert "validate" not in fns, name
        else:
            assert fns["validate"] is registry.resolve_validate(name), name


def test_get_broker_function_respects_enabled_and_type():
    for name in registry.enabled_names():
        assert callable(registry.get_broker_function(name, "trade")), name
        assert callable(registry.get_broker_function(name, "holdings")), name
    assert registry.get_broker_function("Nonexistent", "trade") is None
    assert registry.get_broker_function("Robinhood", "bogus_type") is None


# --- 3. Isolation (the ADR-0003 promise, enforced) -------------------------


def test_importing_registry_imports_no_broker_sdk():
    """`import brokers.registry` must not pull in any broker SDK.

    Run in a clean subprocess so prior imports in this session don't mask it.
    """
    sdk_modules = [
        "robin_stocks",
        "tastytrade",
        "firstrade",
        "schwab",
        "bbae_invest_api",
        "dspac_invest_api",
        "webull",
        "zendriver",
    ]
    code = (
        "import sys; import brokers.registry; "
        f"loaded=[m for m in {sdk_modules!r} if m in sys.modules]; "
        "print(','.join(loaded))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(SRC),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    loaded = [m for m in result.stdout.strip().split(",") if m]
    assert loaded == [], f"registry import pulled in broker SDKs: {loaded}"
