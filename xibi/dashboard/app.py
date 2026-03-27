from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil
from flask import Flask, jsonify, render_template

import xibi.db
from xibi.dashboard import queries


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

    def get_db_conn() -> sqlite3.Connection:
        conn = xibi.db.open_db(app.config["DB_PATH"])
        return conn

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

    @app.route("/")
    def index() -> str:
        return render_template("index.html")

    return app
