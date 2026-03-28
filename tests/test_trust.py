from __future__ import annotations

from pathlib import Path

import pytest

from xibi.db.migrations import SchemaManager
from xibi.trust.gradient import (
    DEFAULT_TRUST_CONFIG,
    TrustConfig,
    TrustGradient,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test_xibi.db"
    SchemaManager(path).migrate()
    return path


@pytest.fixture
def trust_gradient(db_path: Path) -> TrustGradient:
    return TrustGradient(db_path)


def test_record_success_increments_consecutive_clean(trust_gradient: TrustGradient):
    record = trust_gradient.record_success("text", "fast")
    assert record.consecutive_clean == 1

    record = trust_gradient.record_success("text", "fast")
    assert record.consecutive_clean == 2


def test_record_success_increments_total_outputs(trust_gradient: TrustGradient):
    trust_gradient.record_success("text", "fast")
    trust_gradient.record_success("text", "fast")
    record = trust_gradient.record_success("text", "fast")
    assert record.total_outputs == 3


def test_record_failure_resets_consecutive_clean(trust_gradient: TrustGradient):
    for _ in range(5):
        trust_gradient.record_success("text", "fast")

    record = trust_gradient.record_failure("text", "fast")
    assert record.consecutive_clean == 0


def test_record_failure_quality_decrements_consecutive_clean(trust_gradient: TrustGradient):
    from xibi.trust.gradient import FailureType
    for _ in range(5):
        trust_gradient.record_success("text", "fast")

    record = trust_gradient.record_failure("text", "fast", failure_type=FailureType.QUALITY_DEGRADATION)
    assert record.consecutive_clean == 4


def test_record_failure_quality_demotes_interval(db_path: Path):
    from xibi.trust.gradient import FailureType
    config = {"text.fast": TrustConfig(10, 10, True, 2, 50)}
    tg = TrustGradient(db_path, config=config)

    record = tg.record_failure("text", "fast", failure_type=FailureType.QUALITY_DEGRADATION)
    # 10 * 0.75 = 7.5 -> 7
    assert record.audit_interval == 7


def test_record_failure_increments_total_failures(trust_gradient: TrustGradient):
    trust_gradient.record_failure("text", "fast")
    record = trust_gradient.record_failure("text", "fast")
    assert record.total_failures == 2


def test_record_mixed_sequence(trust_gradient: TrustGradient):
    for _ in range(3):
        trust_gradient.record_success("text", "fast")
    trust_gradient.record_failure("text", "fast")
    trust_gradient.record_success("text", "fast")
    record = trust_gradient.record_success("text", "fast")

    assert record.consecutive_clean == 2
    assert record.total_failures == 1
    assert record.total_outputs == 6


def test_promote_after_threshold(db_path: Path):
    config = {"text.fast": TrustConfig(5, 3, True, 2, 50)}
    tg = TrustGradient(db_path, config=config)

    # Initial interval is 5
    for _ in range(2):
        tg.record_success("text", "fast")
    assert tg.get_record("text", "fast").audit_interval == 5

    # 3rd success triggers promotion: 5 -> 10
    record = tg.record_success("text", "fast")
    assert record.audit_interval == 10


def test_promote_caps_at_max_interval(db_path: Path):
    config = {"text.fast": TrustConfig(5, 3, True, 2, 6)}
    tg = TrustGradient(db_path, config=config)

    # 3rd success triggers promotion: 5 -> 10, but capped at 6
    record = tg.record_success("text", "fast")
    record = tg.record_success("text", "fast")
    record = tg.record_success("text", "fast")
    assert record.audit_interval == 6


def test_promote_resets_consecutive_clean(db_path: Path):
    config = {"text.fast": TrustConfig(5, 3, True, 2, 50)}
    tg = TrustGradient(db_path, config=config)

    # 3rd success triggers promotion
    record = tg.record_success("text", "fast")
    record = tg.record_success("text", "fast")
    record = tg.record_success("text", "fast")
    assert record.consecutive_clean == 0


def test_no_promotion_before_threshold(db_path: Path):
    config = {"text.fast": TrustConfig(5, 10, True, 2, 50)}
    tg = TrustGradient(db_path, config=config)

    for _ in range(9):
        tg.record_success("text", "fast")

    assert tg.get_record("text", "fast").audit_interval == 5


def test_demote_on_failure_halves_interval(db_path: Path):
    config = {"text.fast": TrustConfig(10, 10, True, 2, 50)}
    tg = TrustGradient(db_path, config=config)

    record = tg.record_failure("text", "fast")
    assert record.audit_interval == 5


def test_demote_floors_at_min_interval(db_path: Path):
    config = {"text.fast": TrustConfig(3, 10, True, 2, 50)}
    tg = TrustGradient(db_path, config=config)

    record = tg.record_failure("text", "fast")
    assert record.audit_interval == 2


def test_demote_disabled(db_path: Path):
    config = {"text.fast": TrustConfig(10, 10, False, 2, 50)}
    tg = TrustGradient(db_path, config=config)

    record = tg.record_failure("text", "fast")
    assert record.audit_interval == 10


def test_get_record_none_when_missing(trust_gradient: TrustGradient):
    assert trust_gradient.get_record("text", "fast") is None


def test_get_record_returns_correct_data(trust_gradient: TrustGradient):
    trust_gradient.record_success("text", "fast")
    record = trust_gradient.get_record("text", "fast")

    assert record.specialty == "text"
    assert record.effort == "fast"
    assert record.consecutive_clean == 1
    assert record.total_outputs == 1
    assert record.total_failures == 0


def test_get_all_records_multiple_roles(trust_gradient: TrustGradient):
    trust_gradient.record_success("text", "fast")
    trust_gradient.record_success("text", "think")

    records = trust_gradient.get_all_records()
    assert len(records) == 2
    assert records[0].effort == "fast"
    assert records[1].effort == "think"


def test_reset_record(trust_gradient: TrustGradient):
    for _ in range(10):
        trust_gradient.record_success("text", "fast")

    trust_gradient.reset_record("text", "fast")
    record = trust_gradient.get_record("text", "fast")

    assert record.consecutive_clean == 0
    assert record.total_outputs == 0
    assert record.audit_interval == DEFAULT_TRUST_CONFIG.initial_audit_interval


def test_custom_config_used_for_matching_role(db_path: Path):
    config = {"text.fast": TrustConfig(5, 2, True, 2, 50)}
    tg = TrustGradient(db_path, config=config)

    tg.record_success("text", "fast")
    record = tg.record_success("text", "fast")  # Should promote at 2
    assert record.audit_interval == 10


def test_default_config_used_for_missing_role(trust_gradient: TrustGradient):
    # No custom config provided to trust_gradient, uses DEFAULT_TRUST_CONFIG
    tg = trust_gradient
    tg.record_success("text", "think")
    # promote_after is 10 by default
    for _ in range(8):
        tg.record_success("text", "think")

    assert tg.get_record("text", "think").audit_interval == DEFAULT_TRUST_CONFIG.initial_audit_interval


def test_records_persist_across_instances(db_path: Path):
    tg1 = TrustGradient(db_path)
    tg1.record_success("text", "fast")
    tg1.record_success("text", "fast")

    tg2 = TrustGradient(db_path)
    record = tg2.get_record("text", "fast")
    assert record.total_outputs == 2
