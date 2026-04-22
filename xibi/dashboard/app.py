from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil
from flask import Flask, jsonify, render_template, request

import xibi.db
from xibi.dashboard import queries
from xibi.router import Config, get_model


def get_system_health(db_path: Path, config: Config) -> dict:
    checks = {}

    # 1. Database connectivity
    if not db_path.exists():
        checks["database"] = "error: database file missing"
    else:
        try:
            conn = sqlite3.connect(db_path, timeout=2)
            conn.execute("SELECT 1")
            conn.close()
            checks["database"] = "ok"
        except Exception as e:
            checks["database"] = f"error: {e}"

    # 2. Schema up to date
    try:
        from xibi.db.migrations import SCHEMA_VERSION, SchemaManager

        sm = SchemaManager(db_path)
        version = sm.get_version()
        checks["schema"] = "ok" if version == SCHEMA_VERSION else f"stale: v{version} (want v{SCHEMA_VERSION})"
    except Exception as e:
        checks["schema"] = f"error: {e}"

    # 3. At least one LLM provider reachable
    try:
        model = get_model(specialty="text", effort="fast", config=config)
        checks["llm_provider"] = "ok" if model else "no provider available"
    except Exception as e:
        checks["llm_provider"] = f"error: {e}"

    # 4. Skill registry has tools
    try:
        from xibi.skills.registry import SkillRegistry

        # Try to find skills dir relative to db_path or use default
        workdir = db_path.parent.parent
        skills_dir = workdir / "skills"
        if not skills_dir.exists():
            # fallback to repository root skills if we are in a dev environment
            skills_dir = Path(__file__).parent.parent.parent / "skills"

        registry = SkillRegistry(skills_dir)
        # SkillRegistry doesn't have list_tools(), but we can check manifests
        tools = []
        for skill in registry.get_skill_manifests():
            tools.extend(skill.get("tools", []))
        tool_count = len(tools)
        checks["skill_registry"] = f"ok ({tool_count} tools)"
    except Exception as e:
        checks["skill_registry"] = f"error: {e}"

    # 5. System resources
    checks["cpu_pct"] = psutil.cpu_percent()
    checks["ram_pct"] = psutil.virtual_memory().percent

    # Overall status: degraded if ANY check has "error"
    any_error = any("error" in str(v) for v in checks.values())
    checks["status"] = "degraded" if any_error else "healthy"
    return checks


@dataclass
class DashboardConfig:
    db_path: Path
    host: str = "127.0.0.1"
    port: int = 8081
    debug: bool = False


def create_app(config: DashboardConfig) -> Flask:
    # Resolve templates folder relative to the repo root
    # xibi/dashboard/app.py -> xibi/dashboard -> xibi -> root
    template_folder = Path(__file__).parent.parent.parent / "templates"
    app = Flask(__name__, template_folder=str(template_folder))
    app.config["DB_PATH"] = config.db_path

    def get_db_conn() -> AbstractContextManager[sqlite3.Connection]:
        return xibi.db.open_db(app.config["DB_PATH"])  # type: ignore[return-value]

    def _load_secrets(workdir: Any) -> dict:
        """Load secrets.env from ~/.xibi/secrets.env, return key→value dict."""
        import os

        secrets_path = os.path.expanduser("~/.xibi/secrets.env")
        result: dict = {}
        try:
            with open(secrets_path) as sf:
                for line in sf:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        result[k.strip()] = v.strip()
        except FileNotFoundError:
            pass
        return result

    @app.route("/health")
    def health_full() -> Any:
        # We need a Config object. Since create_app doesn't take it, but the health check needs it,
        # and the router can load it from default path if not provided.
        # However, the task description says get_system_health(db_path, config).
        # We can try to load config here.
        from xibi.router import load_config

        workdir = app.config["DB_PATH"].parent.parent
        config_path = workdir / "config.json"
        try:
            cfg = load_config(str(config_path)) if config_path.exists() else load_config()
        except Exception:
            # If config is missing or invalid, get_system_health will report it via LLM check
            cfg = {"models": {}, "providers": {}}  # type: ignore

        report = get_system_health(app.config["DB_PATH"], cfg)
        return jsonify(report)

    @app.route("/api/health")
    def health() -> Any:
        try:
            with get_db_conn() as conn:
                last_trace = queries.get_last_trace(conn)
                # uptime: seconds since earliest schema_version.applied_at
                cursor = conn.execute("SELECT MIN(applied_at) FROM schema_version")
                row = cursor.fetchone()
                if row and row[0]:
                    try:
                        # SQLite default format: 2026-03-25 02:14:00
                        start_time = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                        uptime = int((datetime.utcnow() - start_time).total_seconds())
                    except Exception:
                        uptime = 0
                else:
                    uptime = 0

                return jsonify(
                    {
                        "status": "ok",
                        "bot_alive": True,  # Standardize for dashboard
                        "system_ram_pct": psutil.virtual_memory().percent,
                        "last_active": last_trace["created_at"] if last_trace else "Never",
                        "model_loaded": last_trace["model"] if last_trace else "unknown",
                        "last_trace": last_trace["created_at"] if last_trace else None,
                        "model": last_trace["model"] if last_trace else "unknown",
                        "cpu_percent": psutil.cpu_percent(),
                        "ram_used_mb": psutil.virtual_memory().used / (1024 * 1024),
                        "ram_total_mb": psutil.virtual_memory().total / (1024 * 1024),
                        "uptime_seconds": uptime,
                    }
                )
        except Exception as e:
            return jsonify({"status": "degraded", "error": str(e)}), 200

    @app.route("/api/trends")
    def trends() -> Any:
        with get_db_conn() as conn:
            data = queries.get_conversation_trends(conn)
            return jsonify(data)

    @app.route("/api/errors")
    def errors() -> Any:
        with get_db_conn() as conn:
            data = queries.get_recent_errors(conn)
            return jsonify(data)

    @app.route("/api/recent")
    def recent() -> Any:
        with get_db_conn() as conn:
            data = queries.get_recent_conversations(conn)
            return jsonify(data)

    @app.route("/api/shadow")
    def shadow() -> Any:
        with get_db_conn() as conn:
            data = queries.get_shadow_stats(conn)
            return jsonify(data)

    @app.route("/api/signals")
    def signals() -> Any:
        with get_db_conn() as conn:
            return jsonify(
                {
                    "signals": queries.get_recent_signals(conn),
                    "active_threads": queries.get_active_threads(conn),
                }
            )

    @app.route("/api/signal_pipeline")
    def signal_pipeline() -> Any:
        with get_db_conn() as conn:
            data = queries.get_signal_pipeline(conn)
            return jsonify(data)

    @app.route("/api/inference")
    def inference() -> Any:
        with get_db_conn() as conn:
            return jsonify(queries.get_inference_stats(conn))

    @app.route("/api/trust")
    def trust() -> Any:
        with get_db_conn() as conn:
            return jsonify(queries.get_trust_records(conn))

    @app.route("/api/audit")
    def audit() -> Any:
        with get_db_conn() as conn:
            return jsonify(queries.get_audit_results(conn))

    @app.route("/api/spans")
    def spans() -> Any:
        with get_db_conn() as conn:
            return jsonify(queries.get_latest_spans(conn))

    @app.route("/api/cycles")
    def cycles() -> Any:
        with get_db_conn() as conn:
            return jsonify(queries.get_observation_cycles(conn))

    @app.route("/api/checklists")
    def checklists() -> Any:
        with get_db_conn() as conn:
            return jsonify(queries.get_checklists(conn))

    @app.route("/api/config/models", methods=["GET"])
    def get_model_config() -> Any:
        """Return current model assignments for all effort levels."""
        from xibi.router import load_config

        workdir = app.config["DB_PATH"].parent.parent
        config_path = workdir / "config.json"
        try:
            try:
                cfg = load_config(str(config_path)) if config_path.exists() else load_config()
            except Exception:
                # Config may reference providers not yet in providers section — read raw
                import json as _json

                with open(config_path) as _f:
                    cfg = _json.load(_f)
            models = cfg.get("models", {}).get("text", {})
            providers = set(cfg.get("providers", {}).keys())
            # Also detect cloud providers from secrets.env API keys
            secrets = _load_secrets(workdir)
            if secrets.get("GOOGLE_API_KEY") or secrets.get("GEMINI_API_KEY"):
                providers.add("gemini")
            if secrets.get("OPENAI_API_KEY"):
                providers.add("openai")
            if secrets.get("ANTHROPIC_API_KEY"):
                providers.add("anthropic")
            result: dict[str, Any] = {"assignments": {}, "available_providers": sorted(providers)}
            for effort in ["fast", "think", "review"]:
                role_config: Any = models.get(effort, {})
                result["assignments"][effort] = {
                    "provider": role_config.get("provider", ""),
                    "model": role_config.get("model", ""),
                    "options": role_config.get("options", {}),
                    "fallback": role_config.get("fallback"),
                }
            return jsonify(result)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/config/models", methods=["PUT"])
    def update_model_config() -> Any:
        """Update model assignment for a single effort level. Restarts heartbeat."""
        import json as json_mod
        import subprocess

        workdir = app.config["DB_PATH"].parent.parent
        config_path = workdir / "config.json"
        data = request.get_json()
        effort = data.get("effort")
        if effort not in ("fast", "think", "review"):
            return jsonify({"error": f"Invalid effort level: {effort}"}), 400
        provider = data.get("provider")
        model_name = data.get("model")
        if not provider or not model_name:
            return jsonify({"error": "provider and model are required"}), 400
        try:
            with open(config_path) as f:
                cfg = json_mod.load(f)
            # Accept providers from config.json OR secrets.env
            secrets = _load_secrets(workdir)
            known_providers = set(cfg.get("providers", {}).keys())
            if secrets.get("GOOGLE_API_KEY") or secrets.get("GEMINI_API_KEY"):
                known_providers.add("gemini")
            if secrets.get("OPENAI_API_KEY"):
                known_providers.add("openai")
            if secrets.get("ANTHROPIC_API_KEY"):
                known_providers.add("anthropic")
            if provider not in known_providers:
                return jsonify({"error": f"Unknown provider: {provider}"}), 400
            cfg.setdefault("models", {}).setdefault("text", {})
            role_config = cfg["models"]["text"].get(effort, {})
            role_config["provider"] = provider
            role_config["model"] = model_name
            if "options" in data:
                role_config["options"] = data["options"]
            if "fallback" not in role_config:
                defaults = {"fast": "think", "think": None, "review": None}
                role_config["fallback"] = defaults.get(effort)
            cfg["models"]["text"][effort] = role_config
            # Ensure provider exists in providers section (required by load_config validation)
            provider_defaults = {
                "anthropic": {"api_key_env": "ANTHROPIC_API_KEY"},
                "openai": {"api_key_env": "OPENAI_API_KEY"},
                "gemini": {"api_key_env": "GEMINI_API_KEY"},
            }
            if provider not in cfg.get("providers", {}):
                cfg.setdefault("providers", {})[provider] = provider_defaults.get(provider, {})
            with open(config_path, "w") as f:
                json_mod.dump(cfg, f, indent=2)
            restart_msg = "not attempted"
            try:
                result = subprocess.run(
                    ["systemctl", "--user", "restart", "xibi-heartbeat"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                restart_msg = "restarted" if result.returncode == 0 else f"failed: {result.stderr}"
            except subprocess.TimeoutExpired:
                restart_msg = "restart initiated (timeout waiting)"
            except Exception as e:
                restart_msg = f"error: {e}"
            return jsonify({"status": "ok", "updated": {effort: role_config}, "heartbeat_restart": restart_msg})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/config/available_models", methods=["GET"])
    def get_available_models() -> Any:
        """List models available from each provider."""
        import json as json_mod

        workdir = app.config["DB_PATH"].parent.parent
        config_path = workdir / "config.json"
        with open(config_path) as f:
            cfg = json_mod.load(f)
        providers = cfg.get("providers", {})
        secrets = _load_secrets(workdir)
        available: dict[str, Any] = {}
        if "ollama" in providers:
            try:
                import requests as req

                base_url = providers["ollama"].get("base_url", "http://localhost:11434")
                resp = req.get(f"{base_url}/api/tags", timeout=5)
                if resp.status_code == 200:
                    models = resp.json().get("models", [])
                    available["ollama"] = [{"name": m["name"], "size": m.get("size", 0)} for m in models]
            except Exception as e:
                available["ollama"] = {"error": str(e)}
        if secrets.get("GOOGLE_API_KEY") or secrets.get("GEMINI_API_KEY"):
            # Suggestions only — user can type any valid model name in the UI
            available["gemini"] = [
                {"name": "gemini-3.1-pro-preview"},
                {"name": "gemini-3-flash-preview"},
                {"name": "gemini-3.1-flash-lite-preview"},
                {"name": "gemini-2.5-pro"},
                {"name": "gemini-2.5-flash"},
            ]
        if secrets.get("ANTHROPIC_API_KEY"):
            available["anthropic"] = [
                {"name": "claude-opus-4-6"},
                {"name": "claude-sonnet-4-6"},
                {"name": "claude-haiku-4-5-20251001"},
            ]
        if secrets.get("OPENAI_API_KEY"):
            available["openai"] = [
                {"name": "gpt-5.4"},
                {"name": "gpt-5.4-mini"},
                {"name": "gpt-5.4-nano"},
                {"name": "gpt-5.4-pro"},
                {"name": "o3"},
                {"name": "o4-mini"},
            ]
        return jsonify(available)

    @app.route("/")
    def index() -> str:
        return render_template("index.html")

    @app.route("/caretaker")
    def caretaker_page() -> str:
        return render_template("caretaker.html")

    @app.route("/api/caretaker/pulses")
    def caretaker_pulses() -> Any:
        limit = int(request.args.get("limit", "20"))
        from xibi.caretaker.pulse import Caretaker

        workdir = app.config["DB_PATH"].parent.parent
        ct = Caretaker(db_path=app.config["DB_PATH"], workdir=workdir)
        return jsonify({"pulses": ct.recent_pulses(limit=limit)})

    @app.route("/api/caretaker/drift")
    def caretaker_drift() -> Any:
        from xibi.caretaker import dedup as _dedup

        return jsonify({"active": _dedup.list_active(app.config["DB_PATH"])})

    @app.route("/api/caretaker/run", methods=["POST"])
    def caretaker_run() -> Any:
        """Run one pulse on demand (dashboard button)."""
        from xibi.caretaker.pulse import Caretaker

        workdir = app.config["DB_PATH"].parent.parent
        ct = Caretaker(db_path=app.config["DB_PATH"], workdir=workdir)
        result = ct.pulse()
        return jsonify(
            {
                "pulse_id": result.pulse_id,
                "status": result.status,
                "findings_count": len(result.findings),
                "repeat_count": len(result.repeats),
                "resolved_count": len(result.resolved_keys),
                "duration_ms": result.duration_ms,
            }
        )

    return app
