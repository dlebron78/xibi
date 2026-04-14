"""
bregger_dashboard.py — Read-only Observability Dashboard for Bregger

Serves a tiny Flask app that queries bregger.db and provides JSON endpoints
for a Chart.js frontend. Runs on the NucBox on port 8081 (Tailscale only).
"""

import json
import os
import sqlite3
import sys
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta

import psutil
from flask import Flask, jsonify, render_template, request

from bregger_utils import normalize_topic

# Make sure bregger_core can be imported if running from the deployment dir
deploy_dir = os.environ.get("XIBI_DEPLOY_DIR", os.path.join(os.path.expanduser("~"), "bregger_deployment"))
if deploy_dir not in sys.path:
    sys.path.append(deploy_dir)

try:
    from bregger_core import BreggerRouter

    SYSTEM_PROMPT = BreggerRouter.REACT_SYSTEM_PROMPT
except Exception as e:
    SYSTEM_PROMPT = f"⚠️ Could not load system prompt: {e}"

app = Flask(__name__)
DB_PATH = os.path.join(
    os.environ.get("XIBI_DATA_DIR", os.path.join(os.path.expanduser("~"), "bregger_remote")), "data", "bregger.db"
)


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------


@app.route("/api/health")
def api_health():
    """System health metrics (process, RAM, uptime)."""
    try:
        with get_db() as conn:
            # Active model is in the most recent trace
            row = conn.execute("SELECT model, created_at FROM traces ORDER BY created_at DESC LIMIT 1").fetchone()
            last_trace = row["created_at"] if row else "Never"
            if row and row["model"]:
                model = row["model"]
            else:
                try:
                    config_path = os.path.join(
                        os.environ.get("XIBI_DATA_DIR", os.path.join(os.path.expanduser("~"), "bregger_remote")),
                        "config.json",
                    )
                    with open(config_path) as _f:
                        model = json.load(_f).get("llm", {}).get("model", "unknown")
                except Exception:
                    model = "unknown"

            # Check if bregger_core process is alive (naive check by name)
            bot_alive = any(
                "bregger" in p.name().lower() or "bregger" in " ".join(p.cmdline()).lower()
                for p in psutil.process_iter(["name", "cmdline"])
                if p.pid != psutil.Process().pid
            )

            return jsonify(
                {
                    "status": "ok",
                    "bot_alive": bot_alive,
                    "system_ram_pct": psutil.virtual_memory().percent,
                    "model_loaded": model,
                    "last_active": last_trace,
                }
            )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/trends")
def api_trends():
    """Time-series data for the last 48 hours."""
    try:
        with get_db() as conn:
            # Get all completed/error traces from last 48h
            cutoff = (datetime.utcnow() - timedelta(hours=48)).isoformat()
            cursor = conn.execute(
                """
                SELECT created_at, total_ms, overall_tok_per_sec, ram_end_pct, step_count, status
                FROM traces
                WHERE created_at > ? AND status IN ('completed', 'error')
                ORDER BY created_at ASC
            """,
                (cutoff,),
            )

            rows = cursor.fetchall()

            labels = [r["created_at"].replace(" ", "T") + "Z" for r in rows]  # Full ISO timestamps for UI time-axis
            latency = [r["total_ms"] for r in rows]
            tok_sec = [r["overall_tok_per_sec"] for r in rows]
            ram = [r["ram_end_pct"] for r in rows]
            steps = [r["step_count"] for r in rows]
            errors = [1 if r["status"] == "error" else 0 for r in rows]

            return jsonify(
                {
                    "labels": labels,
                    "latency_ms": latency,
                    "tok_per_sec": tok_sec,
                    "ram_pct": ram,
                    "steps": steps,
                    "errors": errors,
                    "total_requests_48h": len(rows),
                    "error_rate_pct": round((sum(errors) / len(rows) * 100), 1) if rows else 0,
                }
            )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/errors")
def api_errors():
    """Top extracted JSON errors from steps_detail."""
    try:
        with get_db() as conn:
            # Requires SQLite 3.38+ for json_extract `->>` operator
            cursor = conn.execute("""
                SELECT
                    je.value->>'error' as err_msg,
                    je.value->>'tool' as tool,
                    COUNT(*) as freq
                FROM traces, json_each(steps_detail) je
                WHERE je.value->>'error' IS NOT NULL
                GROUP BY err_msg, tool
                ORDER BY freq DESC
                LIMIT 10
            """)
            rows = cursor.fetchall()
            return jsonify([dict(row) for row in rows])
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/recent")
def api_recent():
    """Last N traces for the table view with pagination support."""
    try:
        limit = request.args.get("limit", default=15, type=int)
        offset = request.args.get("offset", default=0, type=int)
        with get_db() as conn:
            cursor = conn.execute(
                """
                SELECT *
                FROM traces
                WHERE intent NOT IN ('react_finish', 'passive_memory', 'control_plane_metric')
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """,
                (limit, offset),
            )
            rows = []
            for r in cursor.fetchall():
                d = dict(r)
                if isinstance(d.get("steps_detail"), str):
                    try:
                        d["steps_detail"] = json.loads(d["steps_detail"])
                    except:
                        d["steps_detail"] = []
                rows.append(d)
            return jsonify(rows)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/shadow")
def api_shadow():
    """Shadow Tier 2 accuracy and readiness metrics."""
    try:
        with get_db() as conn:
            # 1. Per-tool accuracy from shadow_phrases
            cursor = conn.execute("""
                SELECT tool, SUM(hits) as total_hits, SUM(correct) as total_correct
                FROM shadow_phrases
                GROUP BY tool
                ORDER BY total_hits DESC
            """)
            tool_stats = []
            for r in cursor.fetchall():
                acc = (r["total_correct"] / r["total_hits"]) if r["total_hits"] > 0 else 0
                tool_stats.append(
                    {
                        "tool": r["tool"],
                        "hits": r["total_hits"],
                        "correct": r["total_correct"],
                        "accuracy": round(acc, 2),
                        "ready": acc >= 0.90 and r["total_hits"] >= 10,
                    }
                )

            # 2. Recent shadow results from traces
            cursor = conn.execute("""
                SELECT created_at, intent, tier2_shadow
                FROM traces
                WHERE tier2_shadow IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 10
            """)
            recent_matches = []
            for r in cursor.fetchall():
                shadow_data = json.loads(r["tier2_shadow"])
                recent_matches.append(
                    {
                        "time": r["created_at"][11:16],
                        "intent": r["intent"],
                        "predicted": shadow_data.get("predicted_tool"),
                        "match": shadow_data.get("match", False),
                        "score": shadow_data.get("score"),
                    }
                )

            return jsonify({"tool_stats": tool_stats, "recent_matches": recent_matches})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------


@app.route("/api/signals")
def api_signals():
    """Returns top active threads, signal volume, and recent signals with normalization."""
    try:
        with get_db() as conn:
            # 1. Fetch all recent signals with a topic for aggregation
            cursor = conn.execute("""
                SELECT topic_hint, timestamp
                FROM signals
                WHERE topic_hint IS NOT NULL
                  AND timestamp > datetime('now', '-7 days')
            """)
            rows = cursor.fetchall()

            normalized_counts = {}
            last_seen = {}
            for r in rows:
                topic = r["topic_hint"]
                ts = r["timestamp"]
                norm = normalize_topic(topic)
                if not norm:
                    continue
                normalized_counts[norm] = normalized_counts.get(norm, 0) + 1
                if norm not in last_seen or ts > last_seen[norm]:
                    last_seen[norm] = ts

            active_threads = []
            for topic, count in normalized_counts.items():
                if count > 1:
                    active_threads.append(
                        {"topic": topic, "count": count, "last_seen": last_seen[topic], "pinned": False}
                    )
            active_threads = sorted(active_threads, key=lambda x: x["count"], reverse=True)[:5]

            # 1b. Fetch explicitly pinned topics
            pinned = []
            try:
                cursor = conn.execute("SELECT topic FROM pinned_topics")
                for row in cursor.fetchall():
                    # Check if it's already in active_threads to merge counts
                    existing = next(
                        (t for t in active_threads if normalize_topic(t["topic"]) == normalize_topic(row["topic"])),
                        None,
                    )
                    if existing:
                        existing["pinned"] = True
                    else:
                        pinned.append({"topic": row["topic"], "count": "-", "last_seen": "pinned", "pinned": True})
            except sqlite3.OperationalError:
                pass  # Pinned topics feature might not be migrated yet on older DBs

            # Combine them: Pinned first, then active
            final_threads = pinned + [
                t for t in active_threads if not t.get("pinned", False) or t in pinned is False
            ]  # keep existing pinned
            # Deduplicate by normalized topic to be safe
            seen = set()
            deduped = []
            for t in final_threads:
                norm = normalize_topic(t["topic"])
                if norm not in seen:
                    seen.add(norm)
                    deduped.append(t)

            # 2. Total signal volume by source
            cursor = conn.execute("""
                SELECT source, COUNT(*) as count
                FROM signals
                WHERE timestamp > datetime('now', '-7 days')
                GROUP BY source
            """)
            volume = {r["source"]: r["count"] for r in cursor.fetchall()}

            # 3. Recent raw signals (apply normalization for UI consistency)
            cursor = conn.execute("""
                SELECT timestamp, source, topic_hint, content_preview
                FROM signals
                ORDER BY timestamp DESC
                LIMIT 10
            """)
            recent_signals = []
            for r in cursor.fetchall():
                recent_signals.append(
                    {
                        "timestamp": r["timestamp"],
                        "source": r["source"],  # e.g. 'chat' or 'email'
                        "topic": normalize_topic(r["topic_hint"]) if r["topic_hint"] else None,
                        "preview": r["content_preview"],
                    }
                )

            return jsonify({"active_threads": deduped, "volume": volume, "recent_signals": recent_signals})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/signal_pipeline")
def api_signal_pipeline():
    """Phase 1.75 signal pipeline metrics: batch extraction success rate + reflection synthesis breakdown."""
    try:
        with get_db() as conn:
            # 1. Batch extraction success rate (last 48h)
            cutoff = (datetime.utcnow() - timedelta(hours=48)).isoformat()
            cursor = conn.execute(
                """
                SELECT plan FROM traces
                WHERE intent = 'batch_extract' AND created_at > ?
                ORDER BY created_at DESC
            """,
                (cutoff,),
            )

            batch_rows = []
            for r in cursor.fetchall():
                with suppress(Exception):
                    batch_rows.append(json.loads(r["plan"]))

            total_ticks = len(batch_rows)
            total_emails = sum(b.get("total_emails", 0) for b in batch_rows)
            total_llm = sum(b.get("llm_extracted", 0) for b in batch_rows)
            total_regex = sum(b.get("regex_fallback", 0) for b in batch_rows)
            avg_success = round(total_llm / total_emails * 100, 1) if total_emails > 0 else 0

            # Per-tick trend (last 20 ticks)
            extraction_trend = []
            for b in batch_rows[:20]:
                extraction_trend.append(
                    {
                        "total": b.get("total_emails", 0),
                        "llm": b.get("llm_extracted", 0),
                        "regex": b.get("regex_fallback", 0),
                        "rate": b.get("success_rate_pct", 0),
                    }
                )

            # 2. Reflection synthesis breakdown (last 7 days)
            cutoff_7d = (datetime.utcnow() - timedelta(days=7)).isoformat()
            cursor = conn.execute(
                """
                SELECT plan FROM traces
                WHERE intent = 'reflection' AND created_at > ?
            """,
                (cutoff_7d,),
            )

            llm_count = 0
            freq_count = 0
            none_count = 0
            for r in cursor.fetchall():
                try:
                    plan = json.loads(r["plan"])
                    synthesis = plan.get("synthesis", "none")
                    if synthesis == "llm":
                        llm_count += 1
                    elif synthesis == "frequency":
                        freq_count += 1
                    else:
                        none_count += 1
                except Exception:
                    pass

            # 3. Signal source quality — compare topics with/without entities
            cursor = conn.execute("""
                SELECT
                    source,
                    COUNT(*) as total,
                    SUM(CASE WHEN entity_text IS NOT NULL THEN 1 ELSE 0 END) as with_entity,
                    SUM(CASE WHEN topic_hint IS NOT NULL THEN 1 ELSE 0 END) as with_topic
                FROM signals
                WHERE timestamp > datetime('now', '-7 days')
                GROUP BY source
            """)
            quality = {
                r["source"]: {
                    "total": r["total"],
                    "with_entity": r["with_entity"],
                    "with_topic": r["with_topic"],
                    "entity_rate_pct": round(r["with_entity"] / r["total"] * 100, 1) if r["total"] > 0 else 0,
                    "topic_rate_pct": round(r["with_topic"] / r["total"] * 100, 1) if r["total"] > 0 else 0,
                }
                for r in cursor.fetchall()
            }

            return jsonify(
                {
                    "batch_extraction": {
                        "ticks_48h": total_ticks,
                        "total_emails": total_emails,
                        "llm_extracted": total_llm,
                        "regex_fallback": total_regex,
                        "avg_success_rate_pct": avg_success,
                        "trend": extraction_trend,
                    },
                    "reflection": {
                        "llm_synthesis": llm_count,
                        "frequency_fallback": freq_count,
                        "no_proposal": none_count,
                        "total": llm_count + freq_count + none_count,
                    },
                    "signal_quality": quality,
                }
            )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/template")
def api_template():
    """Return the raw system prompt template."""
    return jsonify({"template": SYSTEM_PROMPT})


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    """GET current escalation threshold or POST to update it."""
    try:
        # Load config.json
        config_path = os.path.join(
            os.environ.get("XIBI_DATA_DIR", os.path.join(os.path.expanduser("~"), "bregger_remote")), "config.json"
        )

        if os.path.exists(config_path):
            with open(config_path) as f:
                config = json.load(f)
        else:
            config = {}

        if request.method == "POST":
            data = request.get_json()
            if data and "threshold_pct" in data:
                if "escalation" not in config:
                    config["escalation"] = {}
                config["escalation"]["threshold_pct"] = int(data["threshold_pct"])

                with open(config_path, "w") as f:
                    json.dump(config, f, indent=4)
                return jsonify({"status": "ok", "threshold_pct": config["escalation"]["threshold_pct"]})
            return jsonify({"status": "error", "message": "Missing threshold_pct"}), 400

        # GET
        threshold = config.get("escalation", {}).get("threshold_pct", 70)
        return jsonify({"threshold_pct": threshold})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    # Bind to all interfaces (Tailscale will route it securely)
    # SearXNG uses 8080, so we use 8081
    app.run(host="0.0.0.0", port=8081)
