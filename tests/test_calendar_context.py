import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
from xibi.heartbeat.calendar_context import (
    fetch_upcoming_events,
    tag_event,
    detect_sender_overlap,
    build_next_event_summary,
)


class TestCalendarContext(unittest.TestCase):
    @patch("xibi.heartbeat.calendar_context.gcal_request")
    @patch("xibi.heartbeat.calendar_context.load_calendar_config")
    def test_fetch_upcoming_events_success(self, mock_load_config, mock_gcal_request):
        mock_load_config.return_value = [{"label": "personal", "calendar_id": "primary"}]
        mock_gcal_request.return_value = {
            "items": [
                {
                    "id": "event1",
                    "summary": "Meeting",
                    "start": {"dateTime": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()},
                    "end": {"dateTime": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
                    "attendees": [{"email": "other@example.com", "displayName": "Other"}],
                    "location": "Office",
                    "conferenceData": {"entryPoints": [{"uri": "https://zoom.us/j/123"}]},
                }
            ]
        }

        events = fetch_upcoming_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["title"], "Meeting")
        self.assertEqual(events[0]["location"], "Office")
        self.assertEqual(events[0]["conference_url"], "https://zoom.us/j/123")
        self.assertEqual(len(events[0]["attendees"]), 1)
        self.assertEqual(events[0]["attendees"][0]["email"], "other@example.com")
        self.assertEqual(events[0]["event_tags"], ["meeting"])

    @patch("xibi.heartbeat.calendar_context.gcal_request")
    @patch("xibi.heartbeat.calendar_context.load_calendar_config")
    def test_fetch_upcoming_events_empty(self, mock_load_config, mock_gcal_request):
        mock_load_config.return_value = [{"label": "personal", "calendar_id": "primary"}]
        mock_gcal_request.return_value = {"items": []}
        events = fetch_upcoming_events()
        self.assertEqual(events, [])

    @patch("xibi.heartbeat.calendar_context.gcal_request")
    @patch("xibi.heartbeat.calendar_context.load_calendar_config")
    def test_fetch_upcoming_events_dedup(self, mock_load_config, mock_gcal_request):
        mock_load_config.return_value = [
            {"label": "personal", "calendar_id": "p1"},
            {"label": "work", "calendar_id": "w1"},
        ]
        mock_gcal_request.return_value = {
            "items": [
                {
                    "id": "shared_event",
                    "summary": "Shared",
                    "start": {"dateTime": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()},
                    "end": {"dateTime": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
                }
            ]
        }
        events = fetch_upcoming_events()
        self.assertEqual(len(events), 1)

    @patch("xibi.heartbeat.calendar_context.gcal_request")
    @patch("xibi.heartbeat.calendar_context.load_calendar_config")
    def test_fetch_upcoming_events_gcal_error(self, mock_load_config, mock_gcal_request):
        mock_load_config.return_value = [{"label": "personal", "calendar_id": "primary"}]
        mock_gcal_request.side_effect = Exception("API Error")
        events = fetch_upcoming_events()
        self.assertEqual(events, [])

    def test_tag_event_flight(self):
        tags = tag_event("Flight AA1234 to NYC", None, False)
        self.assertIn("flight", tags)
        self.assertIn("travel", tags)

    def test_tag_event_reservation(self):
        tags = tag_event("Dinner at Casita Miramar", None, False)
        self.assertIn("reservation", tags)
        self.assertIn("dining", tags)

    def test_tag_event_birthday(self):
        tags = tag_event("Mom's Birthday", None, False)
        self.assertIn("birthday", tags)

    def test_tag_event_meeting(self):
        tags = tag_event("1:1 with Sarah", None, True)
        self.assertIn("meeting", tags)

    def test_tag_event_fallback_attendees(self):
        tags = tag_event("Focus time", None, True)
        self.assertEqual(tags, ["meeting"])

    def test_tag_event_fallback_no_attendees(self):
        tags = tag_event("Focus time", None, False)
        self.assertEqual(tags, ["event"])

    def test_detect_sender_overlap_match(self):
        events = [
            {"title": "Meeting", "attendees": [{"email": "sarah@example.com", "name": "Sarah"}], "minutes_until": 30}
        ]
        overlap = detect_sender_overlap(events, "Sarah@example.com")
        self.assertIsNotNone(overlap)
        self.assertEqual(overlap["title"], "Meeting")

    def test_detect_sender_overlap_no_match(self):
        events = [
            {"title": "Meeting", "attendees": [{"email": "sarah@example.com", "name": "Sarah"}], "minutes_until": 30}
        ]
        overlap = detect_sender_overlap(events, "other@example.com")
        self.assertIsNone(overlap)

    def test_build_next_event_summary_meeting(self):
        events = [{"title": "1:1 with Sarah", "minutes_until": 45, "conference_url": "https://zoom.us/j/123"}]
        summary = build_next_event_summary(events)
        self.assertEqual(summary, "1:1 with Sarah in 45min (Zoom)")

    def test_build_next_event_summary_allday(self):
        events = [
            {
                "title": "Mom's Birthday",
                "minutes_until": None,
            }
        ]
        summary = build_next_event_summary(events)
        self.assertEqual(summary, "Mom's Birthday (all day)")


if __name__ == "__main__":
    unittest.main()
