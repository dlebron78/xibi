from __future__ import annotations

import logging
from pathlib import Path

import pytest

from xibi.db.migrations import SchemaManager
from xibi.trust.gradient import (
    FailureType,
    TrustConfig,
    TrustGradient,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test_xibi_hardening.db"
    SchemaManager(path).migrate()
    return path


def test_probabilistic_audit_respects_interval(db_path: Path):
    # audit_interval = 10, expect ~100 audits in 1000 calls
    config = {"text.fast": TrustConfig(10, 1000, True, 2, 50)}
    tg = TrustGradient(db_path, config=config, seed=42)

    audits = sum(1 for _ in range(1000) if tg.should_audit("text", "fast"))
    # 1/10 * 1000 = 100. Allow 20% tolerance: 80 to 120.
    assert 80 <= audits <= 120


def test_probabilistic_audit_no_record_uses_initial(db_path: Path):
    config = {"text.fast": TrustConfig(5, 1000, True, 2, 50)}
    tg = TrustGradient(db_path, config=config, seed=42)

    audits = sum(1 for _ in range(1000) if tg.should_audit("text", "fast"))
    # 1/5 * 1000 = 200. Allow 20% tolerance: 160 to 240.
    assert 160 <= audits <= 240


def test_probabilistic_audit_seeded_is_deterministic(db_path: Path):
    config = {"text.fast": TrustConfig(10, 1000, True, 2, 50)}
    tg1 = TrustGradient(db_path, config=config, seed=42)
    tg2 = TrustGradient(db_path, config=config, seed=42)

    results1 = [tg1.should_audit("text", "fast") for _ in range(100)]
    results2 = [tg2.should_audit("text", "fast") for _ in range(100)]
    assert results1 == results2


def test_transient_failure_gentle_demote(db_path: Path):
    config = {"text.fast": TrustConfig(20, 10, True, 2, 50)}
    tg = TrustGradient(db_path, config=config)

    # Initial audit_interval is 20
    record = tg.record_failure("text", "fast", failure_type=FailureType.TRANSIENT)
    # 20 * 0.75 = 15
    assert record.audit_interval == 15


def test_persistent_failure_hard_demote(db_path: Path):
    config = {"text.fast": TrustConfig(20, 10, True, 2, 50)}
    tg = TrustGradient(db_path, config=config)

    record = tg.record_failure("text", "fast", failure_type=FailureType.PERSISTENT)
    # 20 // 2 = 10
    assert record.audit_interval == 10


def test_transient_failure_floors_at_min(db_path: Path):
    config = {"text.fast": TrustConfig(3, 10, True, 2, 50)}
    tg = TrustGradient(db_path, config=config)

    record = tg.record_failure("text", "fast", failure_type=FailureType.TRANSIENT)
    # 3 * 0.75 = 2.25 -> 2. min_interval = 2.
    assert record.audit_interval == 2


def test_default_failure_type_is_persistent(db_path: Path):
    config = {"text.fast": TrustConfig(20, 10, True, 2, 50)}
    tg = TrustGradient(db_path, config=config)

    record = tg.record_failure("text", "fast")
    assert record.audit_interval == 10  # halved


def test_last_failure_type_stored(db_path: Path):
    tg = TrustGradient(db_path)
    tg.record_failure("text", "fast", failure_type=FailureType.TRANSIENT)

    record = tg.get_record("text", "fast")
    assert record.last_failure_type == "transient"


def test_model_hash_stored_on_first_output(db_path: Path):
    role_configs = {"text.fast": {"model": "gpt-4"}}
    tg = TrustGradient(db_path, role_configs=role_configs)

    record = tg.record_success("text", "fast")
    assert record.model_hash is not None
    assert len(record.model_hash) == 16


def test_model_hash_unchanged_no_reset(db_path: Path):
    role_configs = {"text.fast": {"model": "gpt-4"}}
    tg = TrustGradient(db_path, role_configs=role_configs)

    tg.record_success("text", "fast")
    record = tg.record_success("text", "fast")
    assert record.consecutive_clean == 2


def test_model_hash_changed_triggers_reset(db_path: Path, caplog):
    caplog.set_level(logging.INFO)
    role_configs1 = {"text.fast": {"model": "gpt-4"}}
    tg1 = TrustGradient(db_path, role_configs=role_configs1)
    tg1.record_success("text", "fast")
    tg1.record_success("text", "fast")

    role_configs2 = {"text.fast": {"model": "gpt-4o"}}
    tg2 = TrustGradient(db_path, role_configs=role_configs2)

    # This should trigger reset BEFORE applying the success
    record = tg2.record_success("text", "fast")

    assert record.consecutive_clean == 1  # reset to 0, then +1
    assert "Model changed for text.fast" in caplog.text


def test_model_hash_none_disables_tracking(db_path: Path):
    # No role_configs provided
    tg = TrustGradient(db_path)
    record = tg.record_success("text", "fast")
    assert record.model_hash is None


def test_model_hash_reset_logs_event(db_path: Path, caplog):
    caplog.set_level(logging.INFO)
    role_configs = {"text.fast": {"model": "v1"}}
    tg = TrustGradient(db_path, role_configs=role_configs)
    tg.record_success("text", "fast")

    tg._role_configs = {"text.fast": {"model": "v2"}}
    tg.record_success("text", "fast")
    assert "Model changed for text.fast" in caplog.text


def test_full_lifecycle(db_path: Path):
    config = {"text.fast": TrustConfig(5, 3, True, 2, 50)}
    role_configs = {"text.fast": {"model": "v1"}}
    tg = TrustGradient(db_path, config=config, role_configs=role_configs)

    # 3 successes -> promote (5 -> 10)
    for _ in range(3):
        tg.record_success("text", "fast")
    assert tg.get_record("text", "fast").audit_interval == 10

    # model swap -> reset (10 -> 5)
    tg._role_configs = {"text.fast": {"model": "v2"}}
    record = tg.record_success("text", "fast")
    assert record.audit_interval == 5
    assert record.consecutive_clean == 1

    # 2 more successes (total 3 on new model) -> promote (5 -> 10)
    tg.record_success("text", "fast")
    record = tg.record_success("text", "fast")
    assert record.audit_interval == 10

    # transient failure -> gentle demote (10 * 0.75 = 7)
    record = tg.record_failure("text", "fast", failure_type=FailureType.TRANSIENT)
    assert record.audit_interval == 7

    # persistent failure -> hard demote (7 // 2 = 3)
    record = tg.record_failure("text", "fast", failure_type=FailureType.PERSISTENT)
    assert record.audit_interval == 3
