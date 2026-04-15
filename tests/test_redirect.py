from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import xibi.web.redirect as redirect


@pytest.mark.asyncio
async def test_lookup_deep_link_handles_db_error(tmp_path: Path) -> None:
    """lookup_deep_link returns None when the DB raises."""
    with patch("xibi.web.redirect.open_db", side_effect=RuntimeError("db down")):
        result = await redirect.lookup_deep_link("sig-123", tmp_path / "test.db")
    assert result is None


def test_record_engagement_sync_handles_db_error(tmp_path: Path) -> None:
    """record_engagement_sync does not raise when the DB raises."""
    with patch("xibi.web.redirect.open_db", side_effect=RuntimeError("db down")):
        redirect.record_engagement_sync(
            db_path=tmp_path / "test.db",
            signal_id="sig-123",
            event_type="tap",
            source="telegram",
        )


@pytest.mark.asyncio
async def test_handle_redirect_rejects_malicious_url(tmp_path: Path, monkeypatch) -> None:
    """handle_redirect returns 400 for javascript: and protocol-relative URLs."""
    app = web.Application()
    app["db_path"] = tmp_path / "test.db"

    for bad_url in ("javascript:alert(1)", "//evil.com"):
        async def fake_lookup(*_args, **_kwargs):
            return bad_url

        monkeypatch.setattr(redirect, "lookup_deep_link", fake_lookup)

        request = make_mocked_request(
            "GET",
            f"/go/sig-123",
            match_info={"signal_id": "sig-123"},
            app=app,
            headers={"User-Agent": "pytest"},
        )
        resp = await redirect.handle_redirect(request)
        assert resp.status == 400
