import os


def run(params: dict) -> dict:
    """Read specific file content."""
    filepath = params.get("filepath")

    workdir = params.get("_workdir") or os.environ.get("XIBI_WORKDIR", os.path.expanduser("~/.xibi"))

    if not filepath:
        return {"status": "error", "message": "Missing filepath."}

    filepath = os.path.expanduser(filepath)
    if not os.path.isabs(filepath):
        # Deterministic search priority
        search_paths = []

        # 1. workdir (~/.xibi or env)
        search_paths.append(workdir)

        # 2. workspace_path belief (if provided by core)
        workspace = params.get("workspace_path")
        if workspace:
            search_paths.append(os.path.expanduser(workspace))

        # 3. Common deployment fallback (via environment variables)
        data_dir = os.environ.get("XIBI_DATA_DIR", os.path.join(os.path.expanduser("~"), "xibi_remote"))
        deploy_dir = os.environ.get("XIBI_DEPLOY_DIR", os.path.join(os.path.expanduser("~"), "xibi_deployment"))
        search_paths.append(data_dir)
        search_paths.append(deploy_dir)

        for path in search_paths:
            candidate = os.path.join(path, filepath)
            if os.path.exists(candidate):
                filepath = candidate
                break
        else:
            # If not found anywhere, default back to workdir for the error message
            filepath = os.path.join(workdir, filepath)

    if not os.path.exists(filepath):
        return {"status": "error", "message": f"File not found: {filepath}"}

    try:
        with open(filepath) as f:
            content = f.read()
        return {"status": "success", "content": content}
    except Exception as e:
        return {"status": "error", "message": str(e)}
