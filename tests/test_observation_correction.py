
import pytest

from xibi.db import open_db
from xibi.observation import ObservationCycle


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    from xibi.db.migrations import SchemaManager

    sm = SchemaManager(path)
    sm.migrate()
    return path


def test_manager_stores_correction_reason(db_path):
    # Setup: one signal that gemma classified and manager will correct
    with open_db(db_path) as conn:
        conn.execute("INSERT INTO triage_log (email_id, verdict) VALUES ('msg1', 'LOW')")
        conn.execute(
            "INSERT INTO signals (id, source, content_preview, ref_id, urgency, env) VALUES (1, 'email', 'foo', 'msg1', 'LOW', 'production')"
        )

    cycle = ObservationCycle(db_path=db_path)
    review_data = {
        "signal_flags": [{"signal_id": 1, "suggested_tier": "HIGH", "reason": "This is important", "reclassify": True}]
    }

    cycle._apply_manager_updates(review_data)

    with open_db(db_path) as conn:
        row = conn.execute("SELECT urgency, correction_reason FROM signals WHERE id = 1").fetchone()
        assert row[0] == "HIGH"
        assert row[1] == "This is important"


def test_manager_no_reason(db_path):
    # Setup: one signal that gemma classified and manager will correct without reason
    with open_db(db_path) as conn:
        conn.execute("INSERT INTO triage_log (email_id, verdict) VALUES ('msg1', 'LOW')")
        conn.execute(
            "INSERT INTO signals (id, source, content_preview, ref_id, urgency, env) VALUES (1, 'email', 'foo', 'msg1', 'LOW', 'production')"
        )

    cycle = ObservationCycle(db_path=db_path)
    review_data = {
        "signal_flags": [
            {
                "signal_id": 1,
                "suggested_tier": "HIGH",
                # no reason
            }
        ]
    }

    cycle._apply_manager_updates(review_data)

    with open_db(db_path) as conn:
        row = conn.execute("SELECT urgency, correction_reason FROM signals WHERE id = 1").fetchone()
        assert row[0] == "HIGH"
        assert row[1] is None
