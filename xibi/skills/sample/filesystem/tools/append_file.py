import os


def run(params):
    """Append content to a file."""
    filepath = params.get("filepath")
    content = params.get("content")

    workdir = params.get("_workdir") or os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.bregger"))

    if not filepath or content is None:
        return {"status": "error", "message": "Missing filepath or content."}

    filepath = os.path.expanduser(filepath)
    if not os.path.isabs(filepath):
        filepath = os.path.join(workdir, filepath)

    try:
        with open(filepath, "a") as f:
            f.write(content)
        return {"status": "success", "message": f"Content appended to {filepath}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
