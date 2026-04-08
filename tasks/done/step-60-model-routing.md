# step-60 — Runtime Fallback + Graceful Degradation

> **Depends on:** none
> **Blocks:** nothing architectural; future resilience work (checkpointing, granular errors, router) is opportunistic
> **Type:** Bug fix + minimal resilience foundation
> **Priority:** High — two production bugs found 2026-04-07; fixing them closes a silent-failure class
> **Scope:** Four targeted fixes. ~250 LOC, ~15 tests.
> **Cut material:** See `tasks/backlog/notes/step-60-followups.md` for design ideas that were explored but deferred (Router class, capability profiles, delegate tool, granular error categories, streaming, checkpointing, task queue).

---

## Why This Step Exists

On 2026-04-07, a Telegram job-search workflow failed end-to-end despite the step-58 MCP collision fix landing successfully. Trace analysis revealed two separate bugs and one latent bug, all in the model-call path.

### Bug 1: No Runtime Fallback Chain

The fallback list (`fast` → `think` → `review`) is implemented in `router.get_model()` as a pre-flight selector — it walks the list only when a circuit breaker is in the `open` state. When a model call fails at runtime (e.g., Ollama returns 500 on a large prompt), `BreakerWrappedClient.generate()` catches the `XibiError`, records one failure on the breaker (threshold typically 3, so the breaker stays `closed`), and re-raises. The error propagates up to `react._run_async`'s outer except block at line ~897, which immediately exits the run with `exit_reason="error"`. The fallback chain is never walked at runtime.

Concretely on 2026-04-07: the model fetched 25 jobs successfully via MCP, then on the next ReAct step the prompt grew to ~6800 tokens + the 33,793-char escaped tool output. Ollama returned 500. Circuit breaker recorded 1 failure (still closed). Run died with `exit_reason="error"` and no answer. Anthropic was configured as the fallback for `think.fallback = "review"` but was never tried.

Every production agent system needs a runtime fallback chain because every provider can fail — cards decline, Anthropic has outages, OpenAI rate-limits, streams truncate, local models crash. We currently have no runtime resilience for any of these.

### Bug 2: Tracing Gap on Failure

In `OllamaClient.generate()` (router.py:278-292) and the equivalent methods on every provider client, `_emit_telemetry()` is called **after** `_call_provider()` returns successfully. There is no `try/finally`. When the provider throws, no `llm.generate` span is emitted at all.

Concretely on 2026-04-07: the failing `react.run` span had a 60-second gap between the last `react.step` and its own end timestamp. No `llm.generate` spans existed for the second step. The black hole made the failure invisible until we read `journalctl`. Every Ollama failure since tracing landed has been silent in the same way.

### Latent Bug: `generate_structured` Raises Bare `RuntimeError`

At `router.py:326`, `OllamaClient.generate_structured()` raises a bare `RuntimeError` on JSON parse failure instead of a `XibiError(PARSE_FAILURE)`. Any fallback mechanism catching `XibiError` will not catch it, and a structured-call parse failure would propagate through unchecked and crash the run. This predates step-60 but any runtime fallback work inherits it and must fix it.

### Why Graceful Degradation Belongs Here Too

The 25 jobs were sitting in the scratchpad the entire time on 2026-04-07. Even without any fallback chain at all, returning "I couldn't format this as a document but here are the 25 jobs" would have rescued the user from a crash screen. Chain walking and graceful degradation are complementary: walking makes failures *less likely*; degradation makes failures *less harmful*. Both belong in step-60 because both fire from the same failure path in the same files, and shipping only one means shipping a "fixed half" of the incident.

---

## The Fix

### 1. Tracing Gap Fix (`xibi/router.py`)

Wrap every provider client's `generate`, `generate_structured`, and `generate_with_tools` in `try/finally` so `_emit_telemetry()` always fires:

```python
def generate(self, prompt: str, system: str | None = None, **kwargs) -> str:
    recovery_attempt = kwargs.get("recovery_attempt", False)
    t_start = time.monotonic()
    text = ""
    error: XibiError | None = None
    try:
        text = self._call_provider(prompt, system, **kwargs)
        return text
    except XibiError as e:
        error = e
        raise
    finally:
        duration_ms = int((time.monotonic() - t_start) * 1000)
        self._emit_telemetry(
            prompt=prompt,
            system=system,
            response_text=text,
            duration_ms=duration_ms,
            parse_status="failed" if error else "ok",
            recovery_attempt=recovery_attempt,
            error=error,
        )
```

`_emit_telemetry()` gains an optional `error: XibiError | None = None` parameter. When set, the emitted span has `status="error"` and attributes include `error.category`, `error.message[:200]`, and `error.component`. The inference event row flips `degraded=1` on error.

Apply mechanically to all five provider clients: `OllamaClient`, `AnthropicClient`, `GeminiClient`, `OpenAIClient`, `GroqClient`.

### 2. `generate_structured` Latent Bug Fix (`xibi/router.py`)

In every provider client, convert the bare `RuntimeError` on JSON parse failure to a proper `XibiError`:

```python
except json.JSONDecodeError as e:
    # telemetry already emitted above via try/finally from §1
    raise XibiError(
        category=ErrorCategory.PARSE_FAILURE,
        message=f"Provider returned invalid JSON: {e}",
        component=self._component_name(),
        detail=f"Response: {response_text[:500]}",
        retryable=True,
    ) from e
```

Apply to `generate_structured` on `OllamaClient`, `AnthropicClient`, `GeminiClient`, `OpenAIClient`, `GroqClient`. Check each provider for similar `raise RuntimeError(...)` paths on shape/parse failures and convert them all. This is a prerequisite for §3 — without it, a structured-call parse failure bypasses the chain entirely.

### 3. Runtime Fallback Chain (`xibi/router.py`)

Introduce `ChainedModelClient`. It wraps the existing `RoleConfig.fallback` chain — no new schema, no new abstractions. The chain is whatever `fallback` already points to in config.

```python
class ChainedModelClient:
    """Walks the existing RoleConfig.fallback chain on runtime provider failure.

    Invariant: walks are ephemeral. This class does not mutate config or any
    shared state. The next call starts from the configured primary.

    Invariant: walks go through BreakerWrappedClient instances, so walking
    past a failed role increments that role's breaker. Eventually the breaker
    opens and get_model() pre-flight-skips that role on future calls.
    """

    def __init__(self, primary_role: str, specialty: str, config: Config):
        self.primary_role = primary_role
        self.specialty = specialty
        self.config = config
        self._chain = self._resolve_chain(primary_role)  # list of (role_name, BreakerWrappedClient)

    def generate(self, prompt: str, system: str | None = None, **kwargs) -> str:
        last_err: XibiError | None = None
        attempts: list[dict] = []

        for i, (role_name, client) in enumerate(self._chain):
            if i > 0:
                time.sleep(min(0.1 * (2 ** i), 1.0))  # exponential backoff, capped at 1s
            try:
                return client.generate(prompt, system, **kwargs)
            except XibiError as e:
                last_err = e
                attempts.append({
                    "role": role_name,
                    "category": e.category.value,
                    "message": e.message[:200],
                })
                if not self._should_walk(e):
                    raise
                logger.warning(
                    "role %s failed (%s); walking fallback chain to next role",
                    role_name, e.category.value,
                )
                continue

        raise XibiError(
            category=(last_err.category if last_err else ErrorCategory.PROVIDER_DOWN),
            message=f"All {len(self._chain)} roles in fallback chain failed",
            component="router",
            detail=json.dumps({"attempts": attempts}),
            retryable=False,
        )

    def _should_walk(self, e: XibiError) -> bool:
        # Walk on network-shaped failures. Do NOT walk on validation,
        # tool-not-found, permission, or circuit-open. The walkable set is
        # intentionally coarse because our ErrorCategory taxonomy is coarse
        # (everything non-timeout lands in PROVIDER_DOWN today). When we add
        # granular categories, revisit this set — see followups file.
        return e.category in (
            ErrorCategory.PROVIDER_DOWN,
            ErrorCategory.TIMEOUT,
            ErrorCategory.PARSE_FAILURE,  # only if recovery_attempt=False
        )
```

`get_model()` returns `ChainedModelClient` instead of a single `BreakerWrappedClient`. `BreakerWrappedClient` is kept as the per-role inner wrapper that records breaker state. `ChainedModelClient` composes multiple `BreakerWrappedClient`s along the existing fallback list.

`generate_structured` and `generate_with_tools` get the same chain-walk treatment via the same pattern. The retry semantics inside each call (`recovery_attempt` for parse failures) are unchanged — those happen within a single role before any chain walking. A first parse failure walks; a retry parse failure surfaces (graceful degradation in §4 picks it up from there).

**Walkable error set — coarse by design.** We walk on `PROVIDER_DOWN`, `TIMEOUT`, and first-attempt `PARSE_FAILURE`. We do *not* walk on `TOOL_NOT_FOUND`, `VALIDATION`, `PERMISSION`, `CIRCUIT_OPEN`, or `UNKNOWN`. This is coarse because every provider client currently lumps non-timeout failures into `PROVIDER_DOWN` via a catch-all `RequestException` handler. More granular categories (`RATE_LIMIT`, `BILLING`, `AUTH`, `CONTENT_FILTER`) are intentionally deferred — see followups.

### 4. Run-Level Graceful Degradation (`xibi/react.py`)

The chain handles provider failures. This section handles everything else — tool crashes, parse failures on retry, max-steps exhaustion, any path that causes the ReAct loop to exit without a clean `finish`. The goal: **if the scratchpad has any useful observations, surface them to the user instead of dying with `exit_reason="error"`.**

Add `_build_partial_answer(scratchpad, reason)` to `react.py`:

```python
def _build_partial_answer(scratchpad: list[dict], reason: str) -> str | None:
    """Extract useful observations from the scratchpad and format them as a
    degraded answer. Returns None if nothing salvageable exists.

    Pure Python. No model call. Deterministic.
    """
    useful: list[tuple[str, str]] = []
    for entry in scratchpad:
        if entry.get("type") != "observation":
            continue
        if entry.get("status") != "ok":
            continue
        tool = entry.get("tool", "tool")
        output = entry.get("output")
        if output in (None, "", [], {}):
            continue
        useful.append((tool, _format_observation_for_user(output)))

    if not useful:
        return None

    lines = [
        f"I couldn't complete this task cleanly ({reason}), but here's what I gathered:",
        "",
    ]
    for i, (tool, formatted) in enumerate(useful[-5:], start=1):
        lines.append(f"**{i}. From `{tool}`:**")
        lines.append(formatted)
        lines.append("")
    return "\n".join(lines)


def _format_observation_for_user(output: Any) -> str:
    """Cheap formatter. Strings truncate. Lists-of-dicts render as numbered
    items with top-level fields. Everything else stringifies and truncates."""
    if isinstance(output, str):
        return output[:2000] + ("…" if len(output) > 2000 else "")
    if isinstance(output, list):
        return _render_list_for_user(output[:20])
    if isinstance(output, dict):
        return _render_dict_for_user(output)
    return str(output)[:2000]
```

In the outer `except` block of `_run_async`, before exiting with `exit_reason="error"`, call `_build_partial_answer()`:

```python
except XibiError as e:
    partial = _build_partial_answer(scratchpad=scratchpad, reason=e.user_message())
    if partial is not None:
        logger.warning("run %s degraded: exiting with partial answer after %s", run_id, e.category.value)
        return RunResult(
            answer=partial,
            exit_reason="partial",
            degraded=True,
            steps=step_count,
            last_error=e.to_dict(),
        )
    return RunResult(
        answer=None,
        exit_reason="error",
        degraded=True,
        steps=step_count,
        last_error=e.to_dict(),
    )
```

Apply the same salvage path to the max-steps exit and any catch-all handler.

**New exit reason.** `exit_reason="partial"` joins `finish`, `error`, `max_steps`. Answer is populated, `degraded=True`.

**New flag.** `RunResult.degraded: bool`. `True` means "user should know this isn't a clean success."

**Telegram surface.** When `result.degraded == True`, the Telegram response formatter prepends *"⚠️ I ran into trouble completing this — here's what I managed to gather:"* before the answer. ~5-line change in `services/telegram_bot.py` (or wherever response formatting lives).

**Explicit non-goals.**
- Does not retry failed steps (that's checkpointing — see followups).
- Does not mutate or persist scratchpad state beyond the run's existing span.
- Does not call the model to summarize or reformat salvaged content (that would re-enter the failing path).
- Does not hide the failure — the caveat is visible, the span clearly shows `exit_reason="partial"`.

---

## Test Changes

Fifteen tests total. Grouped by file.

**`tests/test_router_telemetry.py`** (new):
1. `test_failure_emits_span_with_error_status` — provider throws, verify span with `status="error"` and error attributes in spans table.
2. `test_failure_records_inference_event_with_degraded_flag` — same path, verify `inference_events` row has `degraded=1`.
3. `test_success_path_unchanged` — happy-path regression: success has `status="ok"`, no error attributes.

**`tests/test_router_chain.py`** (new):
4. `test_chain_walks_on_provider_down` — fast throws PROVIDER_DOWN, chain walks to think, returns think's result.
5. `test_chain_walks_on_timeout` — fast throws TIMEOUT, chain walks.
6. `test_chain_does_not_walk_on_validation_error` — fast throws VALIDATION, exception propagates without trying think.
7. `test_chain_exhausted_raises_enriched_error` — every role throws, final XibiError's `detail` is JSON with per-role attempt history.
8. `test_chain_applies_backoff_between_walks` — mock `time.sleep`, verify exponential durations capped at 1.0s.
9. `test_chain_does_not_mutate_config_after_walk` — run a walk, assert `config` object is byte-for-byte unchanged, next call starts from configured primary.
10. `test_structured_parse_failure_raises_xibi_error_not_runtime` — regression on the latent bug: `generate_structured` with invalid JSON raises `XibiError(PARSE_FAILURE)`, not bare `RuntimeError`.
11. `test_structured_parse_failure_walks_chain` — end-to-end: fast returns invalid JSON, chain walks to think, think returns valid JSON, result is think's output.

**`tests/test_react_degradation.py`** (new):
12. `test_partial_answer_built_from_successful_observations` — scratchpad with 3 successful + 1 failed observation, partial answer contains all 3 successes, `exit_reason="partial"`, `degraded=True`.
13. `test_partial_answer_returns_none_when_nothing_salvageable` — empty scratchpad, `_build_partial_answer` returns None, run exits `exit_reason="error"`, `answer=None`.
14. `test_partial_answer_on_chain_exhaustion` — mock all roles failing, scratchpad has prior successful tool results, run exits `exit_reason="partial"` with populated answer.
15. `test_partial_answer_does_not_call_model` — mock all LLM calls to raise if invoked, run graceful degradation on populated scratchpad, verify no LLM call was made during salvage.

---

## Acceptance Criteria

1. The 2026-04-07 failed workflow (`"Let's start with remote jobs. Can you create a document with like 20+ results?"`) completes successfully when replayed against the patched build. "Successfully" means the user receives a usable answer — either `exit_reason="finish"` with a clean document, or `exit_reason="partial"` with the 25 jobs surfaced as a salvaged list. The run must not die silently with `exit_reason="error"` and an empty answer.
2. Replaying the same workflow **with chain walking disabled** still rescues the 25 jobs via graceful degradation alone. This proves §4 is independently valuable, not just a subordinate of §3.
3. Replaying any provider-500 failure produces a span with `status="error"` and populated `error` attributes. No black holes in traces.
4. `journalctl --user -u xibi-telegram` no longer contains `requests.exceptions.HTTPError: 500` traces propagating to the user without an attempted fallback log line.
5. A `generate_structured` call receiving invalid JSON raises `XibiError(PARSE_FAILURE)` that the chain can catch. No bare `RuntimeError`.
6. Chain walking applies exponential backoff capped at 1 second per walk.
7. Chain walking does not mutate config or any shared state.
8. All 15 new tests pass on CI.
9. Existing `test_resilience.py` and `test_executor_core.py` tests still pass.
10. No performance regression on the happy path: a successful turn makes exactly one `generate()` call, same as today. Graceful degradation adds zero cost on the happy path (only fires in outer except).

---

## Out of Scope

Deliberately deferred. Design thinking preserved in `tasks/backlog/notes/step-60-followups.md`:

- **Router class, capability profiles, pre-flight token routing** — one signal (token budget) does not justify the abstraction. Revisit when a second signal exists.
- **Post-tool size signal** — heuristic from a single data point. Over-fitting.
- **`delegate` tool** — half-baked sub-agent. Belongs in step-63 (sub-agents).
- **Granular error categories** (`RATE_LIMIT`, `BILLING`, `AUTH`, `CONTENT_FILTER`) — own spec, touches every provider client.
- **Streaming + liveness detection** — own spec, 400-500 LOC refactor across providers.
- **Run checkpointing and resume** — own spec, changes run lifecycle.
- **Task/job queue with retry policies** — own spec, layer above runs.
- **OpenClaw-defensive tests** — don't write tests for bugs we haven't made. Revisit when refactoring or when marketing needs the claim.
- **Session-scoped "definitely broken role" blacklist** — depends on granular categories.
- **Multi-provider tier groups** (LiteLLM `order=` style) — v2 router work.

---

## Notes for Implementer

- Implement in this order: (1) tracing gap fix across all provider clients, (2) `generate_structured` bare-RuntimeError fix across all provider clients, (3) `ChainedModelClient` with backoff and enriched exhaustion error, (4) graceful degradation in `react.py` + Telegram surface update. Each is independently testable.
- **Do not add a Router class, capability profiles, or a `KNOWN_MODEL_PROFILES` table.** The chain uses the existing `RoleConfig.fallback` field. If you find yourself reaching for a new abstraction, stop and check the followups file — the thinking is captured there and the decision to defer was deliberate.
- **Do not add a `delegate` tool.** It belongs in the sub-agents spec (step-63).
- **`ChainedModelClient` must not mutate state.** No writes to `self.config`, no sticky "current role," no side channels. The next call starts from the configured primary. Lock this in with a test.
- `_build_partial_answer` is pure Python. No LLM calls, no prompt engineering, no model dependency. Formatting is deterministic string construction. Lock this in with a test.
- The walkable error set in `_should_walk` is intentionally coarse. When granular categories land in a later spec, revisit this set as part of that spec's scope, not by patching step-60.
- Pipeline driver (Sonnet) should not modify this spec. If anything is unclear, kick back to Opus for revision.
