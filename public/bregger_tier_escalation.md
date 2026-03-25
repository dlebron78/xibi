# Bregger — Tier Escalation Architecture

> **Status**: Consensus (4 rounds of architect debate + expert scoping review)  
> **Date**: March 2026  
> **Companion doc**: [bregger_multistep_loop.md](bregger_multistep_loop.md) (the execution engine)  
> **This doc**: Intelligence routing (how the loop decides who does what)

---

## The Core Principle

**The local model decides _what_ to do. Python decides _who_ does it.**

This extends "LLM Thinks, Python Does" from the vision doc into the intelligence tier. The local model stays in the driver's seat — it runs the ReAct loop, picks tools, sequences steps, decides when to finish. But when a step requires capabilities beyond the local model's tier, Python transparently routes that step's execution to a more capable model.

The local model doesn't need self-awareness of its own limitations. Python knows which tasks are hard because the tools declare it.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│  Layer 1: Tool-Declared Escalation (Primary)        V1  │
│  Tools declare min_tier in manifest → Python enforces   │
│  Catches: known hard tasks before they're attempted     │
└─────────────────────────────┬───────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────┐
│  Layer 2: Prompt-Based Self-Escalation             V1.5 │
│  Local model can request escalation via action: escalate│
│  Catches: edge cases, unforeseen complexity             │
└─────────────────────────────┬───────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────┐
│  Layer 3: Output Validation (Safety Net)           V1.5 │
│  Python heuristics check output quality → re-run if bad │
│  Catches: model tried and failed silently               │
└─────────────────────────────┬───────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────┐
│  Layer 4: Post-Hoc Audit (Offline Learning)          V2 │
│  Batch review of completed traces by cloud model        │
│  Catches: systematic reasoning errors, missed patterns  │
└─────────────────────────────────────────────────────────┘
```

---

## V1 Scope

V1 ships **Layer 1 only** plus the failure recovery mechanisms:

| Ships in V1 | Deferred |
|---|---|
| Layer 1: `min_tier` in manifests | Layer 2 handler: `action: escalate` loop code |
| Unknown tool handling | Layer 3: output validators |
| Execution error + retry limit | Layer 4: post-hoc audit |
| Goal pinning | Auto-promote feedback loop |
| Scratchpad compression | Validator functions |
| Dual loop limits (steps + seconds) | |
| Repetition + stuck detection | |
| Input validation | |

> **Note on Layer 2 prompt text**: The self-escalation instructions are included in the ReAct system prompt at V1 (they're free — just text). But the loop handler that processes `action: escalate` is deferred to V1.5. **V1 behavior**: If the model outputs `action: escalate` before the handler ships, the loop treats it as an unknown tool and injects the available tools list. The model recovers by picking a real tool. Not ideal, but safe.

---

## Layer 1: Tool-Declared Escalation

> **When**: V1  
> **Reliability**: High — deterministic, no model judgment  
> **Cost**: Zero — classification before any LLM call

### Manifest Extension

Every tool gains two optional fields:

```json
{
  "name": "generate_creative",
  "description": "Generate creative content: ad concepts, marketing copy, scripts",
  "output_type": "raw",
  "min_tier": 4,
  "escalation_context": ["business_profile", "brand_voice", "campaign_history"],
  "examples": [
    "write ad concepts for my business",
    "brainstorm marketing hooks",
    "draft a video script"
  ]
}
```

| Field | Purpose | Default |
|---|---|---|
| `min_tier` | Minimum intelligence tier. `3` = local. `4` = cloud. | `3` |
| `escalation_context` | Memory categories to pull for focused cloud prompts. | `[]` |

### ReAct Loop Integration

```python
tool = registry.get(step.action)

if tool is None:
    # See Failure Recovery: Unknown Tool
    step.tool_output = {
        "error": f"Tool '{step.action}' not found",
        "available_tools": [
            {"name": t.name, "description": t.description}
            for t in registry.values()
        ]
    }
    continue

if tool.min_tier > current_tier:
    cloud_prompt = build_escalation_prompt(
        task=step.thought,
        tool=tool,
        context=recall(tool.escalation_context),
        scratchpad=compress_scratchpad(scratchpad, step.step_num)
    )
    step = generate_step(cloud_prompt, model=config.cloud_model)
    step.escalated = True
    step.escalation_source = "manifest"
else:
    step = execute_tool(tool, step.tool_input)
```

**Critical**: Only the tool execution escalates. The local model still runs the loop.

### Escalation Prompt Design

Focused prompt — not the full system prompt:

```
You are a specialist assisting an AI agent.

TASK: {step.thought}
TOOL: {tool.name} — {tool.description}

RELEVANT CONTEXT:
{recalled context from escalation_context categories}

PREVIOUS STEPS:
{compressed scratchpad}

Generate the requested output. Be specific and actionable.
```

Token budget: ~2,000 input, ~1,500 output.

### Default Tier Assignments

| Tool | min_tier | Rationale |
|---|---|---|
| `read_file`, `search`, `recall`, `send_email` | 3 | Local model handles fine |
| `list_files`, `remember`, `append_file` | 3 | Trivial |
| `generate_creative` | 4 | Creative quality drops sharply on <13B |
| `analyze_complex` | 4 | Multi-factor analysis, financial reasoning |
| `draft_long_form` | 4 | Reports, docs >500 words |
| `generate_video` | 3 | API passthrough — tiers attach to cognitive work, not API calls |

---

## Layer 2: Prompt-Based Self-Escalation

> **When**: Prompt text at V1 (free). Loop handler at V1.5 (after data justifies it).  
> **Reliability**: Low for quality, moderate for capability gaps

### System Prompt Text (Ships V1)

```
ESCALATION RULES:
If a step requires creative writing, complex multi-factor analysis, 
or generation you are uncertain about, you may respond with:

Thought: This task requires higher-quality generation than I can provide.
Action: escalate
Action Input: {"reason": "...", "task": "..."}

Only escalate when QUALITY matters. Do not escalate for:
- Tool selection, lookups, searches, file reads
- Summarization of content you've already retrieved
- Deciding what to do next
```

### Loop Handler (Ships V1.5)

```python
if step.action == "escalate":
    cloud_prompt = build_escalation_prompt(
        task=step.tool_input["task"],
        reason=step.tool_input["reason"],
        scratchpad=scratchpad
    )
    result = generate_step(cloud_prompt, model=config.cloud_model)
    step.tool_output = result
    step.escalated = True
    step.escalation_source = "self"
    continue
```

### Why Include Prompt Text Before Handler?

1. Catches genuine capability gaps the manifest didn't anticipate.
2. Escalation logs feed back into Layer 1 — if self-escalation fires 10+ times for a tool, update its `min_tier`.
3. V1 fallback is safe: unhandled `escalate` action is treated as unknown tool → model picks a real tool.

---

## Layer 3: Output Validation

> **When**: V1.5  
> **Reliability**: High for specific failure patterns  
> **Cost**: Microseconds (deterministic heuristics)

### Convention-Based Validators

Auto-discovered by file convention, not declared in manifest (security concern for distributable system):

```
tools/
  generate_creative.py          ← the tool
  validate_generate_creative.py ← auto-discovered validator
```

### Validator Interface

```python
# validate_generate_creative.py
def validate(output: dict) -> dict:
    issues = []
    text = output.get("text", "")
    if len(text) < 200:
        issues.append("output_too_short")
    # ... repetition, diversity checks ...
    return {"valid": len(issues) == 0, "issues": issues}
```

### Auto-Promote Feedback Loop

```
If tool X fails validation >30% at Tier 3 (over 50+ invocations):
  → Auto-promote: min_tier = 4 in runtime config
  → Log recommendation for human manifest update
```

---

## Layer 4: Post-Hoc Audit

> **When**: V2  
> **Cost**: Batch API calls — pennies per audit cycle

Offline batch review of completed traces. Zero latency impact.

### What the Audit Feeds Back Into

| Finding | Action |
|---|---|
| Wrong tool consistently chosen | Update Tier 2 example patterns |
| Model skips `recall` before `send_email` | Strengthen "verify before acting" prompt |
| Tool poor local output >30% | Promote `min_tier` to 4 |
| Recurring step pattern (3+ steps, 50+ occurrences) | Candidate for Plan-then-Execute template |
| Unnecessary escalation | Add to "do not escalate" list |

Cost: ~100 traces/week → a few cents per cycle.

---

## Failure Recovery (All V1)

### Loop Limits

```json
{ "max_steps": 7, "max_loop_seconds": 30 }
```

`max_steps: 7` catches pathological spinning. `max_loop_seconds: 30` is the NucBox UX pain threshold. Whichever hits first triggers force-finish.

### Goal Pinning

Original user message at **top** of every step prompt. Prevents goal drift.

### Scratchpad Compression

```python
def compress_scratchpad(scratchpad, current_step):
    result = []
    for step in scratchpad:
        if step.step_num >= current_step - 1:
            result.append(step.full_text())       # Last 1-2: full detail
        else:
            result.append(step.one_line_summary()) # Older: compressed
    return "\n".join(result)
```

### Unknown Tool

```python
if tool is None:
    step.tool_output = {
        "error": f"Tool '{step.action}' not found",
        "available_tools": [{"name": t.name, "description": t.description} for t in registry.values()]
    }
```

### Execution Error + Retry

One retry per tool per query. After that: `"Do not retry. Try a different approach or finish."`

### Repetition Detection

```python
def is_repeat(step, scratchpad):
    for prev in scratchpad:
        if prev.tool == step.tool:
            overlap = word_overlap(prev.tool_input, step.tool_input)
            if overlap > 0.6:
                return True
    return False
```

### Stuck Detection

After 2 consecutive errors or uncertain thoughts, inject: `"SYSTEM: You've taken {n} steps without progress. If you can't accomplish this, finish and say what capability is missing."`

### Input Validation

Tools validate own inputs before execution. Returns structured error + suggestion (e.g., "Use recall() to look up the address").

### Force-Finish

```python
if at_limit:
    last_useful = get_last_successful_observation(scratchpad)
    return f"I wasn't able to fully complete your request. Here's what I found:\n{last_useful}"
```

---

## Complete Flow Example

"Create video ad concepts for my business":

```
USER: "Create some video ad concepts for my business"

CONTROL PLANE: No regex match → enter ReAct loop

STEP 1 (Local — Gemma 9B):
  THINK: I need business context first.
  ACT: recall(categories=["business_profile", "brand_voice"])
  → min_tier: 3, current: 3 → local
  OBSERVE: {business: "Coastal coffee roastery", voice: "warm, artisanal"...}

STEP 2 (Escalated — Cloud):
  THINK: Now I'll generate creative ad concepts.
  ACT: generate_creative(context=..., format="video_ad", count=3)
  → min_tier: 4, current: 3 → ESCALATE
  → Python builds focused prompt with business context
  → Cloud generates 3 concepts
  OBSERVE: {concepts: [{hook, script, visuals}, ...]}

STEP 3 (Local — Gemma 9B):
  THINK: Present options for user selection before spending API credits.
  ACT: finish
  REPORT: "Here are three concepts for Coastal Coffee:
           1. [concept] 2. [concept] 3. [concept]
           Which should I turn into videos?"

--- USER: "1 and 3" ---

STEP 4 (Local):
  ACT: generate_video(concept_1) → async, returns job_id_1
  OBSERVE: {job_id: "abc123", status: "processing"}

STEP 5 (Local):
  ACT: generate_video(concept_3) → async, returns job_id_2
  OBSERVE: {job_id: "def456", status: "processing"}
  → Both jobs handed off to Heartbeat for polling

STEP 6 (Local):
  ACT: finish
  REPORT: "Generating videos for concepts 1 and 3. I'll send them when ready."

--- HEARTBEAT (minutes later) ---
  Videos complete → deliver via Telegram
```

> **Note**: Video generation is sequential (V1 constraint). Parallel fan-out is V2.

**Cost**: Steps 1, 3-6 local (free). Step 2 one cloud call (~few cents).

---

## Manifest Schema (Complete)

```json
{
  "name": "string",
  "description": "string",
  "output_type": "raw | synthesis | action",
  "risk": "safe | reversible | irreversible",
  "min_tier": "3 | 4 (default: 3)",
  "async": "boolean (default: false)",
  "max_output_size": "integer bytes (default: 2048)",
  "escalation_context": ["memory categories for cloud prompt"],
  "examples": ["invocation examples for Tier 2 matching"]
}
```

> **Validators**: Auto-discovered by convention (`validate_{tool_name}.py`), not in manifest.  
> **Output type**: Three values only. `raw` (show as-is), `synthesis` (LLM narrates), `action` (confirm done). Tools that need formatting should format their own output — the tool knows its data shape.

Backward-compatible. All new fields have defaults.

---

## Configuration

```json
{
  "chat_model": "gemma2:9b",
  "cloud": {
    "enabled": false,
    "api_url": "https://api.anthropic.com/v1",
    "model": "claude-sonnet",
    "api_key_env": "BREGGER_CLOUD_API_KEY",
    "budget_monthly_cents": 500,
    "max_steps_per_query": 2
  },
  "escalation": {
    "log_all": true,
    "auto_promote_threshold": 0.3,
    "auto_promote_min_invocations": 50,
    "validation_enabled": false,
    "audit_enabled": false,
    "audit_frequency": "weekly"
  },
  "loop": {
    "max_steps": 7,
    "max_loop_seconds": 30,
    "max_retries_per_tool": 1,
    "conversation_history_tokens": 1500,
    "show_intermediate_steps": true
  }
}
```

**Cloud is vendor-agnostic.** `api_url` points at any OpenAI-compatible endpoint. API key from env var, never in config.

**Privacy**: `cloud.enabled: false` by default. Fully local out of the box.

---

## What Ships When

| Version | Ships | ~Lines |
|---|---|---|
| **V1** | Layer 1 (`min_tier` routing) + all failure recovery | ~200 |
| **V1.1** | Tier 2 reads `min_tier` for direct routing | ~50 |
| **V1.5** | Layer 2 handler + Layer 3 validators + auto-promote | ~150 |
| **V2** | Layer 4 audit + self-optimizing feedback loops | ~200 |

---

## Reliability Assessment

| Failure Type | Status | Mechanism |
|---|---|---|
| Infinite loop | **Solved** | Dual limits: max_steps + max_loop_seconds |
| Tool doesn't exist | **Solved** | Registry check + available tools injection |
| Tool execution error | **Solved** | Structured errors + retry limit + recovery |
| Model repeats itself | **Solved** | Repetition detection + forced redirection |
| Context overflow | **Solved** | Scratchpad compression + token-budgeted history |
| Runaway cloud cost | **Solved** | Budget cap + per-query step limit |
| Parse failure | **Solved** | Fallback to finish + parse failure metric |
| Nonsensical tool input | **Solved** | Per-tool input validation + helpful errors |
| Model stuck | **Solved** | Stuck detection + permission to give up |
| Wrong tool selection | **Reduced** | Tier 2 patterns help; not eliminated |
| Premature finishing | **Reduced** | Goal pinning; not eliminated |
| Hallucinated inputs | **Reduced** | "Verify before acting" prompt; not eliminated |
| Goal drift | **Reduced** | Goal pinning + compression; not eliminated |
| Bad result interpretation | **Not solved** | Model capability limit |

---

## Metrics

| Metric | Purpose |
|---|---|
| Escalations per query | Over-escalating? |
| Escalation source (L1/L2/L3) | Which mechanism works |
| Cloud tokens per month | Budget tracking |
| Parse failure rate per model | ReAct compatibility |
| Repetition/stuck triggers | How often model gets lost |
| Force-finish rate | Limit calibration |
| Validation failure rate per tool | Feeds auto-promote (V1.5) |
| Audit findings per cycle | System improving? (V2) |

---

## References

- Companion spec: [bregger_multistep_loop.md](bregger_multistep_loop.md)
- Architecture: [bregger_architecture.md](bregger_architecture.md)
- Roadmap: [bregger_roadmap_v2.md](bregger_roadmap_v2.md)
