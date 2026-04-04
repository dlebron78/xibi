"""
Tests for xibi/skills/sample/nudge/tools/nudge.py

Covers:
  1. Message formatting — emoji prefix per category, thread reference inclusion
  2. Missing-message error path
  3. Skill auto-discovery via SkillRegistry
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from xibi.skills.registry import SkillRegistry
from xibi.skills.sample.nudge.tools.nudge import run

# ---------------------------------------------------------------------------
# 1. Message formatting
# ---------------------------------------------------------------------------


def test_nudge_formats_urgent_prefix():
    """urgent category → 🚨 prefix in the text sent to Telegram."""
    sent_texts = []

    def mock_urlopen(req, timeout=10):
        body = req.data.decode("utf-8")
        import json

        sent_texts.append(json.loads(body)["text"])
        resp = MagicMock()
        resp.read.return_value = b'{"ok": true, "result": {}}'
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with (
        patch("xibi.skills.sample.nudge.tools.nudge.Path.exists", return_value=False),
        patch(
            "os.environ.get",
            side_effect=lambda k, *d: {
                "XIBI_TELEGRAM_TOKEN": "fake-token",
                "TELEGRAM_BOT_TOKEN": None,
                "XIBI_WORKDIR": None,
                "XIBI_TELEGRAM_CHAT_ID": "12345",
                "TELEGRAM_CHAT_ID": None,
            }.get(k, d[0] if d else None),
        ),
        patch("urllib.request.urlopen", side_effect=mock_urlopen),
    ):
        result = run({"message": "High priority email from Sarah", "category": "urgent", "thread_id": 42})

    assert result["status"] == "ok"
    assert result["delivered"] is True
    assert result["category"] == "urgent"
    assert result["thread_id"] == 42
    assert len(sent_texts) == 1
    assert sent_texts[0].startswith("🚨"), f"Expected 🚨 prefix, got: {sent_texts[0]!r}"
    assert "Thread #42" in sent_texts[0]


def test_nudge_formats_info_prefix():
    """info category (default) → ℹ️ prefix."""
    sent_texts = []

    def mock_urlopen(req, timeout=10):
        import json

        sent_texts.append(json.loads(req.data.decode())["text"])
        resp = MagicMock()
        resp.read.return_value = b'{"ok": true, "result": {}}'
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with (
        patch("xibi.skills.sample.nudge.tools.nudge.Path.exists", return_value=False),
        patch(
            "os.environ.get",
            side_effect=lambda k, *d: {
                "XIBI_TELEGRAM_TOKEN": "fake-token",
                "TELEGRAM_BOT_TOKEN": None,
                "XIBI_WORKDIR": None,
                "XIBI_TELEGRAM_CHAT_ID": "12345",
                "TELEGRAM_CHAT_ID": None,
            }.get(k, d[0] if d else None),
        ),
        patch("urllib.request.urlopen", side_effect=mock_urlopen),
    ):
        result = run({"message": "Daily digest ready", "category": "info"})

    assert result["status"] == "ok"
    assert sent_texts[0].startswith("ℹ️"), f"Expected ℹ️ prefix, got: {sent_texts[0]!r}"


def test_nudge_includes_refs_count():
    """refs list → signal count appended to text."""
    sent_texts = []

    def mock_urlopen(req, timeout=10):
        import json

        sent_texts.append(json.loads(req.data.decode())["text"])
        resp = MagicMock()
        resp.read.return_value = b'{"ok": true, "result": {}}'
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with (
        patch("xibi.skills.sample.nudge.tools.nudge.Path.exists", return_value=False),
        patch(
            "os.environ.get",
            side_effect=lambda k, *d: {
                "XIBI_TELEGRAM_TOKEN": "fake-token",
                "TELEGRAM_BOT_TOKEN": None,
                "XIBI_WORKDIR": None,
                "XIBI_TELEGRAM_CHAT_ID": "12345",
                "TELEGRAM_CHAT_ID": None,
            }.get(k, d[0] if d else None),
        ),
        patch("urllib.request.urlopen", side_effect=mock_urlopen),
    ):
        result = run({"message": "Check these signals", "refs": ["sig-1", "sig-2", "sig-3"]})

    assert result["status"] == "ok"
    assert "3 related signal(s)" in sent_texts[0]


# ---------------------------------------------------------------------------
# 2. Error path — missing message
# ---------------------------------------------------------------------------


def test_nudge_error_on_missing_message():
    """`{}` → `{status: "error", error: "message is required"}` with no Telegram call."""
    with patch("urllib.request.urlopen") as mock_open:
        result = run({})

    assert result == {"status": "error", "error": "message is required"}
    mock_open.assert_not_called()


def test_nudge_error_on_empty_message():
    """Empty string message also triggers the missing-message error."""
    with patch("urllib.request.urlopen") as mock_open:
        result = run({"message": ""})

    assert result["status"] == "error"
    assert "message is required" in result["error"]
    mock_open.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Skill auto-discovery via SkillRegistry
# ---------------------------------------------------------------------------


def test_nudge_auto_discovered(tmp_path):
    """
    SkillRegistry pointed at the actual sample skills dir discovers the nudge skill
    and can resolve find_skill_for_tool("nudge").
    """
    # Locate the sample skills directory relative to this repo
    repo_root = Path(__file__).parent.parent
    sample_skills_dir = repo_root / "xibi" / "skills" / "sample"

    registry = SkillRegistry(str(sample_skills_dir))
    skill_name = registry.find_skill_for_tool("nudge")

    assert skill_name == "nudge", (
        f"Expected registry to discover 'nudge' skill, got: {skill_name!r}. "
        f"Skills loaded: {list(registry.skills.keys())}"
    )


def test_nudge_manifest_has_required_fields():
    """Manifest is well-formed: has name, description, input_schema, and required=['message']."""
    repo_root = Path(__file__).parent.parent
    sample_skills_dir = repo_root / "xibi" / "skills" / "sample"

    registry = SkillRegistry(str(sample_skills_dir))
    tool_meta = registry.get_tool_meta("nudge", "nudge")

    assert tool_meta is not None, "nudge tool not found in manifest"
    assert "input_schema" in tool_meta, "input_schema missing from manifest"
    assert tool_meta["input_schema"]["required"] == ["message"]
    assert tool_meta.get("tier") == "YELLOW"
