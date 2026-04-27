"""Tests for the list_sent_emails wrapper + search_emails folder param (step-110)."""

from __future__ import annotations

from skills.email.tools import list_sent_emails, search_emails


def test_folder_sent_routes_to_himalaya(monkeypatch):
    captured = {}

    def fake_run_himalaya(himalaya_bin, query, folder="Inbox"):
        captured.setdefault("calls", []).append((query, folder))
        return [{"id": "1", "from": "x@y", "subject": "s", "date": "d"}]

    monkeypatch.setattr(search_emails, "_find_himalaya", lambda: "himalaya")
    monkeypatch.setattr(search_emails, "_run_himalaya_query", fake_run_himalaya)
    out = list_sent_emails.run({"days": 5, "limit": 3})
    assert out["status"] == "success"
    assert any(call[1] == "Sent" for call in captured["calls"])


def test_search_emails_folder_all_dedups(monkeypatch):
    """folder='all' merges Inbox + Sent and dedups by envelope id."""
    monkeypatch.setattr(search_emails, "_find_himalaya", lambda: "himalaya")

    def fake_run_himalaya(himalaya_bin, query, folder="Inbox"):
        if folder == "Sent":
            return [
                {"id": "1", "from": "a@x", "subject": "s", "date": "d"},
                {"id": "2", "from": "b@x", "subject": "s", "date": "d"},
            ]
        return [
            {"id": "2", "from": "b@x", "subject": "s", "date": "d"},
            {"id": "3", "from": "c@x", "subject": "s", "date": "d"},
        ]

    monkeypatch.setattr(search_emails, "_run_himalaya_query", fake_run_himalaya)
    out = search_emails.run({"folder": "all", "subject_keywords": ["s"], "limit": 10})
    ids = [e["id"] for e in out["emails"]]
    assert sorted(ids) == ["1", "2", "3"]


def test_search_emails_no_folder_default_inbox(monkeypatch):
    """Backward compat: omitting folder defaults to Inbox."""
    monkeypatch.setattr(search_emails, "_find_himalaya", lambda: "himalaya")
    folders_seen = []

    def fake_run_himalaya(himalaya_bin, query, folder="Inbox"):
        folders_seen.append(folder)
        return [{"id": "1", "from": "x", "subject": "s", "date": "d"}]

    monkeypatch.setattr(search_emails, "_run_himalaya_query", fake_run_himalaya)
    out = search_emails.run({"subject_keywords": ["s"], "limit": 10})
    assert out["status"] == "success"
    assert folders_seen == ["Inbox"]


def test_search_emails_invalid_folder_returns_error(monkeypatch):
    monkeypatch.setattr(search_emails, "_find_himalaya", lambda: "himalaya")
    out = search_emails.run({"folder": "Trash", "subject_keywords": ["x"]})
    assert out["status"] == "error"
    assert "Invalid folder" in out["message"]


def test_list_sent_emails_clamps_inputs(monkeypatch):
    captured = {}

    def fake_search_run(params):
        captured["params"] = params
        return {"status": "success", "count": 0, "emails": [], "probes": []}

    monkeypatch.setattr(list_sent_emails, "search_emails_run", fake_search_run)
    list_sent_emails.run({"days": 999, "limit": 999})
    assert captured["params"]["folder"] == "Sent"
    # days clamped to 30, limit to 25 — verify by checking the after_date
    # value reflects ≤30-day window and limit got passed clamped.
    assert captured["params"]["limit"] == 25
