"""F10 — structured logging for every MCP tool invocation.

Each call to a router method or broker-MCP method emits one JSONL line to
`logs/mcp-{YYYY-MM-DD}.jsonl`:

  {
    "ts": "2026-05-27T18:42:01.123456+00:00",
    "tool": "place_at_broker",     // method name
    "broker": "Fennel",            // broker context when applicable
    "args": {...},                 // arg dict with secrets redacted
    "dry_run": true | false,       // explicit field — no arg-parsing required
    "ok": true | false,
    "duration_ms": 12.3,
    "result_summary": {...},       // small dict, no raw broker payload
    "error_reason": null | "..."   // GateError.reason if applicable
  }

Token-shaped args (`confirmation_token`, `leg_token`, `proposal_id`,
`idempotency_key`) are logged as the first 8 chars + ellipsis so a log
audit can correlate calls without enabling replay. Credential-shaped keys
in any arg dict are dropped entirely via the same `sanitize_holdings`
helper the router uses at its MCP boundary (defense in depth).

The log file is one per UTC day. Rotation is filename-driven — no
size-based rotation here; the audit log + WAL story is separate (ISC-45).
"""

from __future__ import annotations

import functools
import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

# Argument keys whose values must be truncated, not logged in full. The
# value-side check (regex on values) lives in sanitize for any nested dicts.
_TRUNCATE_TOKEN_KEYS = (
    "confirmation_token",
    "leg_token",
    "proposal_id",
    "idempotency_key",
    "token",
)

_CREDENTIAL_KEY_STEMS = (
    "password",
    "_secret",
    "oauth_token",
    "refresh_token",
    "access_token",
    "session_cookie",
    "session_id",
    "api_key",
    "bearer_token",
    "mfa_code",
    "otp_code",
    "cookie",
)


def _truncate(token: Any) -> str:
    s = str(token)
    if len(s) <= 8:
        return s + "…"
    return s[:8] + "…"


def _redact(value: Any) -> Any:
    """Recursively scrub credential-shaped keys + truncate token values."""
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            ks = str(k).lower()
            if any(stem in ks for stem in _CREDENTIAL_KEY_STEMS):
                continue
            if ks in _TRUNCATE_TOKEN_KEYS:
                out[k] = _truncate(v) if v else v
                continue
            out[k] = _redact(v)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _summarize_result(result: Any) -> dict[str, Any]:
    """Compress an arbitrary tool result into a small log-friendly dict."""
    if isinstance(result, dict):
        keys = list(result.keys())
        summary: dict[str, Any] = {"shape": "dict", "keys": keys[:12]}
        # Echo a few selected scalar fields when present
        for k in ("ok", "success_count", "failure_count", "count", "leg_count"):
            if k in result and isinstance(result[k], (int, float, bool, str)):
                summary[k] = result[k]
        return summary
    if hasattr(result, "ok") and hasattr(result, "broker"):
        return {
            "shape": "PlaceResult",
            "ok": bool(getattr(result, "ok", False)),
            "broker": getattr(result, "broker", None),
            "dry_run": bool(getattr(result, "dry_run", False)),
            "reason": getattr(result, "reason", None),
        }
    if isinstance(result, list):
        return {"shape": "list", "len": len(result)}
    return {"shape": type(result).__name__}


class TelemetryLog:
    """Append-only JSONL writer with a daily-rotating filename.

    Default location is `logs/mcp-{YYYY-MM-DD}.jsonl` under the project root.
    Override via `SSG_MCP_LOG_DIR` env var. The writer is thread-safe under
    free-threaded Python via a `threading.Lock`.
    """

    def __init__(self, log_dir: str | Path | None = None):
        d = Path(
            log_dir if log_dir is not None else os.getenv("SSG_MCP_LOG_DIR", "logs")
        )
        d.mkdir(parents=True, exist_ok=True)
        self.dir = d
        self._lock = threading.Lock()

    def _path_for_today(self) -> Path:
        return self.dir / f"mcp-{datetime.now(UTC).date().isoformat()}.jsonl"

    def append(self, record: dict[str, Any]) -> None:
        with self._lock:
            with self._path_for_today().open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")


_log_singleton: TelemetryLog | None = None
_log_singleton_lock = threading.Lock()


def telemetry_log() -> TelemetryLog:
    global _log_singleton
    with _log_singleton_lock:
        if _log_singleton is None:
            _log_singleton = TelemetryLog()
        return _log_singleton


def reset_telemetry_log() -> None:
    """Tests use this to point at a fresh logs dir before each case."""
    global _log_singleton
    with _log_singleton_lock:
        _log_singleton = None


def configure_telemetry_log(log_dir: str | Path) -> TelemetryLog:
    """Tests / runtime configuration: install a TelemetryLog at the given dir."""
    global _log_singleton
    with _log_singleton_lock:
        _log_singleton = TelemetryLog(log_dir)
        return _log_singleton


def logged_tool(
    *,
    tool: str,
    broker: str | None = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Decorator: wrap a router/broker method with structured-log emission.

    The wrapped method's positional args are captured by name from its
    signature; `dry_run` is surfaced as a top-level log field if it appears
    in the kwargs (ISC-32 — distinguishable without arg-parsing the args
    blob). On exception the log line records `ok=False` + `error_reason`.
    """

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            ok = False
            result: Any = None
            err_reason: str | None = None
            try:
                result = await fn(*args, **kwargs)
                ok = True
                return result
            except Exception as e:
                err_reason = getattr(e, "reason", type(e).__name__)
                raise
            finally:
                duration_ms = (time.perf_counter() - start) * 1000.0
                # `args[0]` is `self` for methods — skip it
                pos = args[1:] if args else ()
                pos_dump = [_redact(a) for a in pos]
                kw_dump = _redact(kwargs)
                # `dry_run` may live in kwargs OR positional; pull from kwargs only
                dry_run = bool(kwargs.get("dry_run", False))
                record = {
                    "ts": datetime.now(UTC).isoformat(),
                    "tool": tool,
                    "broker": broker
                    or getattr(getattr(args[0], "spec", None), "name", None)
                    if args
                    else None,
                    "args": {"positional": pos_dump, "kwargs": kw_dump},
                    "dry_run": dry_run,
                    "ok": ok,
                    "duration_ms": round(duration_ms, 3),
                    "result_summary": _summarize_result(result) if ok else None,
                    "error_reason": err_reason,
                }
                try:
                    telemetry_log().append(record)
                except Exception:  # never fail the call because of logging
                    pass

        return wrapper

    return decorator
