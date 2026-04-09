import json
import os


def run(params: dict) -> dict:
    """Create or overwrite a file."""
    filepath = params.get("filepath")
    content = params.get("content")
    handle_payload = params.get("handle") # Resolved payload from react.py

    workdir = params.get("_workdir") or os.environ.get("XIBI_WORKDIR", os.path.expanduser("~/.xibi"))

    if not filepath:
        return {"status": "error", "message": "Missing filepath."}

    if content is None and handle_payload is None:
        return {"status": "error", "message": "Provide exactly one of `content` or `handle`"}
    if content is not None and handle_payload is not None:
        return {"status": "error", "message": "Provide exactly one of `content` or `handle`"}

    if handle_payload is not None:
        if isinstance(handle_payload, (dict, list)):
            content = json.dumps(handle_payload, indent=2, default=str)
        else:
            content = str(handle_payload)

    filepath = os.path.expanduser(filepath)
    if not os.path.isabs(filepath):
        filepath = os.path.join(workdir, filepath)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    try:
        with open(filepath, "w") as f:
            f.write(str(content))
        return {"status": "success", "message": f"File written to {filepath}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
