# step-84 — Subagent MCP Tool Prefetch

> **Epic:** Subagent Runtime & Domain Agent System (`tasks/EPIC-subagent.md`)
> **Block:** 4 of 6 — Runtime Tool Access
> **Phase:** 4 — depends on Blocks 1-3 (steps 81-83, all merged)
> **Acceptance criteria:** see below (11 items)

---

## Context

The subagent runtime executes skills as pure LLM calls — prompt in, JSON out. This was the right constraint for v1: it kept the trust boundary simple and prevented skills from doing anything the runtime didn't explicitly allow. But it also means skills can't access real data during execution. Career-ops evaluate runs without the scoring rubric. Research writes company briefs from training knowledge. Scan doesn't exist because it needs HTTP fetch.

The core gap: `checklist.py` line 99 calls `router.call()` (LLM) but has no path to call an MCP tool. Meanwhile, Xibi already has a mature MCP client (`xibi/mcp/client.py`) with `call_tool()`, connection management, error handling, and truncation. The heartbeat uses it to call jobspy every 8 hours. The infrastructure exists — it's just not available to the checklist executor.

**What this step builds:** A pre-fetch phase in the checklist executor that calls MCP tools before the LLM call and injects results into `scoped_input`. Skills declare what tools they need in the manifest; the executor provisions the data. The LLM still never calls tools directly — the runtime does it on the skill's behalf, maintaining the trust boundary.

**What this step validates:** Can career-ops skills consume real external data through the existing MCP infrastructure without redesigning the execution model?

---

## Goal

1. **Tool declaration in manifest** — Skills can declare `tools:` with MCP server/tool/args, resolved at checklist-build time
2. **Pre-fetch in executor** — Before the LLM call, executor calls declared tools via MCPClient and injects results into scoped_input
3. **Reference doc injection** — `resolve_checklist` injects reference docs into `scoped_input.references` (gap from step-83)
4. **Input validation guardrail** — Skills with `required` inputs refuse to execute when those inputs are missing (prevents hallucinated-data runs)
5. **Scan skill** — First MCP-backed skill: calls jobspy, returns structured postings

---

## Architecture

### Tool Declaration (manifest change)

Skills gain an optional `tools` array in `agent.yml`:

```yaml
- name: scan
  description: "Search job boards for matching postings"
  prompt_file: skills/scan.md
  trust: L1
  model: haiku
  standalone: true
  tools:
    - server: jobspy
      tool: search_jobs
      args_from: scoped_input.criteria    # JSONPath-ish reference
      args_default:                       # fallback if args_from resolves to null
        search_term: "Product Manager Director"
        location: "Remote"
        is_remote: true
        results_wanted: 15
        hours_old: 48
      inject_as: raw_postings             # key added to scoped_input
      required: true                      # skill fails if tool call fails
```

`args_from` resolves against the run's `scoped_input` at execution time. If the referenced field is null/missing, `args_default` is used. If neither produces valid args, the tool call is skipped (or fails if `required: true`).

`inject_as` names the key under `scoped_input` where the tool result lands. The skill prompt references it: "Your input includes `scoped_input.raw_postings` — an array of job postings from the search."

Multiple tools per skill are allowed (executed sequentially). Tool results from one don't feed into the next — they all inject into scoped_input independently.

### Pre-fetch Phase (checklist.py change)

The execute loop gains a tool phase before line 99's `router.call()`:

```python
# --- Pre-fetch: call declared MCP tools and inject results ---
if "tools" in step_cfg:
    for tool_decl in step_cfg["tools"]:
        server_name = tool_decl["server"]
        tool_name = tool_decl["tool"]
        
        # Resolve args from scoped_input or use defaults
        args = _resolve_args(run.scoped_input, tool_decl)
        
        # Get or create MCPClient for this server
        client = _get_mcp_client(server_name, mcp_configs)
        result = client.call_tool(tool_name, args)
        
        if result["status"] == "ok":
            inject_key = tool_decl.get("inject_as", tool_name)
            run.scoped_input[inject_key] = _parse_tool_result(result["result"])
        elif tool_decl.get("required", False):
            raise RuntimeError(f"Required tool {server_name}/{tool_name} failed: {result['error']}")
        else:
            logger.warning(f"Optional tool {server_name}/{tool_name} failed: {result['error']}")
```

**MCP client lifecycle:** Clients are created per-run (not per-step) and closed when the run completes. The executor receives `mcp_configs` from the caller — either from `config.json`'s `mcp_servers` list or from the registry. This avoids importing config.json knowledge into checklist.py.

**Cost tracking:** MCP tool calls are not LLM calls, so they don't increment `actual_calls` or `actual_cost_usd`. They're logged but don't count against the budget. If MCP tools eventually have cost (paid APIs), a separate `tool_cost` field can be added later.

### Reference Doc Injection (registry.py change)

`resolve_checklist` currently reads prompt files but doesn't inject reference docs. The fix:

```python
# In resolve_checklist, after reading prompt_content:
references = {}
references_dir = agent_dir / "references"
if references_dir.exists():
    # Scan prompt for reference mentions
    for ref_file in references_dir.iterdir():
        if ref_file.suffix == ".md" and ref_file.name in prompt_content:
            references[ref_file.name] = ref_file.read_text()

# Add to checklist entry
checklist.append({
    "skill_name": skill.name,
    "model": skill.model,
    "trust": skill.trust,
    "prompt": prompt_content,
    "references": references,       # NEW
    "tools": skill.tools or [],     # NEW
})
```

The executor injects `references` into `scoped_input.references` before the LLM call:

```python
if step_cfg.get("references"):
    run.scoped_input.setdefault("references", {}).update(step_cfg["references"])
```

This means evaluate gets `scoped_input.references["scoring-rubric.md"]`, `scoped_input.references["archetypes.md"]`, etc. — exactly what the spec-83 prompts were written to expect.

### Input Validation Guardrail

Two layers:

**Layer 1 — Schema validation at spawn time (already exists, extend it):**
Currently only validates single-skill standalone input (runtime.py line 56). Extend to validate per-skill required inputs even in multi-skill runs. If a skill declares `standalone_input.required: ["posting"]` and `scoped_input.posting` is missing, fail before execution.

**Layer 2 — Prompt-level refusal:**
Add a preamble to the executor's prompt assembly (checklist.py line 85):

```python
prompt += "\nIMPORTANT: If any required input referenced in the prompt below is missing or empty "
prompt += "in scoped_input, return {\"error\": \"missing_input\", \"detail\": \"<field>\"} — "
prompt += "do NOT fabricate or hallucinate the missing data.\n"
```

This catches cases where the schema doesn't cover the requirement but the prompt does. Belt and suspenders.

### Scan Skill

New skill prompt: `domains/career-ops/skills/scan.md`

```markdown
# Scan — Job Board Search

## Context
You are a job search scanner working as part of Xibi's career-ops agent.
Your job is to filter and structure raw job board results into a clean pipeline.

## Input
- `scoped_input.raw_postings` — Raw results from job board search (injected by MCP prefetch)
- `scoped_input.user_config["profile.yml"]` — Daniel's career profile (target roles, industries, preferences)

## Instructions
1. Parse each posting from raw_postings
2. Filter out clearly irrelevant results (wrong seniority, wrong domain, non-English)
3. Structure each remaining posting as: {title, company, location, remote, url, text, source}
4. Quick-tag each with primary archetype (use training knowledge, not references)
5. Sort by likely relevance to profile

## Output Format
Return ONLY a JSON object:
{
  "postings": [
    {
      "title": "...",
      "company": "...",
      "location": "...",
      "remote": true/false,
      "url": "...",
      "text": "first 500 chars of description",
      "source": "indeed|linkedin|etc",
      "archetype_tag": "Technology|Finance|etc",
      "relevance_note": "one-line reason this matches"
    }
  ],
  "filtered_count": 0,
  "filter_reasons": ["3 postings removed: wrong seniority (intern/entry)"]
}
```

Manifest addition to `agent.yml`:

```yaml
- name: scan
  description: "Search job boards for matching postings via MCP"
  prompt_file: skills/scan.md
  trust: L1
  model: haiku
  standalone: true
  tools:
    - server: jobspy
      tool: search_jobs
      args_from: scoped_input.criteria
      args_default:
        search_term: "Product Manager Director VP"
        location: "Remote"
        is_remote: true
        results_wanted: 15
        hours_old: 48
      inject_as: raw_postings
      required: true
  standalone_input:
    type: object
    required: []
    properties:
      criteria:
        type: object
        description: "Search criteria override (optional — defaults from profile)"
```

With scan in place, `default_sequence` can finally be defined:

```yaml
default_sequence: [scan, triage, evaluate]
```

---

## Legacy Code Boundary

**All changes in this step go into `xibi/subagent/` and `domains/`.** No code is added to or modified in:

- `bregger_core.py` — Dead subagent handlers (test-echo only). Not used by `xibi-telegram.service`.
- `bregger_dashboard.py` — Existing subagent dashboard endpoints already work. No changes needed for MCP prefetch.

The subagent runtime, registry, checklist executor, and manifest validator all live in `xibi/subagent/` — that's the only package this step touches.

---

## What This Step Does NOT Build

- **Interactive tool use** — Skills don't call tools mid-generation. Pre-fetch only: data is gathered before the LLM call starts. Interactive tool use (agent-style) is a future enhancement.
- **Tool-to-tool chaining** — Multiple tools per skill execute independently. Tool A's result doesn't feed into Tool B's args. If needed, the LLM can reason about all injected results together.
- **MCP server lifecycle management** — Assumes servers are already running (jobspy is managed by systemd). Starting/stopping servers is out of scope.
- **New MCP servers** — Only jobspy is wired. Company portal scanning (Greenhouse, Lever, Ashby) needs new MCP servers built — that's a separate step (Block 6).
- **Story bank persistence** — The post-run hook for accumulating STAR+R stories is a step-83 gap but not related to MCP prefetch. Track separately.
- **Telegram or observation dispatch** — Triggering career-ops from signals or user messages is step-85. This step only enables skills to consume external data.

---

## Files Changed

| File | Change |
|------|--------|
| `xibi/subagent/checklist.py` | Pre-fetch loop before `router.call()`, reference injection, input validation preamble |
| `xibi/subagent/registry.py` | `resolve_checklist` reads reference docs, passes `tools` and `references` in checklist entries |
| `xibi/subagent/manifest.py` | Schema update: `tools` array in skill declarations |
| `xibi/subagent/models.py` | `SkillDeclaration` gains `tools: list[dict]` field |
| `domains/career-ops/agent.yml` | Add scan skill, add `default_sequence`, add `tools` to research skill |
| `domains/career-ops/skills/scan.md` | New scan skill prompt |
| `tests/test_subagent.py` | Tests for pre-fetch, reference injection, input validation, scan execution |

---

## Implementation Order

1. **Models + manifest schema** — Add `tools` field to SkillDeclaration and ManifestValidator
2. **Registry: resolve_checklist** — Inject reference docs, pass `tools` and `references` in checklist entries
3. **Checklist: pre-fetch loop** — MCP tool calls before LLM call, result injection into scoped_input
4. **Checklist: input validation preamble** — Refuse to hallucinate missing data
5. **Scan skill** — Write prompt, add to manifest, add `default_sequence`
6. **Tests** — Mock MCP client, verify pre-fetch → inject → LLM flow; verify reference injection; verify input refusal
7. **Integration test** — scan skill on NucBox with real jobspy: search → filter → structured output

---

## Acceptance Criteria

1. Skills can declare `tools:` in agent.yml with server, tool, args_from, args_default, inject_as
2. Checklist executor calls declared MCP tools before the LLM call and injects results into scoped_input
3. `args_from` resolves against scoped_input; falls back to `args_default` when field is missing
4. Failed required tool call aborts the skill with a clear error; failed optional tool call logs a warning and continues
5. Reference docs from `domains/{agent}/references/` are injected into `scoped_input.references` when referenced in the skill prompt
6. Skills with required inputs refuse to execute when those inputs are missing — return error, don't hallucinate
7. Scan skill calls jobspy via MCP, filters results, returns structured postings
8. `default_sequence: [scan, triage, evaluate]` runs the full pipeline end-to-end
9. MCP tool calls don't count against the LLM call budget
10. MCPClient instances are reused across steps within a run and closed on run completion
11. All changes pass existing tests; new tests cover pre-fetch, reference injection, and input validation
