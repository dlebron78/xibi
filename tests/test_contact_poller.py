import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from xibi.db import migrate, open_db
from xibi.heartbeat.contact_poller import _extract_recipients, poll_sent_folder
from xibi.signal_intelligence import _upsert_contact_core


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test_xibi.db"
    migrate(path)
    return path


def test_contact_id_deterministic(db_path):
    email = "test@example.com"
    expected_id = "contact-" + hashlib.md5(email.lower().encode()).hexdigest()[:8]

    cid = _upsert_contact_core(email, "Test", None, db_path, direction="inbound")
    assert cid == expected_id


def test_upsert_contact_inbound(db_path):
    email = "inbound@example.com"
    name = "Inbound User"

    _upsert_contact_core(email, name, None, db_path, direction="inbound")

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM contacts WHERE email = ?", (email,)).fetchone()
        assert row is not None
        assert row["display_name"] == name
        assert row["signal_count"] == 1
        assert row["outbound_count"] == 0
        assert row["discovered_via"] == "email_inbound"

        # Check channel
        channel = conn.execute("SELECT * FROM contact_channels WHERE contact_id = ?", (row["id"],)).fetchone()
        assert channel is not None
        assert channel["handle"] == email
        assert channel["channel_type"] == "email"


def test_upsert_contact_outbound(db_path):
    email = "outbound@example.com"
    name = "Outbound User"

    _upsert_contact_core(email, name, None, db_path, direction="outbound")

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM contacts WHERE email = ?", (email,)).fetchone()
        assert row is not None
        assert row["display_name"] == name
        assert row["signal_count"] == 0
        assert row["outbound_count"] == 1
        assert row["discovered_via"] == "email_outbound"


def test_upsert_contact_existing_inbound_then_outbound(db_path):
    email = "mixed@example.com"

    _upsert_contact_core(email, "Mixed", None, db_path, direction="inbound")
    _upsert_contact_core(email, "Mixed", None, db_path, direction="outbound")

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM contacts WHERE email = ?", (email,)).fetchone()
        assert row["signal_count"] == 1
        assert row["outbound_count"] == 1


def test_extract_recipients_basic():
    envelope = {"id": "1", "to": [{"name": "Alice", "addr": "alice@example.com"}], "cc": ["bob@example.com"]}
    # Mocking himalaya_bin as it shouldn't be called if To/CC are present
    recipients = _extract_recipients("mock_himalaya", envelope)

    assert len(recipients) == 2
    assert any(r["addr"] == "alice@example.com" and r["role"] == "to" for r in recipients)
    assert any(r["addr"] == "bob@example.com" and r["role"] == "cc" for r in recipients)


def test_extract_recipients_string_format():
    envelope = {"id": "1", "to": "Alice <alice@example.com>", "cc": "bob@example.com"}
    recipients = _extract_recipients("mock_himalaya", envelope)
    assert len(recipients) == 2
    assert any(r["name"] == "Alice" and r["addr"] == "alice@example.com" for r in recipients)
    assert any(r["addr"] == "bob@example.com" for r in recipients)


def test_pagination_logic(db_path, mocker):
    # Mock _list_envelopes to return two pages, then empty
    mock_list = mocker.patch("xibi.heartbeat.contact_poller._list_envelopes")

    now = datetime.now(timezone.utc)
    env1 = {"id": "1", "date": now.isoformat().replace("+00:00", "Z"), "to": "alice@example.com"}
    env2 = {
        "id": "2",
        "date": (now - timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
        "to": "bob@example.com",
    }
    env3 = {
        "id": "3",
        "date": (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
        "to": "charlie@example.com",
    }

    # Page 1 returns env1, env2
    # Page 2 returns env3
    # Page 3 returns empty
    mock_list.side_effect = [[env1, env2], [env3], []]

    mocker.patch("xibi.heartbeat.contact_poller._discover_sent_folder", return_value="Sent")

    # Watermark at 1 hour ago.
    # Should process env1, env2, then stop at env3 because it's older than watermark.
    watermark = now - timedelta(hours=1)
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO heartbeat_state (key, value) VALUES ('sent_mail_watermark', ?)", (watermark.isoformat(),)
        )

    stats = poll_sent_folder("mock_himalaya", db_path, page_size=2)

    # env1 and env2 are newer than watermark.
    # env3 is older, so it should trigger reached_watermark = True.
    # but env3 is in page 2.

    assert stats["emails_scanned"] == 3  # env1, env2, env3 were fetched

    with open_db(db_path) as conn:
        row1 = conn.execute("SELECT id FROM contacts WHERE email = 'alice@example.com'").fetchone()
        row2 = conn.execute("SELECT id FROM contacts WHERE email = 'bob@example.com'").fetchone()
        row3 = conn.execute("SELECT id FROM contacts WHERE email = 'charlie@example.com'").fetchone()
        assert row1 is not None
        assert row2 is not None
        assert row3 is None  # charlie is older than watermark


def test_poll_sent_folder_stamps_account_origin(db_path, mocker, monkeypatch):
    """Hotfix: contact_poller must thread received_via_account through to
    _upsert_contact_core so contacts created from sent-folder polling get
    account_origin + seen_via_accounts populated (step-110 condition #7).
    """
    monkeypatch.setenv("XIBI_SENT_MAIL_NICKNAME", "personal")
    # Module-level constant is read at import time, so reload to re-evaluate.
    import importlib

    import xibi.heartbeat.contact_poller as cp

    importlib.reload(cp)

    now = datetime.now(timezone.utc)
    env1 = {
        "id": "1",
        "date": now.isoformat().replace("+00:00", "Z"),
        "to": "newcontact@example.com",
    }
    mocker.patch("xibi.heartbeat.contact_poller._list_envelopes", side_effect=[[env1], []])
    mocker.patch("xibi.heartbeat.contact_poller._discover_sent_folder", return_value="Sent")

    watermark = now - timedelta(hours=1)
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO heartbeat_state (key, value) VALUES ('sent_mail_watermark', ?)",
            (watermark.isoformat(),),
        )

    cp.poll_sent_folder("mock_himalaya", db_path, page_size=10)

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT account_origin, seen_via_accounts FROM contacts WHERE email = 'newcontact@example.com'"
        ).fetchone()
    assert row is not None
    assert row[0] == "personal"
    import json as _json

    assert _json.loads(row[1]) == ["personal"]


def test_sent_mail_account_nickname_default(monkeypatch):
    """The default nickname is 'personal' when XIBI_SENT_MAIL_NICKNAME is unset."""
    monkeypatch.delenv("XIBI_SENT_MAIL_NICKNAME", raising=False)
    import importlib

    import xibi.heartbeat.contact_poller as cp

    importlib.reload(cp)
    assert cp.SENT_MAIL_ACCOUNT_NICKNAME == "personal"


# ── Hotfix regression tests: himalaya v1.x --account flag position ──
#
# Pre-fix: `--account` was placed BEFORE the `envelope list` /
# `message export` subcommand, which himalaya v1.x rejects with
# `unexpected argument '--account' found`. Effect: poll_sent_folder
# and backfill_contacts silently returned zero results.
#
# These tests assert argv ordering — they don't actually exec himalaya;
# they capture the cmd list passed to subprocess.run and verify
# `--account` appears AFTER the subcommand verb.


def _index_after(cmd, marker):
    return cmd.index(marker) if marker in cmd else -1


def test_list_envelopes_places_account_after_subcommand(mocker):
    """_list_envelopes must put --account after `envelope list` (himalaya v1.x)."""
    from xibi.heartbeat import contact_poller

    captured = {}

    class _FakeRun:
        def __init__(self, *args, **kwargs):
            captured["cmd"] = args[0] if args else kwargs.get("args")
            self.returncode = 0
            self.stdout = "[]"
            self.stderr = ""

    mocker.patch("subprocess.run", side_effect=_FakeRun)
    contact_poller._list_envelopes("/path/to/himalaya", folder="Sent", page_size=1)

    cmd = captured["cmd"]
    list_idx = _index_after(cmd, "list")
    account_idx = _index_after(cmd, "--account")
    assert list_idx >= 0, "envelope `list` subcommand missing"
    assert account_idx > list_idx, (
        f"--account at {account_idx} must come AFTER `list` at {list_idx} "
        f"for himalaya v1.x compatibility. Full cmd: {cmd}"
    )


def test_discover_sent_folder_places_account_after_subcommand(mocker, tmp_path):
    """_discover_sent_folder probe must put --account after `envelope list`."""
    from xibi.heartbeat import contact_poller

    db = tmp_path / "test.db"
    migrate(db)
    captured = {"cmds": []}

    class _FakeRun:
        def __init__(self, *args, **kwargs):
            captured["cmds"].append(args[0] if args else kwargs.get("args"))
            self.returncode = 0
            self.stdout = "[]"
            self.stderr = ""

    mocker.patch("subprocess.run", side_effect=_FakeRun)
    contact_poller._discover_sent_folder("/path/to/himalaya", db)

    assert captured["cmds"], "no candidate-folder probe was issued"
    for cmd in captured["cmds"]:
        list_idx = _index_after(cmd, "list")
        account_idx = _index_after(cmd, "--account")
        assert list_idx >= 0
        assert account_idx > list_idx, (
            f"--account at {account_idx} must come AFTER `list` at {list_idx} for himalaya v1.x. Full cmd: {cmd}"
        )


def test_fetch_recipients_full_places_account_after_subcommand(mocker):
    """_fetch_recipients_full must put --account after `message export`."""
    from xibi.heartbeat import contact_poller

    captured = {}

    class _FakeRun:
        def __init__(self, *args, **kwargs):
            captured["cmd"] = args[0] if args else kwargs.get("args")
            self.returncode = 0
            self.stdout = ""  # parser handles empty gracefully
            self.stderr = ""

    mocker.patch("subprocess.run", side_effect=_FakeRun)
    contact_poller._fetch_recipients_full("/path/to/himalaya", "12345")

    cmd = captured["cmd"]
    export_idx = _index_after(cmd, "export")
    account_idx = _index_after(cmd, "--account")
    assert export_idx >= 0, "`message export` subcommand missing"
    assert account_idx > export_idx, (
        f"--account at {account_idx} must come AFTER `export` at {export_idx} for himalaya v1.x. Full cmd: {cmd}"
    )
