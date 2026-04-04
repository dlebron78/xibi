# Patch: Dashboard Model Configuration (Read-Write)

> **Priority:** P2 — quality of life, eliminates SSH-to-edit-config workflow
> **Conflicts with:** Nothing. Dashboard code is untouched by step-47/48.
> **Effort:** Small-Medium (2-3 hours)
> **Depends on:** Nothing — can land independently

---

## What to Do

Make model assignments (fast/think/review) editable from the dashboard UI. Today the
health endpoint shows the current model but there's no way to change it without SSH-ing
into NucBox and editing `~/.xibi/config.json`.

### 1. Add GET endpoint for current model config

```python
# In xibi/dashboard/app.py

@app.route("/api/config/models", methods=["GET"])
def get_model_config():
    """Return current model assignments for all effort levels."""
    config_path = os.path.expanduser("~/.xibi/config.json")
    try:
        with open(config_path) as f:
            config = json.load(f)

        models = config.get("models", {}).get("text", {})
        providers = config.get("providers", {})

        # Build response with available providers for the dropdown
        result = {
            "assignments": {},
            "available_providers": list(providers.keys()),
        }

        for effort in ["fast", "think", "review"]:
            role_config = models.get(effort, {})
            result["assignments"][effort] = {
                "provider": role_config.get("provider", ""),
                "model": role_config.get("model", ""),
                "options": role_config.get("options", {}),
                "fallback": role_config.get("fallback"),
            }

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
```

### 2. Add PUT endpoint to update model config

```python
@app.route("/api/config/models", methods=["PUT"])
def update_model_config():
    """
    Update model assignments. Writes to config.json and restarts heartbeat.

    Expected body:
    {
        "effort": "fast",           # which effort level to update
        "provider": "ollama",       # provider name
        "model": "gemma4:e4b",      # model identifier
        "options": {"think": false} # optional provider-specific options
    }
    """
    config_path = os.path.expanduser("~/.xibi/config.json")
    data = request.get_json()

    effort = data.get("effort")
    if effort not in ("fast", "think", "review"):
        return jsonify({"error": f"Invalid effort level: {effort}"}), 400

    provider = data.get("provider")
    model = data.get("model")
    if not provider or not model:
        return jsonify({"error": "provider and model are required"}), 400

    try:
        # Read current config
        with open(config_path) as f:
            config = json.load(f)

        # Validate provider exists
        if provider not in config.get("providers", {}):
            return jsonify({"error": f"Unknown provider: {provider}"}), 400

        # Update the assignment
        if "models" not in config:
            config["models"] = {}
        if "text" not in config["models"]:
            config["models"]["text"] = {}

        role_config = config["models"]["text"].get(effort, {})
        role_config["provider"] = provider
        role_config["model"] = model

        if "options" in data:
            role_config["options"] = data["options"]

        # Preserve fallback chain
        if "fallback" not in role_config:
            defaults = {"fast": "think", "think": "review", "review": None}
            role_config["fallback"] = defaults.get(effort)

        config["models"]["text"][effort] = role_config

        # Write back — preserve formatting
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)

        # Restart heartbeat to pick up new config
        restart_result = _restart_heartbeat()

        return jsonify({
            "status": "ok",
            "updated": {effort: role_config},
            "heartbeat_restart": restart_result,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _restart_heartbeat():
    """Restart the heartbeat service to pick up config changes."""
    import subprocess
    try:
        result = subprocess.run(
            ["systemctl", "--user", "restart", "xibi-heartbeat"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return "restarted"
        else:
            return f"restart failed: {result.stderr}"
    except subprocess.TimeoutExpired:
        # systemctl restart over SSH can be slow — fire and forget
        return "restart initiated (timeout waiting for confirmation)"
    except Exception as e:
        return f"restart error: {e}"
```

### 3. Add available models endpoint (for Ollama)

```python
@app.route("/api/config/available_models", methods=["GET"])
def get_available_models():
    """
    List models available from each provider.
    For Ollama: query the local API. For cloud providers: return known model list.
    """
    config_path = os.path.expanduser("~/.xibi/config.json")
    with open(config_path) as f:
        config = json.load(f)

    providers = config.get("providers", {})
    available = {}

    # Ollama: query local API
    if "ollama" in providers:
        try:
            import requests
            base_url = providers["ollama"].get("base_url", "http://localhost:11434")
            resp = requests.get(f"{base_url}/api/tags", timeout=5)
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                available["ollama"] = [
                    {
                        "name": m["name"],
                        "size": m.get("size", 0),
                        "modified": m.get("modified_at", ""),
                    }
                    for m in models
                ]
        except Exception as e:
            available["ollama"] = {"error": str(e)}

    # Cloud providers: static known models (update as needed)
    if "gemini" in providers:
        available["gemini"] = [
            {"name": "gemini-2.5-flash"},
            {"name": "gemini-2.5-pro"},
        ]

    if "anthropic" in providers:
        available["anthropic"] = [
            {"name": "claude-haiku-4-5-20251001"},
            {"name": "claude-sonnet-4-6"},
        ]

    return jsonify(available)
```

### 4. Frontend panel

Add a "Model Configuration" section to the dashboard HTML. The existing dashboard serves
`index.html` from the templates folder. Add a card with three rows (fast/think/review),
each showing:

- Current provider + model (text)
- Dropdown for provider (populated from `/api/config/available_models`)
- Dropdown for model (populated per-provider)
- "Options" text field (JSON, for things like `{"think": false}`)
- Save button per row

```html
<!-- Add to existing dashboard index.html -->
<div class="card" id="model-config">
  <h2>Model Configuration</h2>
  <div id="model-assignments">
    <!-- Populated by JS -->
  </div>
</div>

<script>
async function loadModelConfig() {
  const [configRes, modelsRes] = await Promise.all([
    fetch('/api/config/models'),
    fetch('/api/config/available_models'),
  ]);
  const config = await configRes.json();
  const available = await modelsRes.json();

  const container = document.getElementById('model-assignments');
  container.innerHTML = '';

  for (const effort of ['fast', 'think', 'review']) {
    const assignment = config.assignments[effort] || {};
    const row = document.createElement('div');
    row.className = 'model-row';
    row.innerHTML = `
      <h3>${effort}</h3>
      <label>Provider:
        <select class="provider-select" data-effort="${effort}">
          ${config.available_providers.map(p =>
            `<option value="${p}" ${p === assignment.provider ? 'selected' : ''}>${p}</option>`
          ).join('')}
        </select>
      </label>
      <label>Model:
        <select class="model-select" data-effort="${effort}">
          ${(available[assignment.provider] || []).map(m => {
            const name = m.name || m;
            return `<option value="${name}" ${name === assignment.model ? 'selected' : ''}>${name}</option>`;
          }).join('')}
        </select>
      </label>
      <label>Options:
        <input type="text" class="options-input" data-effort="${effort}"
          value='${JSON.stringify(assignment.options || {})}' />
      </label>
      <button onclick="saveModel('${effort}')">Save</button>
      <span class="status" id="status-${effort}"></span>
    `;
    container.appendChild(row);
  }

  // Update model dropdown when provider changes
  document.querySelectorAll('.provider-select').forEach(sel => {
    sel.addEventListener('change', (e) => {
      const effort = e.target.dataset.effort;
      const provider = e.target.value;
      const modelSel = document.querySelector(`.model-select[data-effort="${effort}"]`);
      const models = available[provider] || [];
      modelSel.innerHTML = models.map(m => {
        const name = m.name || m;
        return `<option value="${name}">${name}</option>`;
      }).join('');
    });
  });
}

async function saveModel(effort) {
  const provider = document.querySelector(`.provider-select[data-effort="${effort}"]`).value;
  const model = document.querySelector(`.model-select[data-effort="${effort}"]`).value;
  const optionsStr = document.querySelector(`.options-input[data-effort="${effort}"]`).value;
  const status = document.getElementById(`status-${effort}`);

  let options = {};
  try {
    options = JSON.parse(optionsStr || '{}');
  } catch (e) {
    status.textContent = 'Invalid JSON in options';
    return;
  }

  status.textContent = 'Saving...';
  const res = await fetch('/api/config/models', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ effort, provider, model, options }),
  });
  const result = await res.json();

  if (result.status === 'ok') {
    status.textContent = `✓ Saved — heartbeat: ${result.heartbeat_restart}`;
  } else {
    status.textContent = `✗ ${result.error}`;
  }
}

// Load on page init
loadModelConfig();
</script>
```

---

## Deploy

```bash
# On NucBox:
cd ~/xibi
# Edit xibi/dashboard/app.py — add the 3 new endpoints
# Edit templates/index.html — add the model config card + JS
# Restart dashboard
systemctl --user restart xibi-dashboard
# Test: open http://100.125.95.42:8082 → model config card should appear
```

---

## What NOT to Change

- Do NOT implement hot-reload in the config module — the restart approach is safer and
  more predictable. Config is read once at startup; that's fine.
- Do NOT add authentication to these endpoints yet — the dashboard is only accessible
  via Tailscale (private network). Auth is a Phase E concern.
- Do NOT add provider credential editing — that's sensitive config that stays in
  `~/.xibi_env` and should not be exposed through a web UI.

---

## Risk Notes

- **Config corruption:** The PUT endpoint reads and writes the full config.json. If two
  writes happen simultaneously, last write wins. Acceptable risk for a single-operator
  system. Add file locking later if needed.
- **Heartbeat restart latency:** systemctl restart can take a few seconds. The dashboard
  UI should show a status indicator, not block on it.
- **Ollama model list:** The `/api/tags` endpoint returns all pulled models. Some might
  not work well for certain effort levels. Consider adding a `recommended_for` field in
  the future, but for now the operator (Daniel) knows which models work.
