"""Step-112: Tier 2 extractor registry tests.

Per TRR condition #4 + checklist line 730: register a fake non-email source's
Tier 2 extractor, verify dispatch via ``Tier2ExtractorRegistry.get()``, and
exercise end-to-end through the shared write path. This is the load-bearing
abstraction proof — the whole point of the registry is that the next source
plugs in via decorator without touching email's code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._helpers import _migrated_db
from xibi.alerting.rules import RuleEngine
from xibi.db import open_db
from xibi.heartbeat.tier2_extractors import Tier2ExtractorRegistry


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return _migrated_db(tmp_path)


def test_email_extractor_is_registered_by_default() -> None:
    assert Tier2ExtractorRegistry.has("email")
    assert "email" in Tier2ExtractorRegistry.sources()


def test_get_returns_none_for_unregistered_source() -> None:
    assert Tier2ExtractorRegistry.get("never-registered-source") is None


def test_register_decorator_adds_a_new_source() -> None:
    """A fake source plugs in via decorator with no other code touched."""

    @Tier2ExtractorRegistry.register("test_source_alpha")
    def fake_extractor(signal: dict, body: str | None, model: str) -> dict | None:
        return {"type": "fake_fact", "fields": {"echo": signal.get("ref_id")}}

    try:
        assert Tier2ExtractorRegistry.has("test_source_alpha")
        fn = Tier2ExtractorRegistry.get("test_source_alpha")
        assert fn is not None
        result = fn({"ref_id": "abc"}, None, "model-x")
        assert result == {"type": "fake_fact", "fields": {"echo": "abc"}}
    finally:
        Tier2ExtractorRegistry._registry.pop("test_source_alpha", None)


def test_fake_source_dispatches_through_shared_write_path(db_path: Path) -> None:
    """End-to-end: register fake source → dispatch via registry → write
    extracted_facts via shared :meth:`RuleEngine.log_signal_with_conn`.

    This is the proof that the substrate is source-agnostic. Email today,
    Slack tomorrow, calendar next week — all flow through the same write.
    """

    @Tier2ExtractorRegistry.register("test_source_beta")
    def fake_extractor(signal: dict, body: str | None, model: str) -> dict | None:
        return {
            "type": "test_event",
            "fields": {"id": signal["ref_id"], "kind": "synthetic"},
        }

    try:
        signal = {
            "source": "test_source_beta",
            "ref_id": "fake-001",
            "ref_source": "test_source_beta",
            "entity_text": "fake-entity",
            "entity_type": "test",
            "topic_hint": "fake topic",
        }

        extractor = Tier2ExtractorRegistry.get("test_source_beta")
        assert extractor is not None
        facts = extractor(signal, body=None, model="any")
        assert facts is not None
        assert facts["type"] == "test_event"

        # Shared write path — no source-specific branch.
        rules = RuleEngine(db_path)
        with open_db(db_path) as conn:
            rules.log_signal_with_conn(
                conn,
                source=signal["source"],
                topic_hint=signal["topic_hint"],
                entity_text=signal["entity_text"],
                entity_type=signal["entity_type"],
                content_preview="synthetic preview",
                ref_id=signal["ref_id"],
                ref_source=signal["ref_source"],
                extracted_facts=facts,
            )
            conn.commit()

        with open_db(db_path) as conn:
            row = conn.execute(
                "SELECT source, extracted_facts FROM signals WHERE ref_id = 'fake-001'"
            ).fetchone()

        assert row is not None
        assert row[0] == "test_source_beta"
        assert json.loads(row[1]) == facts
    finally:
        Tier2ExtractorRegistry._registry.pop("test_source_beta", None)


def test_registry_is_a_parallel_pattern_not_a_clone_of_signal_extractor_registry() -> None:
    """Per TRR condition #4: Tier 2 registry deliberately diverges from
    :class:`xibi.heartbeat.extractors.SignalExtractorRegistry`. They share
    the decorator idiom; everything else differs by design.
    """
    from xibi.heartbeat.extractors import SignalExtractorRegistry

    # Different storage attribute names — proves they're parallel, not aliased.
    assert hasattr(Tier2ExtractorRegistry, "_registry")
    assert hasattr(SignalExtractorRegistry, "extractors")

    # Different accessor surface: Tier 2 exposes get/has/sources;
    # Tier 1 exposes extract() with a generic fallback semantics.
    assert hasattr(Tier2ExtractorRegistry, "get")
    assert hasattr(Tier2ExtractorRegistry, "has")
    assert hasattr(Tier2ExtractorRegistry, "sources")
    assert hasattr(SignalExtractorRegistry, "extract")
