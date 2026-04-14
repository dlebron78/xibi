import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SkillInfo:
    name: str
    manifest: dict[str, Any]
    path: Path  # directory containing manifest.json
    source: str = "local"  # "local" | "mcp" — distinguishes hand-authored skills from synthetic MCP injections


class SkillRegistry:
    def __init__(self, skills_dir: str | Path):
        self.skills_dir = Path(skills_dir)
        self.skills: dict[str, SkillInfo] = {}
        self._load()

    def _load(self) -> None:
        if not self.skills_dir.exists():
            return

        for manifest_path in self.skills_dir.glob("*/manifest.json"):
            try:
                with open(manifest_path) as f:
                    manifest = json.load(f)

                name = manifest.get("name")
                if not name:
                    logger.warning(f"Manifest at {manifest_path} missing 'name'")
                    continue

                self.skills[name] = SkillInfo(name=name, manifest=manifest, path=manifest_path.parent)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load manifest at {manifest_path}: {e}")
                continue

    def get_skill_manifests(self) -> list[dict[str, Any]]:
        return [skill.manifest for skill in self.skills.values()]

    def get_tool_meta(self, skill_name: str, tool_name: str) -> dict[str, Any] | None:
        skill = self.skills.get(skill_name)
        if not skill:
            return None

        tools = skill.manifest.get("tools", [])
        return next((t for t in tools if t.get("name") == tool_name), None)

    def get_tool_min_effort(self, skill_name: str, tool_name: str) -> int:
        tool_meta = self.get_tool_meta(skill_name, tool_name)
        if tool_meta:
            res = tool_meta.get("min_effort", 1)
            return int(res) if res is not None else 1
        return 1

    def find_skill_for_tool(self, tool_name: str) -> str | None:
        for skill_name, skill_info in self.skills.items():
            tools = skill_info.manifest.get("tools", [])
            if any(t.get("name") == tool_name for t in tools):
                return skill_name
        return None

    def find_local_skill_for_tool(self, tool_name: str) -> str | None:
        """Like find_skill_for_tool, but ignores synthetic MCP-injected entries.

        Use this in the executor's dispatch collision check so that an MCP tool
        registered via register() does not falsely appear as a local-skill match
        and block the MCP routing path.
        """
        for skill_name, skill_info in self.skills.items():
            if skill_info.source != "local":
                continue
            tools = skill_info.manifest.get("tools", [])
            if any(t.get("name") == tool_name for t in tools):
                return skill_name
        return None

    def register(self, manifest: dict[str, Any]) -> None:
        """Register a synthetic skill manifest in-memory without writing to disk."""
        name = manifest.get("name")
        if not name:
            logger.warning("Attempted to register manifest without 'name'")
            return

        # SkillInfo expects a path. For synthetic skills, we use a dummy path.
        self.skills[name] = SkillInfo(name=name, manifest=manifest, path=Path("/dev/null"), source="mcp")

    def validate(self) -> list[str]:
        warnings = []
        for skill_name, skill_info in self.skills.items():
            manifest = skill_info.manifest
            if "name" not in manifest:
                warnings.append(f"Skill '{skill_name}': missing 'name'")
            if "description" not in manifest:
                warnings.append(f"Skill '{skill_name}': missing 'description'")

            for tool in manifest.get("tools", []):
                tname = tool.get("name", "unknown")
                if "name" not in tool:
                    warnings.append(f"Skill '{skill_name}': tool missing 'name'")
                if "description" not in tool:
                    warnings.append(f"Skill '{skill_name}', tool '{tname}': missing 'description'")

                output_type = tool.get("output_type")
                valid_output_types = {"raw", "synthesis", "action"}
                if "output_type" not in tool:
                    warnings.append(f"Skill '{skill_name}', tool '{tname}': missing 'output_type'")
                elif output_type not in valid_output_types:
                    warnings.append(f"Skill '{skill_name}', tool '{tname}': invalid 'output_type' '{output_type}'")

                if tool.get("risk") == "irreversible" and output_type != "action":
                    warnings.append(
                        f"Skill '{skill_name}', tool '{tname}': irreversible tools must have output_type 'action'"
                    )

        return warnings
