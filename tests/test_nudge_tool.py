from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

from xibi.skills.registry import SkillRegistry
from xibi.skills.sample.nudge.tools.nudge import run


def test_nudge_formats_urgent_prefix():
    sent_texts = []

    def mock_urlopen(req, timeout=10):
        import json

        sent_texts.append(json.loads(req.data.decode())["text"])
        resp = MagicMock()
        resp.read.return_value = b'{"ok": true}'
        resp.__enter__ = lambda s: s
        return resp

    with (
        patch("xibi.skills.nudge.Path.exists", return_value=False),
        patch(
            "os.environ.get",
            side_effect=lambda k, *d: {"XIBI_TELEGRAM_TOKEN": "fake", "XIBI_TELEGRAM_CHAT_ID": "123"}.get(
                k, d[0] if d else None
            ),
        ),
        patch("urllib.request.urlopen", side_effect=mock_urlopen),
    ):
        result = asyncio.run(asyncio.run(run({"message": "msg", "category": "urgent", "thread_id": "42"})))

    assert result["status"] == "ok"
    assert sent_texts[0].startswith("🚨")


def test_nudge_error_on_missing_message():
    result = asyncio.run(asyncio.run(run({})))
    assert result["status"] == "error"
