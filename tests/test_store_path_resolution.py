"""SSG_DB_PATH resolution for the RSA/automation SQLite store.

A relative store path lets the process working directory decide which live
database a real-money write lands in. The router used to be pinned by a
`cd` inside a `bash -lc` wrapper; once it is launched from a plugin manifest
that crutch is gone, so the path has to be explicit and absolute.
"""

from __future__ import annotations

import pytest

from execution.engine import (
    DEFAULT_AUTOMATION_STORE_PATH,
    DEFAULT_RSA_STORE_PATH,
    resolve_store_path,
)


def test_unset_keeps_historical_relative_default(monkeypatch):
    monkeypatch.delenv("SSG_DB_PATH", raising=False)
    assert resolve_store_path() == DEFAULT_RSA_STORE_PATH
    assert resolve_store_path() == DEFAULT_AUTOMATION_STORE_PATH


def test_absolute_value_is_used(monkeypatch, tmp_path):
    target = tmp_path / "automation.sqlite3"
    monkeypatch.setenv("SSG_DB_PATH", str(target))
    assert resolve_store_path() == str(target)


def test_relative_value_is_rejected(monkeypatch):
    monkeypatch.setenv("SSG_DB_PATH", "logs/automation.sqlite3")
    with pytest.raises(ValueError, match="absolute"):
        resolve_store_path()


def test_blank_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("SSG_DB_PATH", "   ")
    assert resolve_store_path() == DEFAULT_RSA_STORE_PATH


def test_whitespace_is_stripped(monkeypatch, tmp_path):
    target = tmp_path / "automation.sqlite3"
    monkeypatch.setenv("SSG_DB_PATH", f"  {target}  ")
    assert resolve_store_path() == str(target)


def test_engine_fields_resolve_from_env(monkeypatch, tmp_path):
    """The dataclass defaults must be resolved per-instantiation, not frozen
    at import time — the router process reads the env var the plugin sets."""
    from execution.engine import ExecutionEngine

    target = tmp_path / "automation.sqlite3"
    monkeypatch.setenv("SSG_DB_PATH", str(target))
    engine = ExecutionEngine(
        broker_servers={},
        core=object(),  # type: ignore[arg-type]
        provider=object(),  # type: ignore[arg-type]
    )
    assert engine.rsa_store_path == str(target)
    assert engine.automation_store_path == str(target)


def test_engine_fields_keep_default_when_unset(monkeypatch):
    from execution.engine import ExecutionEngine

    monkeypatch.delenv("SSG_DB_PATH", raising=False)
    engine = ExecutionEngine(
        broker_servers={},
        core=object(),  # type: ignore[arg-type]
        provider=object(),  # type: ignore[arg-type]
    )
    assert engine.rsa_store_path == DEFAULT_RSA_STORE_PATH


def test_explicit_argument_still_wins(monkeypatch, tmp_path):
    """An explicit path (CLI `--db-path`, tests pointing at a copy) must not
    be overridden by the env var."""
    from execution.engine import ExecutionEngine

    monkeypatch.setenv("SSG_DB_PATH", str(tmp_path / "env.sqlite3"))
    engine = ExecutionEngine(
        broker_servers={},
        core=object(),  # type: ignore[arg-type]
        provider=object(),  # type: ignore[arg-type]
        rsa_store_path="/tmp/explicit.sqlite3",
    )
    assert engine.rsa_store_path == "/tmp/explicit.sqlite3"


def test_unexpanded_placeholder_is_reported_as_such(monkeypatch):
    """An MCP manifest declaring env as {"SSG_DB_PATH": "${SSG_DB_PATH}"} passes
    the placeholder through verbatim when the variable is unset. That killed the
    plugin's router on first load; the error must name the real cause."""
    monkeypatch.setenv("SSG_DB_PATH", "${SSG_DB_PATH}")
    with pytest.raises(ValueError, match="unexpanded placeholder"):
        resolve_store_path()
