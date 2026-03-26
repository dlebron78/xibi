# Step 09 — MessageModeClassifier Redesign

## Goal

Replace the binary `MessageModeClassifier` from `bregger_core.py` with a probabilistic
scoring classifier that integrates with `ShadowMatcher` to produce nuanced routing decisions.
Also fix the Shadow `direct` dispatch gap: when BM25 identifies a tool directly, populate
`tool_input` from the query so the executor has real parameters, not empty `{}`.

Two deliverables:
- `xibi/routing/classifier.py` — `MessageModeClassifier` returning `ModeScores` (probabilistic)
- Update `xibi/routing/shadow.py` — add `extract_tool_input(query, match)` function

Public API when done:

```python
from xibi.routing.classifier import MessageModeClassifier, ModeScores

clf = MessageModeClassifier(shadow=shadow_matcher)
scores: ModeScores = clf.classify("list my unread emails")
# ModeScores(command=0.87, conversation=0.13, dominant="command", confidence=0.74)
```

---

## File structure

```
xibi/
  routing/
    classifier.py      ← NEW
    shadow.py          ← MODIFY (add extract_tool_input)
    __init__.py        ← MODIFY (export MessageModeClassifier, ModeScores)
xibi/__init__.py       ← add MessageModeClassifier, ModeScores to exports
tests/
  test_classifier.py   ← NEW
```

---

## Source reference

Read `bregger_core.py` at the repo root and find the `MessageModeClassifier` class.
Do NOT copy it — reimplement cleanly with the design changes listed below.

Key differences from the Bregger version:
- Returns `ModeScores` dataclass (probabilistic) instead of a binary string `"command"|"conversation"`
- Integrates with `ShadowMatcher` so a high BM25 score shifts `command` probability upward
- No module-level mutable state
- `from __future__ import annotations` at top of both files
- Full type annotations throughout

---

## `xibi/routing/classifier.py`

### `ModeScores` dataclass

```python
@dataclass
class ModeScores:
    command: float      # 0.0–1.0
    conversation: float # 0.0–1.0
    dominant: str       # "command" | "conversation"
    confidence: float   # abs(command - conversation), 0.0–1.0
    shadow_hit: bool    # True if ShadowMatcher contributed to the score
    shadow_tier: str    # "direct" | "hint" | "none"
```

The two scores are not required to sum to 1.0 — they are independent probability estimates
that reflect different signals. The `dominant` field is whichever is higher.

### `MessageModeClassifier`

```python
class MessageModeClassifier:
    def __init__(
        self,
        shadow: ShadowMatcher | None = None,
        command_keywords: list[str] | None = None,
        conversation_keywords: list[str] | None = None,
    ) -> None:
        ...
```

`shadow` — optional `ShadowMatcher` instance. If provided, its match score informs
the command probability (see scoring rules below).

`command_keywords` — if None, use the default list below.
`conversation_keywords` — if None, use the default list below.

#### Default keyword lists

```python
DEFAULT_COMMAND_KEYWORDS = [
    "list", "show", "find", "search", "get", "fetch", "send", "create",
    "add", "delete", "remove", "update", "set", "check", "run", "execute",
    "read", "write", "summarize", "schedule", "remind", "email", "calendar",
    "file", "open", "close", "restart", "status", "ping",
]

DEFAULT_CONVERSATION_KEYWORDS = [
    "what", "why", "how", "who", "when", "where", "tell me", "explain",
    "help", "can you", "could you", "would you", "is it", "are you",
    "do you", "did you", "think", "feel", "opinion", "advice",
    "thanks", "thank you", "hi", "hello", "bye", "good morning",
]
```

#### `classify(query: str, shadow_match: ShadowMatch | None = None) -> ModeScores`

Compute `ModeScores` for the given query. Algorithm:

**Step 1: Keyword scoring**

Tokenize `query` (lowercase, split on whitespace and punctuation). For each token:
- If it's in `command_keywords`: add 0.15 to `raw_command` (cap contribution at 0.60 total from keywords)
- If it's in `conversation_keywords`: add 0.15 to `raw_conversation` (cap at 0.60 total from keywords)

Start both at 0.30 as a prior (neither mode has zero probability before any signal).

**Step 2: Shadow integration**

If `shadow_match` is provided (from caller), use it.
Else if `self.shadow` is set, call `self.shadow.query(query)` to get a match.

- If shadow_tier == `"direct"` (score ≥ 0.85): `raw_command += 0.40`, set `shadow_hit=True`, `shadow_tier="direct"`
- If shadow_tier == `"hint"` (0.65 ≤ score < 0.85): `raw_command += 0.20`, set `shadow_hit=True`, `shadow_tier="hint"`
- Else: `shadow_hit=False`, `shadow_tier="none"`

**Step 3: Question mark heuristic**

If `query.strip()` ends with `"?"`: `raw_conversation += 0.25`

**Step 4: Normalize**

Clamp both scores to `[0.0, 1.0]`. Do NOT normalize so they sum to 1.0 — keep them independent.

**Step 5: Build result**

```python
dominant = "command" if raw_command >= raw_conversation else "conversation"
confidence = abs(raw_command - raw_conversation)
return ModeScores(
    command=raw_command,
    conversation=raw_conversation,
    dominant=dominant,
    confidence=confidence,
    shadow_hit=shadow_hit,
    shadow_tier=shadow_tier,
)
```

#### `classify_bulk(queries: list[str]) -> list[ModeScores]`

Call `classify()` for each query. Return list of results in same order.

---

## `xibi/routing/shadow.py` — add `extract_tool_input`

### `extract_tool_input(query: str, match: ShadowMatch) -> dict[str, str]`

Given a query and a direct ShadowMatch, extract the most likely tool input parameters
by parsing the query relative to the matched phrase.

Algorithm:
1. Remove all words in the `match.phrase` from `query` (case-insensitive, whole-word only)
2. Strip leading/trailing whitespace and punctuation from the remainder
3. If the remainder is non-empty: return `{"input": remainder}`
4. Else: return `{}`

Example:
- query = `"list my unread emails"`, phrase = `"list unread emails"` → removes "list", "unread", "emails" → remainder = "my" → `{"input": "my"}`
- query = `"send email to bob about meeting"`, phrase = `"send email"` → remainder = `"to bob about meeting"` → `{"input": "to bob about meeting"}`
- query = `"list unread"`, phrase = `"list unread emails"` → remainder = `""` → `{}`

This is intentionally simple — it provides a starting point for the executor rather than trying
to do NLP parameter extraction. The executor or tool can refine further.

Add to `ShadowMatch` dataclass a new field:
```python
tool_input: dict[str, str] = field(default_factory=dict)  # populated by extract_tool_input for direct matches
```

Update `ShadowMatcher.query()` to call `extract_tool_input(query, match)` and populate
`match.tool_input` when `match.tier == "direct"`.

---

## `xibi/routing/__init__.py`

Add exports:
```python
from xibi.routing.classifier import MessageModeClassifier, ModeScores
```
Add both to `__all__`.

## Update `xibi/__init__.py`

Add to imports and `__all__`:
```python
from xibi.routing.classifier import MessageModeClassifier, ModeScores
```

---

## Tests — `tests/test_classifier.py`

Use no live network or LLM calls. Mock `ShadowMatcher` where needed.

1. `test_default_prior_no_signals` — empty query `""` → both scores ≈ 0.30, confidence < 0.05
2. `test_command_keyword_bumps_command` — query `"list emails"` → `command > conversation`
3. `test_conversation_keyword_bumps_conversation` — query `"what is the weather?"` → `conversation > command`
4. `test_question_mark_heuristic` — query `"are you busy?"` → conversation score higher than without `?`
5. `test_shadow_direct_bumps_command_strongly` — mock shadow returning `ShadowMatch(tier="direct", score=0.90, ...)` → `command >= 0.70`, `shadow_hit=True`, `shadow_tier="direct"`
6. `test_shadow_hint_bumps_command_moderately` — mock shadow returning `ShadowMatch(tier="hint", score=0.70, ...)` → command bumped but less than direct; `shadow_tier="hint"`
7. `test_shadow_none_no_bump` — mock shadow returning `ShadowMatch(tier="none", score=0.40, ...)` → `shadow_hit=False`
8. `test_dominant_command` — query with strong command signal → `dominant == "command"`
9. `test_dominant_conversation` — query `"tell me about yourself"` → `dominant == "conversation"`
10. `test_confidence_monotone` — high BM25 + command keyword → confidence > 0.50
11. `test_scores_clamped` — no score exceeds 1.0 even with many command keywords
12. `test_classify_bulk` — list of 3 queries → returns list of 3 `ModeScores`, correct length
13. `test_caller_provided_shadow_match_used` — pass explicit `shadow_match` param; verify no call to `self.shadow.query()`
14. `test_no_shadow_no_error` — `MessageModeClassifier(shadow=None)` works without error on any query
15. `test_modeScores_fields` — check all fields (`command`, `conversation`, `dominant`, `confidence`, `shadow_hit`, `shadow_tier`) are present and correctly typed

Tests for `extract_tool_input` (in `tests/test_classifier.py` or `tests/test_shadow.py`):

16. `test_extract_tool_input_basic` — `query="list my unread emails"`, `phrase="list unread emails"` → result contains non-empty `"input"` key
17. `test_extract_tool_input_empty_remainder` — query and phrase are identical → returns `{}`
18. `test_extract_tool_input_preserves_extra_words` — query has significant remainder after phrase words removed → `"input"` value is non-empty

---

## Type annotations

- `from __future__ import annotations` at top of `classifier.py`
- All public and private methods fully annotated
- `ModeScores` uses `@dataclass` with explicit field types

## Linting

Run `ruff check xibi/routing/classifier.py xibi/routing/shadow.py tests/test_classifier.py`
and `ruff format` before committing. Zero lint errors.
`mypy xibi/routing/classifier.py xibi/routing/shadow.py --ignore-missing-imports` must pass.

## Constraints

- Zero new external dependencies (stdlib only: `re`, `math`, `dataclasses`)
- No LLM calls in `classify()` — purely heuristic/statistical
- No module-level mutable state
- `ShadowMatcher.query()` must remain backward-compatible (existing callers unaffected)
- All tests pass with `pytest -m "not live"` — no live network or LLM calls
- CI must pass: `ruff check`, `ruff format --check`, `mypy`, `pytest`
- Do NOT import from `bregger_core.py` or any legacy module
