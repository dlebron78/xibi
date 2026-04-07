"""
Tests for xibi/skills/send_document.py helper functions and main send_document().
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from xibi.skills.send_document import (
    _build_multipart_body,
    _load_config,
    _resolve_chat_id,
    _resolve_token,
    send_document,
)

# ── _resolve_token ────────────────────────────────────────────────────────


def test_resolve_token_from_env_var(tmp_path):
    with patch.dict(os.environ, {"XIBI_TELEGRAM_TOKEN": "tok123"}, clear=False):
        assert _resolve_token(tmp_path) == "tok123"


def test_resolve_token_telegram_bot_token_env(tmp_path):
    with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "bot456", "XIBI_TELEGRAM_TOKEN": ""}, clear=False):
        # empty XIBI_TELEGRAM_TOKEN is falsy
        result = _resolve_token(tmp_path)
        assert result == "bot456"


def test_resolve_token_from_xibi_env_file(tmp_path):
    env_file = tmp_path / ".xibi_env"  # noqa: F841
    env_file = tmp_path / ".xibi_env"
    env_file.write_text("XIBI_TELEGRAM_TOKEN=filetoken\n")

    # Test via a direct call with a workdir whose parent contains .xibi_env
    child = tmp_path / "workdir"
    child.mkdir()
    # Place .xibi_env at child's parent (tmp_path)
    env_no_system = {k: v for k, v in os.environ.items() if k not in ("XIBI_TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN")}
    with patch.dict(os.environ, env_no_system, clear=True):
        token = _resolve_token(child)
    assert token == "filetoken"


def test_resolve_token_returns_none_when_missing(tmp_path):
    workdir = tmp_path / "wd"
    workdir.mkdir()
    env_clean = {k: v for k, v in os.environ.items() if k not in ("XIBI_TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN")}
    with patch.dict(os.environ, env_clean, clear=True):
        assert _resolve_token(workdir) is None


# ── _resolve_chat_id ──────────────────────────────────────────────────────


def test_resolve_chat_id_from_config():
    config = {"telegram": {"chat_id": "12345"}}
    assert _resolve_chat_id(config) == 12345


def test_resolve_chat_id_from_env():
    with patch.dict(os.environ, {"XIBI_TELEGRAM_CHAT_ID": "99999"}, clear=False):
        assert _resolve_chat_id({}) == 99999


def test_resolve_chat_id_from_telegram_chat_id_env():
    env_no_xibi = {k: v for k, v in os.environ.items() if k != "XIBI_TELEGRAM_CHAT_ID"}
    with patch.dict(os.environ, {**env_no_xibi, "TELEGRAM_CHAT_ID": "77777"}, clear=True):
        assert _resolve_chat_id({}) == 77777


def test_resolve_chat_id_from_allowed_chat_ids():
    env_clean = {k: v for k, v in os.environ.items() if k not in ("XIBI_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID")}
    with patch.dict(os.environ, {**env_clean, "XIBI_TELEGRAM_ALLOWED_CHAT_IDS": "11111,22222"}, clear=True):
        assert _resolve_chat_id({}) == 11111


def test_resolve_chat_id_returns_none_when_missing():
    env_clean = {
        k: v
        for k, v in os.environ.items()
        if k not in ("XIBI_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID", "XIBI_TELEGRAM_ALLOWED_CHAT_IDS")
    }
    with patch.dict(os.environ, env_clean, clear=True):
        assert _resolve_chat_id({}) is None


def test_resolve_chat_id_invalid_config_value():
    config = {"telegram": {"chat_id": "not-a-number"}}
    env_clean = {
        k: v
        for k, v in os.environ.items()
        if k not in ("XIBI_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID", "XIBI_TELEGRAM_ALLOWED_CHAT_IDS")
    }
    with patch.dict(os.environ, env_clean, clear=True):
        assert _resolve_chat_id(config) is None


# ── _load_config ──────────────────────────────────────────────────────────


def test_load_config_returns_explicit_config(tmp_path):
    explicit = {"telegram": {"chat_id": 1}}
    assert _load_config(tmp_path, explicit) == explicit


def test_load_config_reads_json_file(tmp_path):
    cfg = {"telegram": {"chat_id": 55555}}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    result = _load_config(tmp_path, None)
    assert result["telegram"]["chat_id"] == 55555


def test_load_config_returns_empty_when_no_files(tmp_path):
    assert _load_config(tmp_path, None) == {}


def test_load_config_handles_invalid_json(tmp_path):
    (tmp_path / "config.json").write_text("not valid json {{{")
    result = _load_config(tmp_path, None)
    assert result == {}


# ── _build_multipart_body ─────────────────────────────────────────────────


def test_build_multipart_body_contains_chat_id():
    body = _build_multipart_body(
        boundary="testboundary",
        chat_id=12345,
        file_bytes=b"hello",
        file_name="test.txt",
        mime_type="text/plain",
        caption=None,
    )
    assert b"12345" in body
    assert b"test.txt" in body
    assert b"hello" in body


def test_build_multipart_body_with_caption():
    body = _build_multipart_body(
        boundary="b",
        chat_id=1,
        file_bytes=b"data",
        file_name="f.csv",
        mime_type="text/csv",
        caption="My caption",
    )
    assert b"My caption" in body


def test_build_multipart_body_no_caption_field_when_none():
    body = _build_multipart_body(
        boundary="b",
        chat_id=1,
        file_bytes=b"data",
        file_name="f.txt",
        mime_type="text/plain",
        caption=None,
    )
    # caption field should NOT appear
    assert b"caption" not in body


# ── send_document ─────────────────────────────────────────────────────────


def test_send_document_missing_file_path():
    result = send_document("")
    assert result["status"] == "error"
    assert "required" in result["error"]


def test_send_document_file_not_found(tmp_path):
    result = send_document(str(tmp_path / "nonexistent.txt"))
    assert result["status"] == "error"
    assert "not found" in result["error"]


def test_send_document_empty_file(tmp_path):
    f = tmp_path / "empty.txt"
    f.write_bytes(b"")
    result = send_document(str(f))
    assert result["status"] == "error"
    assert "empty" in result["error"]


def test_send_document_file_too_large(tmp_path):
    f = tmp_path / "big.txt"
    f.write_bytes(b"x")
    import stat as stat_mod

    from xibi.skills.send_document import MAX_FILE_BYTES

    stat_mock = MagicMock()
    stat_mock.st_size = MAX_FILE_BYTES + 1
    stat_mock.st_mode = stat_mod.S_IFREG | 0o644  # regular file mode
    with patch.object(Path, "stat", return_value=stat_mock):
        result = send_document(str(f))
    assert result["status"] == "error"
    assert "too large" in result["error"]


def test_send_document_missing_token(tmp_path):
    f = tmp_path / "test.txt"
    f.write_bytes(b"content")
    env_clean = {k: v for k, v in os.environ.items() if k not in ("XIBI_TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN")}
    with patch.dict(os.environ, env_clean, clear=True):
        result = send_document(str(f), _workdir=str(tmp_path), chat_id=12345)
    assert result["status"] == "error"
    assert "Telegram not configured" in result["error"]


def test_send_document_success(tmp_path):
    f = tmp_path / "report.csv"
    f.write_bytes(b"col1,col2\n1,2\n")

    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({"ok": True}).encode()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with (
        patch.dict(os.environ, {"XIBI_TELEGRAM_TOKEN": "tok", "XIBI_TELEGRAM_CHAT_ID": "9999"}, clear=False),
        patch("urllib.request.urlopen", return_value=mock_response),
    ):
        result = send_document(str(f), caption="Here it is", _workdir=str(tmp_path))

    assert result["status"] == "ok"
    assert result["delivered"] is True
    assert result["channel"] == "telegram"
    assert result["filename"] == "report.csv"


def test_send_document_telegram_api_not_ok(tmp_path):
    f = tmp_path / "report.txt"
    f.write_bytes(b"data")

    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({"ok": False, "description": "Bad Request"}).encode()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with (
        patch.dict(os.environ, {"XIBI_TELEGRAM_TOKEN": "tok", "XIBI_TELEGRAM_CHAT_ID": "9999"}, clear=False),
        patch("urllib.request.urlopen", return_value=mock_response),
    ):
        result = send_document(str(f), _workdir=str(tmp_path))

    assert result["status"] == "error"
    assert "Bad Request" in result["error"]


def test_send_document_upload_exception(tmp_path):
    f = tmp_path / "report.txt"
    f.write_bytes(b"data")

    with (
        patch.dict(os.environ, {"XIBI_TELEGRAM_TOKEN": "tok", "XIBI_TELEGRAM_CHAT_ID": "9999"}, clear=False),
        patch("urllib.request.urlopen", side_effect=Exception("network error")),
    ):
        result = send_document(str(f), _workdir=str(tmp_path))

    assert result["status"] == "error"
    assert "upload failed" in result["error"]


def test_send_document_path_is_directory(tmp_path):
    result = send_document(str(tmp_path))
    assert result["status"] == "error"
    assert "not a regular file" in result["error"]


def test_send_document_read_bytes_fails(tmp_path):
    f = tmp_path / "unreadable.txt"
    f.write_bytes(b"data")
    with (
        patch.dict(os.environ, {"XIBI_TELEGRAM_TOKEN": "tok", "XIBI_TELEGRAM_CHAT_ID": "9999"}, clear=False),
        patch.object(Path, "read_bytes", side_effect=PermissionError("denied")),
    ):
        result = send_document(str(f), _workdir=str(tmp_path))
    assert result["status"] == "error"
    assert "could not read file" in result["error"]


def test_send_document_unknown_mime_type(tmp_path):
    f = tmp_path / "report.xibi_unknown_ext"
    f.write_bytes(b"some data")
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({"ok": True}).encode()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)
    with (
        patch.dict(os.environ, {"XIBI_TELEGRAM_TOKEN": "tok", "XIBI_TELEGRAM_CHAT_ID": "9999"}, clear=False),
        patch("urllib.request.urlopen", return_value=mock_response),
    ):
        result = send_document(str(f), _workdir=str(tmp_path))
    # Unknown extension → falls back to application/octet-stream, still succeeds
    assert result["status"] == "ok"


def test_resolve_token_xibi_env_read_error(tmp_path):
    child = tmp_path / "workdir"
    child.mkdir()
    # Create .xibi_env at parent but make read_text raise
    env_file = tmp_path / ".xibi_env"
    env_file.write_text("XIBI_TELEGRAM_TOKEN=secret\n")
    env_clean = {k: v for k, v in os.environ.items() if k not in ("XIBI_TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN")}
    with (
        patch.dict(os.environ, env_clean, clear=True),
        patch.object(Path, "read_text", side_effect=OSError("permission denied")),
    ):
        token = _resolve_token(child)
    # Should return None since read failed
    assert token is None
