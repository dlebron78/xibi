import pytest
import sys
import os
import json
from pathlib import Path
from xibi.handles import HandleStore
from xibi.react import dispatch

# Add skills/data/tools to sys.path so we can import transform_data
# Actually dispatch will handle it if we provide a registry.
# But for unit testing the run function directly:
import xibi.skills.sample.data.tools.transform_data as transform_data

def test_filter_then_sort_then_slice():
    payload = {
        "jobs": [
            {"title": "Dev", "salary": 100000, "location": "NY"},
            {"title": "Dev", "salary": 120000, "location": "Remote"},
            {"title": "Manager", "salary": 150000, "location": "Remote"},
            {"title": "Dev", "salary": 90000, "location": "Remote"},
        ]
    }

    params = {
        "handle": payload,
        "operations": [
            {"op": "filter", "args": {"field": "location", "op": "==", "value": "Remote"}},
            {"op": "sort", "args": {"field": "salary", "order": "desc"}},
            {"op": "slice", "args": {"start": 0, "end": 2}}
        ]
    }

    result = transform_data.run(params)
    assert len(result["jobs"]) == 2
    assert result["jobs"][0]["salary"] == 150000
    assert result["jobs"][1]["salary"] == 120000
    assert result["jobs"][0]["title"] == "Manager"

def test_group_by_returns_dict_of_lists():
    payload = [
        {"name": "Alice", "team": "A"},
        {"name": "Bob", "team": "B"},
        {"name": "Charlie", "team": "A"},
    ]
    params = {
        "handle": payload,
        "operations": [
            {"op": "group_by", "args": {"field": "team"}}
        ]
    }
    result = transform_data.run(params)
    assert isinstance(result, dict)
    assert len(result["A"]) == 2
    assert len(result["B"]) == 1
    assert result["A"][0]["name"] == "Alice"
    assert result["A"][1]["name"] == "Charlie"

def test_dedupe():
    payload = [
        {"id": 1, "val": "x"},
        {"id": 2, "val": "y"},
        {"id": 1, "val": "z"},
    ]
    params = {
        "handle": payload,
        "operations": [
            {"op": "dedupe", "args": {"field": "id"}}
        ]
    }
    result = transform_data.run(params)
    assert len(result) == 2
    assert result[0]["id"] == 1
    assert result[0]["val"] == "x"
    assert result[1]["id"] == 2

def test_project():
    payload = [{"a": 1, "b": 2, "c": 3}]
    params = {
        "handle": payload,
        "operations": [
            {"op": "project", "args": {"fields": ["a", "c"]}}
        ]
    }
    result = transform_data.run(params)
    assert result == [{"a": 1, "c": 3}]

def test_transformation_error():
    payload = [{"a": 1}]
    params = {
        "handle": payload,
        "operations": [
            {"op": "filter", "args": {"field": "nonexistent", "op": ">", "value": 10}}
        ]
    }
    result = transform_data.run(params)
    assert result["status"] == "error"
    assert "Transformation error" in result["message"]

import skills.filesystem.tools.write_file as write_file

def test_write_file_resolves_handle(tmp_path):
    payload = {"a": 1, "b": 2}
    filepath = str(tmp_path / "test.json")

    # Simulate resolved handle
    params = {
        "filepath": filepath,
        "handle": payload,
        "_workdir": str(tmp_path)
    }

    result = write_file.run(params)
    assert result["status"] == "success"

    with open(filepath, "r") as f:
        written = json.load(f)
    assert written == payload
