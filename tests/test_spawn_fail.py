from pathlib import Path
import unittest
from unittest.mock import MagicMock, PropertyMock
from xibi.subagent.runtime import spawn_subagent
from xibi.subagent.registry import AgentRegistry

class TestSpawnFail(unittest.TestCase):
    def setUp(self):
        self.db_path = Path("test_spawn.db")
        if self.db_path.exists():
            self.db_path.unlink()
        from xibi.db.migrations import migrate
        migrate(self.db_path)

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()

    def test_spawn_invalid_agent(self):
        registry = MagicMock(spec=AgentRegistry)
        registry.get.return_value = None

        run = spawn_subagent(
            agent_id="invalid",
            trigger="manual",
            trigger_context={},
            scoped_input={},
            registry=registry,
            db_path=self.db_path
        )

        self.assertEqual(run.status, "FAILED")
        self.assertIn("not found in registry", run.error_detail)

    def test_spawn_missing_config(self):
        registry = MagicMock(spec=AgentRegistry)
        manifest = MagicMock()
        manifest.name = "agent"
        registry.get.return_value = manifest
        registry.check_mcp_dependencies.return_value = (True, [])

        # Mocking protected _validator and its method
        validator = MagicMock()
        validator.validate_user_config.return_value = (False, ["missing file"])
        type(registry)._validator = PropertyMock(return_value=validator)

        registry.domains_dir = Path("domains")

        run = spawn_subagent(
            agent_id="agent",
            trigger="manual",
            trigger_context={},
            scoped_input={},
            registry=registry,
            db_path=self.db_path
        )

        self.assertEqual(run.status, "FAILED")
        self.assertIn("User config not ready", run.error_detail)

if __name__ == "__main__":
    unittest.main()
