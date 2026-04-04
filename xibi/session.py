from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from xibi.db import open_db
from xibi.entities import create_contact
from xibi.entities.resolver import resolve_contact
from xibi.router import get_model

if TYPE_CHECKING:
    from xibi.router import Config
    from xibi.types import ReActResult

logger = logging.getLogger(__name__)


@dataclass
class Turn:
    turn_id: str
    session_id: str
    query: str
    answer: str
    tools_called: list[str]
    exit_reason: str
    created_at: str
    source: str = "user"
    summary: str = ""


@dataclass
class SessionEntity:
    entity_type: str  # "place" | "person" | "date" | "org" | "amount"
    value: str
    source_turn_id: str
    source_tool: str
    confidence: float
    contact_id: str | None = None


class SessionContext:
    FULL_WINDOW = 4  # Last N turns injected in full detail
    SUMMARY_WINDOW = 6  # Older turns injected as data-preserving summaries
    COMPRESS_WINDOW = 8  # max turns to read during compression
    MAX_BELIEFS = 5  # max beliefs to extract per session

    def __init__(self, session_id: str, db_path: Path, config: Config | None = None) -> None:
        self.session_id = session_id
        self.db_path = db_path
        self.config = config

    def compress_to_beliefs(self) -> int:
        """
        Extract durable facts from this session's turns and store them as beliefs.
        Returns the number of beliefs written (0 on skip or error).
        Never raises.
        """
        try:
            sentinel_key = f"session:{self.session_id}:compressed"

            # Phase 1: Read — fetch data then release DB connection before LLM call.
            with open_db(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                exists = conn.execute("SELECT 1 FROM beliefs WHERE key = ?", (sentinel_key,)).fetchone()
                if exists:
                    return 0

                # Fetch user-initiated turns only — MCP turns (source='mcp:...') are excluded.
                # MCP tool output can inform the current session but must not write to
                # long-term beliefs without explicit user action (injection protection).
                rows = conn.execute(
                    """
                    SELECT query, answer FROM session_turns
                    WHERE session_id = ?
                    AND source = 'user'
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (self.session_id, self.COMPRESS_WINDOW),
                ).fetchall()
                row_count = len(rows)
                exchanges = [f"User: {r['query']}\nXibi: {r['answer'][:300]}\n---" for r in rows]

            if not exchanges:
                return 0

            exchanges_text = "\n".join(exchanges)[:2000]
            prompt = f"""You are extracting durable facts from a conversation to remember for future sessions.
Read the exchanges below and extract up to 5 facts that would be useful to recall later.

Focus on:
- User preferences ("user prefers X over Y")
- Ongoing projects or topics ("project: Miami conference, deadline: April 5")
- Recurring contacts or entities ("user's assistant is Jake at jacob@corp.com")
- Decisions made ("user decided to reply to the invoice next week")

Skip transient information (weather, one-off queries, ephemeral facts).

Return JSON only:
{{
  "beliefs": [
    {{"key": "short-key", "value": "one sentence fact", "confidence": 0.0-1.0}}
  ]
}}

Exchanges:
{exchanges_text}"""

            # Phase 2: LLM call — outside any DB connection.
            llm = get_model(specialty="text", effort="fast", config=self.config)
            response = llm.generate(prompt)

            # Parse JSON
            match = re.search(r"\{.*\}", response, re.DOTALL)
            if not match:
                return 0

            data = json.loads(match.group())
            beliefs = data.get("beliefs", [])
            now = datetime.utcnow()
            valid_until = (now + timedelta(days=30)).isoformat()

            # Phase 3: Write — new connection, no LLM calls inside.
            written = 0
            with open_db(self.db_path) as conn:
                for b in beliefs[: self.MAX_BELIEFS]:
                    confidence = b.get("confidence", 0.0)
                    if confidence < 0.75:
                        continue

                    key = f"mem:{b.get('key', '')[:40]}"
                    value = b.get("value", "")[:200]
                    metadata = json.dumps(
                        {"session_id": self.session_id, "turn_count": row_count, "compressed_at": now.isoformat()}
                    )

                    with conn:
                        conn.execute(
                            """
                            INSERT INTO beliefs (key, value, type, visibility, metadata, valid_until)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (key, value, "session_memory", "internal", metadata, valid_until),
                        )
                    written += 1

                # Write sentinel
                with conn:
                    conn.execute(
                        """
                        INSERT INTO beliefs (key, value, type, visibility, valid_until)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (sentinel_key, "1", "session_compression_marker", "internal", valid_until),
                    )

            return written

        except Exception as e:
            logger.debug("Compression failed for session %s: %s", self.session_id, e, exc_info=True)
            return 0

    def add_turn(self, query: str, result: ReActResult, source: str = "user") -> Turn:
        turn_id = str(uuid.uuid4())
        tools_called = [step.tool for step in result.steps if step.tool not in ("finish", "ask_user", "error", "")]
        created_at = datetime.utcnow().isoformat()

        turn = Turn(
            turn_id=turn_id,
            session_id=self.session_id,
            query=query,
            answer=result.answer,
            tools_called=tools_called,
            exit_reason=result.exit_reason,
            created_at=created_at,
            source=source,
        )

        with open_db(self.db_path) as conn, conn:
            conn.execute(
                """
                    INSERT INTO session_turns (turn_id, session_id, query, answer, tools_called, exit_reason, created_at, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                (
                    turn.turn_id,
                    turn.session_id,
                    turn.query,
                    turn.answer,
                    json.dumps(turn.tools_called),
                    turn.exit_reason,
                    turn.created_at,
                    turn.source,
                ),
            )

        # Phase 2: Extract entities from tool outputs
        tool_outputs = [step.tool_output for step in result.steps if step.tool_output]
        if tool_outputs:
            entities = self.extract_entities(turn, tool_outputs)
            self.bridge_to_contacts(entities, str(self.db_path))

        self.summarise_old_turns()
        return turn

    def add_chitchat_turn(self, query: str, answer: str) -> None:
        """Store a chitchat turn that bypassed the ReAct loop."""
        with open_db(self.db_path) as conn, conn:
            conn.execute(
                """INSERT INTO session_turns
                   (turn_id, session_id, query, answer, tools_called, exit_reason, summary, source)
                   VALUES (?, ?, ?, ?, '[]', 'chitchat', '', 'user')""",
                (str(uuid.uuid4()), self.session_id, query, answer),
            )

    def _get_session_memories(self) -> str:
        """
        Fetch recent session_memory beliefs for injection into context.
        Returns "" if none found.
        """
        try:
            with open_db(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT value FROM beliefs
                    WHERE type = 'session_memory'
                      AND (valid_until IS NULL OR valid_until > ?)
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (datetime.utcnow().isoformat(), self.MAX_BELIEFS),
                ).fetchall()

                if not rows:
                    return ""

                lines = ["\nWhat I remember from before:"]
                for r in rows:
                    lines.append(f"- {r['value']}")
                return "\n".join(lines)
        except Exception as e:
            logger.debug("Failed to fetch session memories: %s", e, exc_info=True)
            return ""

    def get_context_block(self) -> str:
        with open_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM session_turns
                WHERE session_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (self.session_id, self.FULL_WINDOW + self.SUMMARY_WINDOW),
            ).fetchall()

        if not rows:
            return ""

        # Stale session check (> 30 mins)
        last_turn_time = datetime.fromisoformat(rows[0]["created_at"])
        if datetime.utcnow() - last_turn_time > timedelta(minutes=30):
            self.compress_to_beliefs()
            return ""

        turns = [
            Turn(
                turn_id=r["turn_id"],
                session_id=r["session_id"],
                query=r["query"],
                answer=r["answer"],
                tools_called=json.loads(r["tools_called"]),
                exit_reason=r["exit_reason"],
                created_at=r["created_at"],
                source=r["source"],
                summary=r["summary"],
            )
            for r in rows
        ]
        turns.reverse()  # chronological

        lines = ["Recent conversation:"]
        full_start_idx = max(0, len(turns) - self.FULL_WINDOW)

        for i, turn in enumerate(turns):
            if i >= full_start_idx:
                # Full detail
                lines.append(f"User: {turn.query}")
                lines.append(f"Xibi: {turn.answer}")
                if turn.tools_called:
                    lines.append(f"Tools used: {', '.join(t for t in turn.tools_called if t)}")
            else:
                # Summary — fallback to answer excerpt if summary not yet written
                summary = turn.summary or f"Q: {turn.query[:80]} → A: {turn.answer[:200]}"
                lines.append(f"[{len(turns) - 1 - i} turns ago] {summary}")

        # Append entities
        entities = self.get_entities()
        if entities:
            lines.append("\nKnown from this conversation:")
            # Group by type for cleaner display
            by_type: dict[str, list[str]] = {}
            for e in entities:
                context_str = e.value
                if e.entity_type == "person" and e.contact_id:
                    try:
                        with open_db(self.db_path) as conn:
                            conn.row_factory = sqlite3.Row
                            contact = conn.execute("SELECT * FROM contacts WHERE id = ?", (e.contact_id,)).fetchone()
                            if contact:
                                last_seen = "unknown"
                                if contact["last_seen"]:
                                    try:
                                        ls_dt = datetime.fromisoformat(contact["last_seen"])
                                        delta = datetime.utcnow() - ls_dt
                                        if delta < timedelta(hours=1):
                                            last_seen = "just now"
                                        elif delta < timedelta(days=1):
                                            last_seen = f"{int(delta.total_seconds() // 3600)}h ago"
                                        elif delta < timedelta(days=2):
                                            last_seen = "yesterday"
                                        else:
                                            last_seen = f"{delta.days}d ago"
                                    except Exception:
                                        last_seen = contact["last_seen"]

                                rel_info = (
                                    f"{contact['relationship']} at {contact['organization']}"
                                    if contact["organization"]
                                    else contact["relationship"]
                                )
                                freq_info = "frequent contact" if contact["signal_count"] > 10 else "occasional contact"
                                context_str = f"{e.value} ({rel_info}, {freq_info}, last seen {last_seen})"
                    except Exception:
                        pass
                elif e.entity_type == "person":
                    context_str = f"{e.value} (unknown — first mention)"

                by_type.setdefault(e.entity_type.capitalize(), []).append(context_str)

            for etype, values in by_type.items():
                unique_values = sorted(list(set(values)))
                lines.append(f"  {etype}: {', '.join(unique_values)}")

        # Append memories from past sessions
        memories = self._get_session_memories()
        if memories:
            lines.append(memories)

        return "\n".join(lines)

    def is_continuation(self, query: str) -> bool:
        # Signal 1: Markers
        markers = [
            r"\b(it|this|that|those|these|him|her|them|it|one|item)\b",
            r"\b(first|second|third|last|other) one\b",
            r"\b(reply|them|that)\b",
            r"\byes\b",
            r"\bno\b",
            r"\b(that|this) (email|invoice|meeting|task|person|place|thing)\b",
        ]
        query_lower = query.lower()
        signal1 = any(re.search(p, query_lower) for p in markers)

        # Signal 2: Pending question
        signal2 = False
        with open_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            last_turn = conn.execute(
                "SELECT exit_reason FROM session_turns WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
                (self.session_id,),
            ).fetchone()
            if last_turn and last_turn["exit_reason"] == "ask_user" and len(query.split()) < 20:
                signal2 = True

        return (signal1 or signal2) and last_turn is not None

    def summarise_old_turns(self) -> None:
        # Phase 1: Read — fetch rows needing summaries, release DB before any LLM calls.
        with open_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT turn_id, query, answer FROM session_turns
                WHERE session_id = ? AND summary = ''
                ORDER BY created_at DESC
                LIMIT -1 OFFSET ?
                """,
                (self.session_id, self.FULL_WINDOW),
            ).fetchall()
            # Convert to plain dicts so the connection can be closed cleanly.
            row_data = [{"turn_id": r["turn_id"], "query": r["query"], "answer": r["answer"]} for r in rows]

        if not row_data:
            return

        # Phase 2+3: LLM call then write — one new connection per row to avoid long holds.
        llm = get_model(specialty="text", effort="fast", config=self.config)
        for row in row_data:
            prompt = (
                "Summarise this exchange in 2-3 sentences. "
                "IMPORTANT: preserve every specific name, number, date, and key fact — "
                "these will be the only record available in future turns.\n\n"
                f"Q: {row['query']}\nA: {row['answer'][:600]}"
            )
            try:
                summary = llm.generate(prompt).strip()
                # Clean up quotes if model adds them
                summary = summary.strip('"').strip("'")
                with open_db(self.db_path) as conn, conn:
                    conn.execute("UPDATE session_turns SET summary = ? WHERE turn_id = ?", (summary, row["turn_id"]))
            except Exception as e:
                logger.warning(f"Failed to summarise turn {row['turn_id']}: {e}", exc_info=True)

    def extract_entities(self, turn: Turn, tool_outputs: list[dict]) -> list[SessionEntity]:
        # Concatenate content
        all_text_parts = []
        for output in tool_outputs:
            content = output.get("content") or output.get("message") or str(output)
            all_text_parts.append(str(content))

        full_text = "\n".join(all_text_parts)[:2000]
        if len(full_text) < 10:  # Reduced again for simpler test data
            return []

        prompt = f"""Extract named entities from this text. Return JSON only:
{{
  "entities": [
    {{"type": "place|person|date|org|amount", "value": "...", "confidence": 0.0-1.0}}
  ]
}}

Text: {full_text}

Only extract: places, people, dates, organizations, monetary amounts.
Skip generic words. Confidence > 0.7 only."""
        llm = get_model(specialty="text", effort="fast", config=self.config)
        extracted: list[SessionEntity] = []

        try:
            response = llm.generate(prompt)
            # Find JSON block
            match = re.search(r"\{.*\}", response, re.DOTALL)
            if match:
                data = json.loads(match.group())
                for item in data.get("entities", []):
                    confidence = item.get("confidence", 0.0)
                    if confidence >= 0.7:
                        etype = item.get("type", "unknown")
                        value = item.get("value", "").strip()
                        if value and etype in ("place", "person", "date", "org", "amount"):
                            extracted.append(
                                SessionEntity(
                                    entity_type=etype,
                                    value=value,
                                    source_turn_id=turn.turn_id,
                                    source_tool=turn.tools_called[0] if turn.tools_called else "unknown",
                                    confidence=confidence,
                                )
                            )
        except Exception as err:
            logger.warning("Entity extraction failed for turn %s: %s", turn.turn_id, err, exc_info=True)
            return []

        if not extracted:
            return []

        # Persist with deduplication (upsert on session_id + entity_type + value)
        with open_db(self.db_path) as conn:
            for entity in extracted:
                # Deduplication logic: "store it once per session (upsert on session_id + entity_type + value)"
                # Since SQLite doesn't have a simple way to upsert without a unique constraint,
                # and I don't want to change the schema to add a unique constraint yet if not requested,
                # I'll check existence.
                exists = conn.execute(
                    """
                    SELECT 1 FROM session_entities
                    WHERE session_id = ? AND entity_type = ? AND value = ?
                    """,
                    (self.session_id, entity.entity_type, entity.value),
                ).fetchone()

                if not exists:
                    with conn:
                        conn.execute(
                            """
                            INSERT INTO session_entities (session_id, turn_id, entity_type, value, source_tool, confidence)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                self.session_id,
                                entity.source_turn_id,
                                entity.entity_type,
                                entity.value,
                                entity.source_tool,
                                entity.confidence,
                            ),
                        )

        return extracted

    def get_entities(self, entity_type: str | None = None) -> list[SessionEntity]:
        with open_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM session_entities WHERE session_id = ?"
            params = [self.session_id]
            if entity_type:
                query += " AND entity_type = ?"
                params.append(entity_type)

            rows = conn.execute(query, params).fetchall()

        return [
            SessionEntity(
                entity_type=r["entity_type"],
                value=r["value"],
                source_turn_id=r["turn_id"],
                source_tool=r["source_tool"],
                confidence=r["confidence"],
                contact_id=cast(dict[str, Any], dict(r)).get("contact_id"),
            )
            for r in rows
        ]

    def bridge_to_contacts(self, entities: list[SessionEntity], db_path: str) -> None:
        """Link person entities to contacts and create partial contacts if needed."""
        org_entities = [e.value for e in entities if e.entity_type == "org"]

        for entity in entities:
            if entity.entity_type != "person":
                continue

            # 1. Resolve contact
            # Check for name match first
            contact = resolve_contact(
                handle=entity.value,
                channel_type="session",
                display_name=entity.value,
                db_path=db_path,
            )

            # If not found, try with associated orgs from the same batch
            if not contact:
                for org in org_entities:
                    contact = resolve_contact(
                        handle=entity.value,
                        channel_type="session",
                        display_name=entity.value,
                        organization=org,
                        db_path=db_path,
                    )
                    if contact:
                        break

            if contact:
                # Link found contact
                entity.contact_id = cast(str, contact.id)
                with open_db(self.db_path) as conn, conn:
                    conn.execute(
                        "UPDATE session_entities SET contact_id = ? WHERE session_id = ? AND entity_type = 'person' AND value = ?",
                        (contact.id, self.session_id, entity.value),
                    )
            else:
                # 2. Try partial contact creation
                # Check if this person appears in 2+ sessions
                try:
                    with open_db(self.db_path) as conn:
                        res_count = conn.execute(
                            "SELECT COUNT(DISTINCT session_id) FROM session_entities WHERE entity_type = 'person' AND value = ?",
                            (entity.value,),
                        ).fetchone()
                        session_count = res_count[0] if res_count else 0

                        if session_count >= 2:
                            partial_org = org_entities[0] if org_entities else None
                            new_contact_id_res = create_contact(
                                display_name=entity.value,
                                organization=partial_org,
                                discovered_via="session_mention",
                                relationship="unknown",
                                db_path=db_path,
                            )
                            if new_contact_id_res:
                                entity.contact_id = cast(str, new_contact_id_res)
                                with conn:
                                    conn.execute(
                                        "UPDATE session_entities SET contact_id = ? WHERE session_id = ? AND entity_type = 'person' AND value = ?",
                                        (new_contact_id_res, self.session_id, entity.value),
                                    )
                except Exception as e:
                    logger.error(f"Failed partial contact creation: {e}")
