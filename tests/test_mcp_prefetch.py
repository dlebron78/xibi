"""Tests for step-84: MCP tool prefetch, reference injection, input validation."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from xibi.db.migrations import migrate
from xibi.subagent.checklist import _resolve_args, execute_checklist
from xibi.subagent.models import SubagentRun
from xibi.subagent.routing import RoutedResponse


def _make_run(run_id: str = "test-run-1", scoped_input: dict | None = None) -> SubagentRun:
    return SubagentRun(
        id=run_id,
        agent_id="career-ops",
        status="SPAWNED",
        trigger="manual",
        trigger_context={},
        scoped_input=scoped_input or {},
        budget_max_calls=10,
        budget_max_cost_usd=1.0,
        budget_max_duration_s=300,
    )


def _mock_response(content: str = '{"status": "ok"}', cost: float = 0.001) -> RoutedResponse:
    return RoutedResponse(
        content=content,
        model_id="test-model",
        input_tokens=100,
        output_tokens=50,
        cost_usd=cost,
        provider="test",
    )


class TestResolveArgs(unittest.TestCase):
    """Test _resolve_args helper."""

    def test_args_from_found(self):
        scoped_input = {"criteria": {"search_term": "PM Director", "location": "Remote"}}
        tool_decl = {"args_from": "scoped_input.criteria", "args_default": {"search_term": "fallback"}}
        result = _resolve_args(scoped_input, tool_decl)
        self.assertEqual(result, {"search_term": "PM Director", "location": "Remote"})

    def test_args_from_missing_falls_back(self):
        scoped_input = {}
        tool_decl = {"args_from": "scoped_input.criteria", "args_default": {"search_term": "fallback"}}
        result = _resolve_args(scoped_input, tool_decl)
        self.assertEqual(result, {"search_term": "fallback"})

    def test_no_args_from_uses_default(self):
        scoped_input = {"criteria": {"search_term": "PM"}}
        tool_decl = {"args_default": {"search_term": "default"}}
        result = _resolve_args(scoped_input, tool_decl)
        self.assertEqual(result, {"search_term": "default"})

    def test_no_args_at_all(self):
        result = _resolve_args({}, {})
        self.assertEqual(result, {})

    def test_nested_path(self):
        scoped_input = {"user_config": {"profile.yml": {"target_roles": ["PM"]}}}
        tool_decl = {"args_from": "scoped_input.user_config"}
        result = _resolve_args(scoped_input, tool_decl)
        self.assertEqual(result, {"profile.yml": {"target_roles": ["PM"]}})


class TestPrefetch(unittest.TestCase):
    """Test MCP prefetch phase in execute_checklist."""

    def setUp(self):
        self.db_path = Path("test_prefetch.db")
        if self.db_path.exists():
            self.db_path.unlink()
        migrate(self.db_path)
        self.config_patcher = patch("xibi.subagent.routing.load_config", return_value={})
        self.config_patcher.start()

    def tearDown(self):
        self.config_patcher.stop()
        if self.db_path.exists():
            self.db_path.unlink()

    def _create_run_with_steps(self, run: SubagentRun, checklist: list[dict]) -> None:
        """Helper to create run + step records in DB before executing."""
        import uuid

        from xibi.subagent.db import create_run, create_step
        from xibi.subagent.models import ChecklistStep

        create_run(self.db_path, run)
        for i, step_cfg in enumerate(checklist):
            step = ChecklistStep(
                id=str(uuid.uuid4()),
                run_id=run.id,
                step_order=i + 1,
                skill_name=step_cfg["skill_name"],
                status="PENDING",
                model=step_cfg.get("model"),
            )
            create_step(self.db_path, step)

    @patch("xibi.subagent.checklist.ModelRouter.call")
    @patch("xibi.subagent.checklist._get_mcp_client")
    def test_prefetch_injects_result(self, mock_get_client, mock_call):
        """Tool result gets injected into scoped_input before LLM call."""
        mock_client = MagicMock()
        mock_client.call_tool.return_value = {"status": "ok", "result": '[{"title": "PM at Acme"}]'}
        mock_get_client.return_value = mock_client
        mock_call.return_value = _mock_response('{"postings": []}')

        run = _make_run()
        checklist = [
            {
                "skill_name": "scan",
                "model": "haiku",
                "trust": "L1",
                "prompt": "Process the postings",
                "references": {},
                "tools": [
                    {
                        "server": "jobspy",
                        "tool": "search_jobs",
                        "args_default": {"search_term": "PM"},
                        "inject_as": "raw_postings",
                        "required": True,
                    }
                ],
            }
        ]
        self._create_run_with_steps(run, checklist)

        execute_checklist(run, self.db_path, checklist, mcp_configs=[{"name": "jobspy", "command": ["echo"]}])

        # Verify the tool was called
        mock_client.call_tool.assert_called_once_with("search_jobs", {"search_term": "PM"})
        # Verify result was injected into scoped_input
        self.assertIn("raw_postings", run.scoped_input)
        self.assertEqual(run.scoped_input["raw_postings"], '[{"title": "PM at Acme"}]')
        # LLM was still called
        mock_call.assert_called_once()

    @patch("xibi.subagent.checklist.ModelRouter.call")
    @patch("xibi.subagent.checklist._get_mcp_client")
    def test_required_tool_failure_aborts(self, mock_get_client, mock_call):
        """A required tool failure should abort the step."""
        mock_client = MagicMock()
        mock_client.call_tool.return_value = {"status": "error", "error": "server down"}
        mock_get_client.return_value = mock_client

        run = _make_run()
        checklist = [
            {
                "skill_name": "scan",
                "model": "haiku",
                "trust": "L1",
                "prompt": "Process the postings",
                "references": {},
                "tools": [
                    {
                        "server": "jobspy",
                        "tool": "search_jobs",
                        "args_default": {},
                        "inject_as": "raw_postings",
                        "required": True,
                    }
                ],
            }
        ]
        self._create_run_with_steps(run, checklist)

        result = execute_checklist(run, self.db_path, checklist, mcp_configs=[{"name": "jobspy", "command": ["echo"]}])

        self.assertEqual(result.status, "FAILED")
        self.assertIn("Required tool", result.error_detail)
        # LLM should NOT have been called
        mock_call.assert_not_called()

    @patch("xibi.subagent.checklist.ModelRouter.call")
    @patch("xibi.subagent.checklist._get_mcp_client")
    def test_optional_tool_failure_continues(self, mock_get_client, mock_call):
        """An optional tool failure should log warning but continue to LLM call."""
        mock_client = MagicMock()
        mock_client.call_tool.return_value = {"status": "error", "error": "timeout"}
        mock_get_client.return_value = mock_client
        mock_call.return_value = _mock_response('{"status": "ok"}')

        run = _make_run()
        checklist = [
            {
                "skill_name": "research",
                "model": "haiku",
                "trust": "L1",
                "prompt": "Research company",
                "references": {},
                "tools": [
                    {
                        "server": "webfetch",
                        "tool": "fetch",
                        "args_default": {"url": "https://example.com"},
                        "inject_as": "web_content",
                        "required": False,
                    }
                ],
            }
        ]
        self._create_run_with_steps(run, checklist)

        execute_checklist(run, self.db_path, checklist, mcp_configs=[{"name": "webfetch", "command": ["echo"]}])

        # Should still complete (LLM was called despite tool failure)
        mock_call.assert_called_once()
        self.assertNotIn("web_content", run.scoped_input)


class TestReferenceInjection(unittest.TestCase):
    """Test that references from checklist are injected into scoped_input."""

    def setUp(self):
        self.db_path = Path("test_refs.db")
        if self.db_path.exists():
            self.db_path.unlink()
        migrate(self.db_path)
        self.config_patcher = patch("xibi.subagent.routing.load_config", return_value={})
        self.config_patcher.start()

    def tearDown(self):
        self.config_patcher.stop()
        if self.db_path.exists():
            self.db_path.unlink()

    def _create_run_with_steps(self, run: SubagentRun, checklist: list[dict]) -> None:
        import uuid

        from xibi.subagent.db import create_run, create_step
        from xibi.subagent.models import ChecklistStep

        create_run(self.db_path, run)
        for i, step_cfg in enumerate(checklist):
            step = ChecklistStep(
                id=str(uuid.uuid4()),
                run_id=run.id,
                step_order=i + 1,
                skill_name=step_cfg["skill_name"],
                status="PENDING",
                model=step_cfg.get("model"),
            )
            create_step(self.db_path, step)

    @patch("xibi.subagent.checklist.ModelRouter.call")
    def test_references_injected(self, mock_call):
        """References from checklist entry are injected into scoped_input.references."""
        mock_call.return_value = _mock_response('{"grade": "A"}')

        run = _make_run()
        checklist = [
            {
                "skill_name": "evaluate",
                "model": "sonnet",
                "trust": "L1",
                "prompt": "Evaluate this posting using scoring-rubric.md",
                "references": {
                    "scoring-rubric.md": "# Scoring Rubric\n1-5 scale...",
                    "archetypes.md": "# Archetypes\nTech, Finance...",
                },
                "tools": [],
            }
        ]
        self._create_run_with_steps(run, checklist)

        execute_checklist(run, self.db_path, checklist)

        # References should be in scoped_input
        self.assertIn("references", run.scoped_input)
        self.assertIn("scoring-rubric.md", run.scoped_input["references"])
        self.assertIn("archetypes.md", run.scoped_input["references"])


class TestInputValidation(unittest.TestCase):
    """Test that the input validation preamble is included in prompts."""

    def setUp(self):
        self.db_path = Path("test_validation.db")
        if self.db_path.exists():
            self.db_path.unlink()
        migrate(self.db_path)
        self.config_patcher = patch("xibi.subagent.routing.load_config", return_value={})
        self.config_patcher.start()

    def tearDown(self):
        self.config_patcher.stop()
        if self.db_path.exists():
            self.db_path.unlink()

    def _create_run_with_steps(self, run: SubagentRun, checklist: list[dict]) -> None:
        import uuid

        from xibi.subagent.db import create_run, create_step
        from xibi.subagent.models import ChecklistStep

        create_run(self.db_path, run)
        for i, step_cfg in enumerate(checklist):
            step = ChecklistStep(
                id=str(uuid.uuid4()),
                run_id=run.id,
                step_order=i + 1,
                skill_name=step_cfg["skill_name"],
                status="PENDING",
                model=step_cfg.get("model"),
            )
            create_step(self.db_path, step)

    @patch("xibi.subagent.checklist.ModelRouter.call")
    def test_validation_preamble_in_prompt(self, mock_call):
        """The input validation preamble should appear in the prompt sent to LLM."""
        mock_call.return_value = _mock_response('{"status": "ok"}')

        run = _make_run()
        checklist = [
            {
                "skill_name": "evaluate",
                "model": "haiku",
                "trust": "L1",
                "prompt": "Evaluate the posting",
                "references": {},
                "tools": [],
            }
        ]
        self._create_run_with_steps(run, checklist)

        execute_checklist(run, self.db_path, checklist)

        # Check that the prompt sent to the LLM includes the validation preamble
        call_args = mock_call.call_args
        prompt = call_args.kwargs.get("prompt", "") or call_args[1].get("prompt", "")
        self.assertIn("missing_input", prompt)
        self.assertIn("do NOT fabricate", prompt)


class TestMCPClientLifecycle(unittest.TestCase):
    """Test that MCP clients are reused across steps and closed at end."""

    def setUp(self):
        self.db_path = Path("test_lifecycle.db")
        if self.db_path.exists():
            self.db_path.unlink()
        migrate(self.db_path)
        self.config_patcher = patch("xibi.subagent.routing.load_config", return_value={})
        self.config_patcher.start()

    def tearDown(self):
        self.config_patcher.stop()
        if self.db_path.exists():
            self.db_path.unlink()

    def _create_run_with_steps(self, run: SubagentRun, checklist: list[dict]) -> None:
        import uuid

        from xibi.subagent.db import create_run, create_step
        from xibi.subagent.models import ChecklistStep

        create_run(self.db_path, run)
        for i, step_cfg in enumerate(checklist):
            step = ChecklistStep(
                id=str(uuid.uuid4()),
                run_id=run.id,
                step_order=i + 1,
                skill_name=step_cfg["skill_name"],
                status="PENDING",
                model=step_cfg.get("model"),
            )
            create_step(self.db_path, step)

    @patch("xibi.subagent.checklist.ModelRouter.call")
    @patch("xibi.subagent.checklist._get_mcp_client")
    def test_client_reused_across_steps(self, mock_get_client, mock_call):
        """Same MCP client should be reused when two steps use the same server."""
        mock_client = MagicMock()
        mock_client.call_tool.return_value = {"status": "ok", "result": "data"}
        mock_get_client.return_value = mock_client
        mock_call.return_value = _mock_response('{"status": "ok"}')

        run = _make_run()
        tool_decl = {
            "server": "jobspy",
            "tool": "search_jobs",
            "args_default": {},
            "inject_as": "raw_postings",
            "required": False,
        }
        checklist = [
            {
                "skill_name": "scan",
                "model": "haiku",
                "trust": "L1",
                "prompt": "p1",
                "references": {},
                "tools": [tool_decl],
            },
            {
                "skill_name": "triage",
                "model": "haiku",
                "trust": "L1",
                "prompt": "p2",
                "references": {},
                "tools": [tool_decl],
            },
        ]
        self._create_run_with_steps(run, checklist)

        mcp_configs = [{"name": "jobspy", "command": ["echo"]}]
        execute_checklist(run, self.db_path, checklist, mcp_configs=mcp_configs)

        # _get_mcp_client is called with the same active_clients dict — reuse happens inside it
        # Both steps should have called the tool
        self.assertEqual(mock_client.call_tool.call_count, 2)


if __name__ == "__main__":
    unittest.main()
