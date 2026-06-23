"""Step-133 — email pipeline idempotency (per-email transactions).

Scope note (per TRR condition 7): the email *age gate* already shipped as a
hotfix and is pre-existing behaviour, so it is intentionally NOT re-tested
here as new work. The age gate is disabled in every test below
(``XIBI_EMAIL_MAX_AGE_DAYS=0``) so the write-phase behaviour under test is
isolated from it. This file covers the net-new step-133 guarantees:

* per-email transaction isolation — one email's failure must not roll back
  the emails that already committed (the old single-batch transaction did);
* nudge-outside-transaction — the Telegram nudge runs after the per-email
  transaction commits, so a send failure cannot roll back the DB writes;
* DEFER removal — the write gate is now ``not is_new`` only (DEFER is not a
  valid classification tier and was a vestigial special-case).
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xibi.alerting.rules import RuleEngine
from xibi.heartbeat.poller import HeartbeatPoller


@pytest.fixture
def mock_db(tmp_path):
    db_path = tmp_path / "test.db"
    from xibi.db.migrations import migrate

    migrate(db_path)
    return db_path


@pytest.fixture
def mock_config(tmp_path):
    cfg = {
        "models": {"text": {"fast": {"provider": "ollama", "model": "gemma4:e4b"}}},
        "providers": {"ollama": {"base_url": "http://localhost:11434"}},
    }
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg))
    return cfg_path


def _email_signal(email_id: str) -> dict:
    """A minimal email raw-signal that passes through the write phase.

    No ``date`` field — combined with the age gate disabled in tests, every
    email reaches the per-email write block.
    """
    return {
        "ref_id": email_id,
        "source": "email",
        "ref_source": "email",
        "topic_hint": f"Subject {email_id}",
        "entity_text": f"{email_id}@example.com",
        "content_preview": f"preview {email_id}",
        "metadata": {"email": {"id": email_id}},
    }


def _make_poller(db_path, config_path, rules):
    return HeartbeatPoller(
        skills_dir=Path("/tmp"),
        db_path=db_path,
        adapter=MagicMock(),
        rules=rules,
        allowed_chat_ids=[123],
        config_path=str(config_path),
    )


@pytest.mark.asyncio
async def test_per_email_transaction_isolation(mock_db, mock_config, monkeypatch):
    """A failure on one email must not roll back emails that already committed.

    e2's ``mark_seen`` raises mid-transaction. e1 (before) and e3 (after) must
    still be durably committed in both ``seen_emails`` and ``triage_log``;
    e2's whole transaction (signal + triage + seen) must roll back.
    """
    monkeypatch.setenv("XIBI_EMAIL_MAX_AGE_DAYS", "0")  # disable age gate
    monkeypatch.setattr("xibi.heartbeat.email_body.find_himalaya", lambda: None)

    rules = RuleEngine(mock_db)
    orig_mark_seen = rules.mark_seen_with_conn

    def failing_mark_seen(conn, email_id):
        if email_id == "e2":
            raise sqlite3.OperationalError("simulated write failure for e2")
        return orig_mark_seen(conn, email_id)

    rules.mark_seen_with_conn = failing_mark_seen

    poller = _make_poller(mock_db, mock_config, rules)
    poller._classify_signal = MagicMock(return_value=("MEDIUM", "reason"))
    # Keep MEDIUM from escalating into a nudge-eligible tier.
    poller._should_escalate = lambda verdict, topic, subject, priority: (verdict, subject)

    raw = [_email_signal(eid) for eid in ("e1", "e2", "e3")]
    await poller._process_email_signals(raw_signals=raw, seen_ids=set(), triage_rules={}, email_rules=[])

    with sqlite3.connect(mock_db) as conn:
        seen = {r[0] for r in conn.execute("SELECT email_id FROM seen_emails").fetchall()}
        triaged = {r[0] for r in conn.execute("SELECT email_id FROM triage_log").fetchall()}

    assert "e1" in seen and "e3" in seen, "emails around the failure must commit"
    assert "e2" not in seen, "the failing email's transaction must roll back"
    assert "e1" in triaged and "e3" in triaged
    assert "e2" not in triaged, "rollback must also drop the failing email's triage row"


@pytest.mark.asyncio
async def test_nudge_outside_transaction(mock_db, mock_config, monkeypatch):
    """The nudge runs after the transaction commits; a nudge failure is benign.

    The mocked ``compose_smart_nudge`` proves ordering by reading the row from
    a *separate* connection (only visible once committed), then raises to
    simulate a Telegram outage. The committed write must survive.
    """
    monkeypatch.setenv("XIBI_EMAIL_MAX_AGE_DAYS", "0")
    monkeypatch.setattr("xibi.heartbeat.email_body.find_himalaya", lambda: None)

    rules = RuleEngine(mock_db)
    poller = _make_poller(mock_db, mock_config, rules)
    poller._classify_signal = MagicMock(return_value=("CRITICAL", "reason"))

    observed = {}

    def nudge_side_effect(ctx, **kwargs):
        # A fresh connection can only see the row if the per-email transaction
        # already committed — i.e. the nudge fires *after* the with-block.
        with sqlite3.connect(mock_db) as c:
            rows = c.execute("SELECT 1 FROM seen_emails WHERE email_id = ?", ("e1",)).fetchall()
        observed["committed_before_nudge"] = bool(rows)
        raise RuntimeError("simulated Telegram failure")

    with patch("xibi.heartbeat.rich_nudge.compose_smart_nudge", side_effect=nudge_side_effect):
        # Must not raise: the nudge failure is caught per-email.
        await poller._process_email_signals(
            raw_signals=[_email_signal("e1")],
            seen_ids=set(),
            triage_rules={},
            email_rules=[],
        )

    assert observed.get("committed_before_nudge") is True, "nudge must run after the per-email transaction commits"

    with sqlite3.connect(mock_db) as c:
        survived = c.execute("SELECT 1 FROM seen_emails WHERE email_id = ?", ("e1",)).fetchall()
    assert survived, "committed DB writes must survive a nudge/Telegram failure"


@pytest.mark.asyncio
async def test_defer_verdict_removed(mock_db, mock_config, monkeypatch):
    """DEFER is no longer special-cased: the write gate is ``not is_new`` only.

    Pre-fix, an ``item["verdict"] == "DEFER"`` email was skipped (``continue``)
    before triage/mark_seen. DEFER is not in VALID_TIERS, so this was dead
    code. A hypothetical DEFER email must now be logged and marked seen like
    any other new email.
    """
    monkeypatch.setenv("XIBI_EMAIL_MAX_AGE_DAYS", "0")
    monkeypatch.setattr("xibi.heartbeat.email_body.find_himalaya", lambda: None)

    rules = RuleEngine(mock_db)
    poller = _make_poller(mock_db, mock_config, rules)
    poller._classify_signal = MagicMock(return_value=("DEFER", "reason"))

    await poller._process_email_signals(
        raw_signals=[_email_signal("edefer")],
        seen_ids=set(),
        triage_rules={},
        email_rules=[],
    )

    with sqlite3.connect(mock_db) as c:
        seen = c.execute("SELECT 1 FROM seen_emails WHERE email_id = ?", ("edefer",)).fetchall()
        verdicts = [
            r[0] for r in c.execute("SELECT verdict FROM triage_log WHERE email_id = ?", ("edefer",)).fetchall()
        ]

    assert seen, "a DEFER email must now be marked seen (no DEFER special-case)"
    assert verdicts == ["DEFER"], "a DEFER email must be triage-logged, not skipped"
