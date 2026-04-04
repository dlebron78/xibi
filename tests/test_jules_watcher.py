import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xibi.heartbeat.jules_watcher import JulesWatcher


@pytest.fixture
def mock_llm():
    return MagicMock()


@pytest.fixture
def broadcast_fn():
    return MagicMock()


@pytest.fixture
def jules_watcher(tmp_path, mock_llm, broadcast_fn):
    history_file = tmp_path / "history.jsonl"
    return JulesWatcher(
        api_key="fake_key",
        history_file=history_file,
        llm=mock_llm,
        broadcast_fn=broadcast_fn,
        state_dir=tmp_path / "state",
    )


def test_load_recent_sessions_empty(jules_watcher):
    assert jules_watcher._load_recent_sessions() == []


def test_load_recent_sessions(jules_watcher):
    history_file = jules_watcher.history_file

    now = datetime.utcnow()
    recent_ts = (now - timedelta(days=1)).isoformat() + "Z"
    old_ts = (now - timedelta(days=10)).isoformat() + "Z"

    entry_recent = {"session": "projects/p/sessions/s1", "ts": recent_ts}
    entry_old = {"session": "projects/p/sessions/s2", "ts": old_ts}
    entry_no_ts = {"session": "projects/p/sessions/s3"}

    with open(history_file, "w") as f:
        f.write(json.dumps(entry_recent) + "\n")
        f.write(json.dumps(entry_old) + "\n")
        f.write(json.dumps(entry_no_ts) + "\n")

    sessions = jules_watcher._load_recent_sessions()
    assert len(sessions) == 2
    session_ids = [s["session"] for s in sessions]
    assert "projects/p/sessions/s1" in session_ids
    assert "projects/p/sessions/s3" in session_ids
    assert "projects/p/sessions/s2" not in session_ids


def test_api_get_success(jules_watcher):
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"state": "AWAITING_USER_FEEDBACK"}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_response

        data = jules_watcher._api_get("sessions/s1")
        assert data["state"] == "AWAITING_USER_FEEDBACK"


def test_get_session_state(jules_watcher):
    with patch.object(jules_watcher, "_api_get", return_value={"state": "RUNNING"}):
        assert jules_watcher._get_session_state("s1") == "RUNNING"


def test_generate_answer(jules_watcher, mock_llm):
    mock_llm.generate.return_value = "This is the answer."
    ans = jules_watcher._generate_answer("question", "spec", "task")
    assert ans == "This is the answer."
    mock_llm.generate.assert_called()


def test_generate_answer_escalate(jules_watcher, mock_llm):
    mock_llm.generate.return_value = "ESCALATE"
    ans = jules_watcher._generate_answer("question", "spec", "task")
    assert ans is None


def test_poll_nothing_to_do(jules_watcher):
    with patch.object(jules_watcher, "_load_recent_sessions", return_value=[]):
        jules_watcher.poll()
        jules_watcher.broadcast.assert_not_called()


def test_poll_auto_answers(jules_watcher, mock_llm):
    sessions = [{"session": "p/s1", "ts": datetime.utcnow().isoformat(), "task": "test-task"}]
    with (
        patch.object(jules_watcher, "_load_recent_sessions", return_value=sessions),
        patch.object(jules_watcher, "_get_session_state", return_value="AWAITING_USER_FEEDBACK"),
        patch.object(jules_watcher, "_find_pending_question", return_value=("Why?", "act1", "spec")),
        patch.object(jules_watcher, "_send_message") as mock_send,
    ):
        mock_llm.generate.return_value = "Because."
        jules_watcher.poll()
        mock_send.assert_called_with("s1", "Because.")
        jules_watcher.broadcast.assert_called()
        assert "act1" in jules_watcher._load_state()


def test_find_pending_question(jules_watcher):
    activities = [
        {"id": "act1", "agentMessaged": {"agentMessage": "Question 1"}},
        {"id": "act2", "agentMessaged": {"agentMessage": "Question 2"}},
    ]
    responded = {"act1": {"ts": "..."}}

    with (
        patch.object(jules_watcher, "_get_all_activities", return_value=activities),
        patch.object(jules_watcher, "_get_session_spec", return_value="The spec"),
    ):
        q, aid, spec = jules_watcher._find_pending_question("s1", responded)
        assert q == "Question 2"
        assert aid == "act2"
        assert spec == "The spec"


def test_get_all_activities_paginated(jules_watcher):
    page1 = {"activities": [{"id": "a1"}], "nextPageToken": "t2"}
    page2 = {"activities": [{"id": "a2"}]}

    def mock_api_get(path):
        if "pageToken=t2" in path:
            return page2
        return page1

    with patch.object(jules_watcher, "_api_get", side_effect=mock_api_get):
        activities = jules_watcher._get_all_activities("s1")
        assert len(activities) == 2
        assert activities[0]["id"] == "a1"
        assert activities[1]["id"] == "a2"


def test_load_state_corrupt(jules_watcher):
    jules_watcher.state_dir.mkdir(parents=True, exist_ok=True)
    jules_watcher.state_file.write_text("corrupt{")
    assert jules_watcher._load_state() == {}


def test_api_get_http_error(jules_watcher):
    with patch("urllib.request.urlopen") as mock_urlopen:
        from urllib.error import HTTPError

        mock_urlopen.side_effect = HTTPError("url", 404, "Not Found", {}, None)
        with pytest.raises(RuntimeError, match="HTTP 404"):
            jules_watcher._api_get("path")


def test_load_recent_sessions_exception(jules_watcher):
    with patch.object(Path, "read_text", side_effect=Exception("Read error")):
        assert jules_watcher._load_recent_sessions() == []


def test_poll_auto_answer_failure(jules_watcher, mock_llm):
    sessions = [{"session": "p/s1", "ts": datetime.utcnow().isoformat()}]
    with (
        patch.object(jules_watcher, "_load_recent_sessions", return_value=sessions),
        patch.object(jules_watcher, "_get_session_state", return_value="AWAITING_USER_FEEDBACK"),
        patch.object(jules_watcher, "_find_pending_question", return_value=("Why?", "act1", "spec")),
    ):
        mock_llm.generate.return_value = None
        jules_watcher.poll()
        jules_watcher.broadcast.assert_called_with("❓ **Jules needs your input on s1:**\n\nWhy?")
