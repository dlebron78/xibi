# Step 05 — Shadow Matcher (BM25 Router)

## Goal

Implement `xibi/routing/shadow.py` — a pure-Python BM25 scorer that routes queries
directly to a tool when confidence is high, or injects a hint into the ReAct scratchpad
when confidence is moderate. This is a promotion from observe-only (bregger_shadow.py)
to an active router in Xibi.

## Source reference

Read `bregger_shadow.py` in the repo root for the BM25 math and corpus-building logic.
Do not copy it — reimplement cleanly with types, dataclasses, and the changes below.

---

## File to create: `xibi/routing/shadow.py`

### Dataclasses

```python
@dataclass
class ShadowMatch:
    tool: str          # matched tool name
    skill: str         # skill that owns the tool
    phrase: str        # corpus phrase that matched
    score: float       # normalised BM25 confidence 0.0–1.0
    tier: str          # "direct" (>=0.85) | "hint" (0.65–0.85) | "none" (<0.65)
```

### Class: `ShadowMatcher`

```python
class ShadowMatcher:
    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None: ...
    def build_corpus(self, documents: list[tuple[str, str, str]]) -> None: ...
    def load_manifests(self, skills_dir: str | Path) -> None: ...
    def match(self, query: str) -> ShadowMatch | None: ...
```

### Routing thresholds (class constants)

```python
DIRECT_THRESHOLD = 0.85   # skip ReAct, call tool directly
HINT_THRESHOLD   = 0.65   # inject as hint into ReAct context
```

### `build_corpus` behaviour

- Input: list of `(skill_name, tool_name, example_phrase)` triples
- Tokenise with `re.findall(r'\b\w+\b', text.lower())`
- Compute standard BM25 IDF: `log(1 + (N - f_t + 0.5) / (f_t + 0.5))`
- Store per-document term frequencies and doc lengths
- Precompute each document's self-score (max possible score) for normalisation
- Empty corpus is valid — `match()` returns `None`

### `load_manifests` behaviour

- Globs `skills_dir/*/manifest.json`
- For each manifest: reads `tools[].examples[]`, strips after `->` or `—`
- Calls `build_corpus()` with resulting triples
- Silently skips malformed manifests (log warning, continue)

### `match` behaviour

- Tokenise query
- Score query against every corpus document using BM25
- Normalise best raw score against that document's self-score
- If normalised score >= `DIRECT_THRESHOLD`: return `ShadowMatch(..., tier="direct")`
- If normalised score >= `HINT_THRESHOLD`: return `ShadowMatch(..., tier="hint")`
- Otherwise: return `None`

---

## Integration: `xibi/react.py`

Add optional `shadow: ShadowMatcher | None = None` parameter to `run()`.

Routing order at top of `run()`:

1. `control_plane.match()` first (exact intents, step 04)
2. `shadow.match()` second:
   - `tier == "direct"` -> call tool via executor, return `ReActResult` without entering loop
   - `tier == "hint"` -> prepend hint to `context`: `f"[Shadow hint: consider using {match.tool}]\n{context}"`
   - `None` -> fall through to ReAct loop as normal

---

## Update: `xibi/routing/__init__.py`

Export `ShadowMatcher` and `ShadowMatch`:

```python
from xibi.routing.control_plane import ControlPlaneRouter, RoutingDecision
from xibi.routing.shadow import ShadowMatch, ShadowMatcher

__all__ = ["ControlPlaneRouter", "RoutingDecision", "ShadowMatch", "ShadowMatcher"]
```

---

## Update: `xibi/__init__.py`

Add to exports:
```python
from xibi.routing.shadow import ShadowMatch, ShadowMatcher
```

---

## Tests: `tests/test_shadow.py`

Write at least 15 tests. Required coverage:

### BM25 scoring
1. `test_exact_phrase_match_is_direct` — single-doc corpus, query = exact phrase -> tier "direct"
2. `test_partial_match_is_hint` — partial overlap query -> tier "hint"
3. `test_no_match_returns_none` — completely unrelated query -> None
4. `test_empty_corpus_returns_none` — `build_corpus([])` then `match()` -> None

### Corpus building
5. `test_build_corpus_sets_avg_doc_length` — check `avg_doc_length` is computed
6. `test_duplicate_documents_handled` — corpus with duplicate phrases -> no crash
7. `test_single_token_query` — one-word query against multi-word corpus -> returns result or None

### Manifest loading
8. `test_load_manifests_builds_corpus` — write a temp manifest.json, call `load_manifests()`, verify `match()` works
9. `test_load_manifests_missing_dir` — nonexistent dir -> no crash, empty corpus
10. `test_load_manifests_skips_malformed` — malformed JSON manifest -> skips, loads valid ones

### Thresholds
11. `test_score_below_hint_threshold_returns_none` — score just under 0.65 -> None
12. `test_score_between_thresholds_is_hint` — score ~0.70 -> tier "hint"
13. `test_score_above_direct_threshold_is_direct` — score ~0.90 -> tier "direct"

### React integration
14. `test_react_shadow_direct_calls_tool` — tier="direct", executor mock -> returns tool result without loop
15. `test_react_shadow_hint_prepends_context` — tier="hint" -> context contains `[Shadow hint: ...]`
16. `test_react_shadow_none_falls_through` — no shadow match -> normal ReAct loop runs
17. `test_react_shadow_after_control_plane` — control plane matches first -> shadow never called

---

## Type annotations

- All public methods fully annotated
- `from __future__ import annotations` at top of file
- No `Any` except where unavoidable

## Linting

Run `ruff check xibi/ tests/test_shadow.py` and `ruff format xibi/ tests/test_shadow.py` before committing.
