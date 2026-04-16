import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from xibi.subagent.registry import AgentRegistry
from xibi.subagent.runtime import spawn_subagent


class TestE2E(unittest.TestCase):
    def setUp(self):
        self.domains_dir = Path("domains_e2e")
        self.domains_dir.mkdir(exist_ok=True)
        self.db_path = Path("test_e2e.db")
        if self.db_path.exists():
            self.db_path.unlink()
        from xibi.db.migrations import migrate

        migrate(self.db_path)

        # Create a dummy config.json
        self.config_path = Path("config.json")
        with open(self.config_path, "w") as f:
            json.dump(
                {
                    "models": {"text": {"haiku": {"provider": "anthropic", "model": "claude-3-haiku"}}},
                    "providers": {"anthropic": {"api_key": "dummy"}},
                },
                f,
            )

    def tearDown(self):
        import shutil

        if self.domains_dir.exists():
            shutil.rmtree(self.domains_dir)
        if self.db_path.exists():
            self.db_path.unlink()
        if self.config_path.exists():
            self.config_path.unlink()

    def test_full_pipeline(self):
        agent_dir = self.domains_dir / "e2e-agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "config").mkdir()
        (agent_dir / "skills").mkdir()

        with open(agent_dir / "agent.yml", "w") as f:
            yaml.dump(
                {
                    "name": "e2e-agent",
                    "version": "1.0.0",
                    "description": "desc",
                    "author": "auth",
                    "expected_duration_s": 10,
                    "max_duration_s": 20,
                    "budget": {"max_calls": 5, "max_cost_usd": 0.1},
                    "summary": {"mode": "terminal"},
                    "output_ttl_hours": 1,
                    "input_schema": {},
                    "output_schema": {},
                    "skills": [
                        {
                            "name": "s1",
                            "description": "d1",
                            "prompt_file": "skills/s1.md",
                            "trust": "L1",
                            "model": "haiku",
                        }
                    ],
                    "default_sequence": ["s1"],
                },
                f,
            )

        with open(agent_dir / "skills/s1.md", "w") as f:
            f.write("s1")

        registry = AgentRegistry(self.domains_dir, {})
        registry.discover()

        # Mock LLM and execution
        with patch("xibi.subagent.routing.ModelRouter.call") as mock_call:
            mock_call.return_value = MagicMock(
                content='{"result": "success"}',
                model_id="haiku",
                provider="anthropic",
                input_tokens=10,
                output_tokens=10,
                cost_usd=0.01,
            )

            run = spawn_subagent(
                agent_id="e2e-agent",
                trigger="manual",
                trigger_context={},
                scoped_input={},
                registry=registry,
                db_path=self.db_path,
            )

        self.assertEqual(run.status, "DONE")
        self.assertEqual(run.output, {"result": "success"})
        self.assertIsNotNone(run.summary)
        self.assertIn("success", run.summary)


if __name__ == "__main__":
    unittest.main()
