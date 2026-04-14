import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from skills.memory.tools import archive, recall, remember
from xibi.db import init_workdir


@pytest.fixture
def xibi_workdir():
    with TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        init_workdir(workdir)
        yield workdir


def test_remember_writes_to_xibi_db(xibi_workdir):
    # 1. call remember.run
    res = remember.run({"content": "test fact", "category": "preference", "_workdir": str(xibi_workdir)})
    assert res["status"] == "success"

    # 2. open xibi.db directly with sqlite3
    db_path = xibi_workdir / "data" / "xibi.db"
    with sqlite3.connect(db_path) as conn:
        # 3. assert the belief was written to the beliefs table
        row = conn.execute("SELECT value FROM beliefs WHERE key = 'test fact'").fetchone()
        assert row is not None
        assert row[0] == "test fact"


def test_recall_reads_from_beliefs_table(xibi_workdir):
    db_path = xibi_workdir / "data" / "xibi.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO beliefs (key, value, type, valid_until) VALUES (?, ?, ?, NULL)",
            ("mem:belief_keyword", "Some belief content", "session_memory"),
        )
        conn.commit()

    res = recall.run({"query": "belief_keyword", "_workdir": str(xibi_workdir)})
    assert res["status"] == "success"
    # assert the belief appears in the response with source: "belief"
    found = [
        item for item in res["items"] if item.get("source") == "belief" and "belief_keyword" in item.get("key", "")
    ]
    assert len(found) > 0
    assert found[0]["content"] == "Some belief content"


def test_recall_reads_from_ledger_table(xibi_workdir):
    db_path = xibi_workdir / "data" / "xibi.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO ledger (id, category, content) VALUES (?, ?, ?)", ("test-id", "note", "ledger_keyword content")
        )
        conn.commit()

    res = recall.run({"query": "ledger_keyword", "_workdir": str(xibi_workdir)})
    assert res["status"] == "success"
    # assert the ledger item appears in the response with source: "ledger"
    found = [
        item for item in res["items"] if item.get("source") == "ledger" and "ledger_keyword" in item.get("content", "")
    ]
    assert len(found) > 0


def test_recall_merges_both_sources(xibi_workdir):
    db_path = xibi_workdir / "data" / "xibi.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO beliefs (key, value, type, valid_until) VALUES (?, ?, ?, NULL)",
            ("mem:shared_key", "Belief content", "session_memory"),
        )
        conn.execute(
            "INSERT INTO ledger (id, category, content) VALUES (?, ?, ?)", ("id-2", "note", "Ledger shared_key content")
        )
        conn.commit()

    res = recall.run({"query": "shared_key", "_workdir": str(xibi_workdir)})
    assert res["status"] == "success"
    sources = [item.get("source") for item in res["items"]]
    assert "belief" in sources
    assert "ledger" in sources


def test_recall_excludes_compression_markers(xibi_workdir):
    db_path = xibi_workdir / "data" / "xibi.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO beliefs (key, value, type, valid_until) VALUES (?, ?, ?, NULL)",
            ("marker_key", "marker value", "session_compression_marker"),
        )
        conn.commit()

    res = recall.run({"query": "marker_key", "_workdir": str(xibi_workdir)})
    assert res["status"] == "success"
    for item in res["items"]:
        assert item.get("key") != "marker_key"


def test_recall_excludes_expired_beliefs(xibi_workdir):
    db_path = xibi_workdir / "data" / "xibi.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO beliefs (key, value, type, valid_until) VALUES (?, ?, ?, ?)",
            ("expired_key", "expired value", "session_memory", "2000-01-01 00:00:00"),
        )
        conn.commit()

    res = recall.run({"query": "expired_key", "_workdir": str(xibi_workdir)})
    assert res["status"] == "success"
    for item in res["items"]:
        assert item.get("key") != "expired_key"


def test_archive_expires_belief(xibi_workdir):
    # write a belief via remember.run
    remember.run(
        {"content": "to be archived", "category": "preference", "entity": "test_key", "_workdir": str(xibi_workdir)}
    )

    # call archive.run
    res = archive.run({"query": "test_key", "_confirmed": True, "_workdir": str(xibi_workdir)})
    assert res["status"] == "success"

    # open xibi.db directly and assert valid_until IS NOT NULL
    db_path = xibi_workdir / "data" / "xibi.db"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT valid_until FROM beliefs WHERE key = 'test_key'").fetchone()
        assert row is not None
        assert row[0] is not None


def test_remember_missing_db_raises_cleanly():
    # call remember.run with nonexistent path
    res = remember.run({"content": "x", "_workdir": "/nonexistent/path/at/all"})
    assert res["status"] == "error"
    # Expecting a useful message from open_db or the tool
    assert (
        "Database not found" in res["message"]
        or "No such file or directory" in res["message"]
        or "unable to open database file" in res["message"]
    )
