import pytest

from xibi.db.migrations import migrate
from xibi.web.redirect import create_app


@pytest.fixture
def db_path(tmp_path):
    db = tmp_path / "test.db"
    migrate(db)
    return db


@pytest.fixture
async def client(aiohttp_client, db_path):
    app = create_app(db_path)
    return await aiohttp_client(app)


async def test_redirect_javascript_payload(client, db_path):
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO signals (source, content_preview, deep_link_url) VALUES ('email', 'test', 'javascript:alert(1)')"
    )
    row = conn.execute("SELECT last_insert_rowid()").fetchone()
    sid = row[0]
    conn.commit()
    conn.close()

    resp = await client.get(f"/go/{sid}")
    assert resp.status == 400
    assert "Invalid redirect URL" in await resp.text()


async def test_redirect_data_url(client, db_path):
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO signals (source, content_preview, deep_link_url) VALUES ('email', 'test', 'data:text/html,<html>')"
    )
    row = conn.execute("SELECT last_insert_rowid()").fetchone()
    sid = row[0]
    conn.commit()
    conn.close()

    resp = await client.get(f"/go/{sid}")
    assert resp.status == 400


async def test_redirect_uppercase_bypass(client, db_path):
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO signals (source, content_preview, deep_link_url) VALUES ('email', 'test', 'HTTP://example.com')"
    )
    row = conn.execute("SELECT last_insert_rowid()").fetchone()
    sid = row[0]
    conn.commit()
    conn.close()

    resp = await client.get(f"/go/{sid}")
    assert resp.status == 200
    text = await resp.text()
    assert "url=HTTP://example.com" in text


async def test_redirect_whitespace_payload(client, db_path):
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO signals (source, content_preview, deep_link_url) VALUES ('email', 'test', '  https://safe.com  ')"
    )
    row = conn.execute("SELECT last_insert_rowid()").fetchone()
    sid = row[0]
    conn.commit()
    conn.close()

    resp = await client.get(f"/go/{sid}")
    assert resp.status == 200
    text = await resp.text()
    assert "url=https://safe.com" in text


async def test_redirect_protocol_relative(client, db_path):
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO signals (source, content_preview, deep_link_url) VALUES ('email', 'test', '//evil.com')")
    row = conn.execute("SELECT last_insert_rowid()").fetchone()
    sid = row[0]
    conn.commit()
    conn.close()

    resp = await client.get(f"/go/{sid}")
    assert resp.status == 400
