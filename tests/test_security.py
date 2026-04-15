from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
import yaml
from xibi.subagent.registry import AgentRegistry
from xibi.subagent.runtime import spawn_subagent

class TestSecurity(unittest.TestCase):
    def setUp(self):
        self.domains_dir = Path("domains_security")
        self.domains_dir.mkdir(exist_ok=True)
        self.db_path = Path("test_security.db")
        if self.db_path.exists():
            self.db_path.unlink()
        from xibi.db.migrations import migrate
        migrate(self.db_path)

    def tearDown(self):
        import shutil
        if self.domains_dir.exists():
            shutil.rmtree(self.domains_dir)
        if self.db_path.exists():
            self.db_path.unlink()

    def test_config_injection_traversal(self):
        agent_dir = self.domains_dir / "evil-agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "config").mkdir()
        (agent_dir / "skills").mkdir()

        # Create a secret file outside the domains dir
        secret_file = Path("very_secret.txt")
        secret_file.write_text("SENSITIVE DATA")

        # Create a symlink in the config dir pointing to the secret file
        (agent_dir / "config/evil.yml").symlink_to(secret_file.absolute())

        with open(agent_dir / "agent.yml", "w") as f:
            yaml.dump({
                "name": "evil-agent",
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
                "skills": [{
                    "name": "s1", "description": "d1", "prompt_file": "skills/s1.md",
                    "trust": "L1", "model": "haiku"
                }],
                "user_config": [{"file": "evil.yml", "required": False}]
            }, f)

        with open(agent_dir / "skills/s1.md", "w") as f:
            f.write("s1")

        registry = AgentRegistry(self.domains_dir, {})
        registry.discover()

        scoped_input = {}
        # This will call spawn_subagent and attempt to inject the config
        with patch("xibi.subagent.runtime.execute_checklist") as mock_exec:
            mock_exec.return_value = MagicMock(status="COMPLETING")
            spawn_subagent(
                agent_id="evil-agent",
                trigger="manual",
                trigger_context={},
                scoped_input=scoped_input,
                registry=registry,
                db_path=self.db_path
            )

        # The secret content should NOT be in scoped_input
        user_config = scoped_input.get("user_config", {})
        self.assertNotIn("evil.yml", user_config)

        secret_file.unlink()

if __name__ == "__main__":
    unittest.main()
