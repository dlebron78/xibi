"""Calendar tools fall through to email_alias resolution after label/cal_id misses.

We don't actually call Google here — the test verifies that when an email-style
``calendar_id`` like ``"lebron@afya.fit"`` is supplied, the inline resolver in
each calendar tool resolves to the right (account, calendar_id) pair via
``OAuthStore.find_by_email_alias``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from xibi.db import migrate
from xibi.oauth.store import OAuthStore


@pytest.fixture
def db_with_alias(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    secrets: dict[str, str] = {}
    monkeypatch.setattr("xibi.oauth.store.secrets_manager.store", lambda k, v: secrets.__setitem__(k, v))
    monkeypatch.setattr("xibi.oauth.store.secrets_manager.load", lambda k: secrets.get(k))
    monkeypatch.setattr("xibi.oauth.store.secrets_manager.delete", lambda k: secrets.pop(k, None))
    s = OAuthStore(db_path)
    s.add_account(
        "default-owner",
        "google_calendar",
        "afya",
        refresh_token="rt",
        client_id="cid",
        client_secret="cs",
        metadata={"email_alias": "lebron@afya.fit"},
    )
    monkeypatch.setenv("XIBI_DB_PATH", str(db_path))
    monkeypatch.setenv("XIBI_INSTANCE_OWNER_USER_ID", "default-owner")
    return db_path


def test_resolve_email_alias_target_returns_primary_for_account(db_with_alias):
    from skills.calendar.tools._google_auth import resolve_email_alias_target

    out = resolve_email_alias_target("lebron@afya.fit")
    assert out is not None
    assert out["account"] == "afya"
    assert out["calendar_id"] == "primary"
    assert out["label"] == "afya"


def test_resolve_email_alias_target_returns_none_for_non_email(db_with_alias):
    from skills.calendar.tools._google_auth import resolve_email_alias_target

    assert resolve_email_alias_target("afya") is None
    assert resolve_email_alias_target("") is None


def test_resolve_email_alias_target_returns_none_for_unknown_alias(db_with_alias):
    from skills.calendar.tools._google_auth import resolve_email_alias_target

    assert resolve_email_alias_target("nope@nowhere.com") is None


def test_label_match_takes_priority_over_email_alias(db_with_alias, monkeypatch):
    """list_events._resolve_targets matches XIBI_CALENDARS labels first."""
    monkeypatch.setenv("XIBI_CALENDARS", "afya=afya:afya-cal-id,personal=personal:primary")
    from skills.calendar.tools._google_auth import load_calendar_config
    from skills.calendar.tools.list_events import _resolve_targets

    config = load_calendar_config()
    targets = _resolve_targets({"calendar_id": "afya"}, config)
    assert len(targets) == 1
    # Label match wins → calendar_id from XIBI_CALENDARS, not "primary"
    assert targets[0]["calendar_id"] == "afya-cal-id"


def test_email_alias_match_returns_primary_calendar_for_account(db_with_alias, monkeypatch):
    monkeypatch.setenv("XIBI_CALENDARS", "personal=personal:primary")
    from skills.calendar.tools._google_auth import load_calendar_config
    from skills.calendar.tools.list_events import _resolve_targets

    config = load_calendar_config()
    targets = _resolve_targets({"calendar_id": "lebron@afya.fit"}, config)
    assert len(targets) == 1
    assert targets[0]["account"] == "afya"
    assert targets[0]["calendar_id"] == "primary"


def test_unmatched_falls_through_to_raw(db_with_alias, monkeypatch):
    monkeypatch.setenv("XIBI_CALENDARS", "personal=personal:primary")
    from skills.calendar.tools._google_auth import load_calendar_config
    from skills.calendar.tools.list_events import _resolve_targets

    config = load_calendar_config()
    targets = _resolve_targets({"calendar_id": "raw-cal-id-xyz"}, config)
    # Doesn't match a label, isn't an email alias — falls through as raw.
    assert targets[0]["calendar_id"] == "raw-cal-id-xyz"
    assert targets[0]["account"] == "default"


def test_find_event_targets_resolve_email_alias(db_with_alias, monkeypatch):
    monkeypatch.setenv("XIBI_CALENDARS", "personal=personal:primary")
    from skills.calendar.tools._google_auth import load_calendar_config
    from skills.calendar.tools.find_event import _resolve_targets

    config = load_calendar_config()
    targets = _resolve_targets({"calendar_id": "lebron@afya.fit"}, config)
    assert targets[0]["account"] == "afya"
    assert targets[0]["calendar_id"] == "primary"


def test_add_event_falls_through_to_email_alias(db_with_alias, monkeypatch):
    """add_event sends to gcal_request — patch it out and verify the target."""
    captured: dict = {}

    def _fake_gcal_request(path, method, body, account):
        captured["path"] = path
        captured["account"] = account
        return {"id": "evt-1", "htmlLink": "http://x"}

    monkeypatch.setenv("XIBI_CALENDARS", "personal=personal:primary")
    from skills.calendar.tools import add_event

    monkeypatch.setattr(add_event, "gcal_request", _fake_gcal_request)

    out = add_event.run(
        {
            "title": "Sync",
            "start_datetime": "2026-04-27T10:00:00",
            "duration_mins": 30,
            "calendar_id": "lebron@afya.fit",
        }
    )
    assert out["status"] == "success"
    assert captured["account"] == "afya"
    assert "/calendars/primary/events" in captured["path"]
