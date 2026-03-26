from __future__ import annotations

import unittest
from unittest.mock import MagicMock
from xibi.routing.classifier import MessageModeClassifier, ModeScores
from xibi.routing.shadow import ShadowMatch, extract_tool_input


class TestMessageModeClassifier(unittest.TestCase):
    def setUp(self):
        self.mock_shadow = MagicMock()
        self.clf = MessageModeClassifier(shadow=self.mock_shadow)

    def test_default_prior_no_signals(self):
        # 1. test_default_prior_no_signals — empty query "" → both scores ≈ 0.30, confidence < 0.05
        self.mock_shadow.match.return_value = None
        scores = self.clf.classify("")
        self.assertAlmostEqual(scores.command, 0.30, places=2)
        self.assertAlmostEqual(scores.conversation, 0.30, places=2)
        self.assertLess(scores.confidence, 0.05)

    def test_command_keyword_bumps_command(self):
        # 2. test_command_keyword_bumps_command — query "list emails" → command > conversation
        self.mock_shadow.match.return_value = None
        scores = self.clf.classify("list emails")
        self.assertGreater(scores.command, scores.conversation)
        self.assertEqual(scores.dominant, "command")

    def test_conversation_keyword_bumps_conversation(self):
        # 3. test_conversation_keyword_bumps_conversation — query "what is the weather?" → conversation > command
        self.mock_shadow.match.return_value = None
        scores = self.clf.classify("what is the weather?")
        self.assertGreater(scores.conversation, scores.command)
        self.assertEqual(scores.dominant, "conversation")

    def test_question_mark_heuristic(self):
        # 4. test_question_mark_heuristic — query "are you busy?" → conversation score higher than without ?
        self.mock_shadow.match.return_value = None
        scores_with_q = self.clf.classify("are you busy?")
        scores_without_q = self.clf.classify("are you busy")
        self.assertGreater(scores_with_q.conversation, scores_without_q.conversation)

    def test_shadow_direct_bumps_command_strongly(self):
        # 5. test_shadow_direct_bumps_command_strongly — mock shadow returning ShadowMatch(tier="direct", score=0.90, ...) → command >= 0.70, shadow_hit=True, shadow_tier="direct"
        self.mock_shadow.match.return_value = ShadowMatch(tool="test", skill="test", phrase="test", score=0.90, tier="direct")
        scores = self.clf.classify("test")
        self.assertGreaterEqual(scores.command, 0.70)
        self.assertTrue(scores.shadow_hit)
        self.assertEqual(scores.shadow_tier, "direct")

    def test_shadow_hint_bumps_command_moderately(self):
        # 6. test_shadow_hint_bumps_command_moderately — mock shadow returning ShadowMatch(tier="hint", score=0.70, ...) → command bumped but less than direct; shadow_tier="hint"
        self.mock_shadow.match.return_value = ShadowMatch(tool="test", skill="test", phrase="test", score=0.70, tier="hint")
        scores_hint = self.clf.classify("test")

        self.mock_shadow.match.return_value = ShadowMatch(tool="test", skill="test", phrase="test", score=0.90, tier="direct")
        scores_direct = self.clf.classify("test")

        self.assertLess(scores_hint.command, scores_direct.command)
        self.assertEqual(scores_hint.shadow_tier, "hint")

    def test_shadow_none_no_bump(self):
        # 7. test_shadow_none_no_bump — mock shadow returning ShadowMatch(tier="none", score=0.40, ...) → shadow_hit=False
        # Actually ShadowMatcher.match returns None for tier="none" according to implementation
        self.mock_shadow.match.return_value = None
        scores = self.clf.classify("test")
        self.assertFalse(scores.shadow_hit)
        self.assertEqual(scores.shadow_tier, "none")

    def test_dominant_command(self):
        # 8. test_dominant_command — query with strong command signal → dominant == "command"
        self.mock_shadow.match.return_value = ShadowMatch(tool="list", skill="test", phrase="list", score=0.95, tier="direct")
        scores = self.clf.classify("list my items")
        self.assertEqual(scores.dominant, "command")

    def test_dominant_conversation(self):
        # 9. test_dominant_conversation — query "tell me about yourself" → dominant == "conversation"
        self.mock_shadow.match.return_value = None
        scores = self.clf.classify("tell me about yourself")
        self.assertEqual(scores.dominant, "conversation")

    def test_confidence_monotone(self):
        # 10. test_confidence_monotone — high BM25 + command keyword → confidence > 0.50
        self.mock_shadow.match.return_value = ShadowMatch(tool="list", skill="test", phrase="list", score=0.95, tier="direct")
        scores = self.clf.classify("list all files")
        # command: 0.3 (prior) + 0.15 (list keyword) + 0.4 (direct shadow) = 0.85
        # conversation: 0.3 (prior)
        # confidence: 0.85 - 0.3 = 0.55
        self.assertGreater(scores.confidence, 0.50)

    def test_scores_clamped(self):
        # 11. test_scores_clamped — no score exceeds 1.0 even with many command keywords
        self.mock_shadow.match.return_value = ShadowMatch(tool="list", skill="test", phrase="list", score=0.95, tier="direct")
        # command keywords: list, show, find, search, get, fetch
        scores = self.clf.classify("list show find search get fetch everything now")
        self.assertLessEqual(scores.command, 1.0)
        self.assertLessEqual(scores.conversation, 1.0)

    def test_classify_bulk(self):
        # 12. test_classify_bulk — list of 3 queries → returns list of 3 ModeScores, correct length
        queries = ["list", "hello", "what is"]
        results = self.clf.classify_bulk(queries)
        self.assertEqual(len(results), 3)
        for res in results:
            self.assertIsInstance(res, ModeScores)

    def test_caller_provided_shadow_match_used(self):
        # 13. test_caller_provided_shadow_match_used — pass explicit shadow_match param; verify no call to self.shadow.query()
        match = ShadowMatch(tool="test", skill="test", phrase="test", score=0.90, tier="direct")
        self.clf.classify("query", shadow_match=match)
        self.mock_shadow.match.assert_not_called()

    def test_no_shadow_no_error(self):
        # 14. test_no_shadow_no_error — MessageModeClassifier(shadow=None) works without error on any query
        clf_no_shadow = MessageModeClassifier(shadow=None)
        scores = clf_no_shadow.classify("list things")
        self.assertIsInstance(scores, ModeScores)

    def test_modeScores_fields(self):
        # 15. test_modeScores_fields — check all fields (command, conversation, dominant, confidence, shadow_hit, shadow_tier) are present and correctly typed
        scores = self.clf.classify("test")
        self.assertIsInstance(scores.command, float)
        self.assertIsInstance(scores.conversation, float)
        self.assertIsInstance(scores.dominant, str)
        self.assertIsInstance(scores.confidence, float)
        self.assertIsInstance(scores.shadow_hit, bool)
        self.assertIsInstance(scores.shadow_tier, str)

    def test_extract_tool_input_basic(self):
        # 16. test_extract_tool_input_basic — query="list my unread emails", phrase="list unread emails" → result contains non-empty "input" key
        query = "list my unread emails"
        match = ShadowMatch(tool="list_emails", skill="email", phrase="list unread emails", score=0.90, tier="direct")
        result = extract_tool_input(query, match)
        self.assertIn("input", result)
        self.assertEqual(result["input"], "my")

    def test_extract_tool_input_empty_remainder(self):
        # 17. test_extract_tool_input_empty_remainder — query and phrase are identical → returns {}
        query = "list unread emails"
        match = ShadowMatch(tool="list_emails", skill="email", phrase="list unread emails", score=0.90, tier="direct")
        result = extract_tool_input(query, match)
        self.assertEqual(result, {})

    def test_extract_tool_input_preserves_extra_words(self):
        # 18. test_extract_tool_input_preserves_extra_words — query has significant remainder after phrase words removed → "input" value is non-empty
        query = "send email to bob about meeting"
        match = ShadowMatch(tool="send_email", skill="email", phrase="send email", score=0.90, tier="direct")
        result = extract_tool_input(query, match)
        self.assertEqual(result["input"], "to bob about meeting")

if __name__ == "__main__":
    unittest.main()
