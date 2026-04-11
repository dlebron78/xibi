import hashlib
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone
import pytest
from xibi.db import open_db, migrate
from xibi.heartbeat.contact_poller import _extract_recipients, poll_sent_folder, backfill_contacts
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
        assert row["discovered_via"] == "inbound_email"

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
        assert row["discovered_via"] == "outbound_email"

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
    envelope = {
        "id": "1",
        "to": [{"name": "Alice", "addr": "alice@example.com"}],
        "cc": ["bob@example.com"]
    }
    # Mocking himalaya_bin as it shouldn't be called if To/CC are present
    recipients = _extract_recipients("mock_himalaya", envelope)

    assert len(recipients) == 2
    assert any(r["addr"] == "alice@example.com" and r["role"] == "to" for r in recipients)
    assert any(r["addr"] == "bob@example.com" and r["role"] == "cc" for r in recipients)

def test_extract_recipients_string_format():
    envelope = {
        "id": "1",
        "to": "Alice <alice@example.com>",
        "cc": "bob@example.com"
    }
    recipients = _extract_recipients("mock_himalaya", envelope)
    assert len(recipients) == 2
    assert any(r["name"] == "Alice" and r["addr"] == "alice@example.com" for r in recipients)
    assert any(r["addr"] == "bob@example.com" for r in recipients)

def test_pagination_logic(db_path, mocker):
    # Mock _list_envelopes to return two pages, then empty
    mock_list = mocker.patch("xibi.heartbeat.contact_poller._list_envelopes")

    now = datetime.now(timezone.utc)
    env1 = {"id": "1", "date": now.isoformat().replace("+00:00", "Z"), "to": "alice@example.com"}
    env2 = {"id": "2", "date": (now - timedelta(minutes=30)).isoformat().replace("+00:00", "Z"), "to": "bob@example.com"}
    env3 = {"id": "3", "date": (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z"), "to": "charlie@example.com"}

    # Page 1 returns env1, env2
    # Page 2 returns env3
    # Page 3 returns empty
    mock_list.side_effect = [
        [env1, env2],
        [env3],
        []
    ]

    mocker.patch("xibi.heartbeat.contact_poller._discover_sent_folder", return_value="Sent")

    # Watermark at 1 hour ago.
    # Should process env1, env2, then stop at env3 because it's older than watermark.
    watermark = now - timedelta(hours=1)
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO heartbeat_state (key, value) VALUES ('sent_mail_watermark', ?)",
            (watermark.isoformat(),)
        )

    stats = poll_sent_folder("mock_himalaya", db_path, page_size=2)

    # env1 and env2 are newer than watermark.
    # env3 is older, so it should trigger reached_watermark = True.
    # but env3 is in page 2.

    assert stats["emails_scanned"] == 3 # env1, env2, env3 were fetched

    with open_db(db_path) as conn:
        row1 = conn.execute("SELECT id FROM contacts WHERE email = 'alice@example.com'").fetchone()
        row2 = conn.execute("SELECT id FROM contacts WHERE email = 'bob@example.com'").fetchone()
        row3 = conn.execute("SELECT id FROM contacts WHERE email = 'charlie@example.com'").fetchone()
        assert row1 is not None
        assert row2 is not None
        assert row3 is None # charlie is older than watermark
