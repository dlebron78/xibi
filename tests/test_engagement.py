from __future__ import annotations

import json
import sqlite3

import pytest

from xibi.db import migrate
from xibi.web.redirect import create_app, record_engagement_sync


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "xibi.db"
    migrate(path)
    return path

@pytest.fixture
async def cli(aiohttp_client, db_path):
    app = create_app(db_path)
    return await aiohttp_client(app)

# 4. test_redirect_valid_signal: GET /go/{signal_id} → 200 with HTML containing deep_link_url
async def test_redirect_valid_signal(cli, db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO signals (source, content_preview, deep_link_url) VALUES (?, ?, ?)",
            ("email", "test preview", "https://example.com/target")
        )
        signal_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    resp = await cli.get(f"/go/{signal_id}")
    assert resp.status == 200
    text = await resp.text()
    assert "https://example.com/target" in text

# 5. test_redirect_unknown_signal: GET /go/nonexistent → 404
async def test_redirect_unknown_signal(cli):
    resp = await cli.get("/go/999")
    assert resp.status == 404

# 6. test_redirect_logs_engagement: GET /go/{signal_id} → engagement row created with event_type="tapped"
async def test_redirect_logs_engagement(cli, db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO signals (source, content_preview, deep_link_url) VALUES (?, ?, ?)",
            ("email", "test preview", "https://example.com/target")
        )
        signal_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    await cli.get(f"/go/{signal_id}")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM engagements WHERE signal_id = ?", (str(signal_id),)).fetchone()
        assert row is not None
        assert row["event_type"] == "tapped"
        assert row["source"] == "deep_link"

# 7. test_redirect_records_timestamp: Tap engagement has correct created_at
async def test_redirect_records_timestamp(cli, db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO signals (source, content_preview, deep_link_url) VALUES (?, ?, ?)",
            ("email", "test preview", "https://example.com/target")
        )
        signal_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    await cli.get(f"/go/{signal_id}")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT created_at FROM engagements WHERE signal_id = ?", (str(signal_id),)).fetchone()
        assert row["created_at"] is not None

# 12. test_record_engagement_tap: record_engagement(event_type="tapped") → row in engagements table
async def test_record_engagement_tap(db_path):
    from xibi.web.redirect import record_engagement
    await record_engagement(db_path, "1", "tapped", "test_source")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM engagements WHERE signal_id = '1'").fetchone()
        assert row["event_type"] == "tapped"

# 13. test_record_engagement_reaction: Telegram reaction → engagement row with emoji in metadata
async def test_record_engagement_reaction(db_path):
    record_engagement_sync(db_path, "1", "reacted", "telegram", {"emoji": "👍"})
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM engagements WHERE event_type = 'reacted'").fetchone()
        assert json.loads(row["metadata"])["emoji"] == "👍"

# 14. test_record_engagement_correction: Correction recorded → engagement row with old/new tier in metadata
async def test_record_engagement_correction(db_path):
    metadata = {"old_tier": "MEDIUM", "new_tier": "HIGH"}
    record_engagement_sync(db_path, "1", "correction", "correction", metadata)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM engagements WHERE event_type = 'correction'").fetchone()
        meta = json.loads(row["metadata"])
        assert meta["old_tier"] == "MEDIUM"
        assert meta["new_tier"] == "HIGH"

# 15. test_record_engagement_nullable_signal: Engagement with signal_id=None → row created successfully
async def test_record_engagement_nullable_signal(db_path):
    record_engagement_sync(db_path, None, "proactive_query", "telegram", {"query": "hello"})
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM engagements WHERE event_type = 'proactive_query'").fetchone()
        assert row is not None
        assert row["signal_id"] is None

# 16. test_engagement_metadata_json: Metadata stored as valid JSON, retrievable as dict
async def test_engagement_metadata_json(db_path):
    metadata = {"key": "value", "nested": [1, 2, 3]}
    record_engagement_sync(db_path, "1", "test", "source", metadata)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM engagements WHERE signal_id = '1'").fetchone()
        assert json.loads(row["metadata"]) == metadata

# 18. test_engagement_query_by_timerange
async def test_engagement_query_by_timerange(db_path):
    record_engagement_sync(db_path, "1", "tapped", "source")
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM engagements WHERE created_at > datetime('now', '-1 hour')").fetchall()
        assert len(rows) == 1

# 19. test_engagement_query_by_type
async def test_engagement_query_by_type(db_path):
    record_engagement_sync(db_path, "1", "type_a", "source")
    record_engagement_sync(db_path, "2", "type_b", "source")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM engagements WHERE event_type = 'type_a'").fetchall()
        assert len(rows) == 1
        assert str(rows[0]["signal_id"]) == "1"
