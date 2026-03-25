import json
import sqlite3
import pytest
from pathlib import Path
from tempfile import TemporaryDirectory

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from bregger_shadow import ShadowMatcher, tokenize


@pytest.fixture
def fake_corpus():
    return [
        ("email", "list_unread", "check my email"),
        ("email", "list_unread", "any new messages"),
        ("email", "send_email", "email dan the meeting notes"),
        ("calendar", "list_events", "whats on my calendar"),
        ("memory", "recall", "what do i know about sarah"),
    ]


def test_tokenize():
    assert tokenize("Hello, World!") == ["hello", "world"]
    assert tokenize("what's on my calendar") == ["what", "s", "on", "my", "calendar"]


def test_shadow_exact_match(fake_corpus):
    matcher = ShadowMatcher()
    matcher.build_corpus(fake_corpus)

    res = matcher.match("check my email")
    assert res is not None
    assert res["predicted_tool"] == "list_unread"
    assert res["score"] > 0.9


def test_shadow_fuzzy_match(fake_corpus):
    matcher = ShadowMatcher()
    matcher.build_corpus(fake_corpus)

    # Needs to normalize well enough to match "any new messages"
    res = matcher.match("do i have any new messages today")
    assert res is not None
    assert res["predicted_tool"] == "list_unread"


def test_shadow_no_match(fake_corpus):
    matcher = ShadowMatcher()
    matcher.build_corpus(fake_corpus)

    res = matcher.match("tell me a joke about dogs")
    assert res is None


def test_shadow_load_manifests(monkeypatch):
    """Test loading examples from JSON manifests."""
    with TemporaryDirectory() as temp_dir:
        skills_path = Path(temp_dir) / "skills"
        skills_path.mkdir()

        # Fake email skill
        email_dir = skills_path / "email"
        email_dir.mkdir()
        email_manifest = email_dir / "manifest.json"

        manifest_data = {
            "name": "email",
            "tools": [{"name": "list_unread", "examples": ["check my inbox", "read mail -> with some comment"]}],
        }
        email_manifest.write_text(json.dumps(manifest_data))

        matcher = ShadowMatcher()
        matcher.load_manifests(str(skills_path))

        assert len(matcher.documents) == 2

        res = matcher.match("check inbox")
        assert res is not None
        assert res["predicted_tool"] == "list_unread"
