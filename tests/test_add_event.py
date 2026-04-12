from unittest.mock import ANY, patch

from skills.calendar.tools.add_event import run


@patch("skills.calendar.tools.add_event.gcal_request")
@patch("skills.calendar.tools.add_event.load_calendar_config")
@patch("skills.calendar.tools.add_event.resolve_calendar_id")
def test_add_event_default_calendar(mock_resolve, mock_load, mock_gcal):
    mock_load.return_value = [{"label": "personal", "calendar_id": "dan@example.com"}]
    mock_resolve.return_value = "dan@example.com"
    mock_gcal.return_value = {"id": "new_evt"}

    params = {
        "title": "New Meeting",
        "start_datetime": "tomorrow_1000",
    }

    res = run(params)
    assert res["status"] == "success"
    mock_gcal.assert_called_with("/calendars/dan@example.com/events", method="POST", body=ANY)


@patch("skills.calendar.tools.add_event.gcal_request")
@patch("skills.calendar.tools.add_event.resolve_calendar_id")
def test_add_event_label_resolution(mock_resolve, mock_gcal):
    mock_resolve.return_value = "work@example.com"
    mock_gcal.return_value = {"id": "new_evt"}

    params = {"title": "Work sync", "start_datetime": "today_1400", "calendar_id": "afya"}

    res = run(params)
    assert res["status"] == "success"
    mock_resolve.assert_called_with("afya")
    mock_gcal.assert_called_with("/calendars/work@example.com/events", method="POST", body=ANY)


@patch("skills.calendar.tools.add_event.gcal_request")
def test_add_event_unknown_alias(mock_gcal):
    # This tests the fallback in resolve_calendar_id indirectly
    mock_gcal.return_value = {"id": "evt"}

    params = {"title": "Some event", "start_datetime": "2026-06-01T12:00:00Z", "calendar_id": "random_id"}

    # We need to mock load_calendar_config too because resolve_calendar_id uses it
    with patch("skills.calendar.tools.add_event.load_calendar_config") as mock_load:
        mock_load.return_value = [{"label": "default", "calendar_id": "primary"}]
        res = run(params)

    assert res["status"] == "success"
    # It should pass random_id through
    mock_gcal.assert_called_with("/calendars/random_id/events", method="POST", body=ANY)
