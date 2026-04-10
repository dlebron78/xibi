import pytest
from unittest.mock import patch, MagicMock
from xibi.checklists import tools

def test_list_checklists():
    with patch("xibi.checklists.api.list_checklists", return_value=[]) as mock_api:
        res = tools.list_checklists({"_db_path": "db.sqlite"})
        assert res == []
        mock_api.assert_called_once_with("db.sqlite")

def test_get_checklist_success():
    with patch("xibi.checklists.api.get_checklist", return_value={"id": "inst1"}) as mock_api:
        res = tools.get_checklist({"_db_path": "db.sqlite", "instance_id": "inst1"})
        assert res["id"] == "inst1"
        mock_api.assert_called_once_with("db.sqlite", "inst1")

def test_get_checklist_no_id():
    res = tools.get_checklist({"_db_path": "db.sqlite"})
    assert res["status"] == "error"
    assert "instance_id is required" in res["error"]

def test_update_checklist_item_success():
    with patch("xibi.checklists.api.update_checklist_item", return_value={"status": "ok"}) as mock_api:
        res = tools.update_checklist_item({"_db_path": "db.sqlite", "instance_id": "inst1", "label_hint": "buy milk"})
        assert res["status"] == "ok"
        mock_api.assert_called_once()

def test_update_checklist_item_no_id():
    res = tools.update_checklist_item({"_db_path": "db.sqlite"})
    assert res["status"] == "error"

def test_update_checklist_item_error():
    with patch("xibi.checklists.api.update_checklist_item", side_effect=ValueError("bad input")):
        res = tools.update_checklist_item({"_db_path": "db.sqlite", "instance_id": "inst1"})
        assert res["status"] == "error"
        assert "bad input" in res["error"]

def test_create_checklist_template_success():
    with patch("xibi.checklists.api.create_checklist_template", return_value={"id": "tpl1"}) as mock_api:
        res = tools.create_checklist_template({
            "_db_path": "db.sqlite",
            "name": "Daily",
            "items": ["a", "b"]
        })
        assert res["id"] == "tpl1"
        mock_api.assert_called_once()

def test_create_checklist_template_missing_name():
    res = tools.create_checklist_template({"_db_path": "db.sqlite", "items": ["a"]})
    assert res["status"] == "error"
    assert "name is required" in res["error"]

def test_create_checklist_template_missing_items():
    res = tools.create_checklist_template({"_db_path": "db.sqlite", "name": "Daily"})
    assert res["status"] == "error"
    assert "items list is required" in res["error"]
