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
            data = queries.get_recent_signals(conn)
            return jsonify(data)

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

    @app.route("/")
    def index() -> str:
        return render_template("index.html")

    return app
