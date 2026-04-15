from __future__ import annotations

import logging
from pathlib import Path
from xibi.subagent.models import AgentManifest
from xibi.subagent.manifest import ManifestValidator

logger = logging.getLogger(__name__)

class AgentRegistry:
    """In-memory registry of validated domain agents."""

    def __init__(self, domains_dir: Path, config: dict):
        self.domains_dir = domains_dir
        self.config = config
        self._agents: dict[str, AgentManifest] = {}
        self._validator = ManifestValidator()

    def discover(self) -> None:
        """Scan domains/*/agent.yml, validate each, register valid ones."""
        if not self.domains_dir.exists():
            logger.warning(f"Domains directory not found: {self.domains_dir}")
            return

        for agent_dir in self.domains_dir.iterdir():
            if not agent_dir.is_dir():
                continue

            manifest_path = agent_dir / "agent.yml"
            if not manifest_path.exists():
                continue

            manifest, errors = self._validator.validate(manifest_path)
            if manifest:
                self._agents[manifest.name] = manifest
                logger.info(f"Registered agent: {manifest.name}")
            else:
                logger.error(f"Failed to validate agent in {agent_dir}: {', '.join(errors)}")

    def get(self, agent_id: str) -> AgentManifest | None:
        """Look up agent by ID. Returns None if not registered."""
        return self._agents.get(agent_id)

    def list_agents(self) -> list[AgentManifest]:
        """All registered agents. Used by Roberto to know what's available."""
        # Refresh config_ready status before returning
        for agent_id, manifest in self._agents.items():
            agent_dir = self.domains_dir / agent_id
            manifest.config_ready, _ = self._validator.validate_user_config(agent_dir, manifest)

        return list(self._agents.values())

    def resolve_checklist(self, agent_id: str, skills: list[str] | None = None) -> list[dict]:
        """Convert manifest skills into checklist dicts for Block 1 runtime.

        If skills is None: use default_sequence from manifest.
        If skills is provided: validate each exists and dependencies are met,
        then build checklist from requested skills only.
        """
        manifest = self.get(agent_id)
        if not manifest:
            raise ValueError(f"Agent not found: {agent_id}")

        requested_skill_names = skills if skills is not None else manifest.default_sequence
        if not requested_skill_names:
            # Fallback to all skills if no default sequence and no requested skills
            requested_skill_names = [s.name for s in manifest.skills]

        # Validate existence and dependencies
        skill_map = {s.name: s for s in manifest.skills}
        for s_name in requested_skill_names:
            if s_name not in skill_map:
                raise ValueError(f"Unknown skill '{s_name}' for agent '{agent_id}'")

            skill = skill_map[s_name]
            for dep in skill.depends_on:
                if dep not in requested_skill_names:
                    raise ValueError(
                        f"Skill '{s_name}' depends on '{dep}' which is not in the "
                        f"requested skill list. Either add '{dep}' or use the full default pipeline."
                    )

        checklist = []
        agent_dir = self.domains_dir / agent_id
        for s_name in requested_skill_names:
            skill = skill_map[s_name]
            prompt_path = agent_dir / skill.prompt_file
            try:
                with open(prompt_path, "r") as f:
                    prompt_content = f.read()
            except Exception as e:
                raise RuntimeError(f"Failed to read prompt file {skill.prompt_file}: {e}")

            checklist.append({
                "skill_name": skill.name,
                "model": skill.model,
                "trust": skill.trust,
                "prompt": prompt_content
            })

        return checklist

    def check_mcp_dependencies(self, agent_id: str) -> tuple[bool, list[str]]:
        """Check if required MCP dependencies are available in config.
        Returns (all_met, list_of_missing).
        """
        manifest = self.get(agent_id)
        if not manifest:
            return False, ["Agent not found"]

        mcp_config = self.config.get("mcp", {})
        missing = []
        for dep in manifest.mcp_dependencies:
            dep_name = dep.get("name")
            if dep.get("required") and dep_name not in mcp_config:
                missing.append(dep_name)

        return len(missing) == 0, missing
