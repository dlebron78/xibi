"""Provenance fields are populated in summarize_email's data dict.

We patch himalaya I/O and feed a synthesized RFC-5322 message so the test
exercises the real header parsing + provenance lookup path without needing
himalaya installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from xibi.db import migrate
from xibi.oauth.store import OAuthStore

SAMPLE_RAW_EMAIL_AFYA = """From: manager@afya.fit
To: lebron@afya.fit
Subject: Q3 plans
Message-ID: <abc@afya.fit>
Date: Tue, 27 Apr 2026 10:00:00 +0000
Content-Type: text/plain

Let's discuss next week.
"""

SAMPLE_RAW_EMAIL_FORWARDED = """From: someone@example.com
To: dannylebron@gmail.com
Delivered-To: lebron@afya.fit
Subject: Forwarded thread
Message-ID: <fwd@example.com>
Date: Tue, 27 Apr 2026 10:00:00 +0000
Content-Type: text/plain

Body
"""

SAMPLE_RAW_EMAIL_UNKNOWN = """From: someone@example.com
To: contractor@somewhere.com
Subject: Unknown alias
Message-ID: <u@example.com>
Date: Tue, 27 Apr 2026 10:00:00 +0000
Content-Type: text/plain

Body
"""


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "xibi.db"
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
    s.add_account(
        "default-owner",
        "google_calendar",
        "personal",
        refresh_token="rt",
        client_id="cid",
        client_secret="cs",
        metadata={"email_alias": "dannylebron@gmail.com"},
    )
    return tmp_path


def _patch_himalaya(monkeypatch, raw_email: str):
    from skills.email.tools import summarize_email as se

    monkeypatch.setattr(se, "_find_himalaya", lambda: "/bin/true")
    monkeypatch.setattr(se, "_read_email", lambda _bin, _id: (raw_email, None))


def test_data_includes_received_via_account_when_match(workdir, monkeypatch):
    _patch_himalaya(monkeypatch, SAMPLE_RAW_EMAIL_AFYA)
    from skills.email.tools import summarize_email as se

    out = se.run({"email_id": "1", "_workdir": str(workdir)})
    assert out["status"] == "success"
    data = out["data"]
    assert data["received_via_account"] == "afya"
    assert data["received_via_email_alias"] == "lebron@afya.fit"
    assert data["calendar_label"] == "afya"


def test_data_includes_none_when_no_match(workdir, monkeypatch):
    _patch_himalaya(monkeypatch, SAMPLE_RAW_EMAIL_UNKNOWN)
    from skills.email.tools import summarize_email as se

    out = se.run({"email_id": "1", "_workdir": str(workdir)})
    data = out["data"]
    assert data["received_via_account"] is None
    # Surfaced unmatched alias for [unknown alias] rendering downstream
    assert data["received_via_email_alias"] == "contractor@somewhere.com"
    assert data["calendar_label"] is None


def test_data_passes_delivered_to_priority(workdir, monkeypatch):
    _patch_himalaya(monkeypatch, SAMPLE_RAW_EMAIL_FORWARDED)
    from skills.email.tools import summarize_email as se

    out = se.run({"email_id": "1", "_workdir": str(workdir)})
    data = out["data"]
    # Delivered-To: lebron@afya.fit must win over To: dannylebron@gmail.com
    assert data["received_via_account"] == "afya"
    assert data["received_via_email_alias"] == "lebron@afya.fit"
