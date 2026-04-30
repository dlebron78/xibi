import pytest

from xibi.db import open_db
from xibi.heartbeat.classification import (
    PRIORITY_CONTEXT_MAX_CHARS,
    build_classification_prompt,
    build_priority_context,
)
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


def test_priority_context_cap_constant_is_6000():
    """Hotfix 2026-04-28: cap raised from 2000 to 6000 to stop truncating
    operational guidance mid-content. Pin the value so a careless edit
    doesn't silently regress it."""
    assert PRIORITY_CONTEXT_MAX_CHARS == 6000


def test_build_priority_context_under_cap_not_truncated(db_path):
    """Content within the cap returns verbatim (no '[truncated]' suffix)."""
    content = ("Daniel is focused on AI. " * 220).strip()  # ~5500 chars
    assert len(content) < PRIORITY_CONTEXT_MAX_CHARS
    with open_db(db_path) as conn, conn:
        conn.execute("INSERT INTO priority_context (content) VALUES (?)", (content,))
    pc = build_priority_context(db_path)
    assert pc is not None
    assert "[truncated]" not in pc
    # Last sentence preserved.
    assert pc.rstrip().endswith("Daniel is focused on AI.")


def test_build_priority_context_over_cap_truncated_to_cap(db_path):
    """Content larger than the cap gets truncated at sentence boundary
    near PRIORITY_CONTEXT_MAX_CHARS, with the [truncated] suffix added."""
    content = ("Daniel is focused on AI. " * 270).strip()  # ~6500 chars
    assert len(content) > PRIORITY_CONTEXT_MAX_CHARS
    with open_db(db_path) as conn, conn:
        conn.execute("INSERT INTO priority_context (content) VALUES (?)", (content,))
    pc = build_priority_context(db_path)
    assert pc is not None
    assert "[truncated]" in pc
    # Body (after the "CURRENT PRIORITIES (from last review):\n" header) is
    # at most cap chars + the suffix.
    body = pc.split("\n", 1)[1]
    body_no_suffix = body.replace(" [truncated]", "")
    assert len(body_no_suffix) <= PRIORITY_CONTEXT_MAX_CHARS
