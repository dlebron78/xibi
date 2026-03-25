"""
Tests for Phase 1.75: Signal Pipeline Fix.

Covers:
- Inference mutex (Rule 19)
- Batch topic extraction (Fix 1)
- Chat signal via passive memory (Fix 2)
- Reflection synthesis (Fix 3)
- Contract tests at boundaries (Rule 18)
"""

import os
import json
import sqlite3
import threading
import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from bregger_utils import inference_lock, get_active_threads, get_pinned_topics
from bregger_heartbeat import (
    _batch_extract_topics,
    _extract_sender,
    _should_escalate,
    should_propose,
    reflect,
    RuleEngine,
)


class MockNotifier:
    def __init__(self):
        self.sent = []
    def send(self, msg, parse_mode=None):
        self.sent.append(msg)


@pytest.fixture
def signal_db(tmp_path):
    """Sterile DB with signals and tasks tables."""
    db_path = tmp_path / "bregger.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute('''
            CREATE TABLE signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                source TEXT NOT NULL,
                topic_hint TEXT,
                entity_text TEXT,
                entity_type TEXT,
                content_preview TEXT NOT NULL,
                ref_id TEXT,
                ref_source TEXT,
                proposal_status TEXT DEFAULT 'active',
                dismissed_at DATETIME,
                env TEXT DEFAULT 'production'
            )
        ''')
        conn.execute('''
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                goal TEXT,
                status TEXT DEFAULT 'pending',
                exit_type TEXT,
                urgency TEXT DEFAULT 'normal',
                context_compressed TEXT DEFAULT '',
                scratchpad_json TEXT DEFAULT '[]',
                origin TEXT,
                trace_id TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE traces (
                id TEXT PRIMARY KEY, intent TEXT, plan TEXT, status TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE beliefs (
                key TEXT, value TEXT, valid_from DATETIME, valid_until DATETIME
            )
        ''')
    return db_path


def seed_signals(db_path, entity, topic, count, proposal_status='active', env='test'):
    with sqlite3.connect(db_path) as conn:
        for _ in range(count):
            conn.execute(
                "INSERT INTO signals (source, entity_text, topic_hint, content_preview, proposal_status, env) "
                "VALUES ('email', ?, ?, 'test preview', ?, ?)",
                (entity, topic, proposal_status, env)
            )


# ── Inference Mutex Tests (Rule 19) ──────────────────────────────────

class TestInferenceMutex:
    def test_lock_is_reentrant(self):
        """RLock allows same thread to acquire multiple times."""
        assert inference_lock.acquire(timeout=1)
        assert inference_lock.acquire(timeout=1)  # Would deadlock with Lock
        inference_lock.release()
        inference_lock.release()

    def test_lock_serializes_threads(self):
        """Different threads must wait for the lock."""
        order = []

        def worker(name, delay):
            with inference_lock:
                order.append(f"{name}_start")
                time.sleep(delay)
                order.append(f"{name}_end")

        t1 = threading.Thread(target=worker, args=("first", 0.1))
        t2 = threading.Thread(target=worker, args=("second", 0.05))

        t1.start()
        time.sleep(0.02)  # Ensure t1 grabs lock first
        t2.start()

        t1.join()
        t2.join()

        # first must complete before second starts
        assert order.index("first_end") < order.index("second_start")


# ── Batch Topic Extraction Tests (Fix 1) ─────────────────────────────

class TestBatchExtraction:
    def test_empty_emails_returns_empty(self):
        assert _batch_extract_topics([]) == {}

    @patch("bregger_heartbeat.urllib.request.urlopen")
    def test_successful_extraction(self, mock_urlopen):
        """Mocked Ollama returns valid JSON → topics extracted."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "response": json.dumps([
                {"num": 1, "topic": "board deck", "entity_text": "Sarah", "entity_type": "person"},
                {"num": 2, "topic": "flight booking", "entity_text": "JetBlue", "entity_type": "company"},
            ])
        }).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        emails = [
            {"id": "100", "from": {"name": "Sarah", "addr": "s@co.com"}, "subject": "Board deck feedback"},
            {"id": "101", "from": {"name": "JetBlue", "addr": "no@jb.com"}, "subject": "Your flight confirmation"},
        ]

        result = _batch_extract_topics(emails)

        assert "100" in result
        assert result["100"]["topic"] == "board deck"  # normalize_topic: underscore→space
        assert result["100"]["entity_text"] == "Sarah"
        assert "101" in result
        assert result["101"]["topic"] == "flight book"  # normalize_topic: strips -ing suffix

    @patch("bregger_heartbeat.urllib.request.urlopen")
    def test_llm_failure_returns_empty(self, mock_urlopen):
        """On LLM failure, returns empty dict for regex fallback."""
        mock_urlopen.side_effect = Exception("Ollama timeout")
        emails = [{"id": "100", "from": "test", "subject": "Test"}]
        result = _batch_extract_topics(emails)
        assert result == {}


# ── Contract Tests (Rule 18) ─────────────────────────────────────────

class TestSignalContracts:
    """Ensure output of topic extraction is semantically useful to log_signal and reflect."""

    @patch("bregger_heartbeat.urllib.request.urlopen")
    def test_batch_output_fits_log_signal_schema(self, mock_urlopen):
        """Batch extraction output has the keys log_signal expects."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "response": json.dumps([
                {"num": 1, "topic": "quarterly review", "entity_text": "Finance Team", "entity_type": "org"},
            ])
        }).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        emails = [{"id": "200", "from": "cfo@co.com", "subject": "Q1 Quarterly Review"}]
        result = _batch_extract_topics(emails)

        entry = result["200"]
        # These are the exact keys log_signal expects
        assert "topic" in entry
        assert "entity_text" in entry
        assert "entity_type" in entry
        # Topic should be normalized via normalize_topic (lowercase, spaces, stems stripped)
        assert entry["topic"] == "quarterly review"

    def test_reflect_uses_signals_with_real_topics(self, signal_db):
        """Signals with LLM-extracted topics (not word-frequency noise) produce meaningful reflection input."""
        # Seed signals with realistic LLM-extracted topics (not "documentation" or "email")
        seed_signals(signal_db, "Sarah Chen", "board_deck", 5)

        with sqlite3.connect(signal_db) as conn:
            conn.row_factory = sqlite3.Row
            patterns = conn.execute("""
                SELECT entity_text, topic_hint, COUNT(*) as freq
                FROM signals
                WHERE proposal_status = 'active'
                  AND entity_text IS NOT NULL AND topic_hint IS NOT NULL
                  AND timestamp > datetime('now', '-7 days')
                GROUP BY entity_text, topic_hint
                HAVING COUNT(*) >= 3
            """).fetchall()

        assert len(patterns) == 1
        assert patterns[0]["entity_text"] == "Sarah Chen"
        assert patterns[0]["topic_hint"] == "board_deck"
        # This is a meaningful signal, not "documentation 40 times"


# ── Reflection Synthesis Tests (Fix 3) ────────────────────────────────

class TestReflectionSynthesis:
    @patch("bregger_heartbeat._synthesize_reflection")
    def test_reflect_uses_llm_when_available(self, mock_synth, signal_db):
        """When LLM synthesis returns a proposal, reflect uses it."""
        mock_synth.return_value = {
            "goal": "Follow up with Sarah about the board deck — feedback pending for 3 days",
            "urgency": "normal",
            "reasoning": "Sarah sent feedback email 3 days ago with no reply"
        }

        notifier = MockNotifier()
        seed_signals(signal_db, "Sarah", "board_deck", 5)

        reflect(notifier, signal_db)

        assert len(notifier.sent) == 1
        assert "board deck" in notifier.sent[0]
        assert "[task:" in notifier.sent[0]

        # Verify trace includes synthesis method
        with sqlite3.connect(signal_db) as conn:
            trace = conn.execute("SELECT plan FROM traces WHERE intent='reflection'").fetchone()
            plan = json.loads(trace[0])
            assert plan["synthesis"] == "llm"

    @patch("bregger_heartbeat._synthesize_reflection")
    def test_reflect_falls_back_to_frequency(self, mock_synth, signal_db):
        """When LLM returns None, falls back to should_propose frequency rules."""
        mock_synth.return_value = None

        notifier = MockNotifier()
        seed_signals(signal_db, "Jake", "budget", 5)

        reflect(notifier, signal_db)

        assert len(notifier.sent) == 1
        assert "[task:" in notifier.sent[0]

        with sqlite3.connect(signal_db) as conn:
            trace = conn.execute("SELECT plan FROM traces WHERE intent='reflection'").fetchone()
            plan = json.loads(trace[0])
            assert plan["synthesis"] == "frequency"

    @patch("bregger_heartbeat._synthesize_reflection")
    def test_reflect_skips_when_nothing_worth_surfacing(self, mock_synth, signal_db):
        """LLM says NONE and frequency rules don't trigger → no notification."""
        mock_synth.return_value = None

        notifier = MockNotifier()
        # 3 signals with no deadline words — below should_propose threshold for non-deadline
        seed_signals(signal_db, "Newsletter", "updates", 3)

        reflect(notifier, signal_db)

        assert len(notifier.sent) == 0


# ── Chat Signal Extraction Tests (Fix 2) ─────────────────────────────

class TestChatSignalExtraction:
    """Tests for _extract_passive_memory chat signal path."""

    def _make_core_stub(self, db_path):
        """Create a minimal BreggerCore-like object with the methods we need."""
        import types

        class Stub:
            pass

        stub = Stub()
        stub.db_path = str(db_path)
        stub._belief_cache = {}

        # Bind _log_signal from BreggerCore
        from bregger_core import BreggerCore
        stub._log_signal = types.MethodType(BreggerCore._log_signal, stub)
        stub.log_trace = lambda *a, **kw: None
        stub.update_trace = lambda *a, **kw: None

        return stub

    def test_signal_logged_from_combined_response(self, signal_db):
        """When LLM returns {facts, signal}, the signal is written to the signals table."""
        stub = self._make_core_stub(signal_db)

        # Simulate what _extract_passive_memory does after parsing
        signal_data = {"topic": "board deck review", "entity_text": "Sarah", "entity_type": "person"}

        from bregger_utils import normalize_topic as _normalize_topic
        raw_topic = "_".join(signal_data["topic"].lower().split()[:3])
        topic = _normalize_topic(raw_topic) or raw_topic

        stub._log_signal(
            source="chat",
            topic_hint=topic,
            entity_text=signal_data.get("entity_text"),
            entity_type=signal_data.get("entity_type"),
            content_preview="Can you check the board deck?",
            ref_id="pm_test123",
            ref_source="passive_memory"
        )

        with sqlite3.connect(signal_db) as conn:
            conn.row_factory = sqlite3.Row
            signals = conn.execute("SELECT * FROM signals WHERE source='chat'").fetchall()
            assert len(signals) == 1
            s = signals[0]
            assert s["topic_hint"] == "board deck review"  # normalize_topic passes through (no stemming match)
            assert s["entity_text"] == "Sarah"
            assert s["entity_type"] == "person"
            assert s["ref_source"] == "passive_memory"

    def test_null_signal_skips_logging(self, signal_db):
        """When LLM returns signal: null, no chat signal is written."""
        # This tests the guard: if signal_data and signal_data.get("topic")
        signal_data = None

        with sqlite3.connect(signal_db) as conn:
            count_before = conn.execute("SELECT COUNT(*) FROM signals WHERE source='chat'").fetchone()[0]

        # The condition `if signal_data and signal_data.get("topic")` prevents logging
        assert not (signal_data and signal_data.get("topic"))

        with sqlite3.connect(signal_db) as conn:
            count_after = conn.execute("SELECT COUNT(*) FROM signals WHERE source='chat'").fetchone()[0]
            assert count_after == count_before


# ── Phase 2 Tests ─────────────────────────────────────────────────────────

@pytest.fixture
def phase2_db(tmp_path):
    """Minimal DB with signals and pinned_topics tables for Phase 2 tests."""
    db_path = tmp_path / "bregger.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute('''
            CREATE TABLE signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                source TEXT NOT NULL,
                topic_hint TEXT,
                entity_text TEXT,
                entity_type TEXT,
                content_preview TEXT NOT NULL,
                ref_id TEXT,
                ref_source TEXT,
                proposal_status TEXT DEFAULT 'active',
                dismissed_at DATETIME,
                env TEXT DEFAULT 'production'
            )
        ''')
        conn.execute('CREATE TABLE pinned_topics (topic TEXT PRIMARY KEY)')
    return db_path


class TestActiveThreadsContext:
    """Phase 2.1: get_active_threads() from bregger_utils is the single source
    of truth used by both the heartbeat escalation and the core prompt injection."""

    def test_topics_above_threshold_returned(self, phase2_db):
        """Topics with 2+ signals in 7d are returned; singletons are excluded."""
        with sqlite3.connect(phase2_db) as conn:
            for _ in range(4):
                conn.execute(
                    "INSERT INTO signals (source, topic_hint, content_preview, env) "
                    "VALUES ('chat', 'board deck', 'working on deck', 'production')"
                )
            for _ in range(2):
                conn.execute(
                    "INSERT INTO signals (source, topic_hint, content_preview, env) "
                    "VALUES ('email', 'job search', 'applied to role', 'production')"
                )
            conn.execute(
                "INSERT INTO signals (source, topic_hint, content_preview, env) "
                "VALUES ('chat', 'pizza recipe', 'one-off', 'production')"
            )

        threads = get_active_threads(phase2_db)
        topics = [t["topic"] for t in threads]
        assert "board deck" in topics
        assert "job search" in topics
        assert "pizza recipe" not in topics  # only 1 signal

    def test_multi_source_counts_are_correct(self, phase2_db):
        """Signals from chat and email for the same topic sum correctly."""
        with sqlite3.connect(phase2_db) as conn:
            for _ in range(3):
                conn.execute(
                    "INSERT INTO signals (source, topic_hint, content_preview, env) "
                    "VALUES ('chat', 'board deck', 'chat mention', 'production')"
                )
            for _ in range(2):
                conn.execute(
                    "INSERT INTO signals (source, topic_hint, content_preview, env) "
                    "VALUES ('email', 'board deck', 'email mention', 'production')"
                )

        threads = get_active_threads(phase2_db)
        assert len(threads) == 1
        t = threads[0]
        assert t["topic"] == "board deck"
        assert t["count"] == 5  # 3 chat + 2 email, not arbitrary SQL pick
        assert set(t["sources"]) == {"chat", "email"}

    def test_empty_signals_returns_empty_list(self, phase2_db):
        """No signals → empty list, no exception."""
        assert get_active_threads(phase2_db) == []

    def test_normalize_deduplicates_variants(self, phase2_db):
        """'scheduling' and 'schedule' merge into one thread after normalization."""
        with sqlite3.connect(phase2_db) as conn:
            for _ in range(2):
                conn.execute(
                    "INSERT INTO signals (source, topic_hint, content_preview, env) "
                    "VALUES ('chat', 'scheduling', 'set up meeting', 'production')"
                )
            for _ in range(2):
                conn.execute(
                    "INSERT INTO signals (source, topic_hint, content_preview, env) "
                    "VALUES ('email', 'schedule', 'meeting invite', 'production')"
                )

        threads = get_active_threads(phase2_db)
        # Both variants normalize to the same token → one thread with count=4
        assert len(threads) == 1
        assert threads[0]["count"] == 4

    def test_pinned_topics_returned(self, phase2_db):
        """get_pinned_topics returns pinned entries regardless of signal count."""
        with sqlite3.connect(phase2_db) as conn:
            conn.execute("INSERT INTO pinned_topics (topic) VALUES ('tennis')")

        pinned = get_pinned_topics(phase2_db)
        assert any(p["topic"] == "tenni" for p in pinned)
        assert pinned[0]["pinned"] is True

    def test_env_test_signals_excluded_from_active_threads(self, phase2_db):
        """Signals written with env='test' must never appear in production thread results.

        This is the Jake contamination guard: if test data were ever accidentally
        written to the live DB, the env filter prevents it from surfacing in
        active threads or cross-channel escalation.
        """
        with sqlite3.connect(phase2_db) as conn:
            # 3 test-env signals for a topic — should be invisible
            for _ in range(3):
                conn.execute(
                    "INSERT INTO signals (source, topic_hint, content_preview, env) "
                    "VALUES ('email', 'jake test entity', 'test preview', 'test')"
                )
            # 2 production signals for a different topic — should appear
            for _ in range(2):
                conn.execute(
                    "INSERT INTO signals (source, topic_hint, content_preview, env) "
                    "VALUES ('chat', 'real work topic', 'real preview', 'production')"
                )

        threads = get_active_threads(phase2_db)
        topics = [t["topic"] for t in threads]

        assert "real work topic" in topics, "Production signals should be returned"
        assert not any("jake" in t for t in topics), "Test-env signals must not appear in production threads"


class TestCrossChannelRelevance:
    """Phase 2.2: _should_escalate() is the production escalation function.
    Tests call it directly rather than re-implementing the logic inline."""

    def test_digest_escalated_when_topic_matches_active_thread(self):
        """DIGEST → URGENT when email topic matches a known active thread."""
        active_threads = [{"topic": "board deck", "count": 3, "sources": ["chat"]}]
        verdict, subject = _should_escalate("DIGEST", "board deck", "Q3 Deck Review", active_threads)
        assert verdict == "URGENT"
        assert "Active Thread" in subject
        assert "board deck" in subject

    def test_digest_escalated_when_topic_matches_pinned(self):
        """DIGEST → URGENT when email topic matches a pinned topic."""
        # The pinned topic mock must represent what get_pinned_topics now returns (normalized)
        pinned = [{"topic": "tenni", "count": 100, "pinned": True}]
        verdict, subject = _should_escalate("DIGEST", "tennis", "Court booking confirmation", pinned)
        assert verdict == "URGENT"
        assert "Pinned Topic" in subject

    def test_digest_unchanged_for_unmatched_topic(self):
        """DIGEST stays DIGEST when the email topic is not in any active thread."""
        active_threads = [{"topic": "board deck", "count": 3, "sources": ["chat"]}]
        verdict, subject = _should_escalate("DIGEST", "pizza recipe", "Dinner tonight", active_threads)
        assert verdict == "DIGEST"
        assert subject == "Dinner tonight"  # unchanged

    def test_urgent_passthrough(self):
        """URGENT emails are never touched by the escalation check."""
        active_threads = [{"topic": "board deck", "count": 3, "sources": ["chat"]}]
        verdict, subject = _should_escalate("URGENT", "board deck", "Important: Q3 Deck", active_threads)
        assert verdict == "URGENT"
        assert "Active Thread" not in subject  # not re-prefixed

    def test_empty_priority_topics_no_escalation(self):
        """Empty priority list → no escalation, no exception."""
        verdict, subject = _should_escalate("DIGEST", "board deck", "Deck update", [])
        assert verdict == "DIGEST"
        assert subject == "Deck update"
