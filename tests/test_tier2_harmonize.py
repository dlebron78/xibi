"""Step-112: type-drift consolidation tests.

Verifies the post-review harmonization step canonicalizes drifting type
labels across recent extracted_facts.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests._helpers import _migrated_db
from xibi.alerting.rules import RuleEngine
from xibi.db import open_db
from xibi.heartbeat.review_cycle import (
    _harmonize_extracted_fact_types,
    _parse_harmonize_response,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return _migrated_db(tmp_path)


def _seed(db_path: Path, rows: list[tuple[str, dict]]) -> None:
    """Insert ``rows`` of ``(ref_id, extracted_facts)`` for testing."""
    rules = RuleEngine(db_path)
    with open_db(db_path) as conn:
        for ref_id, facts in rows:
            rules.log_signal_with_conn(
                conn,
                source="email",
                topic_hint=facts.get("type", ""),
                entity_text="seed@example.com",
                entity_type="email",
                content_preview="seeded",
                ref_id=ref_id,
                ref_source="email",
                extracted_facts=facts,
            )
        conn.commit()


def _seed_many(db_path: Path, type_to_count: dict[str, int]) -> None:
    rows = []
    idx = 0
    for type_name, n in type_to_count.items():
        for _ in range(n):
            rows.append((f"r-{idx}", {"type": type_name, "fields": {}}))
            idx += 1
    _seed(db_path, rows)


def _fake_llm(json_response: str) -> object:
    fake = MagicMock()
    fake.generate.return_value = json_response
    return fake


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_harmonize_response_well_formed() -> None:
    raw = json.dumps(
        {"clusters": [{"canonical": "flight_booking", "variants": ["flight", "trip"]}]}
    )
    clusters = _parse_harmonize_response(raw)
    assert len(clusters) == 1
    assert clusters[0]["canonical"] == "flight_booking"


def test_parse_harmonize_response_strips_fences() -> None:
    raw = '```json\n{"clusters": []}\n```'
    assert _parse_harmonize_response(raw) == []


def test_parse_harmonize_response_invalid_returns_empty() -> None:
    assert _parse_harmonize_response("not json") == []


# ---------------------------------------------------------------------------
# Threshold gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_harmonize_below_distinct_threshold_is_noop(db_path: Path) -> None:
    """<5 distinct types — no LLM call, no rewrites."""
    _seed_many(db_path, {"flight": 60})  # only 1 distinct type even though >50 rows
    fake_llm = _fake_llm("{}")  # would crash if called
    result = await _harmonize_extracted_fact_types(db_path, {}, fake_llm)
    assert result["clusters_merged"] == 0
    assert result["rows_rewritten"] == 0
    fake_llm.generate.assert_not_called()


@pytest.mark.asyncio
async def test_harmonize_below_row_threshold_is_noop(db_path: Path) -> None:
    """<50 total rows — no LLM call, no rewrites."""
    _seed_many(db_path, {"a": 2, "b": 2, "c": 2, "d": 2, "e": 2})  # 5 distinct, 10 rows
    fake_llm = _fake_llm("{}")
    result = await _harmonize_extracted_fact_types(db_path, {}, fake_llm)
    assert result["clusters_merged"] == 0
    fake_llm.generate.assert_not_called()


# ---------------------------------------------------------------------------
# Drift detection — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_harmonize_canonicalizes_drifting_types(db_path: Path) -> None:
    """Three near-synonymous flight labels collapse to one canonical type."""
    _seed_many(db_path, {"flight_booking": 20, "flight": 20, "trip": 15, "interview": 10, "appointment": 5})
    fake_llm = _fake_llm(
        json.dumps(
            {
                "clusters": [
                    {"canonical": "flight_booking", "variants": ["flight", "trip"]}
                ]
            }
        )
    )

    result = await _harmonize_extracted_fact_types(db_path, {}, fake_llm)

    assert result["clusters_merged"] == 1
    assert result["rows_rewritten"] == 35  # 20 (flight) + 15 (trip)

    with open_db(db_path) as conn:
        types = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT json_extract(extracted_facts, '$.type') FROM signals"
            ).fetchall()
        }
    # all flight-shaped rows now share the canonical name
    assert "flight" not in types
    assert "trip" not in types
    assert "flight_booking" in types
    # unrelated types untouched
    assert "interview" in types
    assert "appointment" in types


@pytest.mark.asyncio
async def test_harmonize_no_clusters_means_no_writes(db_path: Path) -> None:
    """When the LLM returns an empty clusters list, nothing is rewritten."""
    _seed_many(db_path, {"a": 11, "b": 11, "c": 11, "d": 11, "e": 11})
    fake_llm = _fake_llm(json.dumps({"clusters": []}))

    result = await _harmonize_extracted_fact_types(db_path, {}, fake_llm)

    assert result["types_examined"] == 5
    assert result["clusters_merged"] == 0
    assert result["rows_rewritten"] == 0


@pytest.mark.asyncio
async def test_harmonize_skips_self_referential_variants(db_path: Path) -> None:
    """A cluster where a variant equals the canonical name must not rewrite."""
    _seed_many(db_path, {"flight_booking": 20, "trip": 20, "x": 11, "y": 11, "z": 11})
    fake_llm = _fake_llm(
        json.dumps(
            {
                "clusters": [
                    {"canonical": "flight_booking", "variants": ["flight_booking", "trip"]}
                ]
            }
        )
    )

    result = await _harmonize_extracted_fact_types(db_path, {}, fake_llm)
    # only the 20 trip rows are rewritten — the self-ref is skipped
    assert result["rows_rewritten"] == 20


@pytest.mark.asyncio
async def test_harmonize_records_inference_event(db_path: Path) -> None:
    """The harmonization run leaves a trace in inference_events."""
    _seed_many(db_path, {"flight": 20, "trip": 20, "x": 11, "y": 11, "z": 11})
    fake_llm = _fake_llm(
        json.dumps(
            {"clusters": [{"canonical": "flight", "variants": ["trip"]}]}
        )
    )

    await _harmonize_extracted_fact_types(db_path, {}, fake_llm)

    with open_db(db_path) as conn:
        events = conn.execute(
            "SELECT operation FROM inference_events WHERE operation = 'tier2_harmonize'"
        ).fetchall()
    assert len(events) == 1


@pytest.mark.asyncio
async def test_harmonize_emits_span(db_path: Path) -> None:
    _seed_many(db_path, {"flight": 20, "trip": 20, "x": 11, "y": 11, "z": 11})
    fake_llm = _fake_llm(
        json.dumps({"clusters": [{"canonical": "flight", "variants": ["trip"]}]})
    )

    await _harmonize_extracted_fact_types(db_path, {}, fake_llm)

    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT operation FROM spans WHERE operation = 'extraction.tier2_harmonize'"
        ).fetchall()
    assert len(rows) >= 1


# ---------------------------------------------------------------------------
# Kill-switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_harmonize_kill_switch_short_circuits(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The env-var kill switch is enforced one level up in run_review_cycle.

    This test directly verifies that the harmonization function itself
    runs when called — the env-var gate is the run_review_cycle's
    responsibility (see the run_review_cycle test below for that
    integration). Here we verify the explicit short-circuit isn't on
    this function.
    """
    # Function does NOT consult the env var directly — the gating is in
    # run_review_cycle. This test documents that contract.
    _seed_many(db_path, {"a": 11, "b": 11, "c": 11, "d": 11, "e": 11})
    monkeypatch.setenv("XIBI_TIER2_HARMONIZE_ENABLED", "0")
    fake_llm = _fake_llm(json.dumps({"clusters": []}))
    # Should still execute (gating is upstream).
    result = await _harmonize_extracted_fact_types(db_path, {}, fake_llm)
    assert result["types_examined"] == 5


@pytest.mark.asyncio
async def test_run_review_cycle_skips_harmonize_when_disabled(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end gate: with the env var set to 0, run_review_cycle
    skips the harmonization step entirely.
    """
    from xibi.heartbeat import review_cycle as rc

    _seed_many(db_path, {"flight": 20, "trip": 20, "x": 11, "y": 11, "z": 11})
    monkeypatch.setenv("XIBI_TIER2_HARMONIZE_ENABLED", "0")

    called: list[bool] = []

    async def fake_harmonize(*a, **kw):  # type: ignore[no-untyped-def]
        called.append(True)
        return {}

    monkeypatch.setattr(rc, "_harmonize_extracted_fact_types", fake_harmonize)

    fake_llm = MagicMock()
    fake_llm.generate.return_value = "<reasoning>ok</reasoning>"
    monkeypatch.setattr(rc, "get_model", lambda **kw: fake_llm)

    await rc.run_review_cycle(db_path, {})

    assert called == []
