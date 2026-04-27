from unittest.mock import ANY, patch

from skills.calendar.tools.add_event import run


@patch("skills.calendar.tools.add_event.gcal_request")
@patch("skills.calendar.tools.add_event.load_calendar_config")
def test_add_event_default_calendar_picks_first(mock_load, mock_gcal):
    """No calendar_id and no XIBI_DEFAULT_CALENDAR_LABEL → first configured."""
    mock_load.return_value = [{"label": "personal", "account": "default", "calendar_id": "dan@example.com"}]
    mock_gcal.return_value = {"id": "new_evt"}

    res = run({"title": "New Meeting", "start_datetime": "tomorrow_1000"})
    assert res["status"] == "success"
    assert res["label"] == "personal"
    assert res["account"] == "default"
    mock_gcal.assert_called_with("/calendars/dan@example.com/events", method="POST", body=ANY, account="default")


@patch("skills.calendar.tools.add_event.gcal_request")
@patch("skills.calendar.tools.add_event.load_calendar_config")
def test_add_event_label_resolves_to_correct_account(mock_load, mock_gcal):
    """Explicit calendar_id matching a label → routed to that account."""
    mock_load.return_value = [
        {"label": "personal", "account": "default", "calendar_id": "dan@example.com"},
        {"label": "afya", "account": "afya", "calendar_id": "primary"},
    ]
    mock_gcal.return_value = {"id": "evt"}

    res = run({"title": "Standup", "start_datetime": "today_1400", "calendar_id": "afya"})
    assert res["status"] == "success"
    assert res["account"] == "afya"
    mock_gcal.assert_called_with("/calendars/primary/events", method="POST", body=ANY, account="afya")


@patch("skills.calendar.tools.add_event.gcal_request")
@patch("skills.calendar.tools.add_event.load_calendar_config")
def test_add_event_unknown_label_returns_ambiguous_error(mock_load, mock_gcal):
    mock_load.return_value = [{"label": "personal", "account": "default", "calendar_id": "primary"}]
    res = run({"title": "x", "start_datetime": "today_1500", "calendar_id": "marsbase"})
    assert res["status"] == "error"
    assert res["error_category"] == "ambiguous_calendar"
    assert "personal" in res["available_labels"]
    mock_gcal.assert_not_called()


@patch("skills.calendar.tools.add_event.gcal_request")
@patch("skills.calendar.tools.add_event.load_calendar_config")
def test_add_event_default_label_routes_to_xibi_default_calendar_label_env(mock_load, mock_gcal, monkeypatch):
    monkeypatch.setenv("XIBI_DEFAULT_CALENDAR_LABEL", "personal")
    mock_load.return_value = [
        {"label": "afya", "account": "afya", "calendar_id": "primary"},
        {"label": "personal", "account": "default", "calendar_id": "me@me.com"},
    ]
    mock_gcal.return_value = {"id": "evt"}

    res = run({"title": "x", "start_datetime": "today_1500"})
    assert res["status"] == "success"
    assert res["label"] == "personal"
    assert res["account"] == "default"


@patch("skills.calendar.tools.add_event.gcal_request")
@patch("skills.calendar.tools.add_event.load_calendar_config")
def test_add_event_default_label_unknown_warns_and_falls_back(mock_load, mock_gcal, monkeypatch, caplog):
    monkeypatch.setenv("XIBI_DEFAULT_CALENDAR_LABEL", "moonbase")
    mock_load.return_value = [
        {"label": "afya", "account": "afya", "calendar_id": "primary"},
    ]
    mock_gcal.return_value = {"id": "evt"}

    import logging

    with caplog.at_level(logging.WARNING):
        res = run({"title": "x", "start_datetime": "today_1500"})
    assert res["status"] == "success"
    assert "xibi_default_calendar_label_unknown" in caplog.text
    assert "label=moonbase" in caplog.text
    assert "falling_back_to=afya" in caplog.text
