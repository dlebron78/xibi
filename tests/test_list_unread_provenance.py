"""list_unread Telegram render and per-envelope provenance fields.

v1 behavior: each envelope gets ``received_via_account=None`` /
``received_via_email_alias=None`` (himalaya envelopes don't carry To/Delivered-To).
Render shows ``[unknown alias]`` for None envelopes.
``XIBI_EMAIL_PROVENANCE_RENDER=false`` suppresses the prefix entirely.
"""

from __future__ import annotations

from skills.email.tools import list_unread


def _envelope(email_id: str, subject: str, **extra) -> dict:
    base = {
        "id": email_id,
        "subject": subject,
        "from": {"name": "Bob", "addr": "bob@example.com"},
        "date": "2026-04-27T10:00:00+00:00",
        "flags": [],
    }
    base.update(extra)
    return base


def test_telegram_render_unknown_alias_when_no_match(monkeypatch):
    monkeypatch.delenv("XIBI_EMAIL_PROVENANCE_RENDER", raising=False)
    emails = [_envelope("1", "Hello"), _envelope("2", "World")]
    out = list_unread.format_page(emails, offset=0, total_unread=2)
    assert "[unknown alias]" in out
    # Both lines tagged
    assert out.count("[unknown alias]") == 2


def test_telegram_render_prefixes_label(monkeypatch):
    monkeypatch.delenv("XIBI_EMAIL_PROVENANCE_RENDER", raising=False)
    emails = [
        _envelope("1", "Q3 plans", received_via_account="afya"),
        _envelope("2", "Sunday dinner", received_via_account="personal"),
    ]
    out = list_unread.format_page(emails, offset=0, total_unread=2)
    assert "[afya]" in out
    assert "[personal]" in out


def test_render_disabled_via_env_var(monkeypatch):
    monkeypatch.setenv("XIBI_EMAIL_PROVENANCE_RENDER", "false")
    emails = [_envelope("1", "Hello", received_via_account="afya")]
    out = list_unread.format_page(emails, offset=0, total_unread=1)
    assert "[afya]" not in out
    assert "[unknown alias]" not in out


def test_per_envelope_received_via_account_populated(monkeypatch):
    """When run() returns, every envelope dict has the provenance keys (None in v1)."""
    fake_envelopes = [
        {"id": "1", "subject": "Hi", "from": {"addr": "a@b.com"}, "date": "", "flags": []},
        {"id": "2", "subject": "There", "from": {"addr": "c@d.com"}, "date": "", "flags": []},
    ]

    class _R:
        returncode = 0
        stdout = __import__("json").dumps(fake_envelopes)
        stderr = ""

    monkeypatch.setattr(list_unread.subprocess, "run", lambda *a, **kw: _R())
    monkeypatch.setattr(list_unread.shutil, "which", lambda _b: "/bin/true")

    result = list_unread.run({})
    assert result["status"] == "success"
    for env in result["emails"]:
        assert "received_via_account" in env
        assert "received_via_email_alias" in env
        assert env["received_via_account"] is None
        assert env["received_via_email_alias"] is None
