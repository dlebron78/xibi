# Step 05 — Shadow Matcher (BM25 Routing)

**Target file:** `xibi/routing/shadow.py`
**Supporting files:** `xibi/routing/__init__.py` (update exports), `tests/test_shadow.py`
**CI lint scope:** add `tests/test_shadow.py` to ruff check/format lines in `.github/workflows/ci.yml`

---

## Context

Bregger had a `ShadowMatcher` (`bregger_shadow.py`) that scored queries against skill manifest examples using BM25, but it was **observe-only** — it logged predictions but never influenced routing. Xibi fixes this from day one: the Shadow Matcher is a real routing tier that sits between the Control Plane Router and the full ReAct loop.

---

## Routing Tiers (post-step-05)

```
query
  └─► ControlPlaneRouter.match()   # exact/regex, step-04
        ├─ matched → short-circuit, canned response
        └─ no match ↓
  └─► ShadowMatcher.match()        # BM25, step-05 (NEW)
        ├─ score ≥ 0.85 → ShadowDecision(action="route",   tool=..., hint=None)
        ├─ 0.65 ≤ score < 0.85 → ShadowDecision(action="hint",    tool=..., hint=phrase)
        └─ score < 0.65  → ShadowDecision(action="passthrough", tool=None, hint=None)
  └─► ReAct loop                   # full reasoning, step-02
```

---

## Data Contract

### `ShadowDecision` dataclass

```python
@dataclass
class ShadowDecision:
    action: str          # "route" | "hint" | "passthrough"
    tool: str | None     # matched tool name (None for passthrough)
    score: float         # normalized BM25 confidence 0.0–1.0
    phrase_matched: str | None  # corpus phrase that scored highest
```

### Thresholds (class-level constants, not hardcoded inline)

```python
ROUTE_THRESHOLD = 0.85
HINT_THRESHOLD  = 0.65
```

---

## `ShadowMatcher` class

Located at `xibi/routing/shadow.py`.

### Constructor

```python
def __init__(self, k1: float = 1.5, b: float = 0.75) -> None
```

BM25 hyperparameters only. Thresholds are class constants (see above), not constructor params — removes the footgun of per-instance threshold drift.

### `load_manifests(skills_dir: str | Path) -> int`

- Walks `skills_dir` looking for `*/manifest.json` files
- For each manifest: reads `name` (skill), iterates `tools[]`, collects `tool.examples[]`
- Strips comments from example phrases: split on `"->"` and `"—"`, take left side, strip
- Skips malformed manifests silently (log warning, continue — don't raise)
- Calls `build_corpus()` internally
- Returns count of example phrases loaded

### `build_corpus(documents: list[tuple[str, str, str]]) -> None`

Documents are `(skill_name, tool_name, example_phrase)` triples. Computes:
- Per-document term frequencies (`collections.Counter`)
- Document lengths
- Average document length
- IDF for each term (standard BM25 formula: `log(1 + (N - df + 0.5) / (df + 0.5))`)
- Per-document max-possible score (document scored against itself) — used for normalization

### `match(query: str) -> ShadowDecision`

Always returns a `ShadowDecision` (never `None` — caller should not need to null-check).

- If corpus is empty → return `ShadowDecision(action="passthrough", tool=None, score=0.0, phrase_matched=None)`
- Tokenize query (lowercase, alphanumeric `\b\w+\b`)
- Score each document using BM25 term scoring
- Normalize winning score against that document's max possible score
- Apply thresholds → set `action`
- Return `ShadowDecision`

---

## BM25 Term Score Formula

For each query term `t` in document `d`:

```
score_t = IDF(t) * (f_td * (k1 + 1)) / (f_td + k1 * (1 - b + b * (doc_len / avg_doc_len)))
```

Where `f_td` = term frequency of `t` in `d`.

Total document score = sum of `score_t` for all query terms present in document.

---

## Integration with `react.py`

Update `run()` signature to accept an optional `shadow_matcher` parameter:

```python
def run(
    query: str,
    config: Config,
    skill_registry: list[dict[str, Any]],
    context: str = "",
    step_callback: Callable[[str], None] | None = None,
    trace_id: str | None = None,
    max_steps: int = 10,
    max_secs: int = 60,
    executor: Executor | None = None,
    control_plane: ControlPlaneRouter | None = None,
    shadow_matcher: ShadowMatcher | None = None,   # NEW
) -> ReActResult:
```

After `control_plane` check, before the ReAct loop:

```python
if shadow_matcher:
    shadow = shadow_matcher.match(query)
    if shadow.action == "route" and shadow.tool:
        # Skip ReAct entirely — dispatch directly via executor
        result = executor.execute(shadow.tool, {"query": query}) if executor else {"answer": f"[shadow] {shadow.tool}"}
        return ReActResult(answer=result.get("answer", ""), steps=[], exit_reason="shadow_route", ...)
    elif shadow.action == "hint" and shadow.hint:
        # Inject hint into context for the ReAct loop
        context = f"[Hint: likely tool is '{shadow.tool}' — {shadow.phrase_matched}]\n{context}"
```

`ReActResult.exit_reason` should accept `"shadow_route"` as a valid value (update `xibi/types.py` if `exit_reason` is an enum or Literal — keep it a plain `str` if it already is).

---

## Required Tests (`tests/test_shadow.py`)

1. `test_build_corpus_empty` — empty corpus, `match()` returns `action="passthrough"`
2. `test_match_above_route_threshold` — build corpus with a phrase, query it exactly → `action="route"`
3. `test_match_in_hint_band` — partial overlap query → `action="hint"`
4. `test_match_below_threshold` — unrelated query → `action="passthrough"`
5. `test_normalized_score_range` — score is always 0.0–1.0
6. `test_load_manifests_skips_missing_dir` — nonexistent path → no exception, 0 loaded
7. `test_load_manifests_skips_malformed_json` — invalid JSON manifest → skips, continues
8. `test_load_manifests_loads_examples` — valid manifest with 3 examples → returns 3
9. `test_route_threshold_constant` — `ROUTE_THRESHOLD == 0.85`
10. `test_hint_threshold_constant` — `HINT_THRESHOLD == 0.65`
11. `test_react_run_shadow_route_skips_react` — `run()` with shadow_matcher high-confidence match → `exit_reason="shadow_route"`, `steps=[]`
12. `test_react_run_shadow_hint_injects_context` — medium-confidence → ReAct runs, hint appears in first prompt
13. `test_react_run_no_shadow_matcher` — `shadow_matcher=None` → ReAct runs normally (regression guard)

---

## Notes

- No new runtime dependencies. BM25 is pure Python (`math`, `collections`, `re`).
- `tokenize()` is a module-level helper (not private to class) — tests can import and use it directly.
- All type annotations use `from __future__ import annotations`.
- Follow existing Xibi code style: dataclasses over dicts, type hints everywhere, no `Optional[X]` (use `X | None`).
