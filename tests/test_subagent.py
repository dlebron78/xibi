from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from xibi.db.migrations import migrate
from xibi.subagent.cost import get_agent_total_cost, get_rolling_total, get_run_cost
from xibi.subagent.db import get_run, get_steps
from xibi.subagent.runtime import cancel_subagent, resume_run, spawn_subagent

TEST_CHECKLIST = [
    {"skill_name": "step1", "model": "haiku", "trust": "L1", "prompt": "prompt1"},
    {"skill_name": "step2", "model": "haiku", "trust": "L2", "prompt": "prompt2"},
]


class TestSubagent(unittest.TestCase):
    def setUp(self):
        self.db_path = Path("test_subagent.db")
        if self.db_path.exists():
            self.db_path.unlink()
        migrate(self.db_path)
        # Mock config for routing
        self.config_patcher = patch(
            "xibi.subagent.routing.load_config",
            return_value={
                "subagent_models": {"haiku": {"provider": "anthropic", "model_id": "claude-3-haiku-20240307"}},
                "subagent_pricing": {"claude-3-haiku-20240307": {"input_per_mtok": 0.25, "output_per_mtok": 1.25}},
            },
        )
        self.config_patcher.start()

    def tearDown(self):
        self.config_patcher.stop()
        if self.db_path.exists():
            self.db_path.unlink()

    @patch("xibi.subagent.routing.AnthropicClient")
    def test_full_lifecycle(self, mock_anthropic):
        # Mock client
        mock_client = MagicMock()
        mock_anthropic.return_value = mock_client
        mock_client.generate.side_effect = [
            '{"status": "ok", "actions": []}',
            '{"status": "ok", "actions": [{"tool": "red_tool", "args": {}}]}',
        ]
        mock_client._last_tokens = (100, 50, 500)

        # 1. Spawn
        run = spawn_subagent(
            agent_id="test-agent",
            trigger="manual",
            trigger_context={},
            scoped_input={"foo": "bar"},
            checklist=TEST_CHECKLIST,
            budget={"max_calls": 5, "max_cost_usd": 0.1, "max_duration_s": 60},
            db_path=self.db_path,
        )

        self.assertEqual(run.status, "DONE")
        self.assertEqual(run.actual_calls, 2)

        # 2. Check DB
        steps = get_steps(self.db_path, run.id)
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0].status, "DONE")
        self.assertEqual(steps[1].status, "DONE")

        # 3. Check trust enforcement (Step 2 was L2)
        self.assertIn("parked_actions", steps[1].output_data)

        # 4. Check cost helpers
        cost = get_run_cost(self.db_path, run.id)
        self.assertGreater(cost, 0)

        total_agent_cost = get_agent_total_cost(self.db_path, "test-agent")
        self.assertEqual(total_agent_cost, cost)

        rolling = get_rolling_total(self.db_path)
        self.assertEqual(rolling, cost)

    def test_cancellation(self):
        # We need to mock execute_checklist to test cancellation polling or
        # use a real loop that we can interrupt.
        # Simplest is to test cancel_subagent helper.
        with patch("xibi.subagent.runtime.execute_checklist", side_effect=lambda run, db_path, checklist: run):
            run = spawn_subagent("agent", "manual", {}, {}, TEST_CHECKLIST, {}, self.db_path)

        cancel_subagent(run.id, self.db_path, reason="Killed")

        db_run = get_run(self.db_path, run.id)
        self.assertIsNotNone(db_run)
        self.assertEqual(db_run.status, "CANCELLED")
        self.assertEqual(db_run.cancelled_reason, "Killed")

    @patch("xibi.subagent.routing.AnthropicClient")
    def test_resume(self, mock_anthropic):
        mock_client = MagicMock()
        mock_anthropic.return_value = mock_client

        # First attempt fails at step 2
        mock_client.generate.side_effect = ['{"step": 1}', RuntimeError("LLM exploded")]
        mock_client._last_tokens = (10, 10, 100)

        run = spawn_subagent("resumable", "manual", {}, {}, TEST_CHECKLIST, {}, self.db_path)
        self.assertEqual(run.status, "FAILED")

        steps = get_steps(self.db_path, run.id)
        self.assertEqual(steps[0].status, "DONE")
        self.assertEqual(steps[1].status, "FAILED")

        # Resume
        mock_client.generate.side_effect = ['{"step": 2}']
        resumed_run = resume_run(run.id, self.db_path, TEST_CHECKLIST)

        self.assertEqual(resumed_run.status, "DONE")
        steps = get_steps(self.db_path, run.id)
        self.assertEqual(steps[1].status, "DONE")

    @patch("xibi.subagent.routing.AnthropicClient")
    @patch("time.sleep", return_value=None)  # Fast-forward retries
    def test_retry_logic(self, mock_sleep, mock_anthropic):
        mock_client = MagicMock()
        mock_anthropic.return_value = mock_client
        mock_client._last_tokens = (10, 10, 100)

        # 1. Test recovery on 3rd attempt
        mock_client.generate.side_effect = [
            RuntimeError("Transient error 1"),
            RuntimeError("Transient error 2"),
            '{"status": "ok"}',
        ]

        # Use a single-step checklist to isolate retry logic
        run = spawn_subagent(
            "retry-success",
            "manual",
            {},
            {},
            [{"skill_name": "retry_me", "model": "haiku", "trust": "L1"}],
            {"max_calls": 5, "max_cost_usd": 0.1, "max_duration_s": 60},
            self.db_path,
        )

        self.assertEqual(run.status, "DONE")
        self.assertEqual(mock_client.generate.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)

        # 2. Test exhaustion after 3 attempts
        mock_client.generate.reset_mock()
        mock_sleep.reset_mock()
        mock_client.generate.side_effect = [
            RuntimeError("Persistent error 1"),
            RuntimeError("Persistent error 2"),
            RuntimeError("Persistent error 3"),
        ]

        run_fail = spawn_subagent(
            "retry-fail",
            "manual",
            {},
            {},
            [{"skill_name": "fail_me", "model": "haiku", "trust": "L1"}],
            {"max_calls": 5, "max_cost_usd": 0.1, "max_duration_s": 60},
            self.db_path,
        )

        self.assertEqual(run_fail.status, "FAILED")
        self.assertEqual(mock_client.generate.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)  # Slept after 1st and 2nd, failed after 3rd


if __name__ == "__main__":
    unittest.main()
