import hashlib
import os
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from xibi.heartbeat.extractors import (
    _extract_extension,
    _extract_filename,
    _path_to_ref_id,
    extract_file_content_signals,
)
from xibi.heartbeat.source_poller import SourcePoller


# Helper function tests
def test_path_to_ref_id_is_stable():
    path = "/home/user/notes.md"
    id1 = _path_to_ref_id(path)
    id2 = _path_to_ref_id(path)
    assert id1 == id2
    assert len(id1) == 16
    assert all(c in "0123456789abcdef" for c in id1)


def test_path_to_ref_id_different_paths_different_ids():
    assert _path_to_ref_id("/a") != _path_to_ref_id("/b")


def test_extract_filename_strips_directory():
    assert _extract_filename("/home/user/notes.md") == "notes.md"


def test_extract_filename_handles_no_dir():
    assert _extract_filename("notes.md") == "notes.md"


def test_extract_extension_basic():
    assert _extract_extension("/path/file.md") == "md"
    assert _extract_extension("/path/FILE.TXT") == "txt"


def test_extract_extension_no_extension():
    assert _extract_extension("/path/Makefile") == ""


# Extractor tests
def test_file_content_extractor_single_file():
    source = "filesystem"
    data = {"content": [{"type": "text", "text": "Hello world"}]}
    context = {"source_metadata": {"path": "/notes.md", "watch_dir": "/notes"}}
    signals = extract_file_content_signals(source, data, context)
    assert len(signals) == 1
    sig = signals[0]
    assert sig["type"] == "file_content"
    assert sig["entity_text"] == "notes.md"
    assert sig["ref_id"] == _path_to_ref_id("/notes.md")
    assert sig["content_preview"] == "Hello world"
    assert sig["metadata"]["path"] == "/notes.md"
    assert sig["metadata"]["watch_dir"] == "/notes"


def test_file_content_extractor_multi_file_pattern():
    source = "filesystem"
    # Testing the multi-file parsing heuristic in a single text block
    data = {"content": [{"type": "text", "text": "/path/file1.md\n---\nContent 1\n/path/file2.md\n---\nContent 2"}]}
    context = {"source_metadata": {"watch_dir": "/path"}}
    signals = extract_file_content_signals(source, data, context)
    assert len(signals) == 2
    assert signals[0]["entity_text"] == "file1.md"
    assert signals[0]["content_preview"] == "Content 1"
    assert signals[1]["entity_text"] == "file2.md"
    assert signals[1]["content_preview"] == "Content 2"


def test_file_content_extractor_skips_binary_type():
    data = {"content": [{"type": "image", "data": "base64..."}]}
    signals = extract_file_content_signals("fs", data, {})
    assert signals == []


def test_file_content_extractor_empty_content():
    data = {"content": []}
    signals = extract_file_content_signals("fs", data, {})
    # Should fall back to generic
    assert len(signals) == 1
    assert signals[0]["type"] == "mcp_result"


def test_file_content_extractor_fallback_on_missing_content_key():
    data = {"result": "some text"}
    signals = extract_file_content_signals("fs", data, {})
    assert len(signals) == 1
    assert signals[0]["needs_llm_extraction"] is True


# SourcePoller tests
@pytest.mark.asyncio
async def test_poll_watch_dirs_calls_mcp_when_due():
    config = {
        "mcp_servers": [{"name": "filesystem", "type": "filesystem"}],
        "watch_dirs": [{"path": "/test/dir", "interval_minutes": 60}],
    }
    mcp_registry = MagicMock()
    client = MagicMock()
    client.call_tool = AsyncMock()
    mcp_registry.get_client.return_value = client

    # list_directory response
    client.call_tool.side_effect = [
        {"content": [{"type": "text", "text": "file1.md\nfile2.txt"}]},  # list_directory
        {"content": [{"type": "text", "text": "file1 content"}]},  # read_multiple_files
    ]

    poller = SourcePoller(config, MagicMock(), mcp_registry)
    now = datetime.utcnow()
    results = await poller._poll_watch_dirs(now)

    assert len(results) == 1
    assert results[0]["extractor"] == "file_content"
    assert client.call_tool.await_count == 2

    # Verify path in first call
    args = client.call_tool.call_args_list[0].args
    assert args[0] == "list_directory"
    assert args[1]["path"].endswith("/test/dir")


@pytest.mark.asyncio
async def test_poll_watch_dirs_skips_when_not_due():
    config = {
        "mcp_servers": [{"name": "fs", "type": "filesystem"}],
        "watch_dirs": [{"path": "/test/dir", "interval_minutes": 60}],
    }
    mcp_registry = MagicMock()
    client = MagicMock()
    client.call_tool = AsyncMock()
    mcp_registry.get_client.return_value = client

    poller = SourcePoller(config, MagicMock(), mcp_registry)
    now = datetime.utcnow()

    resolved_path = os.path.abspath(os.path.expanduser("/test/dir"))
    dir_hash = hashlib.sha256(resolved_path.encode()).hexdigest()[:8]
    poller.last_poll[f"watchdir:{dir_hash}"] = now - timedelta(minutes=30)

    results = await poller._poll_watch_dirs(now)
    assert results == []
    client.call_tool.assert_not_called()


@pytest.mark.asyncio
async def test_poll_watch_dirs_no_server_no_crash():
    config = {"mcp_servers": [], "watch_dirs": [{"path": "/dir"}]}
    poller = SourcePoller(config, MagicMock(), MagicMock())
    results = await poller._poll_watch_dirs(datetime.utcnow())
    assert results == []


@pytest.mark.asyncio
async def test_max_files_clamped_to_20():
    config = {"mcp_servers": [{"name": "fs", "type": "filesystem"}], "watch_dirs": [{"path": "/dir", "max_files": 50}]}
    mcp_registry = MagicMock()
    client = MagicMock()
    client.call_tool = AsyncMock()
    mcp_registry.get_client.return_value = client

    # 30 files in listing
    filenames = "\n".join([f"file{i}.md" for i in range(30)])
    client.call_tool.side_effect = [{"content": [{"type": "text", "text": filenames}]}, {"content": []}]

    poller = SourcePoller(config, MagicMock(), mcp_registry)
    await poller._poll_watch_dirs(datetime.utcnow())

    # Check read_multiple_files call
    call_args = client.call_tool.call_args_list[1]
    assert len(call_args.args[1]["paths"]) == 20


@pytest.mark.asyncio
async def test_poll_watch_dirs_skips_read_on_empty_listing():
    config = {"mcp_servers": [{"name": "fs", "type": "filesystem"}], "watch_dirs": [{"path": "/dir"}]}
    mcp_registry = MagicMock()
    client = MagicMock()
    client.call_tool = AsyncMock()
    mcp_registry.get_client.return_value = client

    client.call_tool.return_value = {"content": []}

    poller = SourcePoller(config, MagicMock(), mcp_registry)
    results = await poller._poll_watch_dirs(datetime.utcnow())

    assert results == []
    assert client.call_tool.await_count == 1  # only list_directory


@pytest.mark.asyncio
async def test_extension_filter_applied():
    config = {
        "mcp_servers": [{"name": "fs", "type": "filesystem"}],
        "watch_dirs": [{"path": "/dir", "extensions": ["md"]}],
    }
    mcp_registry = MagicMock()
    client = MagicMock()
    client.call_tool = AsyncMock()
    mcp_registry.get_client.return_value = client

    client.call_tool.side_effect = [
        {"content": [{"type": "text", "text": "notes.md\nimage.png\ndoc.txt"}]},
        {"content": []},
    ]

    poller = SourcePoller(config, MagicMock(), mcp_registry)
    await poller._poll_watch_dirs(datetime.utcnow())

    # Check read_multiple_files call
    paths = client.call_tool.call_args_list[1].args[1]["paths"]
    assert len(paths) == 1
    assert paths[0].endswith("notes.md")
