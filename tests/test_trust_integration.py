from unittest.mock import MagicMock, patch

import pytest

from xibi.db import SchemaManager, open_db
from xibi.heartbeat.poller import HeartbeatPoller
from xibi.observation import ObservationCycle
from xibi.radiant import Radiant
from xibi.signal_intelligence import SignalIntel, enrich_signals
from xibi.trust.gradient import FailureType, TrustGradient


@pytest.fixture
def mock_profile():
    return {
        "models": {
            "text": {
                "fast": {"provider": "ollama", "model": "qwen3.5:4b"},
                "think": {"provider": "ollama", "model": "qwen3.5:9b"},
                "review": {"provider": "gemini", "model": "gemini-2.5-flash"},
            }
        },
        "providers": {
            "ollama": {"base_url": "http://localhost:11434"},
            "gemini": {"api_key_env": "GEMINI_API_KEY"},
        },
    }


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    SchemaManager(path).migrate()
    return path


@pytest.fixture
def tg(db_path):
    return TrustGradient(db_path)


# --- enrich_signals tests ---


def test_enrich_signals_records_success_on_valid_tier1(db_path, tg):
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO signals (id, source, intel_tier, content_preview) VALUES (1, 'email', 0, ''), (2, 'email', 0, '')"
        )

    mock_intels = [
        SignalIntel(signal_id=1, action_type="request"),
        SignalIntel(signal_id=2, action_type="reply"),
    ]
    with (
        patch("xibi.signal_intelligence.extract_tier1_batch", return_value=mock_intels),
        patch.object(tg, "should_audit", return_value=True),
    ):
        enrich_signals(db_path, config=None, trust_gradient=tg)

    record = tg.get_record("text", "fast")
    assert record is not None
    assert record.consecutive_clean == 1
    assert record.total_outputs == 1
    assert record.total_failures == 0


def test_enrich_signals_records_failure_on_empty_tier1(db_path, tg):
    with open_db(db_path) as conn, conn:
        conn.execute("INSERT INTO signals (id, source, intel_tier, content_preview) VALUES (1, 'email', 0, '')")

    # All fields None
    mock_intels = [SignalIntel(signal_id=1)]
    with (
        patch("xibi.signal_intelligence.extract_tier1_batch", return_value=mock_intels),
        patch.object(tg, "should_audit", return_value=True),
    ):
        enrich_signals(db_path, config=None, trust_gradient=tg)

    record = tg.get_record("text", "fast")
    assert record is not None
    assert record.total_failures == 1
    assert record.consecutive_clean == 0


def test_enrich_signals_skips_tier1_when_should_audit_false(db_path, tg):
    with open_db(db_path) as conn, conn:
        conn.execute("INSERT INTO signals (id, source, intel_tier, content_preview) VALUES (1, 'email', 0, '')")

    with (
        patch.object(tg, "should_audit", return_value=False),
        patch("xibi.signal_intelligence.extract_tier1_batch") as mock_extract,
    ):
        enrich_signals(db_path, config=None, trust_gradient=tg)
        mock_extract.assert_not_called()

    # No record created for skip
    assert tg.get_record("text", "fast") is None


def test_enrich_signals_no_trust_gradient_no_error(db_path):
    with open_db(db_path) as conn, conn:
        conn.execute("INSERT INTO signals (id, source, intel_tier, content_preview) VALUES (1, 'email', 0, '')")

    mock_intels = [SignalIntel(signal_id=1, action_type="request")]
    with patch("xibi.signal_intelligence.extract_tier1_batch", return_value=mock_intels):
        # Should not crash
        enrich_signals(db_path, config=None, trust_gradient=None)


# --- ObservationCycle tests ---


def test_observation_cycle_records_success_on_clean_loop(db_path, tg, mock_profile):
    obs = ObservationCycle(db_path, profile=mock_profile, trust_gradient=tg)

    mock_model = MagicMock()
    mock_model.generate.return_value = '{"thought": "done", "tool": "finish"}'

    with (
        patch("xibi.observation.get_model", return_value=mock_model),
        patch.object(obs, "should_run", return_value=(True, "forced")),
        patch.object(obs, "_collect_signals", return_value=[{"id": 1}]),
        patch.object(obs, "_build_observation_dump", return_value="dump"),
    ):
        res = obs.run()

    assert res.ran
    record = tg.get_record("text", "review")
    assert record is not None
    assert record.consecutive_clean == 1
    assert record.total_failures == 0


def test_observation_cycle_records_failure_on_schema_error(db_path, tg, mock_profile):
    obs = ObservationCycle(db_path, profile=mock_profile, trust_gradient=tg)

    mock_model = MagicMock()
    # First response: call a tool. Second response: finish.
    mock_model.generate.side_effect = [
        '{"thought": "nudge", "tool": "nudge", "tool_input": {"message": "hi"}}',
        '{"thought": "done", "tool": "finish"}',
    ]

    with (
        patch("xibi.observation.get_model", return_value=mock_model),
        patch("xibi.observation.dispatch", return_value={"status": "error", "retry": True}),
        patch.object(obs, "should_run", return_value=(True, "forced")),
        patch.object(obs, "_collect_signals", return_value=[{"id": 1}]),
        patch.object(obs, "_build_observation_dump", return_value="dump"),
    ):
        res = obs.run()

    assert res.ran
    record = tg.get_record("text", "review")
    assert record is not None
    assert record.total_failures == 1
    assert record.consecutive_clean == 0


def test_observation_cycle_trust_failure_never_raises(db_path, tg, mock_profile):
    obs = ObservationCycle(db_path, profile=mock_profile, trust_gradient=tg)
    mock_model = MagicMock()
    mock_model.generate.return_value = '{"thought": "done", "tool": "finish"}'
    with (
        patch("xibi.observation.get_model", return_value=mock_model),
        patch.object(tg, "record_success", side_effect=Exception("DB fail")),
        patch.object(obs, "should_run", return_value=(True, "forced")),
        patch.object(obs, "_collect_signals", return_value=[{"id": 1}]),
        patch.object(obs, "_build_observation_dump", return_value="dump"),
    ):
        # Should not crash
        obs.run()


def test_observation_cycle_no_trust_gradient_no_error(db_path, mock_profile):
    obs = ObservationCycle(db_path, profile=mock_profile, trust_gradient=None)
    mock_model = MagicMock()
    mock_model.generate.return_value = '{"thought": "done", "tool": "finish"}'
    with (
        patch("xibi.observation.get_model", return_value=mock_model),
        patch.object(obs, "should_run", return_value=(True, "forced")),
        patch.object(obs, "_collect_signals", return_value=[{"id": 1}]),
        patch.object(obs, "_build_observation_dump", return_value="dump"),
    ):
        obs.run()


# --- Radiant tests ---


def test_run_audit_demotes_trust_on_low_quality(db_path, tg, mock_profile):
    radiant = Radiant(db_path, profile=mock_profile)
    # Insert some observation cycles to audit
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO observation_cycles (id, completed_at, signals_processed, actions_taken) VALUES (1, '2023-01-01 00:00:00', 5, '[]')"
        )

    mock_model = MagicMock()
    # quality_score < threshold (0.6)
    mock_model.generate_structured.return_value = {"quality_score": 0.4, "findings": [], "summary": "Poor"}
    with (
        patch("xibi.radiant._audit_run_date", ""),
        patch("xibi.radiant.get_model", return_value=mock_model),
    ):
        radiant.run_audit(MagicMock(), trust_gradient=tg)

    record = tg.get_record("text", "review")
    assert record is not None
    assert record.total_failures == 1
    assert record.last_failure_type == FailureType.QUALITY_DEGRADATION.value


def test_run_audit_promotes_trust_on_high_quality(db_path, tg, mock_profile):
    radiant = Radiant(db_path, profile=mock_profile)
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO observation_cycles (id, completed_at, signals_processed, actions_taken) VALUES (1, '2023-01-01 00:00:00', 5, '[]')"
        )

    mock_model = MagicMock()
    mock_model.generate_structured.return_value = {"quality_score": 0.9, "findings": [], "summary": "Great"}
    with (
        patch("xibi.radiant._audit_run_date", ""),
        patch("xibi.radiant.get_model", return_value=mock_model),
    ):
        radiant.run_audit(MagicMock(), trust_gradient=tg)

    record = tg.get_record("text", "review")
    assert record is not None
    assert record.consecutive_clean == 1


def test_run_audit_no_trust_gradient_no_error(db_path, mock_profile):
    radiant = Radiant(db_path, profile=mock_profile)
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO observation_cycles (id, completed_at, signals_processed, actions_taken) VALUES (1, '2023-01-01 00:00:00', 5, '[]')"
        )
    mock_model = MagicMock()
    mock_model.generate_structured.return_value = {"quality_score": 0.9, "findings": [], "summary": "Great"}
    with (
        patch("xibi.radiant._audit_run_date", ""),
        patch("xibi.radiant.get_model", return_value=mock_model),
    ):
        radiant.run_audit(MagicMock(), trust_gradient=None)


def test_summary_trust_key_empty(db_path):
    radiant = Radiant(db_path)
    s = radiant.summary()
    assert s["trust"] == {"records": [], "roles_tracked": 0, "any_demoted": False}


def test_summary_trust_key_with_records(db_path, tg):
    tg.record_success("text", "fast")
    tg.record_success("text", "fast")

    radiant = Radiant(db_path)
    s = radiant.summary()
    assert s["trust"]["roles_tracked"] == 1
    assert s["trust"]["records"][0]["role"] == "text.fast"
    assert s["trust"]["records"][0]["total_outputs"] == 2


def test_summary_trust_any_demoted_false_at_default(db_path, tg):
    tg.record_success("text", "fast")
    radiant = Radiant(db_path)
    s = radiant.summary()
    # default interval is 5
    assert s["trust"]["any_demoted"] is False


def test_summary_trust_any_demoted_true_after_failure(db_path, tg):
    # record persistent failure twice to drop interval below 5 (5 // 2 = 2, 2 // 2 = 1)
    tg.record_failure("text", "fast", FailureType.PERSISTENT)
    tg.record_failure("text", "fast", FailureType.PERSISTENT)

    radiant = Radiant(db_path)
    s = radiant.summary()
    assert s["trust"]["any_demoted"] is True


# --- HeartbeatPoller tests ---


def test_poller_creates_trust_gradient_if_db_path_set(tmp_path):
    db_path = tmp_path / "test.db"
    SchemaManager(db_path).migrate()
    hp = HeartbeatPoller(tmp_path, db_path, MagicMock(), MagicMock(), [1])
    assert hp.trust_gradient is not None
    assert isinstance(hp.trust_gradient, TrustGradient)


def test_poller_uses_provided_trust_gradient(tmp_path):
    db_path = tmp_path / "test.db"
    SchemaManager(db_path).migrate()
    tg = TrustGradient(db_path)
    hp = HeartbeatPoller(tmp_path, db_path, MagicMock(), MagicMock(), [1], trust_gradient=tg)
    assert hp.trust_gradient is tg


def test_poller_passes_trust_to_run_audit(tmp_path):
    db_path = tmp_path / "test.db"
    SchemaManager(db_path).migrate()
    radiant = MagicMock()
    hp = HeartbeatPoller(tmp_path, db_path, MagicMock(), MagicMock(), [1], radiant=radiant)

    with (
        patch.object(hp, "_is_quiet_hours", return_value=False),
        patch.object(hp, "_check_email", return_value=[]),
        patch("xibi.signal_intelligence.enrich_signals") as mock_enrich,
    ):
        mock_enrich.return_value = 0
        # Trigger audit tick
        hp.profile["audit_interval_ticks"] = 1
        hp.tick()

    radiant.run_audit.assert_called()
    # Check that trust_gradient was passed as kwarg
    args, kwargs = radiant.run_audit.call_args
    assert kwargs["trust_gradient"] == hp.trust_gradient


def test_poller_no_db_path_no_trust_gradient(tmp_path):
    # If db_path is None (unlikely in real use but for tests), trust_gradient stays None
    hp = HeartbeatPoller(tmp_path, None, MagicMock(), MagicMock(), [1])
    assert hp.trust_gradient is None
