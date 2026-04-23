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

## Gate 2: Local-Capable, Cloud-Capable

**The principle:** The architecture is environment-agnostic. Local models handle routine work at zero marginal cost. Cloud models are available for complex reasoning and are cost-tracked. Neither is the default — the router decides based on task effort level.

**Review questions (answer each explicitly):**
- Does this PR add new LLM calls? If yes, what effort level do they use? (fast/think = typically local, review = typically cloud)
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

## Gate 5: Source Provenance

**The principle:** Every action should be traceable to its origin — did the owner request this, or did the system ingest content that inspired it? External content triggering write actions should bump the tier.

**Review questions (answer each explicitly):**
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

**The principle:** Xibi tracks progress on two axes: autonomy (what the agent can do) and trust (why you should let it). See the Autonomy-Trust Matrix in xibi_vision.md. Each PR should either hold both levels steady or advance at least one without regressing the other. Increasing autonomy without maintaining trust is a regression.

**Review questions (answer each explicitly):**
- Does this PR add new autonomous capabilities (heartbeat actions, observation cycle tools, proactive signals)? If so, are they properly gated by the permission tier system?
- Does this PR remove or disable existing autonomous behavior without justification?
- If this PR adds user-facing features: do they also work in headless/autonomous mode, or are they interactive-only?
- Does this PR contribute to entity awareness, cross-session memory, or pattern detection? (These are the L2+ capabilities.)
- Does this PR maintain or advance the current trust level? (T2: source provenance, audit trails, permission gating on all execution paths.)

**Red flags:** Autonomous actions without permission tier gating, features that only work when a user is present (limits growth toward L3+), removal of signal extraction or observation capabilities without replacement, new autonomous capabilities that bypass source provenance tracking.

---

## Gate 8: Agent Isolation

**The principle:** Xibi supports multiple agents with different trust profiles (e.g., an owner-facing executive assistant and a public-facing chatbot). Agent boundaries must be enforced structurally — shared state between agents is explicit and auditable, never implicit.

**Review questions (answer each explicitly):**
- Does this PR introduce shared mutable state that could leak between agent contexts? (Shared config, global variables, singleton caches.)
- If new database writes are added: are they scoped to an agent or session context, or do they write to a global namespace?
- If new tools are added: could a public-facing agent invoke tools intended only for the owner agent?
- Does any new code assume a single-agent deployment? (Hardcoded config paths, single-user session assumptions.)
- If new channels are added: is routing to the correct agent explicit, or could messages be misrouted?

**Red flags:** Global mutable state accessible from tool execution, tools that don't check the calling agent's trust profile, database tables without agent scoping, session data shared across agent boundaries without explicit opt-in.

**Note:** Full multi-agent routing is not yet implemented. This gate ensures new code doesn't make it harder. PRs are not required to implement multi-agent support — they are required to not block it.

---

## Gate 9: Signal Fidelity

**The principle:** The signal stream (sources → extraction → intelligence → threads → observation) is Xibi's primary intelligence layer. Memory is what makes a small local model competitive with frontier cloud models. Every PR should maintain or improve the quality and continuity of this pipeline.

**Review questions (answer each explicitly):**
- If this PR modifies source polling or MCP integration: does signal extraction still produce well-formed signals with source, entity, urgency, and thread linkage?
- If this PR modifies signal intelligence or entity resolution: are existing thread associations preserved? Could this change orphan signals or break thread continuity?
- If this PR modifies the observation cycle: does it still consume signals, update thread state, and produce actionable output (nudges, task updates)?
- If new data sources are added: do they have a registered signal extractor, or do they fall through to the generic extractor?
- Does this PR affect the dashboard's ability to display signal and thread data? (Empty panels are a sign of schema drift.)

**Red flags:** Source changes that bypass the signal extractor registry, observation cycle changes that reduce signal consumption, schema changes to the signals or threads tables without corresponding dashboard query updates, new sources with no extractor registration, thread operations that don't update `updated_at`.

---

## Reference Deployment Awareness

> **Purpose:** The pipeline builds infrastructure horizontally (more sources, more resilience, more entity awareness). This section ensures it also considers vertical progress toward real use cases.

Xibi has three reference deployments that validate the architecture (see xibi_vision.md):

1. **Chief of Staff** (L2/T2) — cross-source monitoring, signal intelligence, proactive nudges
2. **Job Search Assistant** (L1/T1) — MCP source polling, profile-based filtering, application tracking
3. **Tourism Chatbot** (L0/T2) — public-facing, restricted tools, multi-agent isolation, RAG

After completing the standard gate review, the reviewer should note (in 1-2 sentences) whether this PR moves any reference deployment closer to production-ready. This is **advisory, not blocking** — infrastructure PRs that don't directly advance a reference deployment are fine. But if a PR *could* easily be extended to close a gap in a reference deployment, the reviewer should note the opportunity.

Example: "This PR adds web search as an MCP source. It advances the Chief of Staff deployment (web monitoring). Advisory: the signal extractor for web search results should include urgency classification to be useful for the observation cycle."

---

## How to Use This in Reviews

The "Vision Alignment" section of every PR review must:

1. **Reference each gate by number** — don't just write a general statement.
2. **Cite specific files and lines** — "Gate 1 satisfied: new tool registered in tools.py line 28, goes through CommandLayer.check() via dispatch() at react.py line 685."
3. **Flag violations clearly** — "Gate 2 VIOLATION: new LLM call at heartbeat/poller.py line 312 hardcodes provider='gemini' instead of using get_model()."
4. **Distinguish blocking from advisory** — Gate violations that break security boundaries (Gates 1, 4, 6, 8) are blocking. Gate violations that affect progress or quality (Gates 2, 3, 7, 9) are advisory notes. Reference deployment notes are always advisory.
5. **Be specific about "not applicable"** — If a gate doesn't apply to this PR, say why in one sentence: "Gate 5: N/A — no new ingestion paths or write tools added."

A review that says "✅ Aligns with vision" without addressing the gates is incomplete.
