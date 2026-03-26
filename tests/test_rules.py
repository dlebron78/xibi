import sqlite3
from datetime import datetime, timedelta

from xibi.alerting.rules import RuleEngine


def test_ensure_tables_creates_schema(tmp_path):
    db_path = tmp_path / "test.db"
    RuleEngine(db_path)

    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}

    expected = {"rules", "triage_log", "heartbeat_state", "seen_emails", "signals"}
    assert expected.issubset(tables)


def test_default_rule_seeded(tmp_path):
    db_path = tmp_path / "test.db"
    re = RuleEngine(db_path)
    rules = re.load_rules("email_alert")
    assert len(rules) >= 1
    assert "@" in rules[0]["condition"]["contains"]


def test_evaluate_email_match(tmp_path):
    db_path = tmp_path / "test.db"
    re = RuleEngine(db_path)
    # Overwrite default rules with a specific one
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE rules SET condition = ?, message = ? WHERE id = 1",
            ('{"field": "from", "contains": "apple"}', "Email from {from}"),
        )
    # Re-init to refresh cache
    re = RuleEngine(db_path)
    rules = re.load_rules("email_alert")

    email = {"from": "updates@apple.com", "subject": "News"}
    res = re.evaluate_email(email, rules)
    assert res == "Email from updates@apple.com"


def test_evaluate_email_no_match(tmp_path):
    db_path = tmp_path / "test.db"
    re = RuleEngine(db_path)
    # Overwrite default rules to avoid match
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE rules SET condition = ? WHERE id = 1",
            ('{"field": "from", "contains": "NONEXISTENT"}',),
        )
    # Re-init to refresh cache
    re = RuleEngine(db_path)
    rules = re.load_rules("email_alert")

    email = {"from": "other@company.com", "subject": "News"}
    res = re.evaluate_email(email, rules)
    assert res is None


def test_log_triage_and_digest_items(tmp_path):
    db_path = tmp_path / "test.db"
    re = RuleEngine(db_path)
    re.log_triage("1", "sender1", "sub1", "DIGEST")
    re.log_triage("2", "sender2", "sub2", "NOISE")
    re.log_triage("3", "sender3", "sub3", "URGENT")

    items = re.get_digest_items()
    assert len(items) == 2
    senders = {i["sender"] for i in items}
    assert "sender1" in senders
    assert "sender2" in senders
    assert "sender3" not in senders


def test_update_watermark_advances_time(tmp_path):
    db_path = tmp_path / "test.db"
    re = RuleEngine(db_path)
    re.log_triage("1", "sender1", "sub1", "DIGEST")
    assert len(re.get_digest_items()) == 1

    re.update_watermark()
    assert len(re.get_digest_items()) == 0


def test_was_digest_sent_since_true(tmp_path):
    db_path = tmp_path / "test.db"
    re = RuleEngine(db_path)
    re.update_watermark()
    assert re.was_digest_sent_since(datetime.now() - timedelta(hours=1)) is True


def test_was_digest_sent_since_false(tmp_path):
    db_path = tmp_path / "test.db"
    re = RuleEngine(db_path)
    # default watermark is 1970
    assert re.was_digest_sent_since(datetime.now() - timedelta(hours=1)) is False


def test_mark_seen_and_get_seen_ids(tmp_path):
    db_path = tmp_path / "test.db"
    re = RuleEngine(db_path)
    re.mark_seen("id1")
    re.mark_seen("id2")
    seen = re.get_seen_ids()
    assert seen == {"id1", "id2"}


def test_log_signal_deduplication(tmp_path):
    db_path = tmp_path / "test.db"
    re = RuleEngine(db_path)
    re.log_signal("src", "topic", "ent", "type", "content", "ref1", "refsrc")
    re.log_signal("src", "topic", "ent", "type", "content", "ref1", "refsrc")

    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM signals")
        count = cursor.fetchone()[0]
    assert count == 1
