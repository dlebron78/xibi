import sqlite3
import time
from unittest.mock import patch

import pytest

from xibi.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitState, FailureType


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test_breaker.db"


def test_ensure_table_failure(db_path, mocker):
    # Mock open_db to fail
    mocker.patch("xibi.circuit_breaker.open_db", side_effect=Exception("DB error"))
    # Reset ensured set for testing
    CircuitBreaker._tables_ensured = set()

    with patch("xibi.circuit_breaker.logger.warning") as mock_warn:
        CircuitBreaker("test", db_path)
        mock_warn.assert_called_once()
        assert "CircuitBreaker._ensure_table failed" in mock_warn.call_args[0][0]


def test_get_row_failure(db_path, mocker):
    cb = CircuitBreaker("test", db_path)
    # Mock open_db inside _get_row to fail
    mocker.patch("xibi.circuit_breaker.open_db", side_effect=Exception("DB error"))

    with patch("xibi.circuit_breaker.logger.warning") as mock_warn:
        row = cb._get_row()
        assert row["state"] == "closed"
        mock_warn.assert_called_with("CircuitBreaker._get_row failed, returning defaults: %s", mocker.ANY)


def test_try_insert_row_failure(db_path, mocker):
    cb = CircuitBreaker("test", db_path)
    mocker.patch("xibi.circuit_breaker.open_db", side_effect=Exception("DB error"))

    with patch("xibi.circuit_breaker.logger.warning") as mock_warn:
        cb._try_insert_row()
        mock_warn.assert_called_with("CircuitBreaker._try_insert_row failed: %s", mocker.ANY)


def test_set_state_failure(db_path, mocker):
    cb = CircuitBreaker("test", db_path)
    mocker.patch("xibi.circuit_breaker.open_db", side_effect=Exception("DB error"))

    with patch("xibi.circuit_breaker.logger.warning") as mock_warn:
        cb._set_state(CircuitState.OPEN)
        mock_warn.assert_called_with("CircuitBreaker._set_state failed: %s", mocker.ANY)


def test_increment_failure_failure(db_path, mocker):
    cb = CircuitBreaker("test", db_path)
    mocker.patch("xibi.circuit_breaker.open_db", side_effect=Exception("DB error"))

    with patch("xibi.circuit_breaker.logger.warning") as mock_warn:
        res = cb._increment_failure()
        assert res == 0
        mock_warn.assert_called_with("CircuitBreaker._increment_failure failed: %s", mocker.ANY)


def test_reset_failure(db_path, mocker):
    cb = CircuitBreaker("test", db_path)
    mocker.patch("xibi.circuit_breaker.open_db", side_effect=Exception("DB error"))

    with patch("xibi.circuit_breaker.logger.warning") as mock_warn:
        cb._reset()
        mock_warn.assert_called_with("CircuitBreaker._reset failed: %s", mocker.ANY)


def test_is_open_invalid_state(db_path, mocker):
    cb = CircuitBreaker("test", db_path)
    mocker.patch.object(cb, "_get_row", return_value={"state": "invalid"})
    assert not cb.is_open()


def test_record_success_invalid_state(db_path, mocker):
    cb = CircuitBreaker("test", db_path)
    mocker.patch.object(cb, "_get_row", return_value={"state": "invalid"})
    # Should just return without error
    cb.record_success()


def test_record_success_half_open_failure(db_path, mocker):
    cb = CircuitBreaker("test", db_path)
    # The Enum uses snake_case: "half_open"
    mocker.patch.object(cb, "_get_row", return_value={"state": "half_open"})
    mocker.patch("xibi.circuit_breaker.open_db", side_effect=Exception("DB error"))

    with patch("xibi.circuit_breaker.logger.warning") as mock_warn:
        cb.record_success()
        mock_warn.assert_called_with("CircuitBreaker.record_success (HALF_OPEN) failed: %s", mocker.ANY)


def test_record_success_closed_failure(db_path, mocker):
    cb = CircuitBreaker("test", db_path)
    mocker.patch.object(cb, "_get_row", return_value={"state": "closed"})
    mocker.patch("xibi.circuit_breaker.open_db", side_effect=Exception("DB error"))

    with patch("xibi.circuit_breaker.logger.warning") as mock_warn:
        cb.record_success()
        mock_warn.assert_called_with("CircuitBreaker.record_success (CLOSED) failed: %s", mocker.ANY)


def test_record_failure_persistent_invalid_state(db_path, mocker):
    cb = CircuitBreaker("test", db_path)
    mocker.patch.object(cb, "_increment_failure", return_value=1)
    mocker.patch.object(cb, "_get_row", return_value={"state": "invalid"})
    cb.record_failure(FailureType.PERSISTENT)


def test_record_failure_transient_failure(db_path, mocker):
    cb = CircuitBreaker("test", db_path)
    mocker.patch("xibi.circuit_breaker.open_db", side_effect=Exception("DB error"))

    with patch("xibi.circuit_breaker.logger.warning") as mock_warn:
        cb.record_failure(FailureType.TRANSIENT)
        mock_warn.assert_called_with("CircuitBreaker.record_failure (transient) failed: %s", mocker.ANY)


def test_get_status_success(db_path):
    cb = CircuitBreaker("test_status", db_path)
    status = cb.get_status()
    assert status["name"] == "test_status"
    assert status["state"] == "closed"


def test_is_open_recovery(db_path, mocker):
    config = CircuitBreakerConfig(recovery_timeout_secs=-1)
    cb = CircuitBreaker("test_recovery", db_path, config=config)
    cb.record_failure(FailureType.PERSISTENT)  # Assuming threshold is default (5)
    # Force it to open
    cb._set_state(CircuitState.OPEN, opened_at=time.time() - 100)

    assert not cb.is_open()  # should transition to HALF_OPEN and return False
    assert cb._get_row()["state"] == "half_open"


def test_record_success_half_open_to_closed(db_path):
    config = CircuitBreakerConfig(success_threshold=1)
    cb = CircuitBreaker("test_h2c", db_path, config=config)
    cb._set_state(CircuitState.HALF_OPEN)
    cb.record_success()
    assert cb._get_row()["state"] == "closed"


def test_get_row_missing_row(db_path):
    cb = CircuitBreaker("missing", db_path)
    # Manually delete the row from DB
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM circuit_breakers WHERE name = 'missing'")

    row = cb._get_row()
    assert row["state"] == "closed"
