import os
from pathlib import Path


def run(params: dict) -> dict:
    """List files in the Xibi workspace directory.

    Returns a newline-separated list of filenames — the LLM can use this
    to identify the correct filename before calling read_file.
    """
    workdir = params.get("_workdir") or os.environ.get("XIBI_WORKDIR")
    if not workdir:
        return {"status": "error", "message": "No workspace path configured."}

    path = Path(workdir)
    if not path.exists():
        return {"status": "error", "message": f"Workspace path does not exist: {workdir}"}

    try:
        files = sorted(f.name for f in path.iterdir() if f.is_file())
        return {
            "status": "success",
            "content": "\n".join(files) if files else "(no files found)",
            "count": len(files),
            "message": f"{len(files)} file(s) in workspace.",
        }
    except Exception as e:
        return {"status": "error", "message": f"Could not list files: {e}"}
