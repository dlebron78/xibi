from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from xibi.channels.telegram import TelegramAdapter
from xibi.db.migrations import migrate
from xibi.react import run as react_run
from xibi.routing.llm_classifier import LLMRoutingDecision
from xibi.routing.shadow import ShadowMatch


class TestReActRouting(unittest.TestCase):
    def setUp(self):
        self.config = {"db_path": ":memory:"}
        self.skill_registry = [{"skill": "email", "tools": [{"name": "list_emails"}]}]
        self.mock_llm = MagicMock()
        self.mock_llm.generate.return_value = '{"thought": "done", "tool": "finish", "tool_input": {"answer": "ok"}}'

    @patch("xibi.react.get_model")
    def test_llm_classifier_called_when_shadow_returns_none(self, mock_get_model):
        mock_get_model.return_value = self.mock_llm
        mock_shadow = MagicMock()
        mock_shadow.match.return_value = None
        mock_classifier = MagicMock()
        mock_classifier.classify.return_value = None

        react_run(
            "test query",
            self.config,
            self.skill_registry,
            shadow=mock_shadow,
            llm_routing_classifier=mock_classifier,
        )

        mock_classifier.classify.assert_called_once_with("test query", self.skill_registry)

    @patch("xibi.react.get_model")
    def test_llm_classifier_hint_injected_into_context(self, mock_get_model):
        mock_get_model.return_value = self.mock_llm
        mock_shadow = MagicMock()
        mock_shadow.match.return_value = None
        mock_classifier = MagicMock()
        mock_classifier.classify.return_value = LLMRoutingDecision(
            skill="email", tool="list_emails", confidence=0.85, reasoning="test"
        )

        react_run(
            "test query",
            self.config,
            self.skill_registry,
            shadow=mock_shadow,
            llm_routing_classifier=mock_classifier,
        )

        # Check if the hint was injected into the context passed to llm.generate
        # The first call to generate should have the hint in the prompt
        args, kwargs = self.mock_llm.generate.call_args
        prompt = args[0]
        self.assertIn("[Routing hint: consider using email/list_emails (confidence=0.85)]", prompt)

    @patch("xibi.react.get_model")
    def test_llm_classifier_not_called_when_shadow_direct(self, mock_get_model):
        mock_get_model.return_value = self.mock_llm
        mock_shadow = MagicMock()
        mock_shadow.match.return_value = ShadowMatch(
            tier="direct",
            tool="list_emails",
            skill="email",
            phrase="list my emails",
            tool_input={},
            score=0.9,
        )
        mock_classifier = MagicMock()

        react_run(
            "test query",
            self.config,
            self.skill_registry,
            shadow=mock_shadow,
            llm_routing_classifier=mock_classifier,
        )

        mock_classifier.classify.assert_not_called()

    @patch("xibi.react.get_model")
    def test_llm_classifier_not_called_when_shadow_hint(self, mock_get_model):
        mock_get_model.return_value = self.mock_llm
        mock_shadow = MagicMock()
        mock_shadow.match.return_value = ShadowMatch(
            tier="hint",
            tool="list_emails",
            skill="email",
            phrase="list my emails",
            tool_input={},
            score=0.72,
        )
        mock_classifier = MagicMock()

        react_run(
            "test query",
            self.config,
            self.skill_registry,
            shadow=mock_shadow,
            llm_routing_classifier=mock_classifier,
        )

        mock_classifier.classify.assert_not_called()

    @patch("xibi.react.get_model")
    def test_llm_classifier_not_called_when_none(self, mock_get_model):
        mock_get_model.return_value = self.mock_llm
        mock_shadow = MagicMock()
        mock_shadow.match.return_value = None

        # Should not raise AttributeError when llm_routing_classifier is None
        react_run(
            "test query",
            self.config,
            self.skill_registry,
            shadow=mock_shadow,
            llm_routing_classifier=None,
        )

    @patch("xibi.react.get_model")
    def test_llm_classifier_exception_does_not_break_react(self, mock_get_model):
        mock_get_model.return_value = self.mock_llm
        mock_shadow = MagicMock()
        mock_shadow.match.return_value = None
        mock_classifier = MagicMock()
        mock_classifier.classify.side_effect = RuntimeError("test error")

        result = react_run(
            "test query",
            self.config,
            self.skill_registry,
            shadow=mock_shadow,
            llm_routing_classifier=mock_classifier,
        )

        self.assertEqual(result.exit_reason, "finish")
        self.assertEqual(result.answer, "ok")

    @patch("xibi.react.get_model")
    def test_llm_classifier_returns_none_no_hint_injected(self, mock_get_model):
        mock_get_model.return_value = self.mock_llm
        mock_shadow = MagicMock()
        mock_shadow.match.return_value = None
        mock_classifier = MagicMock()
        mock_classifier.classify.return_value = None

        react_run(
            "test query",
            self.config,
            self.skill_registry,
            shadow=mock_shadow,
            llm_routing_classifier=mock_classifier,
        )

        args, kwargs = self.mock_llm.generate.call_args
        prompt = args[0]
        self.assertNotIn("[Routing hint:", prompt)

    @patch("xibi.channels.telegram.react_run")
    def test_telegram_adapter_forwards_classifier(self, mock_react_run):
        mock_classifier = MagicMock()
        mock_registry = MagicMock()
        mock_registry.get_skill_manifests.return_value = self.skill_registry

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test_routing.db"
            migrate(db_path)

            with patch.dict(
                "os.environ",
                {
                    "XIBI_TELEGRAM_TOKEN": "test_token",
                    "XIBI_TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "XIBI_SYNC_SESSION": "1",
                },
            ):
                adapter = TelegramAdapter(
                    config=self.config,
                    skill_registry=mock_registry,
                    llm_routing_classifier=mock_classifier,
                    db_path=db_path,
                )

                # Mock _api_call to avoid real Telegram calls
                adapter._api_call = MagicMock(return_value={"ok": True})

                # Trigger _handle_text
                adapter._handle_text(123, "test query")

                # Check if react_run was called with the classifier
                mock_react_run.assert_called()
                args, kwargs = mock_react_run.call_args
                self.assertEqual(kwargs.get("llm_routing_classifier"), mock_classifier)
