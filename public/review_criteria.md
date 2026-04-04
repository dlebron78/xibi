# PR Review Criteria — Vision Alignment Gates

> **Who reads this:** Cowork (Opus) during PR review. Every PR review MUST check these gates and include findings in the "Vision Alignment" section of the review. A shallow "✅ moves Xibi forward" is not sufficient — each gate requires a specific, grounded assessment with file references.

---

## Gate 1: LLM Thinks, Python Does

**The principle:** The LLM reasons and proposes actions. Python validates, gates, and executes. The LLM never has shell access, never runs generated code, and never decides its own permission level.

**Review questions (answer each explicitly):**
- Does any new code allow the LLM to execute arbitrary commands or eval() expressions?
- Does any new code let the LLM influence its own permission tier or bypass CommandLayer.check()?
- Does any new code inject Python-generated metadata into the LLM scratchpad or system prompt? (This is a known anti-pattern — small local models degrade when Python annotations are mixed into their context. See side-channel architecture constraint.)
- If new tools are added: do they go through the skill registry and permission tier system, or do they bypass it?
- If new LLM prompts are added: do they instruct the LLM to reason and propose, or do they give the LLM execution authority?

**Red flags:** `eval()`, `exec()`, `subprocess` called with LLM-generated input, tool output modified before entering scratchpad, new exit reasons that bypass the gate chain.

---

## Gate 2: Local-First, Cloud-Optional

**The principle:** 90% of work runs on local hardware at zero cost. Cloud is a fallback for heavyweight reasoning, not the default path. Cloud calls are logged, auditable, and cost-tracked.

**Review questions (answer each explicitly):**
- Does this PR add new LLM calls? If yes, what effort level do they use? (fast/think = local, review = cloud)
- Are new LLM calls hardcoded to cloud providers, or do they go through get_model() with the fallback chain?
- If cloud calls are added: are they tracked via radiant.py / inference_events?
- Does this PR increase the number of LLM calls per user action? If so, is there a justification?
- Could any new cloud calls be replaced with Python logic or local model calls?

**Red flags:** Direct API calls to OpenAI/Anthropic/Gemini bypassing router.py, new cloud-only features with no local fallback, LLM calls in hot paths (per-message, per-tool-call) without cost justification.

---

## Gate 3: System Over Model

**The principle:** Swapping models should never break a feature. The system must work with any model, any size, from 4B local to cloud.

**Review questions (answer each explicitly):**
- Does any new code depend on a specific model's capabilities (e.g., function calling, JSON mode, vision)?
- If new prompts are added: do they work with the three supported output formats (JSON, XML, plain text)?
- Are there hardcoded model names anywhere in the new code? (Acceptable only in config defaults, never in runtime paths.)
- If new parsing logic is added: does it have recovery/fallback paths for malformed output?
- Does the trust gradient need to be aware of new model-dependent behavior?

**Red flags:** Model name strings in runtime code, format-specific logic without fallbacks, features that silently break with smaller models, provider-specific API features used without abstraction.

---

## Gate 4: Permission Boundaries

**The principle:** GREEN = auto-execute, YELLOW = audit, RED = user confirmation. Tiers can only be promoted via profile config, never demoted in code. Non-interactive contexts block RED.

**Review questions (answer each explicitly):**
- If new tools are added: are they registered in TOOL_TIERS with appropriate tiers?
- Do new tools default to RED (the safe default) if not explicitly registered?
- Does any new code modify or bypass the CommandLayer gate chain?
- If the observation cycle or heartbeat is modified: can it now execute RED-tier tools? (It must not.)
- Are YELLOW-tier actions properly audit-logged?

**Red flags:** Tools with no tier registration, code paths that skip CommandLayer.check(), non-interactive code paths that can trigger RED actions, audit logging gaps for YELLOW actions.

---

## Gate 5: Source Provenance (after step-44)

**The principle:** Every action should be traceable to its origin — did the owner request this, or did the system ingest content that inspired it? External content triggering write actions should bump the tier.

**Review questions (answer each explicit):**
- If new ingestion paths are added (new MCP servers, new channels): do they tag content with the appropriate source?
- If new write tools are added: are they in the WRITE_TOOLS set?
- Does any new code create paths where external content can trigger write actions without source tracking?
- Are decisions logged with provenance context (prev_step_source, source_bumped)?

**Red flags:** New data ingestion without source tagging, write tools missing from WRITE_TOOLS, code paths where MCP content triggers actions without tier awareness.

---

## Gate 6: Data Stays Local

**The principle:** All persistent data in local SQLite. Cloud calls are explicit and logged. No user data leaves the device without going through the permission tier system.

**Review questions (answer each explicitly):**
- Does this PR add new external API calls? If so, what data is sent?
- Does any new code send user content (emails, messages, files) to external services without going through the tier system?
- Are new database writes using open_db() with WAL mode? (No bare sqlite3.connect().)
- If new tables are added: do they follow the migration system in db/migrations.py?
- Is PII (names, emails, content) logged to any external service?

**Red flags:** User content in API request bodies without tier gating, new databases outside the migration system, log statements that include email bodies or message content, external analytics or telemetry.

---

## Gate 7: Autonomy Direction

**The principle:** Xibi is climbing the autonomy ladder: L0 Reactive → L1 Monitoring → L2 Aware → L3 Proactive → L4 Learning. Each PR should either hold the current level steady or move it forward. No PR should regress autonomy.

**Review questions (answer each explicitly):**
- Does this PR add new autonomous capabilities (heartbeat actions, observation cycle tools, proactive signals)? If so, are they properly gated by the permission tier system?
- Does this PR remove or disable existing autonomous behavior without justification?
- If this PR adds user-facing features: do they also work in headless/autonomous mode, or are they interactive-only?
- Does this PR contribute to entity awareness, cross-session memory, or pattern detection? (These are the L2+ capabilities.)

**Red flags:** Autonomous actions without permission tier gating, features that only work when a user is present (limits growth toward L3+), removal of signal extraction or observation capabilities without replacement.

---

## How to Use This in Reviews

The "Vision Alignment" section of every PR review must:

1. **Reference each gate by number** — don't just write a general statement.
2. **Cite specific files and lines** — "Gate 1 satisfied: new tool registered in tools.py line 28, goes through CommandLayer.check() via dispatch() at react.py line 685."
3. **Flag violations clearly** — "Gate 2 VIOLATION: new LLM call at heartbeat/poller.py line 312 hardcodes provider='gemini' instead of using get_model()."
4. **Distinguish blocking from advisory** — Gate violations that break security boundaries (Gates 1, 4, 6) are blocking. Gate violations that slow progress (Gates 2, 3, 7) are advisory notes.
5. **Be specific about "not applicable"** — If a gate doesn't apply to this PR, say why in one sentence: "Gate 5: N/A — no new ingestion paths or write tools added."

A review that says "✅ Aligns with vision" without addressing the gates is incomplete.
