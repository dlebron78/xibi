import sqlite3

import pytest

from skills.email.tools.send_email import _track_outbound
from xibi.db import open_db
from xibi.db.migrations import migrate
from xibi.router import Config
from xibi.signal_intelligence import upsert_contact


@pytest.fixture
def db_path(tmp_path):
    db = tmp_path / "xibi_test.db"
    migrate(db)
    return db


def test_email_ingest_creates_contact(db_path):
    # Setup: Call upsert_contact for new email
    upsert_contact("alice@acme.com", "Alice", "Acme Corp", db_path)

    # Verify: Contact and channel created
    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM contacts").fetchone()
        assert row["display_name"] == "Alice"
        assert row["organization"] == "Acme Corp"
        assert row["discovered_via"] == "email_inbound"

        row = conn.execute("SELECT * FROM contact_channels").fetchone()
        assert row["handle"] == "alice@acme.com"
        assert row["channel_type"] == "email"


def test_email_ingest_domain_colleague(db_path):
    # Setup: Mock config with owner domain
    config = Config()
    config["email_from"] = "me@acme.com"

    # Test: sender at owner domain gets colleague relationship
    upsert_contact("alice@acme.com", "Alice", "Acme Corp", db_path, config=config)

    with open_db(db_path) as conn:
        row = conn.execute("SELECT relationship FROM contacts WHERE email='alice@acme.com'").fetchone()
        assert row[0] == "colleague"


def test_email_ingest_domain_familiar(db_path):
    # Setup: Create 3 contacts at Acme Corp
    upsert_contact("bob1@acme.com", "Bob1", "Acme Corp", db_path)
    upsert_contact("bob2@acme.com", "Bob2", "Acme Corp", db_path)
    upsert_contact("bob3@acme.com", "Bob3", "Acme Corp", db_path)

    # Test: 4th sender at domain with 3+ contacts gets org_known relationship
    upsert_contact("alice@acme.com", "Alice", "Acme Corp", db_path)

    with open_db(db_path) as conn:
        row = conn.execute("SELECT relationship FROM contacts WHERE email='alice@acme.com'").fetchone()
        assert row[0] == "org_known"


def test_outbound_sets_endorsed(db_path):
    # Setup: Send outbound email to new recipient
    _track_outbound("alice@acme.com", str(db_path))

    # Verify: Contact created, outbound_count=1, user_endorsed=1
    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT outbound_count, user_endorsed, discovered_via FROM contacts WHERE email='alice@acme.com'"
        ).fetchone()
        assert row["outbound_count"] == 1
        assert row["user_endorsed"] == 1
        assert row["discovered_via"] == "email_outbound"

    # Send again
    _track_outbound("alice@acme.com", str(db_path))
    with open_db(db_path) as conn:
        row = conn.execute("SELECT outbound_count FROM contacts WHERE email='alice@acme.com'").fetchone()
        assert row[0] == 2
