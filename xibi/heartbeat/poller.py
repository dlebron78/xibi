from __future__ import annotations

import importlib.util
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import xibi.db
import xibi.signal_intelligence as sig_intel
from xibi.alerting.rules import RuleEngine
from xibi.channels.sheets import SheetsExporter
from xibi.channels.telegram import TelegramAdapter
from xibi.command_layer import CommandLayer
from xibi.heartbeat.classification import build_classification_prompt
from xibi.heartbeat.contact_poller import backfill_contacts, find_himalaya, poll_sent_folder
from xibi.heartbeat.extractors import SignalExtractorRegistry
from xibi.heartbeat.review_cycle import execute_review, run_review_cycle
from xibi.heartbeat.sender_trust import (
    _extract_sender_addr,
    _extract_sender_name,
    assess_sender_trust,
)
from xibi.heartbeat.source_poller import SourcePoller
from xibi.heartbeat.tier2_extractors import _emit_tier2_span
from xibi.observation import ObservationCycle
from xibi.radiant import Radiant
from xibi.router import get_model
from xibi.security import trust_gate
from xibi.threads import sweep_resolved_threads, sweep_stale_threads

if TYPE_CHECKING:
    from xibi.heartbeat.context_assembly import SignalContext
    from xibi.trust.gradient import TrustGradient

# Jules watcher — lazy import to avoid hard dependency if Jules not configured
_JulesWatcher = None

# Timeout constants for async_tick phases
_PHASE0_TIMEOUT_SECS = 90  # source polling (MCP + email + JobSpy)
_PHASE1_TIMEOUT_SECS = 10  # DB read (tasks, seen_ids, triage_rules)
_PHASE15_TIMEOUT_SECS = 60  # scheduled actions
_PHASE2_TIMEOUT_SECS = 60  # signal extraction + classification loop
_PHASE3_TIMEOUT_SECS = 180  # signal_intelligence + observation + Jules + Radiant

logger = logging.getLogger(__name__)


class HeartbeatPoller:
    def __init__(
        self,
        skills_dir: Path,
        db_path: Path,
        adapter: TelegramAdapter,
        rules: RuleEngine,
        allowed_chat_ids: list[int],
        interval_minutes: int = 15,
        quiet_start: int = 23,
        quiet_end: int = 8,
        observation_cycle: ObservationCycle | None = None,
        profile: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
        signal_intelligence_enabled: bool = True,
        radiant: Radiant | None = None,
        config_path: str | None = None,
        executor: Any | None = None,
        *,
        trust_gradient: TrustGradient | None = None,
    ) -> None:
        self.skills_dir = skills_dir
        self.db_path = db_path
        self.adapter = adapter
        self.rules = rules
        self.allowed_chat_ids = allowed_chat_ids
        self.interval_minutes = interval_minutes
        self.quiet_start = quiet_start
        self.quiet_end = quiet_end
        self.observation_cycle = observation_cycle
        self.config = config or {}
        self.profile = profile or self.config.get("profile", {})
        self.config_path = config_path or str(Path.home() / ".xibi" / "config.json")
        self.signal_intelligence_enabled = signal_intelligence_enabled
        self.radiant = radiant
        self.executor = executor

        if trust_gradient is None and self.db_path is not None:
            from xibi.trust.gradient import TrustGradient

            self.trust_gradient = TrustGradient(self.db_path)
        else:
            self.trust_gradient = trust_gradient

        self._last_reflection_date: Any = None  # Tracks date as string or None
        self._audit_tick_counter = 0
        self._jules_watcher = self._init_jules_watcher()
        _mcp_exec = getattr(self.executor, "mcp_executor", None)
        self.source_poller = SourcePoller(
            config=self.config,
            executor=self.executor,
            mcp_registry=_mcp_exec.registry if _mcp_exec is not None else None,
        )
        self.sheets_exporter = SheetsExporter(self.config.get("sheets_export", {}))

        from xibi.scheduling import ScheduledActionKernel
        from xibi.tracing import Tracer

        if self.db_path:
            from xibi.checklists import initialize_checklists

            initialize_checklists(self.db_path)

        self.tracer = Tracer(self.db_path) if self.db_path else None
        from xibi.heartbeat.rich_nudge import NudgeRateLimiter

        nudge_config = self.config.get("nudge", {})
        self._nudge_limiter = NudgeRateLimiter(max_per_hour=nudge_config.get("max_urgent_per_hour", 3))
        self.nudge_model = nudge_config.get("model", "gemma4:e4b")
        self.nudge_timeout_ms = nudge_config.get("timeout_ms", 3000)
        self.headless = nudge_config.get("headless", False)
        self._digest_overflow: list[dict] = []
        self._pending_nudges: list[dict] = []
        self._pending_nudge_context: dict | None = None
        self._enable_legacy_digest = self.config.get("enable_legacy_digest", False)
        self._timezone_name = self.config.get("timezone", "UTC")
        self._validate_timezone()

        self.scheduler_kernel: ScheduledActionKernel | None
        if self.executor is not None:
            self.scheduler_kernel = ScheduledActionKernel(
                db_path=self.db_path,
                executor=self.executor,
                trust_gradient=self.trust_gradient,
                tracer=self.tracer,
            )
        else:
            self.scheduler_kernel = None

    def _init_jules_watcher(self) -> Any | None:
        """Set up JulesWatcher if JULES_API_KEY is configured."""
        api_key = None
        xibi_env = Path.home() / ".xibi_env"
        if xibi_env.exists():
            for line in xibi_env.read_text().splitlines():
                if line.startswith("JULES_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        if not api_key:
            api_key = os.environ.get("JULES_API_KEY", "")
        if not api_key:
            return None
        history_file = Path.home() / ".jules_trigger_state" / "history.jsonl"
        try:
            from xibi.heartbeat.jules_watcher import JulesWatcher

            llm = get_model(effort="fast", config_path=self.config_path)
            return JulesWatcher(
                api_key=api_key,
                history_file=history_file,
                llm=llm,
                broadcast_fn=self._broadcast,
            )
        except Exception as e:
            logger.warning("Failed to init JulesWatcher: %s", e)
            return None

    def _validate_timezone(self) -> None:
        """Ensure the configured timezone is valid."""
        import zoneinfo

        try:
            zoneinfo.ZoneInfo(self._timezone_name)
        except Exception:
            logger.warning(f"⚠️ Invalid timezone '{self._timezone_name}' in config. Defaulting review cycle to UTC.")
            self._timezone_name = "UTC"

    def _broadcast(self, text: str, nudge: Any | None = None) -> None:
        """Send nudge via Telegram, or store for headless mode."""
        if self.headless:
            # Store nudge for later retrieval
            self._pending_nudges.append(
                {
                    "text": text,
                    "signal_id": nudge.signal_id if nudge else None,
                    "actions": nudge.actions if nudge else [],
                    "ref_id": nudge.ref_id if nudge else None,
                    "timestamp": datetime.now().isoformat(),
                }
            )
            logger.info(f"Headless mode: queued nudge ({len(self._pending_nudges)} pending)")
            return

        for chat_id in self.allowed_chat_ids:
            try:
                self.adapter.send_message(chat_id, text)
                logger.info(f"Broadcast to {chat_id}: {text[:80]}...")

                # Record nudge in session history so Roberto remembers it
                try:
                    from datetime import date as _date

                    from xibi.session import SessionContext

                    sid = f"telegram:{chat_id}:{_date.today().isoformat()}"
                    ctx = SessionContext(session_id=sid, db_path=self.db_path)
                    ctx.add_nudge_turn(text)
                except Exception:
                    logger.debug("Could not record nudge in session", exc_info=True)
            except Exception as e:
                logger.warning(f"Failed to broadcast to {chat_id}: {e}", exc_info=True)

    def _is_quiet_hours(self) -> bool:
        hour = datetime.now().hour
        if self.quiet_start > self.quiet_end:
            return hour >= self.quiet_start or hour < self.quiet_end
        if self.quiet_start == self.quiet_end:
            return False
        return self.quiet_start <= hour < self.quiet_end

    def _run_tool(self, tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            # Find the tool in any skill subdirectory
            for skill_path in self.skills_dir.iterdir():
                if not skill_path.is_dir():
                    continue
                tool_path = skill_path / "tools" / f"{tool_name}.py"
                if tool_path.exists():
                    logger.info(f"Invoking tool {tool_name} from {tool_path}")
                    spec = importlib.util.spec_from_file_location(tool_name, tool_path)
                    if not spec or not spec.loader:
                        continue
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    if hasattr(module, "run"):
                        res = module.run(params)  # type: ignore
                        return dict(res)
            return {"error": f"Tool {tool_name} not found in {self.skills_dir}"}
        except Exception as e:
            logger.error(f"Error running tool {tool_name}: {e}", exc_info=True)
            return {"error": str(e)}

    def _check_email(self) -> list[dict[str, Any]]:
        result = self._run_tool("list_unread", {})
        if "error" in result:
            logger.warning(f"Email check failed: {result['error']}")
            return []
        emails = result.get("emails", [])
        return list(emails)

    def _classify_signal(self, signal: dict[str, Any], context: SignalContext | None = None) -> tuple[str, str | None]:
        from xibi.condensation import condense
        from xibi.heartbeat.classification import (
            build_fallback_prompt,
            parse_classification_response,
        )

        body = signal.get("body", signal.get("text", ""))
        body_preview = ""
        if body:
            cc = condense(body, source="email", ref_id=signal.get("id"))
            if cc.phishing_flag:
                logger.info(f"Auto-noise: phishing detected in {signal.get('id')}: {cc.phishing_reason}")
                return "NOISE", "Phishing detected"
            body_preview = cc.condensed[:500]

        if context:
            prompt = build_classification_prompt(signal, context)
        else:
            prompt = build_fallback_prompt(signal)
            if body_preview:
                prompt += f"\nBody preview:\n{body_preview}"

        try:
            from xibi.router import set_trace_context

            set_trace_context(trace_id=None, span_id=None, operation="heartbeat_signal_classify")
            model = get_model(effort="fast", config_path=self.config_path)
            response = model.generate(prompt, max_tokens=30).strip()
            return parse_classification_response(response)
        except Exception as e:
            logger.warning(f"LLM classification error: {e}", exc_info=True)
            return "MEDIUM", None

    # Deprecated alias — will be removed in step-77
    _classify_email = _classify_signal

    def _should_escalate(self, verdict: str, topic: str, subject: str, priority_topics: list[str]) -> tuple[str, str]:
        """Escalate verdict based on thread/pinned topic match."""
        if verdict not in ("MEDIUM", "LOW", "DIGEST"):
            return verdict, subject

        if any(pt.lower() in topic.lower() for pt in priority_topics):
            esc_map = {"LOW": "MEDIUM", "MEDIUM": "HIGH", "DIGEST": "URGENT"}
            new_verdict = esc_map.get(verdict, verdict)
            prefix = "[Priority Topic]" if verdict != "DIGEST" else "[Escalated]"
            return new_verdict, f"{prefix} {subject}"

        return verdict, subject

    def tick(self) -> None:
        import asyncio

        asyncio.run(self.async_tick())

    def _sweep_parsed_body(self) -> None:
        """Run the smart-parser body-retention sweep (step-114).

        Best-effort hourly prune of ``signals.parsed_body`` rows older than
        the 30-day TTL. Gating + error handling live in
        :func:`xibi.heartbeat.parsed_body_sweep.maybe_run_parsed_body_sweep`;
        this wrapper only translates an unexpected exception into a warning
        so the heartbeat tick is never broken by the sweep.
        """
        try:
            from xibi.heartbeat.parsed_body_sweep import maybe_run_parsed_body_sweep

            maybe_run_parsed_body_sweep(self.db_path)
        except Exception as exc:
            logger.warning(f"parsed_body sweep wrapper error: {exc}", exc_info=True)

    def _sweep_thread_lifecycle(self) -> None:
        """Mark stale/resolved threads. Runs once per day."""
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            with xibi.db.open_db(self.db_path) as conn:
                cursor = conn.execute("SELECT value FROM heartbeat_state WHERE key = 'thread_sweep_last_run'")
                row = cursor.fetchone()
                if row and row[0] == today:
                    return
                conn.execute(
                    "INSERT OR REPLACE INTO heartbeat_state (key, value) VALUES ('thread_sweep_last_run', ?)",
                    (today,),
                )
        except Exception as e:
            logger.warning(f"Thread sweep gate error: {e}", exc_info=True)
            return

        try:
            stale = sweep_stale_threads(self.db_path)
            resolved = sweep_resolved_threads(self.db_path)
            if stale + resolved > 0:
                logger.info(f"Thread sweep: {stale} stale, {resolved} resolved")
        except Exception as e:
            logger.warning(f"Thread lifecycle sweep failed: {e}", exc_info=True)

    async def _check_contact_backfill(self) -> None:
        """Perform one-time contact backfill if needed."""
        try:
            import asyncio

            with xibi.db.open_db(self.db_path) as conn:
                cursor = conn.execute("SELECT COUNT(*) FROM contacts")
                contact_count = cursor.fetchone()[0]
                cursor = conn.execute("SELECT value FROM heartbeat_state WHERE key = 'contacts_backfilled'")
                backfill_done = cursor.fetchone()

            if contact_count == 0 and not backfill_done:
                logger.info("📇 First run: backfilling contacts from last 90 days of mail...")
                himalaya_bin = find_himalaya()
                # Run sync backfill in a thread
                result = await asyncio.to_thread(backfill_contacts, himalaya_bin, self.db_path, days_back=90)
                logger.info(
                    f"📇 Backfill complete: {result.get('sent_scanned', 0)} sent + "
                    f"{result.get('received_scanned', 0)} received scanned"
                )
                with xibi.db.open_db(self.db_path) as conn, conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO heartbeat_state (key, value) VALUES ('contacts_backfilled', ?)",
                        (datetime.now().isoformat(),),
                    )
        except Exception as e:
            logger.warning(f"Contact backfill check failed: {e}", exc_info=True)

    async def _check_sent_mail_poll(self) -> None:
        """Poll sent mail hourly to update contact graph."""
        try:
            import asyncio

            now = datetime.now()
            run_poll = False
            with xibi.db.open_db(self.db_path) as conn:
                cursor = conn.execute("SELECT value FROM heartbeat_state WHERE key = 'last_sent_poll_at'")
                row = cursor.fetchone()
                if not row:
                    run_poll = True
                else:
                    last_poll = datetime.fromisoformat(row[0])
                    if now - last_poll > timedelta(hours=1):
                        run_poll = True

            if run_poll:
                logger.info("📇 Polling sent mail for new contacts...")
                himalaya_bin = find_himalaya()
                await asyncio.to_thread(poll_sent_folder, himalaya_bin, self.db_path)
                with xibi.db.open_db(self.db_path) as conn, conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO heartbeat_state (key, value) VALUES ('last_sent_poll_at', ?)",
                        (now.isoformat(),),
                    )
        except Exception as e:
            logger.warning(f"Sent mail poll check failed: {e}", exc_info=True)

    async def async_tick(self) -> None:
        """Tick: poll sources → extract signals → intel → observation → digest."""
        import asyncio

        if self._is_quiet_hours():
            logger.info("Quiet hours, skipping tick.")
            return

        self._sweep_thread_lifecycle()
        self._sweep_parsed_body()
        await self._check_contact_backfill()
        await self._check_sent_mail_poll()

        # Phase 0: Multi-source polling
        poll_results: list = []
        try:
            poll_results = await asyncio.wait_for(
                self.source_poller.poll_due_sources(),
                timeout=_PHASE0_TIMEOUT_SECS,
            )
        except asyncio.TimeoutError:
            logger.warning("Phase 0 timeout (%ds): source polling exceeded limit", _PHASE0_TIMEOUT_SECS)
        except Exception as e:
            logger.warning("Phase 0 error: %s", e, exc_info=True)

        # Phase 1: Task Reminders & Rules Load
        due_tasks: list = []
        seen_ids: set[str] = set()
        triage_rules: dict[str, str] = {}
        email_rules = self.rules.load_rules("email_alert")

        try:
            with xibi.db.open_db(self.db_path) as conn, conn:
                conn.execute(
                    "INSERT OR REPLACE INTO heartbeat_state (key, value) VALUES ('tick_lock', ?)",
                    (str(time.time()),),
                )
                conn.row_factory = sqlite3.Row
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'")
                if cursor.fetchone():
                    due_tasks = conn.execute(
                        "SELECT id, goal FROM tasks WHERE status IN ('pending', 'due') AND due <= ?", (now,)
                    ).fetchall()
                seen_ids = self.rules.get_seen_ids_with_conn(conn)
                triage_rules = self.rules.load_triage_rules_with_conn(conn)
        except Exception as e:
            logger.warning(f"Phase 1 error: {e}", exc_info=True)

        for task in due_tasks:
            self._broadcast(f"⏰ Task reminder: {task['goal']} (ID: {task['id']})")

        # Phase 1.5: Scheduled actions
        try:
            # kernel.tick is sync; it uses internal per-action timeouts to
            # stay under the Phase 1.5 budget. getattr is used so test fixtures
            # that bypass __init__ (HeartbeatPoller.__new__) can run without
            # needing to set scheduler_kernel.
            kernel = getattr(self, "scheduler_kernel", None)
            if kernel is not None:
                kernel.tick()
        except Exception as e:
            logger.warning("Phase 1.5 error (Scheduler kernel): %s", e, exc_info=True)

        # Phase 2: Signal Extraction and Classification
        phase2_deadline = time.monotonic() + _PHASE2_TIMEOUT_SECS
        for idx, result in enumerate(poll_results):
            if time.monotonic() > phase2_deadline:
                remaining_count = len(poll_results) - idx
                logger.warning(
                    "Phase 2 timeout: extraction loop exceeded %ds, %d sources skipped",
                    _PHASE2_TIMEOUT_SECS,
                    remaining_count,
                )
                break

            if result.get("error"):
                continue

            data = result["data"]
            source_name = result["source"]
            extractor_name = result["extractor"]

            try:
                raw_signals = SignalExtractorRegistry.extract(
                    extractor_name,
                    source_name,
                    data,
                    context={
                        "db_path": self.db_path,
                        "config": self.profile,
                        "source_metadata": result.get("metadata", {}),
                    },
                )

                # Side-channel: export job signals to Google Sheets (best-effort)
                if extractor_name == "jobs" and raw_signals:
                    try:
                        self.sheets_exporter.export_job_signals(
                            raw_signals,
                            search_profile=source_name,
                        )
                    except Exception as e:
                        logger.warning("Sheets export error for %s: %s", source_name, e)

                # Special processing for email signals (classification/triage)
                if extractor_name == "email":
                    await self._process_email_signals(raw_signals, seen_ids, triage_rules, email_rules)
                else:
                    # Standard signal logging
                    with xibi.db.open_db(self.db_path) as conn, conn:
                        for sig in raw_signals:
                            # 1. Deduplicate via signals table (short-term window)
                            if sig.get("ref_id") and sig_intel.is_duplicate_signal(
                                sig.get("ref_source", ""), sig["ref_id"], self.db_path
                            ):
                                logger.debug(f"Dedup skip (signals): {sig['ref_id']} from {sig.get('ref_source')}")
                                continue

                            # 2. Deduplicate via processed_messages table (long-term/migration)
                            if sig.get("ref_id"):
                                source = sig.get("ref_source") or sig.get("source")
                                row = conn.execute(
                                    "SELECT 1 FROM processed_messages WHERE source = ? AND ref_id = ?",
                                    (source, str(sig["ref_id"])),
                                ).fetchone()
                                if row:
                                    logger.debug(f"Dedup skip (processed): {sig['ref_id']} from {source}")
                                    continue
                            self.rules.log_signal_with_conn(
                                conn,
                                source=sig["source"],
                                topic_hint=sig.get("topic_hint"),
                                entity_text=sig.get("entity_text"),
                                entity_type=sig.get("entity_type", "unknown"),
                                content_preview=sig.get("content_preview", ""),
                                ref_id=sig.get("ref_id"),
                                ref_source=sig.get("ref_source"),
                                metadata=sig.get("metadata"),
                                received_via_account=sig.get("received_via_account"),
                                received_via_email_alias=sig.get("received_via_email_alias"),
                            )
            except Exception as e:
                logger.warning(f"Error processing signals for {source_name}: {e}", exc_info=True)

        # Phase 3: Post-processing
        try:
            await asyncio.wait_for(
                self._run_phase3(),
                timeout=_PHASE3_TIMEOUT_SECS,
            )
        except asyncio.TimeoutError:
            logger.warning("Phase 3 timeout (%ds): intelligence/observation exceeded limit", _PHASE3_TIMEOUT_SECS)
        except Exception as e:
            logger.warning("Phase 3 error: %s", e, exc_info=True)

        # Chief of staff review cycle (3x daily: 8am, 2pm, 8pm)
        try:
            last_review = self._get_last_review_time("chief_of_staff")
            if self._should_run_review(last_review, datetime.now(timezone.utc)):
                logger.info("🧠 Scheduled chief of staff review cycle is due")
                # Run non-blocking
                asyncio.create_task(self._do_review_cycle())
        except Exception as e:
            logger.warning("Chief of staff review trigger error: %s", e)

    def _get_last_review_time(self, mode: str) -> datetime:
        """Query observation_cycles for the last completion of a specific mode.

        Returns a tz-aware UTC datetime. SQLite stores CURRENT_TIMESTAMP as
        UTC but datetime.fromisoformat() returns it naive; without tzinfo
        attachment, downstream .astimezone() in _should_run_review treats
        the value as system-local and silently shifts it, suppressing
        scheduled fires.
        """
        try:
            from xibi.db import open_db

            with open_db(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT completed_at FROM observation_cycles WHERE review_mode = ? AND completed_at IS NOT NULL ORDER BY completed_at DESC LIMIT 1",
                    (mode,),
                ).fetchone()
                if row:
                    return datetime.fromisoformat(row["completed_at"]).replace(tzinfo=timezone.utc)
        except Exception:
            pass
        # Default to long ago if never run
        return datetime.now(timezone.utc) - timedelta(days=1)

    def _should_run_review(self, last_review_time: datetime, now_utc: datetime) -> bool:
        """Check if we've crossed a scheduled review time since last run."""
        import zoneinfo

        tz = zoneinfo.ZoneInfo(self._timezone_name)
        now_local = now_utc.astimezone(tz)
        last_local = last_review_time.astimezone(tz)

        review_schedule = [8, 14, 20]  # hours in local time
        for hour in review_schedule:
            scheduled_local = now_local.replace(hour=hour, minute=0, second=0, microsecond=0)
            if last_local < scheduled_local <= now_local:
                return True
        return False

    async def _do_review_cycle(self) -> None:
        """Run the review cycle and record it in observation_cycles."""
        cycle_id = None
        try:
            from xibi.db import open_db

            with open_db(self.db_path) as conn, conn:
                cursor = conn.execute(
                    "INSERT INTO observation_cycles (started_at, last_signal_id, review_mode) VALUES (CURRENT_TIMESTAMP, 0, 'chief_of_staff')"
                )
                cycle_id = cursor.lastrowid

            output = await run_review_cycle(self.db_path, self.config)
            await execute_review(output, self.db_path, self.config, self.adapter)

            with open_db(self.db_path) as conn, conn:
                conn.execute("UPDATE observation_cycles SET completed_at = CURRENT_TIMESTAMP WHERE id = ?", (cycle_id,))
            logger.info("🧠 Chief of staff review cycle complete")
        except Exception as e:
            logger.error("🧠 Chief of staff review cycle failed: %s", e, exc_info=True)
            if cycle_id:
                from xibi.db import open_db

                with open_db(self.db_path) as conn, conn:
                    conn.execute("UPDATE observation_cycles SET error_log = ? WHERE id = ?", (str(e), cycle_id))

    async def _run_phase3(self) -> None:
        """
        Signal intelligence, observation cycle, Jules watcher, Radiant audit.
        Each sub-task is isolated — one failure does not skip the rest.
        """
        # 3a: Signal intelligence enrichment
        if self.signal_intelligence_enabled:
            try:
                enriched = sig_intel.enrich_signals(
                    db_path=self.db_path,
                    config=None,
                    batch_size=20,
                    config_path=self.config_path,
                    trust_gradient=self.trust_gradient,
                )
                if enriched > 0:
                    logger.debug("Signal intelligence: enriched %d signals", enriched)
            except Exception as e:
                logger.warning("Signal intelligence enrichment failed: %s", e, exc_info=True)

        # 3b: Observation cycle
        if self.observation_cycle is not None:
            try:
                # Before running:
                if self.radiant and self.radiant.ceiling_status()["throttle"]:
                    logger.info("Radiant: cost ceiling reached, skipping observation cycle")
                else:
                    obs_result = self.observation_cycle.run(
                        executor=self.executor if hasattr(self, "executor") else None,
                        command_layer=CommandLayer(
                            db_path=str(self.db_path),
                            profile=self.profile,
                            interactive=False,  # ALWAYS non-interactive in heartbeat context
                        ),
                    )

                    if obs_result and obs_result.ran:
                        logger.info(
                            f"Observation cycle ran: {obs_result.signals_processed} signals, "
                            f"role={obs_result.role_used}, actions={len(obs_result.actions_taken)}"
                        )
                        if self.radiant:
                            self.radiant.record(
                                role=obs_result.role_used,
                                provider=_infer_provider(obs_result.role_used, self.profile),
                                model=_infer_model(obs_result.role_used, self.profile),
                                operation="observation_cycle",
                                prompt_tokens=0,
                                response_tokens=0,
                                duration_ms=0,
                            )
                            self.radiant.check_and_nudge(self.adapter)
                    elif obs_result:
                        logger.debug(f"Observation cycle skipped: {obs_result.skip_reason}")
            except Exception as e:
                logger.warning("Observation cycle error: %s", e, exc_info=True)

        # 3c: Jules watcher
        if self._jules_watcher:
            try:
                self._jules_watcher.poll()
            except Exception as e:
                logger.warning("Jules watcher error: %s", e, exc_info=True)

        # 3d: Radiant audit
        if self.radiant:
            try:
                self._audit_tick_counter += 1
                audit_interval = self.profile.get("audit_interval_ticks", 20)
                if self._audit_tick_counter >= audit_interval:
                    self._audit_tick_counter = 0
                    self.radiant.run_audit(self.adapter, trust_gradient=self.trust_gradient)
            except Exception as e:
                logger.warning("Radiant audit error: %s", e, exc_info=True)

    async def _process_email_signals(
        self,
        raw_signals: list[dict],
        seen_ids: set[str],
        triage_rules: dict,
        email_rules: list,
    ) -> None:
        from xibi.heartbeat.email_body import (
            compact_body,
            fetch_raw_email,
            find_himalaya,
            summarize_email_body,
        )
        from xibi.heartbeat.smart_parser import parse_email_smart

        base_url = self.config.get("redirect_base_url") or os.environ.get("XIBI_REDIRECT_BASE")

        try:
            himalaya_bin = find_himalaya()
        except FileNotFoundError:
            logger.warning("himalaya binary not found. Skipping email body summarization.")
            himalaya_bin = None

        processed: list[dict] = []
        for sig in raw_signals:
            email = sig["metadata"]["email"]
            email_id = sig["ref_id"]
            sender = trust_gate(sig["entity_text"], source="email_sender", mode="metadata")
            subject = trust_gate(sig["topic_hint"], source="email_subject", mode="metadata")
            sender_str = str(sender).lower()

            # Body fetching and summarization for new emails
            summary_data: dict = {}
            parsed_body: str | None = None
            parsed_body_format: str | None = None
            if himalaya_bin and email_id not in seen_ids:
                import asyncio
                import time as _time

                # Use run_in_executor to avoid blocking the event loop for sync LLM calls
                loop = asyncio.get_running_loop()

                raw, err = await loop.run_in_executor(None, fetch_raw_email, himalaya_bin, email_id)
                if raw and not err:
                    parse_start_ms = int(_time.time() * 1000)
                    parsed = await loop.run_in_executor(None, parse_email_smart, raw)
                    parse_duration_ms = int(_time.time() * 1000) - parse_start_ms
                    body = trust_gate(parsed.get("body"), source="email_body", mode="content")
                    parsed_body_format = str(parsed.get("format") or "")
                    parser_chain = list(parsed.get("parser_chain") or [])
                    fallback_used = bool(parsed.get("fallback_used"))
                    if body:
                        # Persist the clean body for Tier 2 backfill (30-day TTL).
                        parsed_body = body
                    if self.tracer is not None:
                        try:
                            self.tracer.span(
                                operation="extraction.smart_parse",
                                attributes={
                                    "email_id": str(email_id or ""),
                                    "format": parsed_body_format,
                                    "body_size": len(body),
                                    "raw_size": len(raw),
                                    "fallback_used": fallback_used,
                                    "parser_chain": ",".join(parser_chain),
                                    "duration_ms": parse_duration_ms,
                                },
                                duration_ms=parse_duration_ms,
                                component="smart_parser",
                            )
                        except Exception as exc:
                            logger.warning(f"smart_parse span emit failed: {exc}")
                    logger.info(
                        "smart_parse ok: format=%s raw_size=%d body_size=%d email_id=%s",
                        parsed_body_format,
                        len(raw),
                        len(body),
                        email_id,
                    )
                    if body and len(body.strip()) >= 20:
                        compacted = compact_body(body)
                        # Use effort="fast" model if available
                        model = self.config.get("models", {}).get("text", {}).get("fast", {}).get("model", "gemma4:e4b")
                        summary_data = await loop.run_in_executor(
                            None,
                            summarize_email_body,
                            compacted,
                            str(sender),
                            str(subject),
                            "http://localhost:11434",
                            model,
                        )

            verdict = ""

            # Sender trust assessment
            sender_addr = _extract_sender_addr(email)
            sender_name = _extract_sender_name(email)
            trust = assess_sender_trust(sender_addr, sender_name, self.db_path)

            processed.append(
                {
                    "email": email,
                    "email_id": email_id,
                    "sender": sender,
                    "subject": subject,
                    "verdict": verdict,
                    "is_new": email_id not in seen_ids,
                    "sig": sig,
                    "summary_data": summary_data,
                    "trust_assessment": trust,
                    "parsed_body": parsed_body,
                    "parsed_body_format": parsed_body_format,
                }
            )

        # ── Context Assembly (Step 70) ─────────────────────────────
        from xibi.heartbeat.context_assembly import assemble_batch_signal_context

        trust_results = {item["email_id"]: item["trust_assessment"] for item in processed}
        email_contexts = assemble_batch_signal_context(
            emails=[item["email"] for item in processed],
            db_path=self.db_path,
            batch_topics={item["email_id"]: item["sig"] for item in processed},
            body_summaries={item["email_id"]: item.get("summary_data", {}) for item in processed},
            trust_results=trust_results,
        )

        for item in processed:
            email_id = item["email_id"]
            sender_str = str(item["sender"]).lower()
            subject = item["subject"]
            ctx = email_contexts.get(email_id)
            item["context"] = ctx

            verdict = ""
            auto_noise = ["noreply@", "no-reply@", "notifications@", "newsletter@", "automated@", "mailer-daemon@"]
            if any(p in sender_str for p in auto_noise):
                verdict = "NOISE"

            if not verdict:
                for entity, status in triage_rules.items():
                    if entity.lower() in sender_str:
                        verdict = status.upper()
                        break

            reasoning = None
            if not verdict and item["is_new"]:
                verdict, reasoning = self._classify_signal(item["email"], context=ctx)

            if verdict in ("MEDIUM", "LOW", "DIGEST"):
                verdict, subject = self._should_escalate(verdict, subject, subject, [])

            item["verdict"] = verdict
            item["subject"] = subject
            item["reasoning"] = reasoning

        try:
            with xibi.db.open_db(self.db_path) as conn, conn:
                conn.row_factory = sqlite3.Row
                for item in processed:
                    sig = item["sig"]
                    summary_data = item.get("summary_data", {})
                    trust = item["trust_assessment"]
                    deep_link_url = None
                    if sig["source"] == "email" and sig.get("ref_id"):
                        # Gmail deep link format
                        deep_link_url = f"https://mail.google.com/mail/u/0/#inbox/{sig['ref_id']}"

                    # Step-110: pull provenance from the SignalContext that
                    # already carries it (assemble_batch_signal_context populates
                    # received_via_account/email_alias from the inbound headers).
                    ctx = item.get("context")
                    received_via_account = getattr(ctx, "received_via_account", None) if ctx else None
                    received_via_email_alias = getattr(ctx, "received_via_email_alias", None) if ctx else None

                    extracted_facts = summary_data.get("extracted_facts")

                    # Step-112: write the parent signal envelope with its
                    # facts (if any). Fan-out for digests happens after the
                    # parent write so the parent is durable even if a
                    # child write hits an unexpected error.
                    parsed_body_value = item.get("parsed_body")
                    parsed_body_format_value = item.get("parsed_body_format")
                    parsed_body_at_value = (
                        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                        if parsed_body_value
                        else None
                    )

                    self.rules.log_signal_with_conn(
                        conn,
                        source=sig["source"],
                        topic_hint=sig.get("topic_hint"),
                        entity_text=sig.get("entity_text"),
                        entity_type=sig.get("entity_type"),
                        content_preview=sig.get("content_preview"),
                        ref_id=sig.get("ref_id"),
                        ref_source=sig.get("ref_source"),
                        summary=summary_data.get("summary"),
                        summary_model=summary_data.get("model"),
                        summary_ms=summary_data.get("duration_ms"),
                        sender_trust=trust.tier,
                        sender_contact_id=trust.contact_id,
                        classification_reasoning=item.get("reasoning"),
                        deep_link_url=deep_link_url,
                        metadata=sig.get("metadata"),
                        received_via_account=received_via_account,
                        received_via_email_alias=received_via_email_alias,
                        extracted_facts=extracted_facts,
                        parsed_body=parsed_body_value,
                        parsed_body_at=parsed_body_at_value,
                        parsed_body_format=parsed_body_format_value,
                    )

                    # Step-112 + observability hotfix: emit the
                    # `extraction.tier2` span unconditionally — null facts
                    # from a marketing email is still a Tier 2 attempt and
                    # the path needs to be observable. The helper handles
                    # span emit, log line, and (only when there are digest
                    # items) fan-out.
                    if summary_data:
                        self._tier2_observe_and_fanout(
                            conn=conn,
                            sig=sig,
                            extracted_facts=extracted_facts,
                            summary_data=summary_data,
                            trust=trust,
                            deep_link_url=deep_link_url,
                            received_via_account=received_via_account,
                            received_via_email_alias=received_via_email_alias,
                        )

                    if not item["is_new"] or item["verdict"] == "DEFER":
                        continue

                    self.rules.log_triage_with_conn(
                        conn, item["email_id"], str(item["sender"]), item["subject"], item["verdict"]
                    )
                    self.rules.mark_seen_with_conn(conn, item["email_id"])

                    if item["verdict"] in ("CRITICAL", "HIGH", "URGENT"):
                        from xibi.heartbeat.rich_nudge import compose_smart_nudge

                        base_url = self.config.get("redirect_base_url") or os.environ.get("XIBI_REDIRECT_BASE")

                        ctx = email_contexts.get(item["email_id"])
                        if ctx and self._nudge_limiter.allow():
                            nudge = await compose_smart_nudge(
                                ctx,
                                model=self.nudge_model,
                                signal_id=item.get("signal_id"),
                                timeout_ms=self.nudge_timeout_ms,
                                base_url=base_url,
                            )
                            self._broadcast(nudge.text, nudge=nudge)
                            # Store nudge context for the adapter
                            self._pending_nudge_context = {
                                "signal_id": nudge.signal_id,
                                "email_context": ctx,
                                "actions": nudge.actions,
                                "sent_at": datetime.now().isoformat(),
                            }
                            # Sync back to adapter if possible
                            if hasattr(self.adapter, "_pending_nudge_context"):
                                self.adapter._pending_nudge_context = self._pending_nudge_context

                            logger.info(
                                f"Rich URGENT nudge sent for signal {nudge.signal_id}: "
                                f"{len(nudge.text)} chars, actions={nudge.actions}"
                            )
                        elif ctx and not self._nudge_limiter.allow():
                            # Rate limited — queue for next digest
                            self._digest_overflow.append(
                                {
                                    "signal_id": item.get("signal_id"),
                                    "preview": ctx.summary or item["subject"],
                                    "topic": ctx.topic,
                                }
                            )
                            logger.info(
                                f"URGENT nudge rate-limited (#{self._nudge_limiter.count_this_hour}/hr), "
                                f"queued for digest"
                            )
                        else:
                            # Fallback — no context assembled
                            alert_msg = self.rules.evaluate_email(item["email"], email_rules, sender_trust=trust)
                            if alert_msg:
                                self._broadcast(alert_msg)
        except Exception as e:
            logger.warning(f"Error in process_email_signals write phase: {e}", exc_info=True)

    def _tier2_observe_and_fanout(
        self,
        conn: sqlite3.Connection,
        sig: dict,
        extracted_facts: dict | None = None,
        summary_data: dict | None = None,
        trust: Any = None,
        deep_link_url: str | None = None,
        received_via_account: str | None = None,
        received_via_email_alias: str | None = None,
    ) -> None:
        """Emit Tier 2 span + log + per-item child rows for digest fan-out.

        Source-agnostic — same code handles email digests today and
        future Slack-DM digests, multi-segment travel itineraries, USPS
        multi-package previews, etc., once those Tier 2 extractors register.

        Hotfix (post-step-112): the span emits **unconditionally** for every
        Tier 2 attempt, not just facts-produced runs. The spec promised
        "extraction.tier2 span on every email that runs the extractor" —
        when the model correctly returns null facts (marketing/FYI emails),
        that's still a Tier 2 attempt and the path needs to be observable.
        Span attributes carry ``facts_emitted`` so the consumer can
        distinguish null-by-design from parse-failure.
        """
        summary_data = summary_data or {}
        items = (extracted_facts or {}).get("digest_items") or []
        is_digest_parent = bool((extracted_facts or {}).get("is_digest_parent")) and len(items) > 0

        # span: extraction.tier2 — fires for every Tier 2 attempt (incl.
        # null-facts case). Reviewer-LLM consumers and operator dashboards
        # need the path to be observable even when the model declines to
        # emit facts.
        _emit_tier2_span(
            tracer=self.tracer,
            sig=sig,
            extracted_facts=extracted_facts,
            summary_data=summary_data,
            source_attr=None,
        )

        if extracted_facts is None:
            logger.info(
                "tier2 ok: email_id=%s facts=null model=%s",
                sig.get("ref_id"),
                summary_data.get("model"),
            )
        else:
            logger.info(
                "tier2 ok: email_id=%s type=%s facts_keys=%d digest_items=%d",
                sig.get("ref_id"),
                extracted_facts.get("type"),
                len(extracted_facts),
                len(items) if is_digest_parent else 0,
            )

        if not is_digest_parent or extracted_facts is None:
            return

        # Fan-out: write one child signal per item with synthetic per-item
        # ref_id (`<parent_ref_id>:<index>`) so the existing 72h
        # (ref_source, ref_id) dedup keeps re-runs idempotent.
        parent_ref_id = str(sig.get("ref_id") or "")
        parent_ref_source = sig.get("ref_source")  # condition #10: child inherits parent's ref_source
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            child_ref_id = f"{parent_ref_id}:{idx}"
            item_fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
            # Construct child preview from per-item fields when available.
            preview_bits: list[str] = []
            for key in ("title", "company", "location", "url", "comp_range", "match_reason"):
                val = item_fields.get(key) if isinstance(item_fields, dict) else None
                if val:
                    preview_bits.append(f"{key}={val}")
            child_preview = " | ".join(preview_bits) or str(item)[:280]

            try:
                self.rules.log_signal_with_conn(
                    conn,
                    source=sig["source"],
                    topic_hint=str(item.get("type") or extracted_facts.get("type") or ""),
                    entity_text=sig.get("entity_text"),
                    entity_type=sig.get("entity_type"),
                    content_preview=child_preview,
                    ref_id=child_ref_id,
                    ref_source=parent_ref_source,
                    summary=None,
                    summary_model=summary_data.get("model"),
                    summary_ms=None,
                    sender_trust=trust.tier,
                    sender_contact_id=trust.contact_id,
                    classification_reasoning=None,
                    deep_link_url=deep_link_url,
                    metadata=None,
                    received_via_account=received_via_account,
                    received_via_email_alias=received_via_email_alias,
                    extracted_facts=item,
                    parent_ref_id=parent_ref_id,
                )
            except Exception as exc:
                logger.warning(
                    "tier2 fan-out write failed: parent=%s child_idx=%d err=%s",
                    parent_ref_id,
                    idx,
                    exc,
                )

    def digest_tick(self, force: bool = False) -> None:
        if not self._enable_legacy_digest and not force:
            return

        if self._is_quiet_hours() and not force:
            return

        items = self.rules.pop_digest_items()
        if not items:
            if force:
                self._broadcast("No new items — nothing to report.")
            return

        # Only surface items worth attention
        important = [i for i in items if i.get("verdict") in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "URGENT", "DIGEST")]
        if not important:
            return

        msg_lines = ["\U0001f4e5 **Digest Recap**"]

        from xibi.telegram.formatter import format_signal_link

        base_url = self.config.get("redirect_base_url") or os.environ.get("XIBI_REDIRECT_BASE")

        # Prepend rate-limited URGENT signals
        if self._digest_overflow:
            for item in self._digest_overflow:
                topic = item.get("topic") or "Email"
                linked_topic = format_signal_link(topic, item.get("signal_id"), base_url)
                msg_lines.append(f"⚡ *Rate-limited URGENT* — {linked_topic}: {item['preview'][:100]}")
            self._digest_overflow = []  # Clear after prepending

        # Group by section
        critical_high = [i for i in important if i.get("verdict") in ("CRITICAL", "HIGH", "URGENT")]
        worth_reading = [i for i in important if i.get("verdict") in ("MEDIUM", "LOW", "DIGEST")]

        if critical_high:
            msg_lines.append("\n🚨 *Priority Attention*")
            for item in critical_high[:5]:
                subject = item["subject"]
                linked_subject = format_signal_link(subject, item.get("signal_id"), base_url)
                msg_lines.append(f"• {item['sender']}: {linked_subject} ({item['verdict']})")

        if worth_reading:
            msg_lines.append("\n📥 *Worth Reading*")
            for item in worth_reading[:10]:
                subject = item["subject"]
                linked_subject = format_signal_link(subject, item.get("signal_id"), base_url)
                msg_lines.append(f"• {item['sender']}: {linked_subject}")

        self._broadcast("\n".join(msg_lines))

    def recap_tick(self) -> None:
        if not self._enable_legacy_digest:
            logger.debug("Legacy digest disabled — skipping recap tick")
            return
        logger.info("Running recap tick")
        self.digest_tick(force=True)

    def _cleanup_telegram_cache(self) -> None:
        """Purge processed_messages rows older than 7 days. Runs once per day."""
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            with xibi.db.open_db(self.db_path) as conn:
                # Check if already run today
                cursor = conn.execute("SELECT value FROM heartbeat_state WHERE key = 'ttl_cleanup_last_run'")
                row = cursor.fetchone()
                if row and row[0] == today:
                    return

                logger.info("Cleaning up Telegram message cache...")
                conn.execute("DELETE FROM processed_messages WHERE processed_at < datetime('now', '-7 days')")
                conn.execute(
                    "INSERT OR REPLACE INTO heartbeat_state (key, value) VALUES ('ttl_cleanup_last_run', ?)",
                    (today,),
                )
        except Exception as e:
            logger.warning(f"Telegram cache cleanup error: {e}", exc_info=True)

    def reflection_tick(self) -> None:
        if self._is_quiet_hours():
            return

        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_reflection_date == today:
            return

        try:
            # Query triage patterns from 7 days
            with xibi.db.open_db(self.db_path) as conn:
                cursor = conn.execute("""
                    SELECT sender, COUNT(*) as count FROM triage_log
                    WHERE timestamp > datetime('now', '-7 days')
                    GROUP BY sender ORDER BY count DESC LIMIT 5
                """)
                patterns = cursor.fetchall()

            pattern_str = ", ".join([f"{p[0]} ({p[1]})" for p in patterns])
            prompt = (
                f"Analyze these recent email triage patterns: {pattern_str}.\n"
                "Provide a brief, helpful reflection on what the user has been receiving."
            )

            from xibi.router import set_trace_context

            set_trace_context(trace_id=None, span_id=None, operation="heartbeat_reflection")
            model = get_model(effort="fast", config_path=self.config_path)
            reflection = model.generate(prompt)
            self._broadcast(f"💭 **Reflection**\n\n{reflection}")
            self.rules.log_background_event(reflection, "reflection")
            self._last_reflection_date = today
        except Exception as e:
            logger.warning(f"Reflection tick error: {e}", exc_info=True)

    def run(self) -> None:
        from xibi.shutdown import is_shutdown_requested, wait_for_shutdown

        tick_count = 0
        interval_secs = self.interval_minutes * 60
        logger.info(f"Starting heartbeat loop (interval: {self.interval_minutes}m)")

        while not is_shutdown_requested():
            try:
                self.tick()
                tick_count += 1

                now = datetime.now()

                # Check windows
                if (now.hour == 9 or now.hour == 18) and now.minute < 15:
                    self.recap_tick()
                    tick_count = 0
                # Hourly digest removed — digests only at 9 AM and 6 PM windows
                if now.hour == 7 and now.minute < 15:
                    self.reflection_tick()
                    self._cleanup_telegram_cache()
                    self._cleanup_subagent_runs()

            except Exception as e:
                logger.error(f"Error in heartbeat loop: {e}", exc_info=True)

            # Interruptible inter-tick wait. `wait_for_shutdown` returns True
            # when the shutdown event is set (SIGTERM), so `break` exits the
            # loop directly — bypassing the redundant `while` recheck on the
            # next iteration. Both exit paths hit the same "exiting" log line.
            if wait_for_shutdown(interval_secs):
                break

        logger.info("HeartbeatPoller run loop exiting (shutdown requested)")

    def _cleanup_subagent_runs(self) -> None:
        """Purge expired subagent runs. Runs once per day."""
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            with xibi.db.open_db(self.db_path) as conn:
                cursor = conn.execute("SELECT value FROM heartbeat_state WHERE key = 'subagent_ttl_cleanup_last_run'")
                row = cursor.fetchone()
                if row and row[0] == today:
                    return

                from xibi.subagent.db import cleanup_expired_runs

                count = cleanup_expired_runs(self.db_path)
                if count > 0:
                    logger.info(f"Cleaned up {count} expired subagent runs")

                conn.execute(
                    "INSERT OR REPLACE INTO heartbeat_state (key, value) VALUES ('subagent_ttl_cleanup_last_run', ?)",
                    (today,),
                )
        except Exception as e:
            logger.warning(f"Subagent run cleanup error: {e}", exc_info=True)


def _infer_provider(role: str, config: dict[str, Any]) -> str:
    try:
        return str(config["models"]["text"][role]["provider"])
    except KeyError:
        return "unknown"


def _infer_model(role: str, config: dict[str, Any]) -> str:
    try:
        return str(config["models"]["text"][role]["model"])
    except KeyError:
        return "unknown"
