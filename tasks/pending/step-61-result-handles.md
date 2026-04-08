# step-61 — Result Handles + transform_data

> **Depends on:** step-60 (graceful degradation should ship first so any
> bugs introduced here don't compound the silent-failure class)
> **Blocks:** compose_document (future spec) — composing documents over
> large structured data is impossible until the data survives a ReAct
> step without being destructively truncated
> **Type:** Architectural fix + minimal first consumer
> **Priority:** High — same 2026-04-07 incident as step-60, deeper layer
> **Scope:** One new abstraction (`ToolHandle`), one dispatch hook, one
> serialization change in `Step.full_text()`, one new tool
> (`transform_data`), and handle-aware updates to `write_file`. One
> system prompt addition explaining handles to the model. ~350 LOC,
> ~15 tests.

---

## Why This Step Exists

Step-60 patched the *symptom* of the 2026-04-07 incident: an Ollama 500 on
a large prompt killed a run silently. Reading `xibi/types.py:25-26` reveals
the *cause*:

```python
def full_text(self) -> str:
    out = str(self.tool_output)
    if len(out) > 4000:
        out = out[:4000] + "... [truncated]"
```

Every tool output larger than 4 KB is destructively truncated before it
hits the next ReAct step's prompt. Two consequences fall out of this:

1. **The big payload still bloats the prompt.** 4 KB times the last 4
   steps (`compress_scratchpad` keeps full detail for the most recent 4)
   plus the system prompt and tool schemas is enough to push a structured
   request past Ollama's comfortable window. On 2026-04-07 the prompt
   crossed ~6800 tokens and Ollama returned 500.
2. **The data the user actually asked about is gone.** The 25 jobs were
   truncated to whatever fit in 4000 characters before any subsequent
   step could read them. Even if Ollama had answered cleanly, the model
   could not have written a document over 25 jobs because 25 jobs no
   longer existed in its context. compose_document over any non-trivial
   data set is *fundamentally broken today* and that won't change until
   tool outputs survive across ReAct steps without being mangled.

The fix is a result-handle abstraction. Tools that produce big outputs
return a small reference + summary instead of inlining bytes. The full
payload lives in a session-scoped store. Downstream tools (`write_file`,
`transform_data`, eventually `compose_document`) resolve handles by ID
and operate on the real bytes without round-tripping them through the
LLM prompt.

This is the same architectural pattern as Claude Code's
`tool_use_id`-based output references and is the standard way large agent
systems avoid the "every step pays for every prior tool call" tax.

### Why transform_data Lands With Handles

A handle abstraction with no consumer is dead weight. The user can ask
for "the 25 jobs as a sorted markdown list" today and get a model-mediated
answer; with handles + nothing else, the model still has to load the bytes
into context to do any filtering or sorting. The whole point of handles is
to keep large data *out* of the LLM context — that requires at least one
tool that can manipulate handle contents without an LLM call.

`transform_data` is that tool: filter, sort, group, dedupe, project, slice.
Pure Python, deterministic, operates on a handle ID, returns either a new
handle or an inline summary depending on result size. Pairing the two in
one spec is the difference between shipping an abstraction and shipping a
working data path.

### Why compose_document Is *Not* In This Spec

compose_document is the natural third member of this group, but it's a
substantially larger surface (.md, .docx, .pdf, .xlsx — each with its own
templating story) and shipping it together would balloon the spec. It's
the obvious next step (call it step-62) and it falls out cleanly once
handles + transforms are in place because compose_document just becomes
"resolve a handle, render its contents through a template, write the
file." No new abstractions needed.

---

## The Fix

### 1. `ToolHandle` and the Handle Store (`xibi/handles.py`, new)

```python
@dataclass
class ToolHandle:
    """A reference to a tool output stored out-of-band from the LLM prompt.

    Handles are session-scoped: created during a run, valid for the lifetime
    of that run, dropped at run end. They are NOT persisted across runs.
    """
    handle_id: str          # short, e.g. "h_a8f3" — must be safe to inline
    tool: str               # producing tool name
    schema: str             # one-line shape hint, e.g. "list[dict] (25 items)"
    summary: str            # ≤500 char human-readable preview
    item_count: int | None  # nullable; populated for list-shaped data
    size_bytes: int         # serialized size of underlying payload
    created_at: float       # monotonic timestamp


class HandleStore:
    """Session-scoped, in-memory only. One instance per ReAct run."""

    def __init__(self, max_handles: int = 64, max_total_bytes: int = 32 * 1024 * 1024):
        self._handles: dict[str, ToolHandle] = {}
        self._payloads: dict[str, Any] = {}
        self._max_handles = max_handles
        self._max_total_bytes = max_total_bytes
        self._total_bytes = 0

    def create(self, tool: str, payload: Any) -> ToolHandle: ...
    def get(self, handle_id: str) -> Any: ...
    def get_handle(self, handle_id: str) -> ToolHandle | None: ...
    def drop(self, handle_id: str) -> None: ...
    def __len__(self) -> int: ...
```

**Invariants — lock these in with tests:**

- The store is **per-run**. A new `HandleStore` is constructed at the
  start of each `react._run_async` and dropped on return. There is no
  cross-run persistence and no shared global. This keeps the security
  story trivial: a handle from one user's run is unreachable from
  another user's run by construction.
- `handle_id` is short (4-6 char suffix on `h_`) and contains no payload
  bytes. It is safe to inline in prompts and logs.
- **ID generation is random, not derived from payload.** Use
  `secrets.token_hex(2)` (4 hex chars) for the suffix, which gives 65k
  possible IDs per run. On collision (extremely unlikely with ≤64
  handles per run), retry up to 8 times then expand to `token_hex(3)`
  (6 hex chars). The randomness rules out leaking payload bytes via the
  ID — locked in with `test_handle_id_does_not_leak_payload`.
- `item_count` is populated for top-level lists (`len(payload)`) and
  for top-level dicts whose `data`/`items`/`results`/`jobs` field is a
  list (in which case `item_count = len(that_list)`). Otherwise `None`.
  This is the same heuristic as `_is_large_collection`; reuse the
  helper, don't duplicate it.
- Eviction is **deterministic**: when `max_handles` or `max_total_bytes`
  is exceeded, the oldest handle by `created_at` is dropped, including
  its payload, until the store is back under both caps. Eviction emits
  a warning log line with the dropped handle's ID and creating tool.
  Note: with `max_handles=64` and a typical run of 10-30 tool calls,
  eviction during normal operation essentially never happens. The cap
  is defensive against runaway tools, not load-bearing for correctness.
- `get()` of an evicted handle raises `XibiError(category=VALIDATION,
  message="Handle X has been evicted from this run's store")`. The
  graceful-degradation path from step-60 picks this up cleanly.

### 1.5. Store Threading and Handle Resolution (`xibi/react.py`)

The store is constructed once per run and passed down explicitly. No
globals, no singletons, no executor-level injection.

**Construction.** At the top of `_run_async` (line 426 in current
`xibi/react.py`), construct the store before the main loop:

```python
async def _run_async(...) -> ReActResult:
    handle_store = HandleStore()  # per-run, lives until function returns
    ...
```

**Threading.** Add `handle_store: HandleStore | None = None` as a new
keyword-only parameter to `dispatch()` (line 225). `_run_async` passes
its instance through on every call. The parameter is optional so
existing test call sites that build a `dispatch()` call directly
without a store still work — they just bypass handles.

**Resolution before tool execution.** Inside `dispatch`, after
`command_layer.check()` succeeds and *before* calling
`executor.execute()`, walk `tool_input` once and resolve any string
value matching the handle ID pattern (`^h_[a-f0-9]{4,6}$`) by
substituting the underlying payload. Store lookup failure returns
`XibiError(VALIDATION, "Handle X not in store")` — graceful degradation
from step-60 catches it.

```python
def _resolve_handles_in_input(tool_input: dict, store: HandleStore | None) -> dict:
    if store is None:
        return tool_input
    resolved = {}
    for k, v in tool_input.items():
        if isinstance(v, str) and re.match(r"^h_[a-f0-9]{4,6}$", v):
            resolved[k] = store.get(v)  # raises XibiError on miss
        else:
            resolved[k] = v
    return resolved
```

This means **tools never know handles exist**. `write_file(handle="h_a4f1")`
arrives at the executor as `write_file(handle=<actual payload>)`. The
tool's signature must accept the resolved-payload type (string, dict,
list) — see section 5 for `write_file`'s updated signature.

**Wrapping after tool execution.** Right after `executor.execute()`
returns, before the result becomes `step.tool_output`, call
`_maybe_wrap_in_handle(tool_name, output, handle_store)` (section 2
below). If the output is large, the dict the model sees in scratchpad
is the wrapped handle reference, not the bytes.

**One subtlety: `transform_data` is not special.** It returns its
transformed result as a normal Python value. The wrap step decides
whether to handle-ify it based on size, same as any other tool. The
store is *only* read in `_resolve_handles_in_input` and *only* written
in `_maybe_wrap_in_handle`. No tool ever touches the store directly.
This is the per-run isolation guarantee — locked in with test #10.

### 2. Threshold-Based Handle Creation (`xibi/react.py`, `dispatch` site)

After every successful tool dispatch, decide whether to inline the output
or wrap it in a handle. Threshold = serialized output ≥ **2048 bytes** OR
output is a list/dict containing ≥ **20 items** at the top level.

```python
def _maybe_wrap_in_handle(tool: str, output: dict, store: HandleStore) -> dict:
    serialized = json.dumps(output, separators=(",", ":"))
    if len(serialized) < 2048 and not _is_large_collection(output):
        return output  # small enough to inline; no handle

    handle = store.create(tool, output)
    return {
        "status": "ok",
        "handle": handle.handle_id,
        "schema": handle.schema,
        "summary": handle.summary,
        "item_count": handle.item_count,
    }
```

The wrapped dict is what becomes `step.tool_output`. The full payload is
in the store, addressable by `handle.handle_id`. The model never sees
the bytes — it sees the schema, the summary, the count, and the handle.

`_is_large_collection` is the only heuristic: top-level list with ≥20
items, or top-level dict whose `data`/`items`/`results`/`jobs` field is
a list with ≥20 items. Coarse on purpose. Tools that produce
list-shaped output under a different field name (e.g. `{"signals": [...]}`)
should rename to one of the recognized keys *or* flatten to a top-level
list. The four keys are the convention; new tools should adopt it.

Tools with naturally small outputs (e.g. `recall_conversation` returning
≤5 belief summaries totaling under 1KB) stay under both the byte and
item thresholds and never get wrapped. No opt-out mechanism is needed.

### 3. `Step.full_text()` Stops Truncating Bytes (`xibi/types.py`)

Replace the destructive truncation with handle-aware rendering:

```python
def full_text(self) -> str:
    if "handle" in self.tool_output:
        out = (
            f"<handle:{self.tool_output['handle']} "
            f"schema={self.tool_output['schema']} "
            f"items={self.tool_output.get('item_count', '?')}>"
            f"\nSummary: {self.tool_output.get('summary', '')}"
        )
    else:
        out = str(self.tool_output)
        if len(out) > 4000:
            out = out[:4000] + "... [truncated]"
    return (
        f"Step {self.step_num}:\n"
        f"  Thought: {self.thought}\n"
        f"  Action: {self.tool}\n"
        f"  Input: {json.dumps(self.tool_input, separators=(',', ':'))}\n"
        f"  Output: {out}"
    )
```

The 4000-char truncation **stays** for non-handle outputs as a defense
in depth — handles cover the >2KB case, the legacy truncation covers
anything that slipped through (e.g. an unwrapped tool that returned 8KB
of strings). Both are belt and suspenders against prompt bloat.

`one_line_summary()` gets the same treatment so older compressed steps
also surface handles correctly.

### 4. `transform_data` Tool (`xibi/skills/data/`, new skill)

A new skill `data` containing one tool, `transform_data`. Pure Python,
no model call.

**Tool signature:**

```json
{
  "name": "transform_data",
  "description": "Filter, sort, group, dedupe, project, or slice the contents of a tool result handle. Returns a new handle (or inline result if small).",
  "input_schema": {
    "handle": {"type": "string", "description": "Handle ID from a prior tool call"},
    "operations": {
      "type": "array",
      "description": "Ordered list of operations to apply",
      "items": {
        "type": "object",
        "properties": {
          "op": {"enum": ["filter", "sort", "group_by", "dedupe", "project", "slice"]},
          "args": {"type": "object"}
        }
      }
    }
  }
}
```

**Operations (v1 — keep narrow):**

- `filter` — `{"field": "salary", "op": ">", "value": 100000}`. Operators: `==`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `not_in`, `contains`, `startswith`.
- `sort` — `{"field": "posted_at", "order": "desc"}`. Multi-key via repeated op.
- `group_by` — `{"field": "company"}`. Returns a dict-of-lists.
- `dedupe` — `{"field": "url"}` or `{"fields": ["company", "title"]}`.
- `project` — `{"fields": ["title", "company", "url"]}`. Drops everything else.
- `slice` — `{"start": 0, "end": 10}`.

**Behavior:**

- Resolves the handle, applies operations in order, wraps the result in
  a new handle (or inlines if small).
- On any error (missing field, type mismatch, evicted handle) returns
  `XibiError(category=VALIDATION)`. Step-60 graceful degradation handles
  the run exit.
- Must be deterministic. Same handle + same operations → same output.
  Lock in with a test.

**Anti-scope:**

- No joins, no aggregations beyond `group_by`, no SQL-like syntax, no
  custom functions, no user code execution. If you find yourself reaching
  for any of those, that's a separate spec.

### 4.5. Teaching the Model About Handles (`xibi/prompts/system.py` or equivalent)

Handles are useless if the model doesn't know it can pass them to other
tools. The system prompt needs a small addition explaining the contract.

**Add a section to the system prompt** (after the existing tool-use
guidance, before the example block):

```
HANDLES — large tool outputs

Some tools return outputs containing a `handle` field that looks like
this:

  {"status": "ok", "handle": "h_a4f1", "schema": "list[dict] (25 items)",
   "summary": "...", "item_count": 25}

This means the full data is stored out-of-band and you have a reference
to it. To use the data, pass the handle string as a parameter to any
tool that accepts it. Example:

  write_file(path="jobs.md", handle="h_a4f1")
  transform_data(handle="h_a4f1", operations=[{"op": "sort", "args": {"field": "salary", "order": "desc"}}])

Do NOT try to read the bytes of a handle directly. Do NOT include
handle IDs in prose responses to the user — they are internal
references and will look like noise. The handle is valid only for the
current run.
```

This is the *only* prompt change needed. Tool descriptions in the
registry stay unchanged; the per-tool input schemas already document
their accepted parameters, and resolution is invisible from the tool's
perspective.

**One acceptance test for this** (add as test #15): take a fresh small
model (doesn't matter which), feed it a synthetic scratchpad with one
prior step that returned `{"handle": "h_a4f1", "schema": "list[dict] (25 items)", "summary": "..."}`,
and the user query "save those to a file." The model's next action
should be `write_file(path=..., handle="h_a4f1")`. If it tries to
inline the data or asks for the bytes, the prompt section is unclear
and needs sharpening.

### 5. Handle-Aware `write_file` (`xibi/skills/filesystem/tools/write_file.py`)

`write_file` is the first non-data consumer of handles. Today it takes a
`content` string. Add an optional `handle` parameter that resolves to the
handle's payload before writing.

```python
def write_file(path: str, content: str | None = None, handle: str | None = None, *, store: HandleStore):
    if (content is None) == (handle is None):
        raise XibiError(VALIDATION, "Provide exactly one of `content` or `handle`")
    if handle is not None:
        payload = store.get(handle)
        # Pretty-print structured data for human readability when written to disk.
        if isinstance(payload, (dict, list)):
            content = json.dumps(payload, indent=2, default=str)
        else:
            content = str(payload)
    return _write_to_disk(path, content)
```

This is the minimal end-to-end demo: a user asks for "those 25 jobs in
a file," the agent calls `transform_data` to project relevant fields,
then `write_file` with the resulting handle. No bytes ever touch the
LLM prompt.

---

## Test Changes

Fourteen tests total. Grouped by file.

**`tests/test_handle_store.py`** (new):
1. `test_create_returns_short_id` — ID matches `h_[a-f0-9]{4,6}`.
2. `test_get_returns_original_payload` — round-trip exact equality.
3. `test_max_handles_evicts_oldest` — fill past cap, oldest is gone, newest survive.
4. `test_max_bytes_evicts_oldest` — fill past byte cap, oldest is gone.
5. `test_get_evicted_raises_validation_error` — `XibiError(VALIDATION)` on evicted ID.
6. `test_handle_id_does_not_leak_payload` — ID does not contain any substring of payload.

**`tests/test_react_handles.py`** (new):
7. `test_large_output_wrapped_in_handle` — tool returns 5KB of data, scratchpad sees handle dict not raw bytes.
8. `test_small_output_inlined` — tool returns 200 bytes, scratchpad sees raw output.
9. `test_handle_survives_into_next_step_full_text` — `Step.full_text()` for a handle step renders schema + summary, never raw bytes, always under 600 chars.
10. `test_per_run_isolation` — start two `_run_async` tasks concurrently with `asyncio.gather`. Each makes one tool call that produces a handle. Capture both stores via test fixtures. Assert that `run_a_store.get(run_b_handle_id)` raises `XibiError(VALIDATION)` and vice versa. The mechanism being tested: each `_run_async` constructs its own `HandleStore`, so by construction the IDs do not collide and the dicts are disjoint. The test makes that guarantee explicit so a future refactor can't quietly break it.

**`tests/test_transform_data.py`** (new):
11. `test_filter_then_sort_then_slice` — full happy path on a 50-item list, deterministic output.
12. `test_group_by_returns_dict_of_lists` — shape check.
13. `test_evicted_handle_returns_validation_error` — operate on evicted handle, get `XibiError(VALIDATION)`.
14. `test_write_file_resolves_handle` — `write_file(handle="h_xxx")` writes the same bytes as `write_file(content=json.dumps(payload))`.

**`tests/test_handle_prompt_comprehension.py`** (new):
15. `test_model_uses_handle_when_present` — feed a small model a synthetic scratchpad with one prior step that returned a handle dict and the user query "save those to a file." Assert the model's next action is `write_file(path=..., handle="h_xxx")`. If this fails, the prompt section in §4.5 is unclear and needs sharpening before merge.

---

## Acceptance Criteria

1. The 2026-04-07 workflow (`"Let's start with remote jobs. Can you create a document with like 20+ results?"`) replayed against the patched build produces a file containing **all 25 jobs**, not 4000 characters of truncated JSON. The model never receives the 25-job payload directly — it sees a handle, calls `transform_data` to project, then `write_file` to disk.
2. No tool output larger than 2KB ever appears verbatim in any LLM prompt. Verify by inspection of the spans table after running the smoke test.
3. Cumulative scratchpad size for a 5-step run with 3 large tool outputs stays under 8KB (was unbounded × 4-step window before).
4. `HandleStore` instances are constructed per-run and dropped on return. Verify with a memory-leak test that runs 100 sequential runs and asserts no handle store survives.
5. `transform_data` operations are deterministic. Same handle + same operations → byte-identical output across two invocations.
6. All 15 new tests pass on CI.
7. Existing `test_react.py`, `test_executor_core.py`, and `test_filesystem_skill.py` pass unchanged.
8. No performance regression on small-output happy paths: a tool returning <2KB takes the same code path as today (inline output, no handle store touch).

---

## Out of Scope

Deferred. Each is a separate spec when its time comes:

- **`compose_document`** — the natural next step (call it step-62). Once handles + transforms exist, document composition is "resolve handle, render through template, write file." Owns its own .md/.docx/.pdf/.xlsx complexity.
- **Cross-run handle persistence** — handles are session-scoped by design. If a future workflow needs "the jobs from yesterday," that's a different abstraction (cached query, not handle).
- **Handle versioning / immutability guarantees** — handles are read-only by convention; we don't enforce it. Add enforcement only if a bug shows it's needed.
- **Streaming / async handle creation** — handles are created synchronously when a tool returns. Streaming tool outputs are a separate concern (and a separate spec).
- **Cross-tool join operators in `transform_data`** — see anti-scope above.
- **A `delegate` or `sub_agent` tool that consumes handles** — belongs in the sub-agents spec (step-63).
- **Handle inspection from the dashboard** — debug surface, follow-up.

---

## Notes for Implementer

- Implement in this order: (1) `HandleStore` + `ToolHandle` standalone, fully tested, no react.py changes yet. (2) Wire `_maybe_wrap_in_handle` into `dispatch` and update `Step.full_text()` / `Step.one_line_summary()` together. (3) Build the new `data` skill with `transform_data`. (4) Add the `handle` parameter to `write_file`. Each layer is independently testable; do not skip ahead.
- **Do not make `HandleStore` a singleton or global.** It is constructed at the top of `_run_async` and passed down. If you find yourself reaching for `import` or a class variable to access it, stop. The per-run isolation is a security boundary.
- **Do not persist handles to disk or to the database.** Memory-only by design. Persistence is its own spec because it has its own security model.
- **Do not extend `transform_data` beyond the six operations listed.** Joins, aggregations, custom functions, and SQL-like syntax are all explicitly out of scope. If a real workflow needs more, file a follow-up — don't sneak it in.
- **Do not weaken the 4000-char `Step.full_text()` truncation.** It stays as belt-and-suspenders for any tool output that isn't wrapped in a handle for any reason.
- **Do not write `compose_document` in this spec.** It is the next step but it is its own spec. If you start drafting `.docx` rendering or template handling, you've crossed the boundary.
- The handle ID format (`h_[a-f0-9]{4,6}`) is short on purpose so it survives compression and is cheap to log. Do not embed any payload-derived bytes in the ID — locked in with `test_handle_id_does_not_leak_payload`.
- Pipeline driver (Sonnet) should not modify this spec. If anything is unclear, kick back to Opus for revision.
