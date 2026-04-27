"""Tests for migration 41 + contacts.account_origin / seen_via_accounts."""

from __future__ import annotations

import json
import sqlite3

import pytest

from xibi.db import migrate
from xibi.entities.resolver import Contact
from xibi.signal_intelligence import _upsert_contact_core


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "xibi.db"
    migrate(p)
    return p


def test_migration_applied(db_path):
    with sqlite3.connect(str(db_path)) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(contacts)")}
    assert "account_origin" in cols
    assert "seen_via_accounts" in cols


def test_new_contact_first_account_set_as_origin(db_path):
    _upsert_contact_core(
        email="sarah@example.com",
        display_name="Sarah",
        organization=None,
        db_path=db_path,
        direction="inbound",
        received_via_account="afya",
    )
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT account_origin, seen_via_accounts FROM contacts WHERE email = ?",
            ("sarah@example.com",),
        ).fetchone()
    assert row[0] == "afya"
    assert json.loads(row[1]) == ["afya"]


def test_subsequent_account_extends_seen_list(db_path):
    _upsert_contact_core(
        email="sarah@example.com",
        display_name="Sarah",
        organization=None,
        db_path=db_path,
        direction="inbound",
        received_via_account="afya",
    )
    _upsert_contact_core(
        email="sarah@example.com",
        display_name="Sarah",
        organization=None,
        db_path=db_path,
        direction="inbound",
        received_via_account="personal",
    )
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT account_origin, seen_via_accounts FROM contacts WHERE email = ?",
            ("sarah@example.com",),
        ).fetchone()
    assert row[0] == "afya"  # write-once
    assert sorted(json.loads(row[1])) == ["afya", "personal"]


def test_account_origin_never_updates(db_path):
    _upsert_contact_core(
        email="x@example.com",
        display_name="X",
        organization=None,
        db_path=db_path,
        direction="inbound",
        received_via_account="afya",
    )
    # Many subsequent inbound from a different account — origin must stay.
    for _ in range(3):
        _upsert_contact_core(
            email="x@example.com",
            display_name="X",
            organization=None,
            db_path=db_path,
            direction="inbound",
            received_via_account="personal",
        )
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT account_origin FROM contacts WHERE email = ?",
            ("x@example.com",),
        ).fetchone()
    assert row[0] == "afya"


def test_seen_via_accounts_dedups(db_path):
    """Repeated inbound via same account doesn't grow seen_via_accounts."""
    for _ in range(3):
        _upsert_contact_core(
            email="dup@example.com",
            display_name="Dup",
            organization=None,
            db_path=db_path,
            direction="inbound",
            received_via_account="afya",
        )
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT seen_via_accounts FROM contacts WHERE email = ?",
            ("dup@example.com",),
        ).fetchone()
    assert json.loads(row[0]) == ["afya"]


def test_from_row_parses_json_array(db_path):
    """Contact.from_row JSON-decodes seen_via_accounts (C3 condition)."""
    _upsert_contact_core(
        email="parse@example.com",
        display_name="Parse",
        organization=None,
        db_path=db_path,
        direction="inbound",
        received_via_account="afya",
    )
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM contacts WHERE email = ?", ("parse@example.com",)).fetchone()
    contact = Contact.from_row(row)
    assert contact.seen_via_accounts == ["afya"]
    # The list-typed field must support list semantics — i.e. we can append.
    contact.seen_via_accounts.append("personal")
    assert contact.seen_via_accounts == ["afya", "personal"]
