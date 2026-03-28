# Step 16 — Session Context Phase 2: Entity Extraction

## Goal

Extend the session context layer (step-15) with entity extraction from tool outputs.
This enables cross-domain implicit references — the Miami conference example where
"what's the weather?" should resolve to Miami because a previous tool call returned
email content mentioning the Miami Convention Center.

Phase 1 (step-15) handles turn management and explicit references ("which one",
"that email"). Phase 2 handles implicit references where the connection exists in
tool *output content*, not in the query itself.

## Prerequisite

Step 15 must be merged first.

---

## New: `EntityStore` in `xibi/session.py`

Add to the existing `SessionContext` class.

### `SessionEntity` dataclass

```python
@dataclass
class SessionEntity:
    entity_type: str    # "place" | "person" | "date" | "org" | "amount"
    value: str          # e.g. "Miami", "March 28th", "Acme Corp", "$2,400"
    source_turn_id: str # Which turn this came from
    source_tool: str    # Which tool output it was extracted from
    confidence: float   # 0.0–1.0 from extractor
```

### Extraction method

```python
def extract_entities(self, turn: Turn, tool_outputs: list[dict]) -> list[SessionEntity]:
```

Called inside `add_turn()` after the turn is persisted.

For each tool output in the turn's steps:
1. Concatenate output content into a single string (max 2000 chars)
2. Call `get_model("text", "fast")` with a structured extraction prompt:

```
Extract named entities from this text. Return JSON only:
{
  "entities": [
    {"type": "place|person|date|org|amount", "value": "...", "confidence": 0.0-1.0}
  ]
}

Text: {tool_output_content}

Only extract: places, people, dates, organizations, monetary amounts.
Skip generic words. Confidence > 0.7 only.
```

3. Parse response, filter to `confidence >= 0.7`
4. Persist to `session_entities` table
5. Return extracted entities

### Retrieval method

```python
def get_entities(self, entity_type: str | None = None) -> list[SessionEntity]:
```

Returns all entities from the current session, optionally filtered by type.
Used by `get_context_block()` to append an entities section.

### Updated `get_context_block()`

Append entity block after turn summaries:

```
Recent conversation:
[last turn] Checked email — found conference invite from boss.

Known from this conversation:
  Place: Miami Convention Center
  Date: March 28th
  Org: Acme Corp
```

This gives the LLM the connective tissue to infer "what's the weather?" means Miami.

---

## DB migration

Add `session_entities` table. Bump `SCHEMA_VERSION`.

```sql
CREATE TABLE IF NOT EXISTS session_entities (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    turn_id      TEXT NOT NULL,
    entity_type  TEXT NOT NULL,
    value        TEXT NOT NULL,
    source_tool  TEXT NOT NULL,
    confidence   REAL NOT NULL,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_session_entities_session
    ON session_entities (session_id, entity_type);
```

TTL: purge with parent session (30 days).

---

## Cost and performance notes

**One LLM call per tool output per turn.** Using `text.fast` (Ollama local or
Gemini Flash). At personal agent scale (5-10 tool calls/day) this is negligible.
At 100 tool calls/day it's ~100 fast model calls — still acceptable.

**Extraction is async from the user's perspective.** Call `add_turn()` in a
background thread so entity extraction doesn't block the response to the user.
The entities will be available by the next turn.

**Empty output handling.** If the tool output has no extractable entities (e.g.
a status check returning `{"status": "ok"}`), the extractor returns an empty list
immediately without an LLM call. Check output length < 50 chars → skip.

**Deduplication.** If "Miami" appears in 3 consecutive tool outputs, store it
once per session (upsert on `session_id + entity_type + value`).

---

## Failure modes and mitigations

**LLM returns malformed JSON** → catch parse error, log warning, return empty list.
Entity extraction is best-effort — a failed extraction degrades gracefully (no entities
for that turn, not a crash).

**LLM hallucinates entities not in the text** → confidence threshold of 0.7 filters
most hallucinations. Low-confidence extractions are ignored entirely.

**Entity from old context misleads new query** → entities are scoped to the current
session (daily reset in Telegram, process lifetime in CLI). If the session resets,
entities reset too. Stale entities from yesterday don't pollute today's context.

---

## Tests: `tests/test_entities.py`

1. `test_extract_entities_from_email_content` — email mentioning Miami → place entity extracted
2. `test_extract_entities_filters_low_confidence` — confidence <0.7 → not stored
3. `test_extract_entities_skips_short_output` — output <50 chars → no LLM call
4. `test_extract_entities_handles_parse_error` — malformed LLM response → empty list, no crash
5. `test_entities_deduplicated` — Miami appears 3 times → stored once
6. `test_get_entities_filtered_by_type` — get_entities("place") → only places
7. `test_context_block_includes_entities` — after email turn → context block shows Miami
8. `test_weather_query_resolves_to_conference_city` — email mentions Miami → weather query gets Miami in context
9. `test_entities_reset_with_session` — new session_id → no entities from previous session
10. `test_extraction_runs_async` — add_turn() returns before extraction completes

---

## Linting

`ruff check xibi/ tests/test_entities.py` and `ruff format` before committing.

## Constraints

- Entity extraction is best-effort — failures degrade gracefully, never crash
- Extraction is async — never blocks the user response
- `text.fast` only — never use `text.think` or `text.review` for extraction
- Max 2000 chars of tool output fed to extractor — truncate if longer
- Zero new external dependencies (no spaCy, no NLTK)
