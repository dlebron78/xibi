from datetime import datetime, timedelta, timezone

import pytest

from xibi.scheduling.triggers import compute_next_run


def test_interval_trigger():
    config = {"every_seconds": 3600}
    now = datetime.now(timezone.utc)
    next_run = compute_next_run("interval", config, now)
    assert next_run == now + timedelta(seconds=3600)


def test_oneshot_trigger():
    at_dt = datetime.now(timezone.utc) + timedelta(minutes=10)
    config = {"at": at_dt.isoformat()}

    # Before 'at' time
    now = at_dt - timedelta(minutes=5)
    next_run = compute_next_run("oneshot", config, now)
    assert next_run == at_dt

    # After 'at' time
    now = at_dt + timedelta(minutes=5)
    next_run = compute_next_run("oneshot", config, now)
    assert next_run == datetime.max


def test_cron_trigger_placeholder():
    with pytest.raises(NotImplementedError, match="cron triggers ship in a follow-up spec"):
        compute_next_run("cron", {}, datetime.now())


def test_unknown_trigger():
    with pytest.raises(ValueError, match="Unknown trigger type"):
        compute_next_run("unknown", {}, datetime.now())
