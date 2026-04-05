import unittest
from xibi.heartbeat.extractors import SignalExtractorRegistry


class TestExtractors(unittest.TestCase):
    def test_email_extractor(self):
        data = [{"id": "1", "from": "dan@example.com", "subject": "Test"}]
        signals = SignalExtractorRegistry.extract("email", "email", data, {})
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["entity_text"], "dan@example.com")

    def test_calendar_extractor(self):
        data = {"events": [{"id": "c1", "summary": "Meeting", "start": "2026-03-31T10:00:00Z"}]}
        signals = SignalExtractorRegistry.extract("calendar", "calendar", data, {})
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["topic_hint"], "Meeting")

    def test_generic_extractor(self):
        data = {"result": "Raw text", "structured": {"foo": "bar"}}
        signals = SignalExtractorRegistry.extract("generic", "slack", data, {})
        self.assertEqual(len(signals), 1)
        self.assertTrue(signals[0]["needs_llm_extraction"])
