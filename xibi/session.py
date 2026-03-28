from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from xibi.db import open_db
from xibi.router import Config, get_model

if TYPE_CHECKING:
    from xibi.types import ReActResult, Step

logger = logging.getLogger(__name__)


@dataclass
class SessionEntity:
    entity_type: str  # "place" | "person" | "date" | "org" | "amount"
    value: str  # e.g. "Miami", "March 28th", "Acme Corp", ",400"
    turn_id: str  # Which turn this came from
    source_tool: str  # Which tool output it was extracted from
    confidence: float  # 0.0–1.0 from extractor


@dataclass
class Turn:
    turn_id: str  # UUID
    session_id: str  # Groups turns into a conversation
    query: str  # What the user asked
    answer: str  # What Xibi responded
    tools_called: list[str]  # Tool names used in this turn
    exit_reason: str  # finish / timeout / error / ask_user
    created_at: str  # ISO UTC datetime
    summary: str = ""  # Compressed summary (populated for old turns)


class SessionContext:
    FULL_WINDOW = 2  # Last N turns injected in full detail
    SUMMARY_WINDOW = 4  # Turns 3-6 injected as one-liner summaries

    def __init__(self, session_id: str, db_path: Path) -> None:
        self.session_id = session_id
        self.db_path = db_path

    def add_turn(self, query: str, result: ReActResult, config: Config | None = None) -> Turn:
        tools_called = [step.tool for step in result.steps if step.tool and step.tool not in ("finish", "ask_user")]

        turn = Turn(
            turn_id=str(uuid.uuid4()),
            session_id=self.session_id,
            query=query,
            answer=result.answer,
            tools_called=tools_called,
            exit_reason=result.exit_reason,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        # Check if the DB table exists before querying
        try:
            with open_db(self.db_path) as conn:
                conn.execute("SELECT 1 FROM session_turns LIMIT 1")
        except sqlite3.OperationalError:
            return turn

        with open_db(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO session_turns (turn_id, session_id, query, answer, tools_called, exit_reason, summary, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn.turn_id,
                    turn.session_id,
                    turn.query,
                    turn.answer,
                    json.dumps(turn.tools_called),
                    turn.exit_reason,
                    turn.summary,
                    turn.created_at,
                ),
            )
            conn.commit()

        # Everything else runs in background to avoid blocking the user response
        def run_background_tasks() -> None:
            self.summarise_old_turns(config=config)

            # Phase 2: extract entities asynchronously if there is content to extract from
            tool_steps = [s for s in result.steps if s.tool and s.tool_output]
            has_extractable_content = any(
                len(
                    str(
                        s.tool_output.get("content")
                        or s.tool_output.get("results")
                        or s.tool_output.get("events")
                        or s.tool_output
                    )
                )
                >= 50
                for s in tool_steps
            )
            if has_extractable_content:
                self.extract_entities(turn, tool_steps, config)

        threading.Thread(target=run_background_tasks, daemon=True).start()

        return turn

    def extract_entities(self, turn: Turn, tool_steps: list[Step], config: Config | None = None) -> list[SessionEntity]:
        extracted = []
        for step in tool_steps:
            output = step.tool_output
            tool_name = step.tool
            content = str(output.get("content") or output.get("results") or output.get("events") or output)
            if len(content) < 50:
                continue

            content = content[:2000]  # Truncate if longer

            llm = get_model("text", "fast", config=config)
            prompt = f'Extract named entities from this text. Return JSON only:\n{{\n  "entities": [\n    {{"type": "place|person|date|org|amount", "value": "...", "confidence": 0.0-1.0}}\n  ]\n}}\n\nText: {content}\n\nOnly extract: places, people, dates, organizations, monetary amounts.\nSkip generic words. Confidence > 0.7 only.'
            try:
                res = llm.generate(prompt)
                try:
                    import re as regex

                    json_match = regex.search(r"\{.*\}", res, regex.DOTALL)
                    parsed = json.loads(json_match.group()) if json_match else json.loads(res)

                    entities_raw = parsed.get("entities", [])
                    for e in entities_raw:
                        conf = e.get("confidence", 0.0)
                        if conf >= 0.7:
                            entity = SessionEntity(
                                entity_type=e.get("type", "unknown"),
                                value=e.get("value", ""),
                                turn_id=turn.turn_id,
                                source_tool=tool_name,
                                confidence=conf,
                            )
                            extracted.append(entity)
                except Exception as e:
                    logger.warning(f"Failed to parse entity extraction JSON: {e}")
            except Exception as e:
                logger.warning(f"Entity extraction LLM call failed: {e}")

        if extracted:
            # Check if the DB table exists before querying
            try:
                with open_db(self.db_path) as conn:
                    conn.execute("SELECT 1 FROM session_entities LIMIT 1")
            except sqlite3.OperationalError:
                return extracted

            with open_db(self.db_path) as conn:
                for entity in extracted:
                    # Deduplication: store once per session (upsert on session_id + entity_type + value)
                    conn.execute(
                        """
                        INSERT INTO session_entities (session_id, turn_id, entity_type, value, source_tool, confidence)
                        SELECT ?, ?, ?, ?, ?, ?
                        WHERE NOT EXISTS (
                            SELECT 1 FROM session_entities
                            WHERE session_id = ? AND entity_type = ? AND value = ?
                        )
                        """,
                        (
                            self.session_id,
                            entity.turn_id,
                            entity.entity_type,
                            entity.value,
                            entity.source_tool,
                            entity.confidence,
                            self.session_id,
                            entity.entity_type,
                            entity.value,
                        ),
                    )
                conn.commit()

        return extracted

    def get_entities(self, entity_type: str | None = None) -> list[SessionEntity]:
        # Check if the DB table exists before querying
        try:
            with open_db(self.db_path) as conn:
                conn.execute("SELECT 1 FROM session_entities LIMIT 1")
        except sqlite3.OperationalError:
            return []

        with open_db(self.db_path) as conn:
            query = (
                "SELECT entity_type, value, turn_id, source_tool, confidence FROM session_entities WHERE session_id = ?"
            )
            params = [self.session_id]
            if entity_type:
                query += " AND entity_type = ?"
                params.append(entity_type)

            rows = conn.execute(query, params).fetchall()
            return [
                SessionEntity(entity_type=row[0], value=row[1], turn_id=row[2], source_tool=row[3], confidence=row[4])
                for row in rows
            ]

    def get_context_block(self) -> str:
        # Check if the DB table exists before querying
        try:
            with open_db(self.db_path) as conn:
                conn.execute("SELECT 1 FROM session_turns LIMIT 1")
        except sqlite3.OperationalError:
            return ""

        with open_db(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT turn_id, query, answer, tools_called, exit_reason, summary, created_at
                FROM session_turns
                WHERE session_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (self.session_id, self.FULL_WINDOW + self.SUMMARY_WINDOW),
            ).fetchall()

        if not rows:
            return ""

        # Check for stale session (>30 min)
        last_turn_time = datetime.fromisoformat(rows[0][6])
        if datetime.now(timezone.utc) - last_turn_time > timedelta(minutes=30):
            return ""

        lines = ["Recent conversation:"]
        # Rows are in DESC order (most recent first)
        rows.reverse()  # Now in ASC order

        num_turns = len(rows)
        for i, row in enumerate(rows):
            turn_idx_from_end = num_turns - i

            query = row[1]
            answer = row[2]
            tools = json.loads(row[3])
            summary = row[5]

            prefix = "[last turn]" if turn_idx_from_end == 1 else f"[{turn_idx_from_end} turns ago]"

            if turn_idx_from_end <= self.FULL_WINDOW:
                # Full detail
                tools_str = f" (Tools: {', '.join(tools)})" if tools else ""
                lines.append(f"{prefix} Q: {query}\n{prefix} A: {answer}{tools_str}")
            else:
                # Summary
                if summary:
                    lines.append(f"{prefix} {summary}")
                else:
                    # Fallback if summary not yet generated
                    lines.append(f"{prefix} {query} -> {answer[:50]}...")

        # Append entities
        entities = self.get_entities()
        if entities:
            lines.append("\nKnown from this conversation:")
            for entity in entities:
                lines.append(f"  {entity.entity_type.capitalize()}: {entity.value}")

        return "\n".join(lines)

    def is_continuation(self, query: str) -> bool:
        # Check if the DB table exists before querying
        try:
            with open_db(self.db_path) as conn:
                conn.execute("SELECT 1 FROM session_turns LIMIT 1")
        except sqlite3.OperationalError:
            return False

        markers = [
            r"\b(it|this|that|those|these|him|her|them|message|one|item)\b",
            r"\b(first|second|third|last|other) one\b",
            r"\b(all of them|that)\b",
            r"\byes\b",
            r"\bno\b",
            r"\b(the same thing)\b",
        ]

        # Signal 1: pronoun/reference detection
        signal1 = any(re.search(marker, query, re.IGNORECASE) for marker in markers)

        # Signal 2: pending question check
        signal2 = False
        with open_db(self.db_path) as conn:
            last_turn = conn.execute(
                "SELECT exit_reason FROM session_turns WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
                (self.session_id,),
            ).fetchone()

            if last_turn and last_turn[0] == "ask_user" and len(query.split()) < 20:
                signal2 = True

        # Return True only if at least one prior turn exists AND (Signal 1 OR Signal 2)
        has_prior_turns = False
        with open_db(self.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM session_turns WHERE session_id = ?", (self.session_id,)
            ).fetchone()[0]
            has_prior_turns = count > 0

        return has_prior_turns and (signal1 or signal2)

    def summarise_old_turns(self, config: Config | None = None) -> None:
        # Check if the DB table exists before querying
        try:
            with open_db(self.db_path) as conn:
                conn.execute("SELECT 1 FROM session_turns LIMIT 1")
        except sqlite3.OperationalError:
            return

        with open_db(self.db_path) as conn:
            # Find turns that need summary: those beyond FULL_WINDOW + SUMMARY_WINDOW but still in SUMMARY_WINDOW?
            # Actually spec says: "For turns beyond the FULL_WINDOW + SUMMARY_WINDOW range"
            # WAIT. get_context_block uses FULL_WINDOW in full detail, and next SUMMARY_WINDOW as one-liner.
            # summarise_old_turns says: "For turns beyond the FULL_WINDOW + SUMMARY_WINDOW range"
            # But get_context_block says "Next SUMMARY_WINDOW turns: one-liner summary only".
            # This implies turns in SUMMARY_WINDOW should have a summary.

            # Re-reading: "For turns beyond the FULL_WINDOW + SUMMARY_WINDOW range: ... skip if already populated ... generate one-liner ... Store result in summary column"
            # "This runs after each add_turn() call. Keeps the DB clean without losing history."

            # Actually, SUMMARY_WINDOW is for turns 3-6. So turns 3 and older should be summarized.
            # Let's get turns where summary is empty and they are not the 2 most recent.
            rows = conn.execute(
                """
                SELECT turn_id, query, answer
                FROM session_turns
                WHERE session_id = ? AND summary = ''
                ORDER BY created_at DESC
                LIMIT -1 OFFSET ?
                """,
                (self.session_id, self.FULL_WINDOW),
            ).fetchall()

            if not rows:
                return

            llm = get_model(specialty="text", effort="fast", config=config)
            for turn_id, query, answer in rows:
                prompt = f"Summarise this exchange in one sentence: Q: {query} A: {answer}"
                try:
                    summary = llm.generate(prompt).strip()
                    conn.execute("UPDATE session_turns SET summary = ? WHERE turn_id = ?", (summary, turn_id))
                except Exception as e:
                    logger.warning(f"Failed to summarise turn {turn_id}: {e}")

            conn.commit()
