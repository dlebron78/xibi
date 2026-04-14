from __future__ import annotations

import importlib.util
import logging
import sqlite3
import time
from datetime import datetime, timedelta
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
from xibi.heartbeat.sender_trust import (
    _extract_sender_addr,
    _extract_sender_name,
    assess_sender_trust,
)
from xibi.heartbeat.source_poller import SourcePoller
from xibi.observation import ObservationCycle
from xibi.radiant import Radiant
from xibi.router import get_model
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
            import os as _os

            api_key = _os.environ.get("JULES_API_KEY", "")
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
            parse_email_body,
            summarize_email_body,
        )

        try:
            himalaya_bin = find_himalaya()
        except FileNotFoundError:
            logger.warning("himalaya binary not found. Skipping email body summarization.")
            himalaya_bin = None

        processed: list[dict] = []
        for sig in raw_signals:
            email = sig["metadata"]["email"]
            email_id = sig["ref_id"]
            sender = sig["entity_text"]
            subject = sig["topic_hint"]
            sender_str = str(sender).lower()

            # Body fetching and summarization for new emails
            summary_data = {}
            if himalaya_bin and email_id not in seen_ids:
                import asyncio

                # Use run_in_executor to avoid blocking the event loop for sync LLM calls
                loop = asyncio.get_running_loop()

                raw, err = await loop.run_in_executor(None, fetch_raw_email, himalaya_bin, email_id)
                if raw and not err:
                    body = parse_email_body(raw)
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
                    )

                    if not item["is_new"] or item["verdict"] == "DEFER":
                        continue

                    self.rules.log_triage_with_conn(
                        conn, item["email_id"], str(item["sender"]), item["subject"], item["verdict"]
                    )
                    self.rules.mark_seen_with_conn(conn, item["email_id"])

                    if item["verdict"] in ("CRITICAL", "HIGH", "URGENT"):
                        from xibi.heartbeat.rich_nudge import compose_smart_nudge

                        ctx = email_contexts.get(item["email_id"])
                        if ctx and self._nudge_limiter.allow():
                            nudge = await compose_smart_nudge(
                                ctx,
                                model=self.nudge_model,
                                signal_id=item.get("signal_id"),
                                timeout_ms=self.nudge_timeout_ms,
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

    def digest_tick(self, force: bool = False) -> None:
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
        from xibi.shutdown import is_shutdown_requested

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

            except Exception as e:
                logger.error(f"Error in heartbeat loop: {e}", exc_info=True)

            time.sleep(interval_secs)

        logger.info("HeartbeatPoller run loop exiting (shutdown requested)")


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
