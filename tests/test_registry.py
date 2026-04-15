from pathlib import Path
import unittest
import yaml
from xibi.subagent.registry import AgentRegistry

class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.domains_dir = Path("domains_test")
        self.domains_dir.mkdir(exist_ok=True)
        self.config = {"mcp": {"greenhouse": {}}}

    def tearDown(self):
        import shutil
        if self.domains_dir.exists():
            shutil.rmtree(self.domains_dir)

    def test_discovery_and_resolution(self):
        agent_dir = self.domains_dir / "test-agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "skills").mkdir()

        with open(agent_dir / "agent.yml", "w") as f:
            yaml.dump({
                "name": "test-agent",
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
                        "model": "haiku"
                    }
                ],
                "default_sequence": ["s1"]
            }, f)

        with open(agent_dir / "skills/s1.md", "w") as f:
            f.write("s1 prompt")

        registry = AgentRegistry(self.domains_dir, self.config)
        registry.discover()

        agents = registry.list_agents()
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0].name, "test-agent")

        checklist = registry.resolve_checklist("test-agent")
        self.assertEqual(len(checklist), 1)
        self.assertEqual(checklist[0]["skill_name"], "s1")
        self.assertEqual(checklist[0]["prompt"], "s1 prompt")

    def test_dependency_validation(self):
        agent_dir = self.domains_dir / "dep-agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "skills").mkdir()

        with open(agent_dir / "agent.yml", "w") as f:
            yaml.dump({
                "name": "dep-agent",
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
                        "model": "haiku"
                    },
                    {
                        "name": "s2",
                        "description": "d2",
                        "prompt_file": "skills/s2.md",
                        "trust": "L1",
                        "model": "haiku",
                        "depends_on": ["s1"]
                    }
                ]
            }, f)

        with open(agent_dir / "skills/s1.md", "w") as f:
            f.write("s1")
        with open(agent_dir / "skills/s2.md", "w") as f:
            f.write("s2")

        registry = AgentRegistry(self.domains_dir, self.config)
        registry.discover()

        # Should fail because s1 is not included
        with self.assertRaises(ValueError):
            registry.resolve_checklist("dep-agent", skills=["s2"])

        # Should pass
        checklist = registry.resolve_checklist("dep-agent", skills=["s1", "s2"])
        self.assertEqual(len(checklist), 2)
