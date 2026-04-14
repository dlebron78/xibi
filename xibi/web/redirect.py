from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from html import escape as html_escape
from pathlib import Path
from typing import Any

from aiohttp import web

from xibi.db import open_db

logger = logging.getLogger(__name__)


async def lookup_deep_link(signal_id: str, db_path: Path) -> str | None:
    """Look up the deep_link_url for a given signal_id."""
    try:
        with open_db(db_path) as conn:
            cursor = conn.execute("SELECT deep_link_url FROM signals WHERE id = ?", (signal_id,))
            row = cursor.fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.error(f"Error looking up deep link for signal {signal_id}: {e}")
        return None


def record_engagement_sync(
    db_path: Path,
    signal_id: str | None,
    event_type: str,
    source: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record an engagement event in the database (synchronous)."""
    engagement_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    metadata_json = json.dumps(metadata) if metadata else None

    try:
        with open_db(db_path) as conn, conn:
            conn.execute(
                """
                INSERT INTO engagements (id, signal_id, event_type, source, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (engagement_id, signal_id, event_type, source, created_at, metadata_json),
            )
        logger.info(f"👆 Engagement: {event_type} on {signal_id}")
    except Exception as e:
        logger.error(f"Error recording engagement for signal {signal_id}: {e}")


async def record_engagement(
    db_path: Path,
    signal_id: str | None,
    event_type: str,
    source: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record an engagement event in the database (asynchronous)."""
    import asyncio

    await asyncio.to_thread(record_engagement_sync, db_path, signal_id, event_type, source, metadata)


async def handle_redirect(request: web.Request) -> web.Response:
    """Handle GET /go/{signal_id} and redirect to the deep link."""
    signal_id = request.match_info["signal_id"]
    db_path = request.app["db_path"]

    # Look up destination
    deep_link_url = await lookup_deep_link(signal_id, db_path)
    if not deep_link_url:
        logger.warning(f"⚠️ Redirect failed for {signal_id}: Signal not found or no deep link")
        return web.Response(status=404, text="Signal not found")

    # Security: Only allow http/https schemes to prevent javascript: or other URI-based XSS
    if not (deep_link_url.startswith("http://") or deep_link_url.startswith("https://")):
        logger.error(f"❌ Malicious deep link detected for signal {signal_id}: {deep_link_url}")
        return web.Response(status=400, text="Invalid redirect URL")

    # Log the engagement event
    await record_engagement(
        db_path=db_path,
        signal_id=signal_id,
        event_type="tapped",
        source="deep_link",
        metadata={"user_agent": request.headers.get("User-Agent", "")},
    )

    # Return HTML page that triggers native app opening
    # Meta-refresh + JS redirect works better than raw 302
    # for opening native apps from Telegram's in-app browser
    # JS interpolation uses json.dumps to prevent XSS
    html = f"""<!DOCTYPE html>
    <html><head>
        <meta http-equiv="refresh" content="0;url={html_escape(deep_link_url, quote=True)}">
        <script>window.location.replace({json.dumps(deep_link_url)});</script>
    </head><body>Redirecting to native app...</body></html>"""

    return web.Response(text=html, content_type="text/html")


def create_app(db_path: Path) -> web.Application:
    """Create the aiohttp application."""
    app = web.Application()
    app["db_path"] = db_path
    app.router.add_get("/go/{signal_id}", handle_redirect)
    return app
