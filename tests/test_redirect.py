from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

import xibi.web.redirect as redirect


def test_lookup_deep_link_handles_db_error(tmp_path: Path) -> None:
    """lookup_deep_link returns None when the DB raises."""
    with patch("xibi.web.redirect.open_db", side_effect=RuntimeError("db down")):
        result = asyncio.get_event_loop().run_until_complete(
            redirect.lookup_deep_link("sig-123", tmp_path / "test.db")
        )
    assert result is None


def test_record_engagement_sync_handles_db_error(tmp_path: Path) -> None:
    """record_engagement_sync does not raise when the DB raises."""
    with patch("xibi.web.redirect.open_db", side_effect=RuntimeError("db down")):
        # Should not raise
        redirect.record_engagement_sync(
            db_path=tmp_path / "test.db",
            signal_id="sig-123",
            event_type="tap",
            source="telegram",
        )
