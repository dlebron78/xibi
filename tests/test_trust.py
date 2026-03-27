from __future__ import annotations

from pathlib import Path

import pytest

from xibi.db.migrations import SchemaManager
from xibi.trust.gradient import DEFAULT_TRUST_CONFIG, TrustConfig, TrustGradient


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test_xibi.db"
    SchemaManager(path).migrate()
    return path


def test_record_success_increments_consecutive_clean(db_path: Path):
    tg = TrustGradient(db_path)
    record = tg.record_success("text", "fast")
    assert record.consecutive_clean == 1


def test_record_success_increments_total_outputs(db_path: Path):
    tg = TrustGradient(db_path)
    tg.record_success("text", "fast")
    tg.record_success("text", "fast")
    record = tg.record_success("text", "fast")
    assert record.total_outputs == 3


def test_record_failure_resets_consecutive_clean(db_path: Path):
    tg = TrustGradient(db_path)
    for _ in range(5):
        tg.record_success("text", "fast")
    record = tg.record_failure("text", "fast")
    assert record.consecutive_clean == 0


def test_record_failure_increments_total_failures(db_path: Path):
    tg = TrustGradient(db_path)
    tg.record_failure("text", "fast")
    record = tg.record_failure("text", "fast")
    assert record.total_failures == 2


def test_record_mixed_sequence(db_path: Path):
    tg = TrustGradient(db_path)
    tg.record_success("text", "fast")
    tg.record_success("text", "fast")
    tg.record_success("text", "fast")
    tg.record_failure("text", "fast")
    tg.record_success("text", "fast")
    record = tg.record_success("text", "fast")
    assert record.consecutive_clean == 2
    assert record.total_failures == 1


def test_promote_after_threshold(db_path: Path):
    config = {"text.fast": TrustConfig(5, 3, True, 2, 50)}
    tg = TrustGradient(db_path, config=config)
    # initial audit_interval = 5 (from config)
    tg.record_success("text", "fast")
    tg.record_success("text", "fast")
    record = tg.record_success("text", "fast")
    # After 3 successes, audit_interval should double: 5 -> 10
    assert record.audit_interval == 10


def test_promote_caps_at_max_interval(db_path: Path):
    config = {"text.fast": TrustConfig(5, 3, True, 2, 6)}
    tg = TrustGradient(db_path, config=config)
    tg.record_success("text", "fast")
    tg.record_success("text", "fast")
    record = tg.record_success("text", "fast")
    # 5 * 2 = 10, but capped at 6
    assert record.audit_interval == 6


def test_promote_resets_consecutive_clean(db_path: Path):
    config = {"text.fast": TrustConfig(5, 3, True, 2, 50)}
    tg = TrustGradient(db_path, config=config)
    tg.record_success("text", "fast")
    tg.record_success("text", "fast")
    record = tg.record_success("text", "fast")
    assert record.consecutive_clean == 0


def test_no_promotion_before_threshold(db_path: Path):
    config = {"text.fast": TrustConfig(5, 10, True, 2, 50)}
    tg = TrustGradient(db_path, config=config)
    for _ in range(9):
        record = tg.record_success("text", "fast")
    assert record.audit_interval == 5
    assert record.consecutive_clean == 9


def test_demote_on_failure_halves_interval(db_path: Path):
    config = {"text.fast": TrustConfig(10, 10, True, 2, 50)}
    tg = TrustGradient(db_path, config=config)
    record = tg.record_failure("text", "fast")
    assert record.audit_interval == 5


def test_demote_floors_at_min_interval(db_path: Path):
    config = {"text.fast": TrustConfig(3, 10, True, 2, 50)}
    tg = TrustGradient(db_path, config=config)
    record = tg.record_failure("text", "fast")
    # 3 // 2 = 1, but floor at 2
    assert record.audit_interval == 2


def test_demote_disabled(db_path: Path):
    config = {"text.fast": TrustConfig(10, 10, False, 2, 50)}
    tg = TrustGradient(db_path, config=config)
    record = tg.record_failure("text", "fast")
    assert record.audit_interval == 10


def test_should_audit_every_nth(db_path: Path):
    config = {"text.fast": TrustConfig(5, 10, True, 2, 50)}
    tg = TrustGradient(db_path, config=config)
    # total_outputs = 0, should_audit = 0 % 5 == 0 -> True
    assert tg.should_audit("text", "fast") is True
    tg.record_success("text", "fast")  # total_outputs = 1
    assert tg.should_audit("text", "fast") is False
    tg.record_success("text", "fast")  # 2
    tg.record_success("text", "fast")  # 3
    tg.record_success("text", "fast")  # 4
    assert tg.should_audit("text", "fast") is False
    tg.record_success("text", "fast")  # 5
    assert tg.should_audit("text", "fast") is True


def test_should_audit_no_record_defaults_to_initial_config(db_path: Path):
    config = {"text.fast": TrustConfig(7, 10, True, 2, 50)}
    tg = TrustGradient(db_path, config=config)
    assert tg.should_audit("text", "fast") is True  # 0 % 7 == 0


def test_should_audit_after_promotion_less_frequent(db_path: Path):
    # Set promote_after high enough so it doesn't promote again during the test
    config = {"text.fast": TrustConfig(5, 3, True, 2, 50)}
    tg = TrustGradient(db_path, config=config)
    tg.record_success("text", "fast")
    tg.record_success("text", "fast")
    tg.record_success("text", "fast")
    # audit_interval is now 10, total_outputs is 3
    record = tg.get_record("text", "fast")
    assert record.audit_interval == 10
    assert tg.should_audit("text", "fast") is False  # 3 % 10 != 0

    # Update config to prevent further promotion for this test
    tg.config["text.fast"] = TrustConfig(5, 100, True, 2, 50)

    for _ in range(6):
        tg.record_success("text", "fast")
    # total_outputs is 9
    assert tg.should_audit("text", "fast") is False
    tg.record_success("text", "fast")  # total_outputs is 10
    assert tg.should_audit("text", "fast") is True


def test_get_record_none_when_missing(db_path: Path):
    tg = TrustGradient(db_path)
    assert tg.get_record("nonexistent", "fast") is None


def test_get_record_returns_correct_data(db_path: Path):
    tg = TrustGradient(db_path)
    tg.record_success("text", "fast")
    record = tg.get_record("text", "fast")
    assert record.specialty == "text"
    assert record.effort == "fast"
    assert record.consecutive_clean == 1
    assert record.total_outputs == 1


def test_get_all_records_multiple_roles(db_path: Path):
    tg = TrustGradient(db_path)
    tg.record_success("text", "fast")
    tg.record_success("text", "think")
    records = tg.get_all_records()
    assert len(records) == 2
    assert records[0].effort == "fast"
    assert records[1].effort == "think"


def test_reset_record(db_path: Path):
    tg = TrustGradient(db_path)
    for _ in range(10):
        tg.record_success("text", "fast")
    tg.reset_record("text", "fast")
    record = tg.get_record("text", "fast")
    assert record.audit_interval == DEFAULT_TRUST_CONFIG.initial_audit_interval
    assert record.total_outputs == 0
    assert record.consecutive_clean == 0


def test_custom_config_used_for_matching_role(db_path: Path):
    config = {"text.fast": TrustConfig(5, 2, True, 2, 50)}
    tg = TrustGradient(db_path, config=config)
    tg.record_success("text", "fast")
    record = tg.record_success("text", "fast")
    assert record.audit_interval == 10  # promoted after 2


def test_default_config_used_for_missing_role(db_path: Path):
    tg = TrustGradient(db_path, config={})
    record = tg.record_success("text", "think")
    assert record.audit_interval == DEFAULT_TRUST_CONFIG.initial_audit_interval


def test_records_persist_across_instances(db_path: Path):
    tg1 = TrustGradient(db_path)
    for _ in range(5):
        tg1.record_success("text", "fast")

    tg2 = TrustGradient(db_path)
    record = tg2.get_record("text", "fast")
    assert record.total_outputs == 5
