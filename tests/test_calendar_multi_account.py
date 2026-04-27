from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

import pytest

import skills.calendar.tools._google_auth as ga
from skills.calendar.tools._google_auth import (
    get_access_token,
    load_calendar_config,
    resolve_account_for_label,
)
from xibi.db import migrate
from xibi.oauth.store import OAuthStore


@pytest.fixture(autouse=True)
def reset_token_cache():
    ga._TOKEN_CACHE.clear()
    ga._CACHE_LOCKS.clear()
    yield
    ga._TOKEN_CACHE.clear()
    ga._CACHE_LOCKS.clear()


@pytest.fixture
def two_accounts(tmp_path: Path, monkeypatch):
    db = tmp_path / "xibi.db"
    migrate(db)
    secrets: dict[str, str] = {}
    monkeypatch.setattr("xibi.oauth.store.secrets_manager.store", lambda k, v: secrets.update({k: v}))
    monkeypatch.setattr("xibi.oauth.store.secrets_manager.load", lambda k: secrets.get(k))
    monkeypatch.setattr("xibi.oauth.store.secrets_manager.delete", lambda k: secrets.pop(k, None))
    monkeypatch.setenv("XIBI_DB_PATH", str(db))

    s = OAuthStore(db)
    s.add_account("default-owner", "google_calendar", "default", "rt-default", "cid", "cs")
    s.add_account("default-owner", "google_calendar", "afya", "rt-afya", "cid", "cs")
    return db


# ── Config parser ────────────────────────────────────────────────────────


def test_legacy_xibi_calendars_format_still_parses(monkeypatch):
    monkeypatch.setenv("XIBI_CALENDARS", "personal:dan@example.com,work:work@example.com")
    cfg = load_calendar_config()
    assert len(cfg) == 2
    assert all(c["account"] == "default" for c in cfg)
    assert cfg[0]["label"] == "personal"
    assert cfg[0]["calendar_id"] == "dan@example.com"


def test_new_xibi_calendars_format_parses(monkeypatch):
    monkeypatch.setenv(
        "XIBI_CALENDARS",
        "personal=default:primary,afya=afya:primary,team=afya:team@afya.fit",
    )
    cfg = load_calendar_config()
    assert cfg == [
        {"label": "personal", "account": "default", "calendar_id": "primary"},
        {"label": "afya", "account": "afya", "calendar_id": "primary"},
        {"label": "team", "account": "afya", "calendar_id": "team@afya.fit"},
    ]


def test_resolve_account_for_label(monkeypatch):
    monkeypatch.setenv("XIBI_CALENDARS", "afya=afya:primary,personal=default:dan@x.com")
    assert resolve_account_for_label("afya") == "afya"
    assert resolve_account_for_label("personal") == "default"
    assert resolve_account_for_label("unknown") == "default"


# ── Token resolution + caching ───────────────────────────────────────────


def test_two_accounts_two_tokens(two_accounts):
    with patch("xibi.oauth.google.refresh_access_token") as mock_refresh:
        mock_refresh.side_effect = lambda rt, cid, cs: (f"at-for-{rt}", 3600)
        t_default = get_access_token("default")
        t_afya = get_access_token("afya")
    assert t_default == "at-for-rt-default"
    assert t_afya == "at-for-rt-afya"


def test_label_resolves_to_correct_account(two_accounts, monkeypatch):
    monkeypatch.setenv("XIBI_CALENDARS", "personal=default:primary,afya=afya:primary")

    captured: list[str] = []

    def _refresh(rt, cid, cs):
        captured.append(rt)
        return ("at", 3600)

    with patch("xibi.oauth.google.refresh_access_token", side_effect=_refresh):
        get_access_token(account="afya")
    assert captured == ["rt-afya"]


def test_concurrent_refresh_single_token_request(two_accounts):
    """Two threads racing for the same account → exactly one Google call."""
    call_counter = {"n": 0}
    barrier = threading.Barrier(2)

    def _slow_refresh(rt, cid, cs):
        # Both threads should reach the lock; only one gets to call this.
        call_counter["n"] += 1
        return ("at", 3600)

    with patch("xibi.oauth.google.refresh_access_token", side_effect=_slow_refresh):
        results: list[str] = []

        def _worker():
            barrier.wait()
            results.append(get_access_token("afya"))

        t1 = threading.Thread(target=_worker)
        t2 = threading.Thread(target=_worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    assert call_counter["n"] == 1
    assert results == ["at", "at"]


def test_revoked_account_returns_structured_error(two_accounts):
    from xibi.oauth.google import OAuthRevokedError

    s = OAuthStore(two_accounts)
    s.mark_revoked("default-owner", "google_calendar", "afya")
    with pytest.raises(OAuthRevokedError):
        get_access_token("afya")


def test_invalid_grant_marks_account_revoked(two_accounts):
    from xibi.oauth.google import OAuthRevokedError

    def _refresh(rt, cid, cs):
        raise OAuthRevokedError(account="afya")

    with (
        patch("xibi.oauth.google.refresh_access_token", side_effect=_refresh),
        pytest.raises(OAuthRevokedError),
    ):
        get_access_token("afya")
    s = OAuthStore(two_accounts)
    row = s.get_account("default-owner", "google_calendar", "afya")
    assert row["status"] == "revoked"


# ── list_events provenance + multi-cal merge ────────────────────────────


def test_list_events_no_calendar_id_merges_all_accounts(monkeypatch):
    monkeypatch.setenv("XIBI_CALENDARS", "personal=default:dan@x.com,afya=afya:primary")

    from skills.calendar.tools.list_events import run as list_run

    def _fake(path, account="default"):
        if account == "afya":
            return {
                "items": [
                    {
                        "id": "a1",
                        "summary": "Standup",
                        "start": {"dateTime": "2026-05-01T09:00:00Z"},
                        "end": {"dateTime": "2026-05-01T09:30:00Z"},
                    }
                ]
            }
        return {
            "items": [
                {
                    "id": "p1",
                    "summary": "Lunch",
                    "start": {"dateTime": "2026-05-01T12:00:00Z"},
                    "end": {"dateTime": "2026-05-01T13:00:00Z"},
                }
            ]
        }

    with patch("skills.calendar.tools.list_events.gcal_request", side_effect=_fake):
        out = list_run({})

    assert out["status"] == "success"
    titles = [e["title"] for e in out["events"]]
    assert "Standup" in titles and "Lunch" in titles


def test_list_events_each_event_tagged_with_account_and_label(monkeypatch):
    monkeypatch.setenv("XIBI_CALENDARS", "afya=afya:primary")

    from skills.calendar.tools.list_events import run as list_run

    with patch(
        "skills.calendar.tools.list_events.gcal_request",
        return_value={
            "items": [
                {
                    "id": "x",
                    "summary": "T",
                    "start": {"dateTime": "2026-05-01T09:00:00Z"},
                    "end": {"dateTime": "2026-05-01T10:00:00Z"},
                }
            ]
        },
    ):
        out = list_run({})
    ev = out["events"][0]
    assert ev["account"] == "afya"
    assert ev["label"] == "afya"
    assert ev["calendar_id"] == "primary"


def test_list_events_partial_failure_surfaces_partial_errors(monkeypatch):
    monkeypatch.setenv("XIBI_CALENDARS", "personal=default:dan@x.com,afya=afya:primary")

    from skills.calendar.tools.list_events import run as list_run

    def _fake(path, account="default"):
        if account == "afya":
            raise RuntimeError("API error 500")
        return {
            "items": [
                {
                    "id": "p1",
                    "summary": "Lunch",
                    "start": {"dateTime": "2026-05-01T12:00:00Z"},
                    "end": {"dateTime": "2026-05-01T13:00:00Z"},
                }
            ]
        }

    with patch("skills.calendar.tools.list_events.gcal_request", side_effect=_fake):
        out = list_run({})

    assert out["count"] == 1  # personal succeeded
    assert "partial_errors" in out
    assert out["partial_errors"][0]["label"] == "afya"


def test_list_events_calendar_id_singular_param(monkeypatch):
    monkeypatch.setenv("XIBI_CALENDARS", "personal=default:dan@x.com,afya=afya:primary")

    from skills.calendar.tools.list_events import run as list_run

    seen_accounts: list[str] = []

    def _fake(path, account="default"):
        seen_accounts.append(account)
        return {"items": []}

    with patch("skills.calendar.tools.list_events.gcal_request", side_effect=_fake):
        list_run({"calendar_id": "afya"})
    assert seen_accounts == ["afya"]


def test_find_event_searches_all_accounts_when_no_calendar_id(monkeypatch):
    monkeypatch.setenv("XIBI_CALENDARS", "personal=default:dan@x.com,afya=afya:primary")

    from skills.calendar.tools.find_event import run as find_run

    seen: list[str] = []

    def _fake(path, account="default"):
        seen.append(account)
        return {"items": []}

    with patch("skills.calendar.tools.find_event.gcal_request", side_effect=_fake):
        find_run({"query": "standup"})
    assert sorted(seen) == ["afya", "default"]


# ── calendar_context [label] prefix ──────────────────────────────────────


def test_calendar_context_event_carries_calendar_label(monkeypatch):
    """fetch_upcoming_events must tag every event with its source calendar_label.

    The classification + review_cycle renderers consume that field to emit
    the `[label]` provenance prefix on every event line in the agent's
    prompt context (see classification.py:179-191 and review_cycle.py:264-272).
    """
    monkeypatch.setenv("XIBI_CALENDARS", "afya=afya:primary,personal=default:dan@x.com")
    from xibi.heartbeat import calendar_context as cc

    def _fake(path, account="default"):
        if account == "afya":
            return {
                "items": [
                    {
                        "id": "a1",
                        "summary": "Standup",
                        "start": {"dateTime": "2099-05-01T09:00:00Z"},
                        "end": {"dateTime": "2099-05-01T09:30:00Z"},
                    }
                ]
            }
        return {
            "items": [
                {
                    "id": "p1",
                    "summary": "Lunch",
                    "start": {"dateTime": "2099-05-01T12:00:00Z"},
                    "end": {"dateTime": "2099-05-01T13:00:00Z"},
                }
            ]
        }

    with patch("xibi.heartbeat.calendar_context.gcal_request", side_effect=_fake):
        events = cc.fetch_upcoming_events(lookahead_hours=24)

    by_title = {e["title"]: e for e in events}
    assert by_title["Standup"]["calendar_label"] == "afya"
    assert by_title["Lunch"]["calendar_label"] == "personal"


def test_classification_block_includes_label_prefix():
    """Eyeball-grade source check: the renderer formats events as `[label] title`."""
    src = Path(__file__).resolve().parent.parent / "xibi" / "heartbeat" / "classification.py"
    text = src.read_text()
    assert "[{cal_label}] {title}" in text


def test_review_cycle_xml_includes_label_prefix():
    src = Path(__file__).resolve().parent.parent / "xibi" / "heartbeat" / "review_cycle.py"
    text = src.read_text()
    assert "[{cal_label}] {title}" in text
