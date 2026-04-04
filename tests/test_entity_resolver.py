import pytest

from xibi.db import open_db
from xibi.db.migrations import migrate
from xibi.entities import create_contact, upsert_contact_channel
from xibi.entities.resolver import resolve_contact


@pytest.fixture
def db_path(tmp_path):
    db = tmp_path / "xibi_test.db"
    migrate(db)
    return str(db)


def test_resolve_exact_channel_match(db_path):
    # Setup: Create contact and channel
    cid = create_contact("Alice", email="alice@acme.com", db_path=db_path)
    upsert_contact_channel(cid, "alice@acme.com", "email", verified=1, db_path=db_path)

    # Test: Resolve via email
    contact = resolve_contact("alice@acme.com", "email", db_path=db_path)
    assert contact is not None
    assert contact.id == cid
    assert contact.display_name == "Alice"


def test_resolve_cross_channel_match(db_path):
    # Setup: Contact with Slack handle but no email recorded in channels yet
    cid = create_contact("Alice", db_path=db_path)
    upsert_contact_channel(cid, "@alice_slack", "slack", verified=1, db_path=db_path)
    # Update contact display name
    with open_db(db_path) as conn:
        conn.execute("UPDATE contacts SET display_name = 'Alice' WHERE id = ?", (cid,))

    # Test: Resolve new email via domain + name
    # Note: For cross-channel domain match to work, there must be another handle at the same domain
    upsert_contact_channel(cid, "alice.personal@acme.com", "email", verified=1, db_path=db_path)

    contact = resolve_contact("alice.work@acme.com", "email", display_name="Alice", db_path=db_path)
    assert contact is not None
    assert contact.id == cid


def test_resolve_name_org_match(db_path):
    # Setup: Create contact with name and org
    cid = create_contact("Alice", organization="Acme Corp", db_path=db_path)

    # Test: Resolve via name + org
    contact = resolve_contact("irrelevant", "session", display_name="Alice", organization="Acme Corp", db_path=db_path)
    assert contact is not None
    assert contact.id == cid


def test_resolve_ambiguous_no_match(db_path):
    # Setup: Two contacts named Alice at Acme Corp
    create_contact("Alice", organization="Acme Corp", email="alice1@acme.com", db_path=db_path)
    create_contact("Alice", organization="Acme Corp", email="alice2@acme.com", db_path=db_path)

    # Test: Ambiguous match returns None
    contact = resolve_contact("irrelevant", "session", display_name="Alice", organization="Acme Corp", db_path=db_path)
    assert contact is None


def test_resolve_creates_no_contact(db_path):
    # Test: Resolver does not create contacts
    contact = resolve_contact("bob@acme.com", "email", db_path=db_path)
    assert contact is None

    with open_db(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()
        assert row[0] == 0


def test_resolve_updates_last_seen(db_path):
    # Setup: Create contact with old last_seen
    cid = create_contact("Alice", email="alice@acme.com", db_path=db_path)
    upsert_contact_channel(cid, "alice@acme.com", "email", verified=1, db_path=db_path)
    with open_db(db_path) as conn:
        conn.execute("UPDATE contacts SET last_seen = '2000-01-01 00:00:00' WHERE id = ?", (cid,))

    # Test: Resolve updates last_seen
    import time

    time.sleep(1.1)  # Ensure time passes for SQLite CURRENT_TIMESTAMP
    resolve_contact("alice@acme.com", "email", db_path=db_path)

    with open_db(db_path) as conn:
        row = conn.execute("SELECT last_seen FROM contacts WHERE id = ?", (cid,)).fetchone()
        assert row[0] > "2000-01-01 00:00:00"
