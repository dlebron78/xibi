# Bregger Architecture Review — March 2026

> Full codebase audit of bregger_core.py (3,375 lines), bregger_shadow.py, bregger_telegram.py, bregger_heartbeat.py, bregger_utils.py, all 5 skill manifests, config.json, and 2,600 lines of tests. Read with the lens: "make this the smartest routing engine possible across local and cloud LLMs."

---

## What's Working Well

**The bones are solid.** The P-D-A-R architecture (Plan → Dispatch → Act → Report) with a layered Control Plane → Shadow → ReAct → Cloud escalation is the right design. Specific strengths:

1. **Control Plane (Tier 1)** — KeywordRouter + IntentMapper is clean. Regex patterns are fail-closed (incomplete extraction returns None). Dynamic registration from manifests via `control_plane_triggers` is elegant. User-defined shortcuts from the Ledger are a nice touch.

2. **Shadow Matcher (Tier 2)** — BM25 is a smart choice over TF-IDF for this use case. The normalized scoring (confidence = score / max_possible_score) is correct. The threshold at 0.65 is reasonable.

3. **ReAct Loop** — The failure recovery suite is genuinely impressive: dual loop limits, repetition detection, stuck detection, truncation guards, parse fallbacks, goal pinning. This is better than most production agent loops I've seen.

4. **Data densification** — `_densify_data()` converting JSON tool output to compact text for the local LLM is a critical optimization for CPU inference. The email and search-specific formatters save hundreds of tokens per step.

5. **Memory architecture** — Signals, beliefs (bi-temporal), ledger, and conversation history give you a solid foundation. The passive memory extraction thread is clever.

---

## The 7 Things Holding You Back

### 1. Shadow Matcher is observe-only — it should be routing

**Current state:** Shadow runs, prints a prediction, and does nothing. The ReAct loop always fires regardless.

```python
# Line 2615 — shadow runs but result is ignored
_shadow_prediction = self.shadow_matcher.match(user_input)
if _shadow_prediction:
    print(f"👻 Shadow predicts: {_shadow_prediction['predicted_tool']} ({_shadow_prediction['score']:.2f})")
```

**Impact:** Every single query — even "check my email" — goes through a full LLM inference call (~7 tok/s on your NucBox = 5-15 seconds). Shadow could route common queries in <1ms.

**Fix:** Graduate Shadow from observer to router. When confidence is high (>0.85), skip ReAct entirely and go straight to tool execution. When confidence is medium (0.65-0.85), use the prediction as a hint to the ReAct loop (inject it as a "suggested first step"). When low, fall through to ReAct normally.

**Estimated impact:** 40-60% of queries handled without any LLM call. Transforms perceived latency from "10 seconds" to "instant."

---

### 2. The routing pipeline has no unified decision point

**Current state:** Routing decisions are scattered across 4 separate code paths in `_process_query_internal()`:

1. MessageModeClassifier decides if it's "command" vs "conversation" (lines 2306-2312)
2. Control Plane only fires if mode == "command" (line 2317)
3. Pending action gate intercepts (lines 2479-2577)
4. ReAct fires as the catch-all (line 2579+)

The Shadow Matcher sits inside the ReAct section but doesn't participate in routing at all. There's no single place where the system says "I've considered all options, here's the best route."

**Fix:** Create a unified `route()` method that evaluates all tiers in order and returns a `RoutingDecision`:

```python
@dataclass
class RoutingDecision:
    tier: int              # 1=control_plane, 2=shadow, 3=react_local, 4=react_cloud
    tool: str | None       # Pre-selected tool (tiers 1-2) or None (tiers 3-4)
    confidence: float      # 0.0-1.0
    reason: str            # For tracing
    provider: str          # "none", "ollama", "gemini"
```

This makes routing observable, testable, and tunable. Right now you can't even answer "why did Bregger choose this path?" without reading logs.

---

### 3. Cloud escalation is blunt — one threshold, all-or-nothing

**Current state:** The only escalation trigger is context window overflow:

```python
# Line 2598 — escalation is purely about context size
if approx_tokens > (num_ctx * threshold_pct / 100) and self.router.providers.get("gemini"):
    self.router.default_provider = "gemini"
```

This means: a simple "what's the weather" with a long conversation history gets routed to Gemini (expensive), while a complex multi-hop reasoning task with a fresh context stays on local (likely to fail).

**What's missing:**
- **Task complexity signals:** Multi-entity queries, long generation requests, creative writing, summarization of long documents — these should escalate based on what's being asked, not just context size.
- **Per-step escalation:** The spec says "local model decides what tool to use, Python routes hard generation to cloud." This isn't implemented. Every step in a ReAct chain uses the same provider.
- **Tool-declared min_tier:** The manifests have `min_tier` fields (calendar tools = 2, filesystem = 3), but `min_tier: 4` isn't used to trigger cloud escalation during ReAct. It only works in the Control Plane path (IntentMapper line 1527).

**Fix:** Implement per-step provider selection inside the ReAct loop. After `generate_step()` selects a tool, check if `min_tier >= 4` and swap to cloud for that step's report generation. Also add complexity heuristics: if the user's request contains multiple entities + temporal reasoning + creative output, start on cloud.

---

### 4. MessageModeClassifier creates false dichotomies

**Current state:** The classifier uses heuristic scoring to bucket messages into exactly one of: command, conversation, drafting, confirmation. If the score is below 3 or tied, it defaults to "conversation" — which skips the Control Plane entirely.

**Problems:**
- "Hey, check my email" scores +5 conversation (starts with narrative "hey") AND +3 command (contains "email"). Conversation wins → Control Plane is skipped → goes to ReAct → 10 second wait for what should be instant.
- "What did that email say about the project deadline?" — is this a command (email) or conversation (question)? The classifier can't express "both."
- The classifier has zero awareness of tool availability. It doesn't know that "email" is a domain noun that maps to a skill.

**Fix:** Remove the classifier as a routing gate. Instead, always try the Control Plane first (it's microseconds and fail-closed). Always try Shadow second (also microseconds). Only if both return None do you enter ReAct. The classifier's value is in detecting confirmations and draft continuations — keep it for that, but don't let it block the fast path.

---

### 5. bregger_core.py is a 3,375-line god object

This isn't a routing problem per se, but it's the #1 thing that will slow down every improvement you try to make. `BreggerCore` contains:

- Routing logic (process_query, the entire ReAct loop)
- Database schema migrations (7 ensure_* methods)
- Memory management (prewarm, passive extraction, signal logging)
- Task layer (create, resume, cancel, expire)
- Context building (user context, history compaction, temporal resolution)
- Trace logging
- Confirmation gate handling
- Status/capability formatting

Every change touches this file. Every test has to mock half of it. The ReAct loop alone (from line 2579 to ~3100) is 500+ lines embedded inside `_process_query_internal()`.

**Fix (incremental, not a rewrite):**
1. Extract `ReActRunner` class (the loop, scratchpad, step generation)
2. Extract `RoutingEngine` class (control plane + shadow + escalation logic)
3. Extract `MemoryManager` class (signals, beliefs, ledger, passive extraction)
4. Extract `TaskManager` class (create, resume, cancel, expire)
5. Keep `BreggerCore` as the orchestrator that wires these together

---

### 6. No feedback loop from execution to routing

**Current state:** Shadow tracks hits/correct in a `shadow_phrases` table, but nothing writes to it. The ShadowMatcher has no learning. The Control Plane has no learning. Cloud escalation has no learning.

Your V2 roadmap mentions "self-optimizing: system analyzes traces, adjusts min_tier, generates Tier 2 patterns" — but there's no infrastructure for this yet.

**Quick win:** After every successful ReAct execution, log the (user_input → first_tool_used) pair. Periodically (heartbeat), scan these pairs and:
- If a phrase→tool pair appears 3+ times with 100% accuracy, auto-register it as a Shadow phrase
- If a tool consistently fails on local but succeeds on cloud, bump its `min_tier`
- Surface these as proposed changes (like the reflection loop does for tasks)

---

### 7. The two-provider architecture limits cloud flexibility

**Current state:** You have exactly two providers: Ollama and Gemini. The config hardcodes `tier4_model: "gemini-2.5-flash"`. But the spec says "vendor-agnostic (any OpenAI-compatible endpoint)."

**What's missing:**
- No OpenAI-compatible provider (which would give you access to Claude, GPT-4, Groq, Together, Fireworks, etc.)
- No provider selection per task type (e.g., Gemini for search synthesis, Claude for email drafting)
- No cost tracking per provider to inform routing decisions

**Fix:** Add an `OpenAICompatibleProvider` that works with any `/v1/chat/completions` endpoint. This is ~40 lines of code and instantly opens up every cloud LLM. Then make the config support multiple cloud providers with per-provider cost rates, so routing can factor in cost.

---

## Recommended Build Order

| Priority | What | Why | Effort |
|----------|------|-----|--------|
| **1** | Graduate Shadow to active routing | Biggest UX win. 40-60% of queries go from 10s → instant. | ~50 lines |
| **2** | Remove classifier as routing gate | Fixes the "hey check my email" class of bugs. | ~20 lines (delete code) |
| **3** | Unified `route()` method | Makes everything that follows testable and observable. | ~100 lines |
| **4** | OpenAI-compatible provider | Unlocks every cloud LLM. Vendor-agnostic per your spec. | ~40 lines |
| **5** | Per-step cloud escalation | Local LLM picks tools, hard steps run on cloud. Your spec, finally real. | ~60 lines |
| **6** | Extract ReActRunner + RoutingEngine | Stop the god object before it gets worse. | ~200 lines moved |
| **7** | Feedback loop (traces → shadow phrases) | The moat. System gets smarter over time. | ~80 lines |

**Total estimated new code: ~350 lines.** Most of the work is reorganizing what exists, not writing from scratch.

---

## One More Thing

Your NucBox runs Gemma 9B at ~7 tok/s on CPU. The outline recommends swapping to Qwen3 8B for better tool calling. I'd go further: **the smartest routing decision is often to not call the LLM at all.** Priorities 1 and 2 above make the NucBox feel fast by avoiding inference for the common case. Priority 5 (per-step escalation) makes the hard cases feel smart by using cloud only where it matters. The combination — fast when it can be, smart when it needs to be — is the actual moat.
