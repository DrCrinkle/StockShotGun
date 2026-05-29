"""Recap ingest — parse_chat_recap_full extracts all four signal tiers,
and the router's recap_ingest tool persists them to the automation store.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentic.router import NullAccountStatusProvider, Router
from automation_recap import (  # type: ignore[import-untyped]
    AutomationRecapStore,
    parse_chat_recap,
    parse_chat_recap_full,
)
from enforcement import AuditLog, EnforcementCore, ProposalStore
from enforcement.circuit_breaker import CircuitBreaker


SAMPLE_RECAP = """ -Stocks Back and Latest-
CUPR - Going CIL

 -UPCOMING BUYS-
-05/27-
RPGL - 1:40 - Went CIL last time

-05/29-
ZOOZ - 1:20

-06/22-
MNDR - 1:6

 -CHATTER-

-RESEARCH POSTED-
-05/27-
BIS - CIL

-05/28-
SLXN - CIL

-05/29-
SCYX - CIL

-06/03-
ATHE - ADR - CIL

 -TBA-
AERT - round down
AREB - 1:250
ASNS - 1:25
DD - CIL
ENZN - OTC - 1:100 merger
GGR - CIL
IPSC - ratio and date TBA
PSNT - CIL
SDIG - merger CIL
SLGD - OTC - merger-R/S, delayed, CIL
SNYR - 1:40
WETO - 1:10 - delayed

 -NOTICES-
*Please remember to post in individual threads*
"""


def test_legacy_parse_chat_recap_still_returns_two_tuple():
    """`main.py` unpacks `upcoming, stock_back = parse_chat_recap(text)` —
    that contract must keep working."""
    upcoming, stock_back = parse_chat_recap(SAMPLE_RECAP)
    assert len(upcoming) == 3
    assert {u.ticker for u in upcoming} == {"RPGL", "ZOOZ", "MNDR"}
    assert len(stock_back) == 1
    assert stock_back[0].ticker == "CUPR"


def test_parse_chat_recap_full_returns_all_four_tiers():
    result = parse_chat_recap_full(SAMPLE_RECAP)
    assert len(result.upcoming) == 3
    assert len(result.stock_back) == 1
    assert len(result.research) == 4
    assert len(result.tba) == 12


def test_research_signals_carry_date_and_notes():
    result = parse_chat_recap_full(SAMPLE_RECAP)
    by_ticker = {r.ticker: r for r in result.research}
    assert by_ticker["BIS"].date_mmdd == "05/27"
    assert by_ticker["BIS"].notes == "CIL"
    assert by_ticker["ATHE"].date_mmdd == "06/03"
    assert "ADR" in by_ticker["ATHE"].notes
    assert "CIL" in by_ticker["ATHE"].notes


def test_tba_extracts_embedded_ratios():
    """TBA entries like 'ENZN - OTC - 1:100 merger' carry the ratio embedded
    in the notes column — the parser must surface it as `ratio` regardless."""
    result = parse_chat_recap_full(SAMPLE_RECAP)
    by_ticker = {t.ticker: t for t in result.tba}
    assert by_ticker["AREB"].ratio == "1:250"
    assert by_ticker["ENZN"].ratio == "1:100"  # embedded in middle of notes
    assert "OTC" in by_ticker["ENZN"].notes
    assert "merger" in by_ticker["ENZN"].notes
    assert by_ticker["WETO"].ratio == "1:10"
    assert "delayed" in by_ticker["WETO"].notes
    # Entries with no ratio at all
    assert by_ticker["AERT"].ratio is None
    assert by_ticker["AERT"].notes == "round down"
    assert by_ticker["DD"].ratio is None
    assert by_ticker["DD"].notes == "CIL"


def test_chatter_and_notices_sections_produce_no_signals():
    """Empty CHATTER and meta NOTICES contribute zero signals."""
    result = parse_chat_recap_full(SAMPLE_RECAP)
    all_tickers = (
        {u.ticker for u in result.upcoming}
        | {s.ticker for s in result.stock_back}
        | {r.ticker for r in result.research}
        | {t.ticker for t in result.tba}
    )
    # The * line in NOTICES contains no ticker — should not leak through
    assert "Please" not in {t.upper() for t in all_tickers}
    assert "*" not in {t for t in all_tickers}


def test_record_recap_extended_persists_research_and_tba(tmp_path: Path):
    store = AutomationRecapStore(str(tmp_path / "automation.sqlite3"))
    result = parse_chat_recap_full(SAMPLE_RECAP)
    counts = store.record_recap_extended(SAMPLE_RECAP, result, datetime.now())
    assert counts["new_research"] == 4
    assert counts["new_tba"] == 12

    research_rows = store.get_active_research_signals()
    assert {r["ticker"] for r in research_rows} == {"BIS", "SLXN", "SCYX", "ATHE"}

    tba_rows = store.get_active_tba_candidates()
    assert {t["ticker"] for t in tba_rows} == {
        "AERT", "AREB", "ASNS", "DD", "ENZN", "GGR",
        "IPSC", "PSNT", "SDIG", "SLGD", "SNYR", "WETO",
    }
    store.close()


def test_record_recap_extended_deduplicates_on_replay(tmp_path: Path):
    """Re-ingesting the same recap must not double-insert signals."""
    store = AutomationRecapStore(str(tmp_path / "automation.sqlite3"))
    result = parse_chat_recap_full(SAMPLE_RECAP)
    first = store.record_recap_extended(SAMPLE_RECAP, result, datetime.now())
    second = store.record_recap_extended(SAMPLE_RECAP, result, datetime.now())
    assert first["new_research"] == 4
    assert second["new_research"] == 0  # dedup via signal_key UNIQUE
    assert first["new_tba"] == 12
    assert second["new_tba"] == 0
    store.close()


def test_mark_research_promoted_transitions_status(tmp_path: Path):
    store = AutomationRecapStore(str(tmp_path / "automation.sqlite3"))
    result = parse_chat_recap_full(SAMPLE_RECAP)
    store.record_recap_extended(SAMPLE_RECAP, result, datetime.now())
    active = store.get_active_research_signals()
    assert len(active) == 4
    # Promote the first two
    store.mark_research_promoted(
        [active[0]["id"], active[1]["id"]], datetime.now()
    )
    remaining = store.get_active_research_signals()
    assert len(remaining) == 2
    store.close()


@pytest.fixture
def router(tmp_path: Path) -> Router:
    core = EnforcementCore(
        proposal_store=ProposalStore(tmp_path / "proposals.sqlite"),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )
    return Router(
        broker_servers={},
        core=core,
        provider=NullAccountStatusProvider(),
        automation_store_path=str(tmp_path / "automation.sqlite3"),
    )


def test_router_recap_ingest_returns_structured_summary(router: Router):
    out = asyncio.run(router.recap_ingest(SAMPLE_RECAP))
    assert out["ok"]
    assert out["counts"]["new_research"] == 4
    assert out["counts"]["new_tba"] == 12
    assert len(out["upcoming"]) == 3
    assert len(out["research"]) == 4
    assert len(out["tba"]) == 12
    # Spot-check one TBA entry preserves the embedded ratio
    enzn = next(t for t in out["tba"] if t["ticker"] == "ENZN")
    assert enzn["ratio"] == "1:100"


def test_router_recap_ingest_is_registered_as_fastmcp_tool(router: Router):
    from agentic.router import build_router_fastmcp_server

    app = build_router_fastmcp_server(router)
    tools = asyncio.run(app.list_tools())
    assert "recap_ingest" in {t.name for t in tools}
