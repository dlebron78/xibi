from unittest.mock import MagicMock, patch

import pytest

from xibi.db import open_db
from xibi.heartbeat.review_cycle import ReviewOutput, _parse_review_response, execute_review, run_review_cycle


@pytest.fixture
def db_path(tmp_path):
    db = tmp_path / "test.db"
    from xibi.db.migrations import migrate
    migrate(db)
    return db

def test_parse_review_response():
    response = """
<reasoning>
Everything is fine.
</reasoning>

<reclassifications>
123 | CRITICAL | Important miss
456 | LOW | Too much noise
</reclassifications>

<priority_context>
Daniel is focused on testing.
</priority_context>

<memory_notes>
preference | loves pizza
</memory_notes>

<contact_updates>
c1 | colleague | Sarah from HR
</contact_updates>

<message>
Hello Daniel.
</message>
"""
    output = _parse_review_response(response)

    assert output.reasoning == "Everything is fine."
    assert len(output.reclassifications) == 2
    assert output.reclassifications[0] == {"signal_id": 123, "new_tier": "CRITICAL", "reason": "Important miss"}
    assert output.priority_context == "Daniel is focused on testing."
    assert output.memory_notes == [{"key": "preference", "value": "loves pizza"}]
    assert output.contact_updates == [{"contact_id": "c1", "relationship": "colleague", "notes": "Sarah from HR"}]
    assert output.message == "Hello Daniel."

@pytest.mark.asyncio
async def test_execute_review(db_path):
    # Setup some data
    with open_db(db_path) as conn:
        conn.execute("INSERT INTO signals (id, source, content_preview, urgency) VALUES (123, 'email', 'test', 'MEDIUM')")
        conn.execute("INSERT INTO contacts (id, display_name) VALUES ('c1', 'Sarah')")

    output = ReviewOutput(
        reclassifications=[{"signal_id": 123, "new_tier": "CRITICAL", "reason": "Urgent"}],
        priority_context="New priorities",
        memory_notes=[{"key": "m1", "value": "v1"}],
        contact_updates=[{"contact_id": "c1", "relationship": "colleague", "notes": "HR"}],
        message="Hi",
        reasoning="Reasoning"
    )

    adapter = MagicMock()
    config = {"telegram_chat_id": 12345}

    await execute_review(output, db_path, config, adapter)

    with open_db(db_path) as conn:
        # Check reclassification
        row = conn.execute("SELECT urgency FROM signals WHERE id = 123").fetchone()
        assert row[0] == "CRITICAL"

        # Check engagement log
        row = conn.execute("SELECT event_type FROM engagements WHERE signal_id = '123'").fetchone()
        assert row[0] == "reclassified"

        # Check priority context
        row = conn.execute("SELECT content FROM priority_context").fetchone()
        assert row[0] == "New priorities"

        # Check memory note
        row = conn.execute("SELECT value FROM beliefs WHERE key = 'm1'").fetchone()
        assert row[0] == "v1"

        # Check contact update
        row = conn.execute("SELECT relationship, notes FROM contacts WHERE id = 'c1'").fetchone()
        assert row[0] == "colleague"
        assert row[1] == "HR"

        # Check trace
        row = conn.execute("SELECT reasoning FROM review_traces").fetchone()
        assert row[0] == "Reasoning"

    adapter.send_message.assert_called_once_with(12345, "Hi")

@pytest.mark.asyncio
@patch("xibi.heartbeat.review_cycle.get_model")
async def test_run_review_cycle(mock_get_model, db_path):
    mock_model = MagicMock()
    mock_model.generate.return_value = "<reasoning>All good</reasoning><priority_context>Work hard</priority_context>"
    mock_get_model.return_value = mock_model

    output = await run_review_cycle(db_path, {})

    assert output.reasoning == "All good"
    assert output.priority_context == "Work hard"
    mock_model.generate.assert_called_once()
