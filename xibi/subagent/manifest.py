from __future__ import annotations

import yaml
from pathlib import Path
from typing import Any
from xibi.subagent.models import AgentManifest, SkillDeclaration

class ManifestValidator:
    """Validates agent.yml against the contract schema."""

    REQUIRED_FIELDS = [
        "name", "version", "description", "author",
        "expected_duration_s", "max_duration_s",
        "budget", "summary", "output_ttl_hours",
        "input_schema", "output_schema", "skills"
    ]

    def validate(self, manifest_path: Path) -> tuple[AgentManifest | None, list[str]]:
        """Parse and validate a manifest file.

        Returns (manifest, errors).
        If errors is non-empty, manifest is None.
        Errors are human-readable strings suitable for logging.
        """
        errors = []
        if not manifest_path.exists():
            return None, [f"Manifest file not found: {manifest_path}"]

        try:
            with open(manifest_path, "r") as f:
                data = yaml.safe_load(f)
        except Exception as e:
            return None, [f"Failed to parse YAML: {e}"]

        if not isinstance(data, dict):
            return None, ["Manifest must be a dictionary"]

        for field in self.REQUIRED_FIELDS:
            if field not in data:
                errors.append(f"Missing required field: {field}")

        if errors:
            return None, errors

        # Validate budget
        budget = data.get("budget", {})
        if not isinstance(budget, dict):
            errors.append("budget must be a dictionary")
        else:
            if budget.get("max_calls", 0) <= 0:
                errors.append("budget.max_calls must be positive")
            if budget.get("max_cost_usd", 0.0) <= 0:
                errors.append("budget.max_cost_usd must be positive")

        # Validate summary
        summary = data.get("summary", {})
        if not isinstance(summary, dict):
            errors.append("summary must be a dictionary")
        else:
            if summary.get("mode") not in ["dedicated", "terminal"]:
                errors.append("summary.mode must be 'dedicated' or 'terminal'")

        # Validate skills
        skills_data = data.get("skills", [])
        if not isinstance(skills_data, list) or not skills_data:
            errors.append("skills must be a non-empty list")

        skills = []
        agent_dir = manifest_path.parent
        for s_data in (skills_data if isinstance(skills_data, list) else []):
            s_name = s_data.get("name")
            if not s_name:
                errors.append("Skill missing name")
                continue

            if not s_data.get("description"):
                errors.append(f"Skill '{s_name}' missing description")
            if not s_data.get("prompt_file"):
                errors.append(f"Skill '{s_name}' missing prompt_file")
            else:
                prompt_path = agent_dir / s_data["prompt_file"]
                if not prompt_path.exists():
                    errors.append(f"Skill '{s_name}' prompt_file not found: {s_data['prompt_file']}")

            if s_data.get("trust") not in ["L1", "L2"]:
                errors.append(f"Skill '{s_name}' trust must be L1 or L2")
            if not s_data.get("model"):
                errors.append(f"Skill '{s_name}' missing model")

            if s_data.get("standalone") is True:
                if "standalone_input" not in s_data:
                    errors.append(f"Skill '{s_name}' is standalone but missing standalone_input schema")

            skills.append(SkillDeclaration(
                name=s_name,
                description=s_data.get("description", ""),
                prompt_file=s_data.get("prompt_file", ""),
                trust=s_data.get("trust", ""),
                model=s_data.get("model", ""),
                standalone=s_data.get("standalone", False),
                standalone_input=s_data.get("standalone_input"),
                depends_on=s_data.get("depends_on", [])
            ))

        # Check default_sequence
        default_sequence = data.get("default_sequence", [])
        skill_names = {s.name for s in skills}
        for s_name in default_sequence:
            if s_name not in skill_names:
                errors.append(f"default_sequence references unknown skill: {s_name}")

        # Check skill dependencies
        for s in skills:
            for dep in s.depends_on:
                if dep not in skill_names:
                    errors.append(f"Skill '{s.name}' depends on unknown skill: {dep}")

        if errors:
            return None, errors

        manifest = AgentManifest(
            name=data["name"],
            version=data["version"],
            description=data["description"],
            author=data["author"],
            expected_duration_s=int(data["expected_duration_s"]),
            max_duration_s=int(data["max_duration_s"]),
            budget=budget,
            summary=summary,
            output_ttl_hours=int(data["output_ttl_hours"]),
            input_schema=data["input_schema"],
            output_schema=data["output_schema"],
            skills=skills,
            default_sequence=default_sequence,
            mcp_dependencies=data.get("mcp_dependencies", []),
            user_config=data.get("user_config", [])
        )

        # Initial config check
        manifest.config_ready, _ = self.validate_user_config(agent_dir, manifest)

        return manifest, []

    def validate_user_config(self, agent_dir: Path, manifest: AgentManifest) -> tuple[bool, list[str]]:
        """Check that required user config files exist.
        Called at spawn time, not discovery time (config may be added later).
        """
        errors = []
        for config_decl in manifest.user_config:
            if config_decl.get("required"):
                filename = config_decl.get("file")
                if not filename:
                    continue
                config_path = agent_dir / "config" / filename
                if not config_path.exists():
                    errors.append(f"Required user config file missing: {filename}")

        return len(errors) == 0, errors
