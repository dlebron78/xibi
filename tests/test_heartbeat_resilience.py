import asyncio
import contextlib
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xibi.__main__ import cmd_heartbeat
from xibi.heartbeat.poller import (
    _PHASE0_TIMEOUT_SECS,
    _PHASE3_TIMEOUT_SECS,
    HeartbeatPoller,
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test_xibi.db"
    import xibi.db

    xibi.db.migrate(path)
    return path


@pytest.fixture
def mock_poller(db_path):
    poller = HeartbeatPoller.__new__(HeartbeatPoller)
    poller.source_poller = AsyncMock()
    poller.db_path = db_path
    poller.rules = MagicMock()
    poller.allowed_chat_ids = [123]
    poller.adapter = MagicMock()
    poller.profile = {"audit_interval_ticks": 20}
    poller.config = {"profile": poller.profile}
    poller.signal_intelligence_enabled = True
    poller.observation_cycle = MagicMock()
    poller.observation_cycle.run = MagicMock()
    poller.radiant = MagicMock()
    poller.radiant.ceiling_status.return_value = {"throttle": False}
    poller._jules_watcher = MagicMock()
    poller.config_path = "/tmp/config.json"
    poller.trust_gradient = MagicMock()
    poller._last_reflection_date = None
    poller._audit_tick_counter = 0
    poller.executor = MagicMock()
    poller.skills_dir = Path("/tmp/skills")

    # Mock methods that would otherwise run
    poller._is_quiet_hours = MagicMock(return_value=False)
    poller._sweep_thread_lifecycle = MagicMock()
    poller._broadcast = MagicMock()
    return poller


@pytest.mark.asyncio
async def test_phase0_timeout_continues_to_phase2(mock_poller):
    # Mock poll_due_sources to hang
    async def slow_poll():
        await asyncio.sleep(10)
        return [{"source": "slow", "extractor": "job", "data": {}}]

    mock_poller.source_poller.poll_due_sources.side_effect = slow_poll

    # We need to monkeypatch the timeout constant to be small for the test
    with (
        patch("xibi.heartbeat.poller._PHASE0_TIMEOUT_SECS", 0.1),
        patch("xibi.heartbeat.poller.SignalExtractorRegistry.extract") as mock_extract,
    ):
        await mock_poller.async_tick()

        # Verify Phase 0 timed out (poll_results should be empty list)
        # and Phase 2 (extraction) was skipped but didn't crash
        assert mock_extract.call_count == 0


@pytest.mark.asyncio
async def test_phase0_exception_continues_to_phase2(mock_poller):
    mock_poller.source_poller.poll_due_sources.side_effect = RuntimeError("Phase 0 Boom")

    with patch("xibi.heartbeat.poller.SignalExtractorRegistry.extract") as mock_extract:
        await mock_poller.async_tick()
        # poll_results defaults to [] on Phase 0 error
        assert mock_extract.call_count == 0


@pytest.mark.asyncio
async def test_phase2_timeout_partial_processing(mock_poller):
    # Use multiple results
    results = [{"source": f"s{i}", "extractor": "e", "data": {}} for i in range(5)]
    mock_poller.source_poller.poll_due_sources.return_value = results

    with patch("xibi.heartbeat.poller.SignalExtractorRegistry.extract") as mock_extract:
        # Patch time.monotonic ONLY in the poller module
        start_time = 1000.0
        current_time = [start_time]

        def mock_monotonic():
            return current_time[0]

        with patch("xibi.heartbeat.poller.time.monotonic", side_effect=mock_monotonic):
            # side_effect for mock_extract to advance time!
            def extract_side_effect(*args, **kwargs):
                current_time[0] += 100.0  # Advance time past 60s limit
                return []

            mock_extract.side_effect = extract_side_effect

            # Since async_tick uses "from asyncio import wait_for", and wait_for uses loop.time(),
            # patching xibi.heartbeat.poller.time.monotonic is safe as it doesn't affect asyncio.

            await mock_poller.async_tick()

            # It should have called extract exactly once, then timed out and breaked.
            assert mock_extract.call_count == 1


@pytest.mark.asyncio
async def test_phase3_subtask_isolation_signal_intel_crash(mock_poller):
    with patch("xibi.heartbeat.poller.sig_intel.enrich_signals") as mock_enrich:
        mock_enrich.side_effect = RuntimeError("Intel Boom")

        # Ensure _run_phase3 is awaited
        await mock_poller._run_phase3()

        # observation_cycle.run is sync (MagicMock); it should be called
        assert mock_poller.observation_cycle.run.called
        assert mock_poller._jules_watcher.poll.called


@pytest.mark.asyncio
async def test_phase3_subtask_isolation_observation_crash(mock_poller):
    mock_poller.observation_cycle.run.side_effect = RuntimeError("Obs Boom")

    await mock_poller._run_phase3()

    assert mock_poller._jules_watcher.poll.called
    # In _run_phase3 3d: if self.radiant: ... self.radiant.run_audit(...)
    mock_poller._audit_tick_counter = 100
    await mock_poller._run_phase3()
    assert mock_poller.radiant.run_audit.called


@pytest.mark.asyncio
async def test_phase3_subtask_isolation_jules_crash(mock_poller):
    mock_poller._jules_watcher.poll.side_effect = RuntimeError("Jules Boom")
    await mock_poller._run_phase3()
    # Radiant audit should still be attempted (3d)
    mock_poller._audit_tick_counter = 100
    await mock_poller._run_phase3()
    assert mock_poller.radiant.run_audit.called


@pytest.mark.asyncio
async def test_phase3_subtask_isolation_radiant_crash(mock_poller):
    mock_poller.radiant.run_audit.side_effect = RuntimeError("Radiant Boom")
    mock_poller._audit_tick_counter = 100
    await mock_poller._run_phase3()
    # Should not crash the whole method


@pytest.mark.asyncio
async def test_phase3_timeout_logged_not_raised(mock_poller):
    async def slow_phase3():
        await asyncio.sleep(10)

    with (
        patch.object(mock_poller, "_run_phase3", side_effect=slow_phase3),
        patch("xibi.heartbeat.poller._PHASE3_TIMEOUT_SECS", 0.1),
    ):
        # Should not raise TimeoutError, should be caught and logged
        await mock_poller.async_tick()


def test_logging_configured_in_heartbeat_command(tmp_path):
    args = MagicMock()
    workdir = tmp_path / "fake_workdir"
    workdir.mkdir()
    (workdir / "data").mkdir()
    args.workdir = str(workdir)
    args.config = None

    config_path = workdir / "config.json"
    config_path.write_text('{"profile": {}}')

    # Mock necessary parts to avoid real initialization
    with (
        patch("xibi.db.migrate"),
        patch("xibi.router.init_telemetry"),
        patch("xibi.mcp.registry.MCPServerRegistry"),
        patch("xibi.executor.LocalHandlerExecutor"),
        patch("xibi.channels.telegram.TelegramAdapter"),
        patch("xibi.heartbeat.poller.HeartbeatPoller") as mock_poller_cls,
        patch("xibi.db.open_db"),
        patch("os.environ", {"XIBI_TELEGRAM_TOKEN": "fake_token", "XIBI_WORKDIR": str(workdir)}),
    ):
        mock_poller = mock_poller_cls.return_value
        mock_poller.run.side_effect = KeyboardInterrupt()  # Exit loop

        # Clear existing handlers to test basicConfig
        logging.root.handlers = []

        with contextlib.suppress(KeyboardInterrupt, SystemExit):
            cmd_heartbeat(args)

        assert len(logging.root.handlers) > 0
        assert any(isinstance(h, logging.StreamHandler) for h in logging.root.handlers)
        assert logging.root.level == logging.INFO


def test_phase0_timeout_value_is_90_seconds():
    assert _PHASE0_TIMEOUT_SECS == 90


def test_phase3_timeout_value_is_180_seconds():
    assert _PHASE3_TIMEOUT_SECS == 180


def test_phase3_signals_not_passed_to_run_phase3(mock_poller):
    import inspect

    sig = inspect.signature(mock_poller._run_phase3)
    # Instance method signature should have 0 parameters when inspected from the instance
    assert len(sig.parameters) == 0


@pytest.mark.asyncio
async def test_phase2_exception_continues_loop(mock_poller):
    mock_poller.source_poller.poll_due_sources.return_value = [
        {"source": "s1", "extractor": "e1", "data": {}},
        {"source": "s2", "extractor": "e2", "data": {}},
    ]

    with patch("xibi.heartbeat.poller.SignalExtractorRegistry.extract") as mock_extract:
        # First one fails, second one should still be attempted
        mock_extract.side_effect = [RuntimeError("Extraction Boom"), []]

        await mock_poller.async_tick()

        assert mock_extract.call_count == 2


@pytest.mark.asyncio
async def test_phase1_error_continues_to_phase2(mock_poller):
    mock_poller.source_poller.poll_due_sources.return_value = []
    with patch("xibi.db.open_db", side_effect=RuntimeError("DB Boom")):
        # Should log Phase 1 error but continue
        await mock_poller.async_tick()


@pytest.mark.asyncio
async def test_phase2_skips_error_results(mock_poller):
    mock_poller.source_poller.poll_due_sources.return_value = [
        {"source": "s1", "error": "some error", "data": None, "extractor": "e1"}
    ]
    with patch("xibi.heartbeat.poller.SignalExtractorRegistry.extract") as mock_extract:
        await mock_poller.async_tick()
        assert mock_extract.call_count == 0


@pytest.mark.asyncio
async def test_run_phase3_skips_optional_components(mock_poller):
    mock_poller.signal_intelligence_enabled = False
    mock_poller.observation_cycle = None
    mock_poller._jules_watcher = None
    mock_poller.radiant = None

    await mock_poller._run_phase3()
    # Should not crash


def test_sweep_thread_lifecycle_gate_error(mock_poller):
    # Force HeartbeatPoller._sweep_thread_lifecycle to run (it was mocked in fixture)
    orig_sweep = HeartbeatPoller._sweep_thread_lifecycle
    with patch("xibi.db.open_db", side_effect=RuntimeError("DB Boom")):
        orig_sweep(mock_poller)
        # Should catch and return


def test_sweep_thread_lifecycle_sweep_error(mock_poller):
    # Force HeartbeatPoller._sweep_thread_lifecycle to run
    orig_sweep = HeartbeatPoller._sweep_thread_lifecycle
    # Mock the gate to pass
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = None  # Not run today
    with patch("xibi.db.open_db") as mock_open:
        mock_open.return_value.__enter__.return_value = mock_conn
        with patch("xibi.heartbeat.poller.sweep_stale_threads", side_effect=RuntimeError("Sweep Boom")):
            orig_sweep(mock_poller)
            # Should catch
