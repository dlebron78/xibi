import pytest

from xibi.db import open_db
from xibi.heartbeat.classification import build_classification_prompt, build_priority_context
from xibi.heartbeat.context_assembly import SignalContext


@pytest.fixture
def db_path(tmp_path):
    db = tmp_path / "test.db"
    from xibi.db.migrations import migrate

    migrate(db)
    return db


def test_build_classification_prompt_new(db_path):
    ctx = SignalContext(
        signal_ref_id="123", sender_id="alice@example.com", sender_name="Alice", headline="Lunch?", db_path=db_path
    )
    email = {"id": "123"}

    # Write priority context
    with open_db(db_path) as conn, conn:
        conn.execute("INSERT INTO priority_context (content) VALUES ('Daniel is busy.')")

    prompt = build_classification_prompt(email, ctx)

    assert "You are Daniel's chief of staff" in prompt
    assert "Daniel is busy." in prompt
    assert "CONTEXT:" in prompt
    assert "From: Alice <alice@example.com>" in prompt


def test_build_classification_prompt_includes_provenance_line(db_path):
    """Step-109: provenance line renders only when received_via_email_alias is set."""
    ctx = SignalContext(
        signal_ref_id="1",
        sender_id="manager@afya.fit",
        sender_name="Manager",
        headline="Q3 plans",
        db_path=db_path,
        received_via_account="afya",
        received_via_email_alias="lebron@afya.fit",
    )
    prompt = build_classification_prompt({"id": "1"}, ctx)
    assert "📥 [afya] received via lebron@afya.fit" in prompt


def test_build_classification_prompt_omits_provenance_line_when_unset(db_path):
    ctx = SignalContext(
        signal_ref_id="1",
        sender_id="manager@afya.fit",
        sender_name="Manager",
        headline="Q3 plans",
        db_path=db_path,
    )
    prompt = build_classification_prompt({"id": "1"}, ctx)
    assert "📥" not in prompt


def test_build_priority_context(db_path):
    assert build_priority_context(db_path) is None

    with open_db(db_path) as conn, conn:
        conn.execute("INSERT INTO priority_context (content) VALUES ('Focus on AI.')")

    pc = build_priority_context(db_path)
    assert "CURRENT PRIORITIES" in pc
    assert "Focus on AI." in pc
