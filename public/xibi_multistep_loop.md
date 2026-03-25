# Bregger — Multi-Step Execution Loop (ReAct Architecture)

> **Status**: Final — reviewed by PM + external expert + LLM architect (3 rounds)  
> **Date**: March 2026  
> **Prerequisite**: Existing P-D-A-R single-step loop (`bregger_core.py`)

---

## Problem Statement

Bregger's current execution model is **single-step**: one user message → one plan → one tool → one report. This creates three classes of failure:

1. **Blind execution** — The LLM must guess filenames, email addresses, and entity references without inspecting the environment first.
2. **No follow-up** — "Dig deeper into that" fails because the bot has no memory of what "that" refers to.
3. **No composition** — "Read the bugs doc and email me a summary" requires two tools but only one can fire.

Multi-step reasoning eliminates all three by letting the agent **observe before acting**.

---

## Consensus Architecture

After three rounds of review, the agreed approach is a **phased progression** that respects Bregger's local hardware constraints and avoids building optimization before the baseline validates it.

```
V1:    ReAct loop (works for everything — baseline)
V1.1:  Tier 2 intent-to-tool matching (fill the missing tier)
V1.5:  Plan-then-Execute for proven patterns (data-driven)
V2:    Self-optimizing templates (the system compiles itself)
```

### Why this order:
- **V1 first** because you can't optimize what you haven't measured.
- **V1.1 before V1.5** because Tier 2 matching is simpler, gives bigger latency wins for common single-tool cases, and fills the actual gap in the tiered architecture.
- **V1.5 only after traces prove it** — no premature optimization, no DSLs.
- **V2 is the long-term moat** — local-first self-optimization that nobody else is building.

---

## V1: ReAct Loop

### The Loop

```
┌─────────────────────────────────────────────┐
│  User Message                               │
└──────────────┬──────────────────────────────┘
               ▼
┌──────────────────────────────────────────────┐
│  Control Plane (Tier 1 — unchanged)          │
│  Regex match? → Single-step execute → Done   │
│  pending_action? → Execute stored action     │
└──────────────┬──────────────────────────────┘
               │ No match
               ▼
┌──────────────────────────────────────────────┐
│  Reasoning Loop (Tier 3 — NEW)               │
│                                              │
│  while steps < MAX_STEPS:                    │
│    1. THINK   — LLM reasons about next step  │
│    2. ACT     — Execute one tool             │
│       └─ Unknown tool? → Inject error obs    │
│    3. OBSERVE — Append result to scratchpad  │
│       └─ Truncate per tool's max_output_size │
│    4. DECIDE  — Continue or finish?          │
│       └─ Confirmation needed? → Store + ask  │
│                                              │
│  REPORT — Final answer to user               │
│    └─ Step visibility callback to adapter    │
└──────────────────────────────────────────────┘
```

### LLM Prompt Format

```
Thought: I need to find the bugs file. Let me list workspace files.
Action: list_files
Action Input: {}
```

Or to finish:

```
Thought: I have the information the user needs.
Action: finish
Action Input: {"final_answer": "Here are the bugs: ..."}
```

Parsed with simple regex — no JSON mode required. Works with Gemma, Llama, Mistral.

### Conversation History (Token-Budgeted)

Context window is budgeted by **tokens, not turns**, because one turn could be 20 tokens or 2,000.

| Budget | Value | Rationale |
|---|---|---|
| Conversation history | **1,500 tokens** | Hard cap. Most-recent-first truncation. |
| Scratchpad (within loop) | **4,000 tokens** | Room for ~3 tool observations |
| System prompt + tool manifests | ~1,500 tokens | Fixed overhead |
| LLM reasoning headroom | ~1,000 tokens | Space for thought + action output |
| **Total** | **~8,000 tokens** | Fits Gemma 9B's context window |

Configurable via `config.json`:
```json
{ "reasoning": { "conversation_token_budget": 1500, "max_steps": 5 } }
```

### Scratchpad Design (Reflectable from Day 1)

Ephemeral (dies with the request) but structured for future learning:

```python
@dataclass
class Step:
    step_num: int
    thought: str
    tool: str
    tool_input: dict
    tool_output: dict     # truncated per max_output_size
    duration_ms: int
    timestamp: str
```

Stored as a list of `Step` objects:
- **Debuggable today** — print any step to see what happened
- **Learnable later** — can be persisted and analyzed for pattern extraction (V2)
- **Auditable** — every decision is traceable

### Confirmation Gate State Machine

When the loop encounters an irreversible action (e.g., `send_email`), it finishes with a confirmation request instead of executing.

```
┌─────────────────────────────────┐
│ Loop finishes with confirmation │
│ → Store draft in pending_action │
│ → Send "Should I send? [Y/N]"  │
└─────────────┬───────────────────┘
              ▼
┌─────────────────────────────────┐
│ Next user message arrives       │
│                                 │
│ Control Plane checks FIRST:     │
│ if pending_action exists:       │
│   match "yes|confirm|send|go"   │
│     → execute stored action     │
│     → clear pending_action      │
│   else:                         │
│     → clear pending_action      │
│     → route message normally    │
└─────────────────────────────────┘
```

`pending_action` lives in **Working Memory** (RAM, `BreggerCore` instance). It contains the full plan + parameters, ready to execute. If the user says anything other than confirmation, it's discarded — no stale actions.

### Unknown Tool Handling

If the LLM calls a tool not in the registry, the loop catches it and injects an error observation:

```python
if tool_name not in available_tools:
    observation = {
        "error": f"Tool '{tool_name}' not found.",
        "available_tools": list(available_tools.keys())
    }
    # LLM sees this and can self-correct
```

Five lines. Prevents crashes. Lets the LLM recover gracefully.

### Step Visibility (Progress Callback)

On local hardware, a 3-step query takes 10-15 seconds. Users need progress, not silence.

```python
# In the loop, after each step:
if self.step_callback:
    self.step_callback(f"🔍 Step {step.step_num}: {step.thought[:80]}...")
```

The callback is injected by the channel adapter:
- **Telegram**: Edits the "typing..." message in-place with step summaries
- **CLI**: Streams to stdout
- **API**: Ignored (caller handles their own UX)

Default: **on**. Configurable via `config.json`.

---

## Per-Tool Output Truncation

Flat 2KB truncation across all tools is a silent failure. A `read_file` result truncated to 2KB means the LLM sees 20% of a document and presents it as complete.

Each tool declares its truncation limit in the manifest:

```json
{ "name": "read_file",  "output_type": "raw",       "max_output_size": 8192 }
{ "name": "list_files", "output_type": "raw",       "max_output_size": 1024 }
{ "name": "search",     "output_type": "synthesis",  "max_output_size": 2048 }
{ "name": "recall",     "output_type": "raw",       "max_output_size": 4096 }
```

Default: 2048 bytes if not specified. The loop reads `max_output_size` from the manifest when building the observation.

---

## What Changes in Code

| Component | Change | Size |
|---|---|---|
| `process_query()` | Replace Reasoning Plane block with ReAct loop | ~150 lines |
| `BreggerRouter` | New `generate_step()` method (single ReAct step) | ~80 lines |
| `BreggerRouter` | New ReAct system prompt | ~30 lines |
| `generate_report()` | Only called at end of loop; `output_type` gate stays | ~10 lines |
| `list_files` tool | **New tool** in filesystem skill | ~15 lines |
| `_get_user_context()` | Token-budgeted conversation history injection | ~30 lines |
| `pending_action` | Confirmation state machine in Control Plane | ~25 lines |
| Step callback | Progress visibility to channel adapter | ~15 lines |
| Config | `reasoning` block in `config.json` | 5 lines |

**Total**: ~360 lines of new/modified Python. No new dependencies.

### What Doesn't Change

- **Control Plane** — Regex fast-path stays, plus gains `pending_action` check.
- **Manifests** — Gains `max_output_size` field. `output_type` stays.
- **Skill System** — Tools don't change. The loop calls them identically.
- **Heartbeat** — Separate process, untouched.
- **Telegram Adapter** — Gains step callback injection point.

---

## Tool Scenario Coverage

| # | Scenario | Example | Steps | Notes |
|---|---|---|---|---|
| 1 | **Single-shot** | "What's the weather?" | 1 | Same as today |
| 2 | **Lookup-then-act** | "Read the bugs file" | 2 | `list_files` → `read_file` |
| 3 | **Sequential chain** | "Read bugs and email me a summary" | 3 | Read → compose → send |
| 4 | **Entity resolution** | "Email Jake about the meeting" | 2 | `recall(Jake)` → `send_email` |
| 5 | **Confirmation gate** | "Send the report to the board" | 2+gate | Draft → `pending_action` → user confirms → send |
| 6 | **Error recovery** | "Search for X" (API not configured) | 1 | Graceful error guidance |
| 7 | **Follow-up** | "Dig deeper into BUG-002" | 1 | Token-budgeted conversation history provides context |
| 8 | **Conditional** | "If I have unread emails, summarize" | 1-2 | Check → conditionally summarize |
| 9 | **Unknown tool** | LLM hallucinates "post_slack" | 1 | Error observation → self-correct or report |
| 10 | **Parallel fan-out** | "Compare prices on two sites" | N/A | **V2** — sequential only in V1 |
| 11 | **Async / long-running** | "Monitor inbox for Jake's reply" | N/A | Heartbeat domain, not ReAct |

---

## Safety & Guardrails

| Risk | Mitigation |
|---|---|
| Infinite loop | `MAX_STEPS = 5` hard cap (configurable) |
| Context overflow | Token-budgeted history (1,500) + per-tool `max_output_size` |
| Unknown tool | Error observation injected; LLM can self-correct |
| Confirmation bypass | `pending_action` gate for irreversible actions |
| LLM refuses to finish | After MAX_STEPS, Python force-finishes with last observation |
| Parse failure | Non-ReAct output treated as `finish` with raw text; **parse failures logged as metric** |
| Stale pending_action | Any non-confirmation message clears the stored action |

---

## Latency Budget

On the NucBox (Gemma 9B, 7.2 tok/s):

| Query Type | Steps | Estimated Time |
|---|---|---|
| Control Plane hit | 0 (no LLM) | <50ms |
| Single-step (1 tool) | 1 | ~3-5s |
| Two-step (lookup + act) | 2 | ~6-10s |
| Three-step (chain) | 3 | ~10-15s |
| Max budget (5 steps) | 5 | ~20-30s |

Telegram sends step visibility callback during the loop.

> **Latency is acceptable for V1 but not a long-term target.** Local software on your desk should feel responsive. V1.1 and V1.5 exist to push common queries below 1 second. **Escape hatch trigger**: if trace data shows >40% of queries follow the same 2-3 step pattern, build Tier 1.5.

---

## V1.1: Tier 2 — Intent-to-Tool Matching

> **Goal**: Fill the gap between regex (Tier 1) and full LLM reasoning (Tier 3).

### Approach: TF-IDF First, Embeddings If Needed

| Method | Latency | RAM Cost | When to Use |
|---|---|---|---|
| **TF-IDF / BM25** | Microseconds | ~0 | Corpus < 200 examples |
| **Neural embeddings** | ~200-400ms (CPU) | ~500MB | Corpus > 200 or semantic matching needed |

> **Hardware constraint**: NucBox has 16GB shared RAM. Co-loading Gemma 9B + embedding model is tight. Benchmark neural embeddings on actual hardware before committing.

### Confidence Routing

```
Score > 0.9  →  Execute tool directly, skip ReAct
Score 0.7-0.9  →  Enter ReAct for disambiguation
Score < 0.7  →  Full ReAct reasoning
```

### Skill Contract Extension

```json
{
  "name": "read_file",
  "output_type": "raw",
  "max_output_size": 8192,
  "examples": [
    "read the bugs doc",
    "show me the config file",
    "open BUGS_AND_ISSUES.md",
    "what's in the readme"
  ]
}
```

Populates the TF-IDF/embedding index on startup. New skills auto-populate Tier 2.

---

## V1.5: Plan-then-Execute for Proven Patterns

> **Prerequisite**: Trace data from V1 showing recurring step sequences.  
> **Trigger**: >40% of queries follow the same 2-3 step pattern.

Python analyzes scratchpad traces and extracts common tool sequences. When a query matches a known template, Python executes the chain with one LLM call to fill parameters. No per-step reasoning.

**Key constraints**: No DSL. Templates are ordered tool lists. Fall-through to ReAct on failure. Data-driven only — never hand-authored.

---

## V2: Self-Optimizing (The Moat)

The reflection loop periodically scans scratchpad history and:

1. Identifies tool sequences repeating >N times with consistent structure.
2. Generates a Plan-then-Execute template.
3. Registers it in Tier 1.5.
4. Monitors success rate — demotes back to ReAct if failure rate >20%.

**This is what makes Bregger different.** Cloud agents have no incentive to optimize away from the LLM. Bregger does because every token has a time cost. The system literally compiles its own shortcuts.

---

## Resolved Design Decisions

| Question | Decision | Rationale |
|---|---|---|
| Conversation window | 1,500 tokens, most-recent-first truncation | Predictable budget, fits 8K context |
| Typing indicator | Re-send every 5 seconds | Telegram auto-expires; prevents "crash" appearance |
| Model selection | `chat_model` only | One config point. "System Over Model" principle. |
| Step visibility | Default on, configurable | On local hardware, progress > silence |
| Confirmation flow | `pending_action` in Working Memory | Explicit state machine, no magic |
| Unknown tools | Error observation injected | Self-correction, no crash |
| Output truncation | Per-tool `max_output_size` in manifest | Prevents silent data loss |

---

## References

- [ReAct: Synergizing Reasoning and Acting in Language Models](https://arxiv.org/abs/2210.03629) — Yao et al., 2022
- [Toolformer](https://arxiv.org/abs/2302.04761) — Schick et al., 2023
- [LangChain AgentExecutor](https://docs.langchain.com/docs/components/agents/) — Similar loop implementation
- [Home Assistant Intent System](https://developers.home-assistant.io/docs/intent_index/) — Deterministic routing reference
