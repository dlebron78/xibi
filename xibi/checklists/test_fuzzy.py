from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from xibi.checklists.fuzzy import fuzzy_match_item


@pytest.fixture
def temp_db(tmp_path: Path) -> str:
    db_path = tmp_path / "test_checklists.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE checklist_instance_items (
            id TEXT PRIMARY KEY,
            instance_id TEXT,
            label TEXT,
            position INTEGER
        )
    """)
    conn.commit()
    conn.close()
    return str(db_path)

def test_fuzzy_match_token_overlap(temp_db: str) -> None:
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT INTO checklist_instance_items (id, instance_id, label, position) VALUES (?, ?, ?, ?)",
                 ("1", "inst1", "Check email", 0))
    conn.execute("INSERT INTO checklist_instance_items (id, instance_id, label, position) VALUES (?, ?, ?, ?)",
                 ("2", "inst1", "Review metrics", 1))
    conn.commit()
    conn.close()

    # Exact token overlap
    match = fuzzy_match_item(temp_db, "inst1", "email")
    assert match is not None
    assert match["label"] == "Check email"

    match = fuzzy_match_item(temp_db, "inst1", "metrics")
    assert match is not None
    assert match["label"] == "Review metrics"

def test_fuzzy_match_substring_bonus(temp_db: str) -> None:
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT INTO checklist_instance_items (id, instance_id, label, position) VALUES (?, ?, ?, ?)",
                 ("1", "inst1", "Check email inbox", 0))
    conn.commit()
    conn.close()

    match = fuzzy_match_item(temp_db, "inst1", "email inbox")
    assert match is not None
    assert match["label"] == "Check email inbox"

def test_fuzzy_match_ambiguous(temp_db: str) -> None:
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT INTO checklist_instance_items (id, instance_id, label, position) VALUES (?, ?, ?, ?)",
                 ("1", "inst1", "Check email", 0))
    conn.execute("INSERT INTO checklist_instance_items (id, instance_id, label, position) VALUES (?, ?, ?, ?)",
                 ("2", "inst1", "Check blockers", 1))
    conn.commit()
    conn.close()

    # Both have "Check"
    match = fuzzy_match_item(temp_db, "inst1", "check")
    assert match is None

def test_fuzzy_match_stopword_handling(temp_db: str) -> None:
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT INTO checklist_instance_items (id, instance_id, label, position) VALUES (?, ?, ?, ?)",
                 ("1", "inst1", "The Morning Routine", 0))
    conn.commit()
    conn.close()

    match = fuzzy_match_item(temp_db, "inst1", "routine")
    assert match is not None
    assert match["label"] == "The Morning Routine"

    match = fuzzy_match_item(temp_db, "inst1", "the")
    assert match is None # "the" is a stopword and should be filtered out
