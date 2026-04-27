"""Tests for review_cycle <accounts> block + per-signal account attr (step-110)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from xibi.alerting.rules import RuleEngine
from xibi.db import migrate, open_db
from xibi.heartbeat.review_cycle import _accounts_block, _gather_review_context


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "xibi.db"
    migrate(p)
    with sqlite3.connect(str(p)) as conn:
        conn.execute(
            "INSERT INTO oauth_accounts (id, user_id, provider, nickname, scopes, metadata, status) "
            "VALUES (?, 'default-owner', 'google_calendar', 'afya', '', ?, 'active')",
            ("a1", json.dumps({"email_alias": "lebron@afya.fit"})),
        )
        conn.execute(
            "INSERT INTO oauth_accounts (id, user_id, provider, nickname, scopes, metadata, status) "
            "VALUES (?, 'default-owner', 'google_calendar', 'personal', '', ?, 'active')",
            ("a2", json.dumps({"email_alias": "dannylebron@gmail.com"})),
        )
    return p


def test_accounts_block_in_prompt(db_path):
    block = _accounts_block(db_path)
    assert "<accounts>" in block
    assert 'nickname="afya"' in block
    assert 'email_alias="lebron@afya.fit"' in block
    assert 'nickname="personal"' in block


def test_signal_xml_includes_account_attr(db_path):
    engine = RuleEngine(db_path=db_path)
    with open_db(db_path) as conn, conn:
        engine.log_signal_with_conn(
            conn,
            source="email",
            topic_hint="Re: Q3",
            entity_text="manager",
            entity_type="person",
            content_preview="Q3 review",
            ref_id="email-1",
            ref_source="email",
            received_via_account="afya",
            received_via_email_alias="lebron@afya.fit",
        )
    ctx = _gather_review_context(db_path)
    assert 'received_via_account="afya"' in ctx


def test_signal_xml_omits_account_attr_when_null(db_path):
    """A signal with no provenance must not get a hollow attribute."""
    engine = RuleEngine(db_path=db_path)
    with open_db(db_path) as conn, conn:
        engine.log_signal_with_conn(
            conn,
            source="email",
            topic_hint="No-prov",
            entity_text="someone",
            entity_type="person",
            content_preview="no prov",
            ref_id="email-2",
            ref_source="email",
        )
    ctx = _gather_review_context(db_path)
    # The empty attribute should not appear
    assert 'received_via_account=""' not in ctx


def test_empty_accounts_block_when_none(tmp_path):
    """No connected accounts → self-closing <accounts/> block, no crash."""
    p = tmp_path / "xibi.db"
    migrate(p)
    block = _accounts_block(p)
    assert block == "<accounts/>"
