"""Tests for step-129 subagent runtime hardening.

Covers:
- System prompt enrichment (format instructions + UTC timestamp).
- Context-budget truncation of ``scoped_input``.
- Output-schema validation with one corrective retry (fail-open).
- Tool-scope enforcement against the manifest's declared tool list.
- Deep-copy of ``scoped_input`` at ``spawn_subagent`` entry.
- ``trust`` field on skill manifests being optional after deprecation.

Tests mock ``ModelRouter.call`` and capture the ``system`` kwarg so the
prompt-shape assertions don't need a live LLM. Cost-event accounting is
verified through the public ``SubagentRun.actual_calls`` counter.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from xibi.db.migrations import migrate
from xibi.subagent.checklist import (
    _apply_context_budget,
    _validate_step_output,
)
from xibi.subagent.db import get_steps
from xibi.subagent.manifest import ManifestValidator
from xibi.subagent.routing import RoutedResponse
from xibi.subagent.runtime import spawn_subagent
from xibi.subagent.trust import check_tool_scope


def _mock_response(content: str, cost: float = 0.001) -> RoutedResponse:
    return RoutedResponse(
        content=content,
        model_id="test-model",
        input_tokens=10,
        output_tokens=5,
        cost_usd=cost,
    )


_BASE_BUDGET = {"max_calls": 10, "max_cost_usd": 0.5, "max_duration_s": 60}


class _DBTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "hardening.db"
        migrate(self.db_path)
        self._router_patch = patch("xibi.subagent.routing.load_config", return_value={})
        self._router_patch.start()

    def tearDown(self) -> None:
        self._router_patch.stop()
        self._tmp.cleanup()


class TestSystemPromptEnrichment(_DBTestCase):
    """Scenario 1 -- prompt carries format block + UTC timestamp."""

    @patch("xibi.subagent.checklist.ModelRouter.call")
    def test_system_prompt_includes_format_and_timestamp(self, mock_call):
        captured: dict = {}

        def _capture(*args, **kwargs):
            captured.update(kwargs)
            return _mock_response('{"score": 4}')

        mock_call.side_effect = _capture

        checklist = [
            {
                "skill_name": "evaluate",
                "model": "haiku",
                "trust": "L1",
                "prompt": "eval prompt",
                "output_schema": {
                    "type": "object",
                    "required": ["score"],
                    "properties": {"score": {"type": "number"}},
                },
            }
        ]

        spawn_subagent(
            agent_id="a",
            trigger="manual",
            trigger_context={},
            scoped_input={"posting": "x"},
            checklist=checklist,
            budget=_BASE_BUDGET,
            db_path=self.db_path,
        )

        system = captured.get("system", "")
        self.assertIn("Output format:", system)
        self.assertIsNotNone(
            re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", system),
            f"UTC timestamp pattern not found in system prompt: {system!r}",
        )

    @patch("xibi.subagent.checklist.ModelRouter.call")
    def test_system_prompt_omits_format_when_no_schema(self, mock_call):
        captured: dict = {}

        def _capture(*args, **kwargs):
            captured.update(kwargs)
            return _mock_response('{"ok": true}')

        mock_call.side_effect = _capture

        checklist = [
            {
                "skill_name": "evaluate",
                "model": "haiku",
                "trust": "L1",
                "prompt": "p",
            }
        ]

        spawn_subagent(
            agent_id="a",
            trigger="manual",
            trigger_context={},
            scoped_input={},
            checklist=checklist,
            budget=_BASE_BUDGET,
            db_path=self.db_path,
        )

        self.assertNotIn("Output format:", captured.get("system", ""))


class TestContextBudget(unittest.TestCase):
    """Scenario 4 -- scoped_input is truncated before prompt assembly."""

    def test_context_budget_truncation(self):
        big = "x" * 200_000
        scoped_input = {"big_field": big, "small": "y"}
        result = _apply_context_budget(scoped_input, max_bytes=1024)
        self.assertLessEqual(len(json.dumps(result).encode("utf-8")), 1024)
        self.assertIn("[truncated", result["big_field"])
        self.assertEqual(result["small"], "y")

    def test_context_budget_no_truncation_under_limit(self):
        scoped_input = {"a": "1", "b": "2"}
        result = _apply_context_budget(scoped_input, max_bytes=4096)
        self.assertEqual(result, scoped_input)

    def test_context_budget_exempts_user_config(self):
        # user_config dominates the payload but must NOT be truncated.
        big_config = {"data": "z" * 50_000}
        other = "q" * 100  # tiny
        scoped_input = {"user_config": big_config, "other": other}
        result = _apply_context_budget(scoped_input, max_bytes=2048)
        # user_config survives intact even though it's the largest contributor.
        self.assertEqual(result["user_config"], big_config)


class TestOutputValidation(unittest.TestCase):
    """Standalone unit tests for _validate_step_output."""

    def test_output_validation_pass(self):
        schema = {"type": "object", "required": ["score"]}
        valid, err = _validate_step_output({"score": 3.5}, schema)
        self.assertTrue(valid)
        self.assertIsNone(err)

    def test_output_validation_fail(self):
        schema = {"type": "object", "required": ["nonexistent_field"]}
        valid, err = _validate_step_output({"score": 3.5}, schema)
        self.assertFalse(valid)
        self.assertIn("required property", err or "")


class TestOutputValidationRetry(_DBTestCase):
    """Scenario 2 -- runtime retries once on schema failure."""

    @patch("xibi.subagent.checklist.ModelRouter.call")
    def test_output_validation_retry_succeeds(self, mock_call):
        schema = {
            "type": "object",
            "required": ["score"],
            "properties": {"score": {"type": "number"}},
        }
        # First response is missing the required field, second carries it.
        mock_call.side_effect = [
            _mock_response('{"other": "value"}'),
            _mock_response('{"score": 4}'),
        ]
        run = spawn_subagent(
            agent_id="a",
            trigger="manual",
            trigger_context={},
            scoped_input={},
            checklist=[
                {
                    "skill_name": "evaluate",
                    "model": "haiku",
                    "trust": "L1",
                    "prompt": "p",
                    "output_schema": schema,
                }
            ],
            budget=_BASE_BUDGET,
            db_path=self.db_path,
        )
        self.assertEqual(run.status, "DONE")
        # Primary + corrective retry both fired and count against the budget.
        self.assertEqual(run.actual_calls, 2)
        self.assertEqual(run.output, {"score": 4})

    @patch("xibi.subagent.checklist.ModelRouter.call")
    def test_output_validation_retry_fails_open(self, mock_call):
        schema = {"type": "object", "required": ["score"]}
        mock_call.side_effect = [
            _mock_response('{"other": "1"}'),
            _mock_response('{"other": "2"}'),
        ]
        run = spawn_subagent(
            agent_id="a",
            trigger="manual",
            trigger_context={},
            scoped_input={},
            checklist=[
                {
                    "skill_name": "evaluate",
                    "model": "haiku",
                    "trust": "L1",
                    "prompt": "p",
                    "output_schema": schema,
                }
            ],
            budget=_BASE_BUDGET,
            db_path=self.db_path,
        )
        # Fail-open: run still completes, both calls counted, retry output kept.
        self.assertEqual(run.status, "DONE")
        self.assertEqual(run.actual_calls, 2)
        self.assertEqual(run.output, {"other": "2"})


class TestToolScope(unittest.TestCase):
    """Scenario 3 -- undeclared tools are stripped from output."""

    def test_tool_scope_strips_undeclared(self):
        output = {"actions": [{"tool": "send_email", "args": {}}]}
        cleaned, violations = check_tool_scope(output, ["search_jobs"])
        self.assertEqual(violations, [{"tool": "send_email", "reason": "not_in_declared_tools"}])
        self.assertEqual(cleaned["actions"], [])

    def test_tool_scope_passes_declared(self):
        output = {"actions": [{"tool": "search_jobs", "args": {}}]}
        cleaned, violations = check_tool_scope(output, ["search_jobs"])
        self.assertEqual(violations, [])
        self.assertEqual(cleaned["actions"], [{"tool": "search_jobs", "args": {}}])

    def test_tool_scope_empty_declared_passes_all(self):
        output = {"actions": [{"tool": "anything", "args": {}}]}
        cleaned, violations = check_tool_scope(output, [])
        self.assertEqual(violations, [])
        self.assertEqual(cleaned, output)


class TestScopedInputDeepCopy(_DBTestCase):
    """spawn_subagent must not mutate the caller's scoped_input dict."""

    @patch("xibi.subagent.checklist.ModelRouter.call")
    def test_scoped_input_deep_copy(self, mock_call):
        mock_call.side_effect = [_mock_response('{"ok": true}')]
        original = {"criteria": {"role": "swe"}}
        snapshot = json.loads(json.dumps(original))  # deep clone for comparison

        spawn_subagent(
            agent_id="a",
            trigger="manual",
            trigger_context={},
            scoped_input=original,
            checklist=[
                {
                    "skill_name": "echo",
                    "model": "haiku",
                    "trust": "L1",
                    "prompt": "p",
                }
            ],
            budget=_BASE_BUDGET,
            db_path=self.db_path,
        )
        # Caller's dict must be untouched -- runtime injects user_config
        # and other keys on its own copy.
        self.assertEqual(original, snapshot)


class TestManifestTrustOptional(unittest.TestCase):
    """Manifests that omit the deprecated `trust` field still parse."""

    def test_manifest_trust_field_optional(self):
        with TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / "test-agent"
            agent_dir.mkdir()
            prompt_file = agent_dir / "skill.md"
            prompt_file.write_text("hello")
            manifest_path = agent_dir / "agent.yml"
            manifest_path.write_text(
                """name: test-agent
version: "1.0"
description: t
author: t
expected_duration_s: 10
max_duration_s: 60
budget:
  max_calls: 5
  max_cost_usd: 0.1
summary:
  mode: terminal
output_ttl_hours: 1
input_schema: {}
output_schema: {}
skills:
  - name: only
    description: d
    prompt_file: skill.md
    model: haiku
"""
            )

            manifest, errors = ManifestValidator().validate(manifest_path)
            self.assertEqual(errors, [])
            self.assertIsNotNone(manifest)
            assert manifest is not None
            self.assertEqual(manifest.skills[0].trust, "")


if __name__ == "__main__":
    unittest.main()
