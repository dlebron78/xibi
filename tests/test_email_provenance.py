from __future__ import annotations

import logging
from pathlib import Path

import pytest

from xibi.db import migrate
from xibi.email.provenance import (
    parse_addresses_from_header,
    resolve_account_from_email_to,
)
from xibi.oauth.store import OAuthStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
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
        scopes="calendar",
        metadata={"email_alias": "lebron@afya.fit"},
    )
    s.add_account(
        "default-owner",
        "google_calendar",
        "personal",
        refresh_token="rt",
        client_id="cid",
        client_secret="cs",
        scopes="calendar",
        metadata={"email_alias": "dannylebron@gmail.com"},
    )
    return s, db_path


def test_parse_addresses_plain_addr():
    assert parse_addresses_from_header("addr@x.com") == ["addr@x.com"]


def test_parse_addresses_name_format():
    assert parse_addresses_from_header("Daniel Lebron <lebron@afya.fit>") == ["lebron@afya.fit"]


def test_parse_addresses_quoted_name():
    assert parse_addresses_from_header('"Daniel L." <lebron@afya.fit>') == ["lebron@afya.fit"]


def test_parse_addresses_comma_separated():
    out = parse_addresses_from_header(
        '"Daniel" <lebron@afya.fit>, dannylebron@gmail.com, "Other" <c@d.com>'
    )
    assert out == ["lebron@afya.fit", "dannylebron@gmail.com", "c@d.com"]


def test_parse_addresses_malformed_returns_empty():
    assert parse_addresses_from_header("not an email") == []
    assert parse_addresses_from_header("") == []
    assert parse_addresses_from_header(None) == []


def test_parse_addresses_lowercase_normalized():
    assert parse_addresses_from_header("LEBRON@AFYA.FIT") == ["lebron@afya.fit"]


def test_parse_addresses_dedup_preserves_order():
    out = parse_addresses_from_header("a@x.com, b@x.com, A@X.COM")
    assert out == ["a@x.com", "b@x.com"]


def test_resolve_account_matches_to_header(store):
    _, db_path = store
    row = resolve_account_from_email_to(
        to_addresses=['"Daniel" <lebron@afya.fit>'],
        db_path=db_path,
    )
    assert row is not None
    assert row["nickname"] == "afya"
    assert row["email_alias"] == "lebron@afya.fit"


def test_resolve_account_no_match_returns_none_and_logs_warning(store, caplog):
    _, db_path = store
    with caplog.at_level(logging.WARNING):
        out = resolve_account_from_email_to(
            to_addresses=["unknown@example.com"],
            db_path=db_path,
        )
    assert out is None
    assert any("email_provenance_unmatched" in r.message for r in caplog.records)


def test_resolve_account_db_error_returns_none_and_logs_warning(tmp_path, caplog):
    bogus = tmp_path / "does-not-exist" / "x.db"
    with caplog.at_level(logging.WARNING):
        out = resolve_account_from_email_to(
            to_addresses=["x@y.com"],
            db_path=bogus,
        )
    # sqlite3.connect to a non-existent dir raises OperationalError
    assert out is None
    assert any("email_provenance_lookup_error" in r.message for r in caplog.records)


def test_resolve_account_delivered_to_takes_priority(store):
    _, db_path = store
    # To: header points at personal; Delivered-To at afya. Delivered-To wins.
    row = resolve_account_from_email_to(
        to_addresses=["dannylebron@gmail.com"],
        delivered_to="lebron@afya.fit",
        db_path=db_path,
    )
    assert row is not None
    assert row["nickname"] == "afya"


def test_resolve_account_multiple_recipients_first_match_wins(store, caplog):
    _, db_path = store
    with caplog.at_level(logging.INFO):
        row = resolve_account_from_email_to(
            to_addresses=["lebron@afya.fit, dannylebron@gmail.com"],
            db_path=db_path,
        )
    assert row is not None
    assert row["nickname"] == "afya"
    assert any("email_provenance_multiple_match" in r.message for r in caplog.records)


def test_resolve_account_case_insensitive_match(store):
    _, db_path = store
    row = resolve_account_from_email_to(
        to_addresses=["LEBRON@AFYA.FIT"],
        db_path=db_path,
    )
    assert row is not None
    assert row["nickname"] == "afya"


def test_resolve_account_returns_none_when_db_path_missing():
    assert resolve_account_from_email_to(["x@y.com"], db_path=None) is None
