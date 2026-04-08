# step-60 Follow-ups — Ideas Cut from the Tight Spec

This file captures thinking that was explored during step-60's design conversation on 2026-04-07 but did not make the tight spec. Each entry is a candidate for a future spec when its need is proven by a second or third data point. None of these are promised. They are raw material, not commitments.

## Model Router with Capability Profiles (candidate: step-60.5)

**The idea.** A stateless `Router` class with a `select()` method that picks the right model up front based on task signals. v1 has one signal (estimated prompt tokens vs context_tokens); future versions grow to include task class, required output size, trust scores per (specialty, tier), latency budgets, cost budgets, provider health, and capability flags (vision, tools, structured output).

**Why it was cut.** We have exactly one routing signal right now (token budget). A single signal does not justify a `Router` class, a `CapabilityProfile` TypedDict, a `KNOWN_MODEL_PROFILES` lookup table, or per-step router selection. A token budget check is five lines inside the react loop without any new abstraction. The class exists only for the future signals we *might* add, and premature abstraction is the main risk — when the real second signal arrives, the class shape we designed now is almost certainly wrong.

**When to revisit.** When we have a concrete second routing signal that demands pre-flight selection and can't be shoehorned into a five-line check in the react loop. Candidates: task-class routing (classify vs reason vs generate), required-output-size enforcement, or cost-budget routing. Not before.

**Preserved design sketch.**

```python
class CapabilityProfile(TypedDict, total=False):
    context_tokens: int
    output_tokens: int
    tier: int

KNOWN_MODEL_PROFILES = {
    "gemma4:e4b":        {"context_tokens": 8000,   "output_tokens": 1000, "tier": 1},
    "gemma2:9b":         {"context_tokens": 8000,   "output_tokens": 1500, "tier": 2},
    "qwen3.5:4b":        {"context_tokens": 32000,  "output_tokens": 2000, "tier": 1},
    "qwen3.5:9b":        {"context_tokens": 32000,  "output_tokens": 2000, "tier": 2},
    "claude-sonnet-4-6": {"context_tokens": 200000, "output_tokens": 8000, "tier": 5},
    "claude-opus-4-6":   {"context_tokens": 200000, "output_tokens": 8000, "tier": 6},
    "gpt-4o":            {"context_tokens": 128000, "output_tokens": 4000, "tier": 4},
}

class Router:
    def __init__(self, config): ...
    def select(self, specialty, role_hint, prompt_tokens=None, ...) -> str: ...
```

**Design notes.**
- Stateless by design. Reads config + profiles. Does not call providers, does not touch breakers.
- Selection should be per-step, non-sticky. A run that starts on `fast` can route to `think` for one heavy step and back to `fast` for the next.
- Emit a `router_decision` attribute on the next `llm.generate` span recording `{role_hint, selected_role, reason}`.
- The function signature is designed to grow — add new keyword arguments for new signals, keep old callers working.

## Post-Tool Size Signal (candidate: folded into step-60.5)

**The idea.** After a tool dispatch returns, if `tool_output_size > 0.5 * context_tokens`, flag the scratchpad with `_oversized_tool_result = True`. The next router call promotes unconditionally regardless of estimated prompt size.

**Why it was cut.** It's a heuristic generalized from a single data point (the 2026-04-07 25-job failure). Generalizing one failure into a rule is over-fitting. Graceful degradation rescues the same case from a different angle without adding a speculative heuristic.

**When to revisit.** After we see three distinct production traces where this signal would have prevented a failure. Revisit alongside the Router spec (it only makes sense if a Router exists to consume the signal).

## `delegate` Tool (candidate: folded into step-63 sub-agents)

**The idea.** A local skill with a single `delegate(task, role?)` tool that runs a fresh `react.run()` at a higher-tier role and returns the result. Same tool surface, same session, no isolation. Depth limit of 2. Lets the model explicitly request a more capable role for a subtask.

**Why it was cut.** It's a half-baked sub-agent. It duplicates what chain walking + graceful degradation accomplishes through a different mechanism, and when we build real sub-agents in step-63 we'll either throw it away or retrofit it awkwardly. Shipping it now is waste. It belongs in the sub-agent spec where the three execution models (Specialist / Sandbox / Worker) live together.

**When to revisit.** When drafting step-63 (sub-agents). The delegate pattern becomes the Specialist archetype there, with proper depth limits, trust tracking, and interaction with the sub-agent spawn APIs.

**Preserved design sketch.**

```python
# skills/delegate/tools/delegate.py (becomes part of step-63)
MAX_DELEGATION_DEPTH = 2

def execute(task, role=None, **kwargs):
    ctx = current_session_context()
    depth = ctx.get("delegation_depth", 0)
    if depth >= MAX_DELEGATION_DEPTH:
        return {"status": "error", "message": "Depth limit reached."}
    target_role = role or ctx.get("next_tier_role", "review")
    result = react_run(
        query=task,
        config=ctx["config"],
        skill_registry=ctx["skill_registry"],
        mcp_executor=ctx["mcp_executor"],
        session_id=ctx["session_id"],
        delegation_depth=depth + 1,
        role_override=target_role,
        max_steps=5,
    )
    return {
        "status": "ok" if result.exit_reason == "finish" else "error",
        "result": result.answer,
        "exit_reason": result.exit_reason,
        "delegated_role": target_role,
    }
```

## Granular Error Categories (candidate: step-60.5 or later)

**The idea.** Expand `ErrorCategory` from 8 to include `RATE_LIMIT`, `BILLING`, `AUTH`, `CONTENT_FILTER`, `CONTEXT_OVERFLOW`, `INTERNAL`. Each provider client maps its specific error shapes to the right category. The fallback chain then makes smarter walking decisions per category (e.g., walk on RATE_LIMIT but not CONTENT_FILTER since the next provider will likely also refuse).

**Why it was cut.** Requires touching every provider client (Ollama, Anthropic, Gemini, OpenAI, Groq) with its own per-provider error mapping table. That's ~300 LOC and its own test surface. Cramming it into step-60 doubles the spec. The coarse categories (PROVIDER_DOWN / TIMEOUT) work for v1 because everything falls into one of them by accident.

**When to revisit.** When a specific production trace shows us walking on something we shouldn't (e.g., burning through the whole chain on a CONTENT_FILTER refusal that was going to fail identically everywhere). Or when we want per-category retry budgets (LiteLLM-style). Standalone spec.

**Per-provider notes.** Each provider has its own error shape:
- Anthropic: 429 (rate limit), 529 (overloaded), 401/403 (auth)
- OpenAI: 429 (rate limit), 500 (internal), Codex `server_error` payloads
- Gemini: quota-exceeded sub-codes
- Groq: rate-limit headers
- Ollama: everything is just HTTP errors or timeouts

## Streaming + Liveness Detection (candidate: step-60.6)

**The idea.** Replace blocking POSTs with streaming connections. Get tokens as they arrive. Use a "no tokens for 5s" inner timeout as a real liveness signal. This makes the fallback chain walk 12x faster on hung calls (5s instead of 60s wall clock). Also enables cancellation, progress events, and partial output salvage within a single call.

**Why it was cut.** It's a 400-500 LOC refactor across five provider clients, and the `generate_structured` and `generate_with_tools` paths effectively buffer the full stream anyway (you can't parse half a JSON object), so most of the UX benefit applies only to plain `generate()`. The resilience benefit is real but not urgent — the chain works correctly without streaming, just slowly. Shipping the chain first and replacing the underlying provider clients later is a clean separation.

**When to revisit.** When slow fallback walks start showing up as user-visible friction in production traces. Or when we add a non-Telegram interface (web, voice) where streaming UX actually matters.

**Costs to remember.**
- `_call_provider()` becomes iterator-returning; everything downstream has to consume it
- Mid-stream error handling is its own failure mode matrix
- Telemetry needs both time-to-first-token and time-to-last-token
- `requests` doesn't love long-lived streams; probably want `httpx`
- Token counting becomes fiddly per provider
- Testing complexity goes up (mocking iterators)

## Run Checkpointing + Resume (candidate: step-60.8)

**The idea.** The ReAct loop writes its state to SQLite at the end of every step: scratchpad, step count, current role, active tool context. A crashed run can be resumed from the last checkpoint with `react.run(resume_from=run_id)`. Requires a side-effect manifest per step so non-idempotent tools (e.g., `send_telegram`) don't double-fire on resume.

**Why it was cut.** It's a 400-500 LOC spec of its own and it changes the run lifecycle in ways that interact with scheduled actions, sub-agents, and the task queue. Too big to fold into step-60.

**When to revisit.** After step-60 lands and graceful degradation has shaken out. Checkpointing is foundational for sub-agents — a sub-agent that can't be resumed is much less useful than one that can.

**Key design questions.**
- Scratchpad serialization format (probably already JSON-compatible)
- Side-effect manifest schema — how do tools declare "I already did X"
- Resume-on-fallback semantics — can a resumed run walk to a different model than the original?
- Interaction with scheduled actions (step-57/59)
- Interaction with sub-agent delegation boundaries (natural checkpoint points)

## Task/Job Queue + Retry Policies (candidate: step-64)

**The idea.** A layer above runs: a named unit of work that may span multiple runs, with states `pending`, `running`, `waiting_retry`, `done`, `failed`, `dead`. Automatic retry with exponential backoff (max 3 attempts), dead-letter queue for permanent failures, resume-on-failure using checkpoints, progress events surfaced to the user.

**Why it was cut.** Not urgent. The immediate bug (2026-04-07) is fixed by chain walking + graceful degradation inside a single run. Task-level resilience matters for scheduled jobs and long-running workflows, but both can wait.

**When to revisit.** After sub-agents and the basic toolset ship. Task queue depends on checkpointing (60.8) to make retries useful.

## OpenClaw-Defensive Tests

**The idea.** Add tests that specifically defend against the six open production bugs in the OpenClaw fallback layer: overloaded-doesn't-trigger-fallback (#24378), timeout-doesn't-trigger-fallback (#44936), fallback-persists-to-config (#47705), LiveSessionModelSwitchError-loop (#57857), OAuth-error-surfaces-to-UI-after-recovery (#49491), raw-server-error-payloads-not-recognized (#45234).

**Why it was cut.** Writing tests to prove we avoided bugs we didn't introduce is defensive programming in its worst form. The right time to add these is after the chain is written and we notice a specific smell or a refactor is proposed — not upfront as insurance against hypothetical future mistakes.

**When to revisit.** Never preemptively. Revisit when:
- A refactor of ChainedModelClient is proposed and we want to lock in the current behavior
- A production trace shows us drifting toward one of the OpenClaw failure modes
- We want a concrete marketing claim ("these bug classes are architecturally impossible in Xibi")

**Source links for when they're needed.**
- [#24378 overloaded](https://github.com/openclaw/openclaw/issues/24378)
- [#47705 config persist](https://github.com/openclaw/openclaw/issues/47705)
- [#45234 codex server_error](https://github.com/openclaw/openclaw/issues/45234)
- [#57857 switch loop](https://github.com/openclaw/openclaw/issues/57857)
- [#44936 timeout fallback](https://github.com/openclaw/openclaw/issues/44936)
- [#49491 oauth surface](https://github.com/openclaw/openclaw/issues/49491)

## LiteLLM Architectural Validation

**The idea.** A spec section comparing our Router/Chain split to LiteLLM's three-layer architecture (`function_with_fallbacks` → `function_with_retries` → `litellm.completion`) and noting the convergence as validation.

**Why it was cut.** It's design doc material, not spec material. The implementer doesn't need to know we looked at LiteLLM; they need to know what to build. The comparison belongs in a PR description or a retrospective post.

**Preserved observation.** LiteLLM's architecture is the cleanest reference we found. Their separation matches ours:
- Router.select() ≈ picking a model group
- ChainedModelClient.generate() ≈ function_with_fallbacks
- BreakerWrappedClient.generate() ≈ function_with_retries + cooldown tracking
- provider client ≈ base completion call

Future features LiteLLM has and we don't: per-exception retry policies, cooldown durations by error type, multi-provider tier groups via `order=`. Each is a candidate for step-60.5 work.

## Multi-Provider Tier Groups (candidate: v2 router work)

**The idea.** LiteLLM-style `order=1` deployment groups where one tier can have multiple equivalent providers. For example, the `review` tier could have `claude-opus-4-6` and `gpt-4o` as siblings instead of a strict sequential order. The router tries all deployments at `order=1` with retries before escalating to `order=2`.

**Why it was cut.** v2 router feature. Speculative.

**When to revisit.** When we have a legitimate case where two providers should be treated as interchangeable at the same tier (e.g., cost-balancing between Anthropic and OpenAI for the same workload). Not before.

## Session-Scoped "Definitely Broken Role" Blacklist

**The idea.** If a role hard-fails (e.g., card declined on Anthropic) during a run, mark it as blacklisted for the rest of the run so we don't keep retrying it every step until the breaker opens. Requires granular error categories to distinguish "transient" from "permanent" failures.

**Why it was cut.** Depends on granular error categories (60.5). Without them we can't tell "card declined, never come back" from "rate limited, try again in 10s." Premature.

**When to revisit.** After granular error categories land.

## Provider Health History Across Runs

**The idea.** Breaker state + failure history persists across runs. A provider that's been failing all day doesn't get a fresh breaker on your first call of the next session — it starts with historical skepticism.

**Why it was cut.** Operational telemetry improvement, not a correctness fix. Nice to have, not urgent.

**When to revisit.** When operational visibility across runs becomes a pain point.
