from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from xibi.db.migrations import migrate
from xibi.subagent.cost import get_agent_total_cost, get_rolling_total, get_run_cost
from xibi.subagent.db import get_run, get_steps
from xibi.subagent.routing import RoutedResponse
from xibi.subagent.runtime import cancel_subagent, resume_run, spawn_subagent

TEST_CHECKLIST = [
    {"skill_name": "step1", "model": "haiku", "trust": "L1", "prompt": "prompt1"},
    {"skill_name": "step2", "model": "haiku", "trust": "L2", "prompt": "prompt2"},
]


def _mock_response(content: str, cost: float = 0.001) -> RoutedResponse:
    return RoutedResponse(
        content=content,
        model_id="test-model",
        input_tokens=100,
        output_tokens=50,
        cost_usd=cost,
    )


class TestSubagent(unittest.TestCase):
    def setUp(self):
        self.db_path = Path("test_subagent.db")
        if self.db_path.exists():
            self.db_path.unlink()
        migrate(self.db_path)

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()

    @patch("xibi.subagent.checklist.ModelRouter.call")
    def test_full_lifecycle(self, mock_call):
        mock_call.side_effect = [
            _mock_response('{"status": "ok", "actions": []}'),
            _mock_response('{"status": "ok", "actions": [{"tool": "red_tool", "args": {}}]}'),
        ]

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

        steps = get_steps(self.db_path, run.id)
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0].status, "DONE")
        self.assertEqual(steps[1].status, "DONE")

        # Step 2 was L2 — trust enforcement parks its actions
        self.assertIn("parked_actions", steps[1].output_data)

        cost = get_run_cost(self.db_path, run.id)
        self.assertGreater(cost, 0)

        total_agent_cost = get_agent_total_cost(self.db_path, "test-agent")
        self.assertEqual(total_agent_cost, cost)

        rolling = get_rolling_total(self.db_path)
        self.assertEqual(rolling, cost)

    def test_cancellation(self):
        with patch("xibi.subagent.runtime.execute_checklist", side_effect=lambda run, db_path, checklist: run):
            run = spawn_subagent("agent", "manual", {}, {}, TEST_CHECKLIST, {}, self.db_path)

        cancel_subagent(run.id, self.db_path, reason="Killed")

        db_run = get_run(self.db_path, run.id)
        self.assertIsNotNone(db_run)
        self.assertEqual(db_run.status, "CANCELLED")
        self.assertEqual(db_run.cancelled_reason, "Killed")

    @patch("xibi.subagent.checklist.ModelRouter.call")
    def test_resume(self, mock_call):
        # First attempt: step1 succeeds, step2 fails
        mock_call.side_effect = [
            _mock_response('{"step": 1}'),
            RuntimeError("LLM exploded"),
        ]

        run = spawn_subagent("resumable", "manual", {}, {}, TEST_CHECKLIST, {}, self.db_path)
        self.assertEqual(run.status, "FAILED")

        steps = get_steps(self.db_path, run.id)
        self.assertEqual(steps[0].status, "DONE")
        self.assertEqual(steps[1].status, "FAILED")

        # Resume: step2 now succeeds
        mock_call.side_effect = [_mock_response('{"step": 2}')]
        resumed_run = resume_run(run.id, self.db_path, TEST_CHECKLIST)

        self.assertEqual(resumed_run.status, "DONE")
        steps = get_steps(self.db_path, run.id)
        self.assertEqual(steps[1].status, "DONE")

    @patch("time.sleep", return_value=None)
    @patch("xibi.subagent.checklist.ModelRouter.call")
    def test_retry_logic(self, mock_call, mock_sleep):
        # Recovery on 3rd attempt
        mock_call.side_effect = [
            RuntimeError("Transient error 1"),
            RuntimeError("Transient error 2"),
            _mock_response('{"status": "ok"}'),
        ]

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
        self.assertEqual(mock_call.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)

        # Exhaustion after 3 attempts
        mock_call.reset_mock()
        mock_sleep.reset_mock()
        mock_call.side_effect = [
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
        self.assertEqual(mock_call.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
