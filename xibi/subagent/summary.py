from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from xibi.subagent.models import AgentManifest, SubagentRun
from xibi.subagent.routing import ModelRouter


class SummaryGenerator:
    """Generates run summaries for DB storage and optional presentation files."""

    def generate_summary(
        self, run: SubagentRun, manifest: AgentManifest, full_output: dict, router: ModelRouter
    ) -> str:
        """Generate a condensed summary for DB storage.

        If manifest.summary.mode == "terminal":
            Extract summary from last step's output (no LLM call).
        If manifest.summary.mode == "dedicated":
            Make one LLM call to synthesize full_output into a summary.
        """
        mode = manifest.summary.get("mode", "terminal")
        max_chars = manifest.summary.get("max_chars", 2000)

        if mode == "terminal":
            # Last step output as summary
            summary_text = json.dumps(full_output, indent=2)
            if len(summary_text) > max_chars:
                summary_text = summary_text[: max_chars - 3] + "..."
            return summary_text

        # Dedicated synthesis
        model = manifest.summary.get("model", "haiku")
        prompt = (
            f"Synthesize the following subagent run output into a concise summary "
            f"(max {max_chars} characters):\n\n"
            f"{json.dumps(full_output, indent=2)}"
        )
        system_prompt = f"Agent: {manifest.name} Summary Generator"

        try:
            response = router.call(model=model, prompt=prompt, system=system_prompt)
            summary_text = response.content.strip()
            if len(summary_text) > max_chars:
                summary_text = summary_text[: max_chars - 3] + "..."
            return summary_text
        except Exception as e:
            return f"Error generating dedicated summary: {e}. Raw output: {json.dumps(full_output)}"

    def generate_presentation_file(
        self, run: SubagentRun, manifest: AgentManifest, full_output: dict, summary: str, domains_dir: Path
    ) -> Path | None:
        """Generate a human-readable markdown deliverable.

        Only called if manifest.summary.presentation_file is True.
        Written to domains/{agent_id}/output/{run_id}.md.
        """
        if not manifest.summary.get("presentation_file"):
            return None

        agent_output_dir = domains_dir / manifest.name / "output"
        agent_output_dir.mkdir(parents=True, exist_ok=True)

        file_path = agent_output_dir / f"{run.id}.md"

        now = datetime.now(timezone.utc).isoformat()
        content = [
            f"# {manifest.name} - Run Summary",
            f"**Run ID:** {run.id}",
            f"**Agent:** {manifest.name} (v{manifest.version})",
            f"**Completed At:** {now}",
            f"**Status:** {run.status}",
            "",
            "## Summary",
            summary,
            "",
            "## Full Output",
            "```json",
            json.dumps(full_output, indent=2),
            "```",
        ]

        content_str = "\n".join(content)

        try:
            with open(file_path, "w") as f:
                f.write(content_str)
            return file_path
        except Exception:
            return None
