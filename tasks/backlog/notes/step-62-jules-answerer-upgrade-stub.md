# step-62 — JulesWatcher Becomes Cowork's Voice (Stub)

> **Status:** Stub / direction-capture. Not ready to implement.
> **Depends on:** step-60 (so the runtime fallback chain exists when this
> upgrades JulesWatcher to use a more expensive provider).
> **Blocks:** Nothing yet — this is a quality lift, not a foundational
> primitive.
> **Scope (when fleshed out):** Replace JulesWatcher's thin local-LLM
> answer path with a full-context Claude-grade answerer. Same code
> location, same always-on property, completely different substance.

---

## Why This Step Exists (Concept Capture)

Tonight (2026-04-07) we traced through what JulesWatcher actually does
when Jules asks a clarifying question mid-implementation, and found three
compounding limitations on answer quality:

1. **Model tier.** JulesWatcher uses `get_model(effort="fast")`, which
   resolves to `qwen3.5:4b` running locally on the NucBox. Jules's
   questions are typically `think`-tier or harder (architecture flavor,
   layering decisions, "should I do A or B and why").
2. **Spec truncation.** `jules_watcher.py:198` truncates the task spec
   to `spec[:3000]` characters. The most decision-relevant parts of a
   spec — test plan, acceptance criteria, out-of-scope, implementer
   notes — live near the end and get cut off. The very lines where I
   write "do not add a Router class" and "do not extend transform_data
   beyond these six operations" are not in the prompt the answerer sees.
3. **Zero project context.** No CLAUDE.md, no SECURITY.md, no PIPELINE.md,
   no awareness of Xibi's trust model, no awareness of which prior specs
   the current one depends on, no codebase conventions. The model has to
   infer everything about Xibi from a 3000-char excerpt of one spec.

The result is that JulesWatcher today is essentially answering Jules's
questions like a contractor who's been handed a single page of a spec
and asked to make architecture calls without ever being told what the
company does or what the rest of the codebase looks like. It works often
enough because Jules tends to ask narrowly-scoped questions, and the
`ESCALATE` escape hatch catches the worst cases — but the quality
ceiling is exactly where you'd expect given that context.

Daniel's directive (verbatim, 2026-04-07): *"i think you shoulkd touch
the answer loop you already have the spec, the knowledg. no contractors."*

The intent is that the entity which authored a spec should be the entity
defending its interpretation when the implementing agent asks questions.
Today that entity is Cowork (Opus, with full file access to the
xibi-work clone, the auto-memory, and the pushback directive). The
answers Jules gets should come from Cowork-grade context, not from a
thin local-model contractor.

---

## Architectural Shape (Captured From Tonight's Discussion)

There are two readings of "Cowork answers Jules's questions" and the
direction this step takes is the second one.

**Reading A — relocate the answer loop into Cowork.** A scheduled Cowork
task polls Jules directly, generates answers, posts back. Pure but adds
an always-on gap (scheduled tasks fire on an interval, not on Jules
events) and a secrets-handoff problem (the Jules API key would need to
move). **Rejected** for v1 because the latency cost is real — a 6-hour
scheduled cadence means Jules sits stuck for up to 6 hours per question.

**Reading B — keep the location, change the substance.** JulesWatcher
stays as the always-on poller running inside the heartbeat tick. What
changes is *what it sends to the model* and *which model*. From Jules's
perspective, the answer is now coming from Cowork in every sense that
matters — same model class, same context depth, same spec-author voice
— even though the code is running on the NucBox. **Adopted.** This is
the cheapest, most reversible, highest-value version.

### Concrete Changes in `xibi/heartbeat/jules_watcher.py`

1. **Model role.** Replace `get_model(effort="fast")` with the role that
   resolves to Anthropic Claude (today: `review`). When the step-60
   fallback chain ships, this role can walk to other providers cleanly
   on failure without losing the answer.
2. **Drop the spec truncation.** Replace `spec[:3000]` with the full
   spec body. Specs are well under any reasonable model's context
   window; truncation buys nothing and costs the most important parts.
3. **Load standing project context once at startup.** New module
   loads CLAUDE.md, SECURITY.md, PIPELINE.md, and a hand-curated
   `CONTEXT_FOR_JULES.md` (new file in xibi-work, ~2-3K chars,
   summarizes Xibi's trust model, autonomy goals, architectural
   commitments, naming conventions, and pushback rules — basically
   the things Cowork knows and JulesWatcher doesn't).
4. **Load adjacent specs on demand.** When the question is on step-N,
   also load step-(N-1) and step-(N+1) if they exist in backlog or
   done — they're often referenced by the dependency line and Jules's
   question may be about the seam.
5. **Rewrite the answer prompt.** The current prompt is generic. The
   new prompt is written *as if Cowork were answering it directly* —
   same voice, same conventions about pushing back on scope creep,
   same default to "if the spec says do X, the answer is X even if Y
   would be slightly cleaner," same explicit reference to the
   implementer notes section as the source of truth on what NOT to do.
6. **Optional: include the file Jules is currently editing.** If the
   Jules API exposes the active file in the session state (TBD —
   needs API exploration), load that file from the xibi-work clone
   and include it in the prompt. This is the highest-leverage context
   addition because most of Jules's questions are file-local.

### What Stays The Same

- The 15-min heartbeat polling cadence. JulesWatcher remains always-on.
- The Jules API integration code (auth, session listing, activity
  fetching, message posting). All of that is fine as-is.
- The state file (`responded_activities.json`) and the Telegram
  broadcast on every answered question. Both are working well.
- The `ESCALATE` escape hatch. Still needed for the genuinely-unclear
  cases. Probably fires *less often* once context is rich.

---

## Open Questions (Resolve Before Implementing)

- **Should the project-context preamble be cached in memory between
  ticks, or reloaded on every tick?** Reload is simpler and rarely
  expensive (~few KB of disk reads). Cache buys nothing unless the
  heartbeat is hot. Lean reload, revisit if it shows up in profiling.
- **Does the Jules API expose the currently-edited file path?** If yes,
  loading it is the highest-leverage context addition. If no, this
  point gets dropped from v1.
- **What does `CONTEXT_FOR_JULES.md` actually contain?** This is the
  load-bearing artifact. It is *not* CLAUDE.md (which is for human
  contributors). It is a deliberately curated summary of "things Cowork
  would tell Jules if Cowork were briefing Jules personally." Should
  probably be drafted by hand by Daniel + Cowork in a dedicated session,
  not auto-generated.
- **How do we measure whether this actually improved answer quality?**
  Today there's no rubric. Probably the simplest signal is "rate of
  Jules questions that escalate to Daniel via the Telegram broadcast"
  — this should drop after the upgrade. Worth instrumenting before and
  after to validate.
- **Cost.** Each Jules question now costs an Anthropic Claude call
  instead of a free local Ollama call. At Jules's typical question
  cadence (a few per session, a few sessions per day) this is well under
  $1/day, negligible. But worth confirming with one week of real data.

---

## Out of Scope (Future)

- **Cowork-as-direct-poller (Reading A above).** Rejected for v1 on
  latency grounds. Could become a v2 if the always-on gap turns out
  to be solvable cheaply.
- **Auto-correction loop.** A separate scheduled Cowork session could
  read the answers JulesWatcher gave over the past day and flag any
  that look wrong against the full spec. This is the "watch the
  results" pattern Daniel proposed earlier in the session — it pairs
  naturally with this step but is its own work.
- **`CONTEXT_FOR_JULES.md` authoring.** The file itself is its own
  artifact and should be drafted in a dedicated session, not buried
  inside the implementation of this step.
- **Removing the local-model fallback entirely.** Worth keeping the
  qwen path as a degraded fallback for when Anthropic is unreachable
  — `ESCALATE` is a worse user experience than "you got a weaker
  answer because the cloud was down." This step *upgrades* the primary
  path; it does not remove the fallback.

---

## Why This Is a Stub, Not a Real Spec

Captured tonight (2026-04-07) at the end of a session that already
shipped three deliverables (step-60 cleanup, step-61 result handles,
step-59 freshness pass). Per the pushback directive, four-deliverables
on a single evening is scope creep — better to capture the direction
and pick it up clean next session. The kernel design (Reading B above)
is settled enough that the next session can go straight to a real spec
with concrete LOC counts and a test plan, rather than re-deriving the
direction.

When this gets promoted to a real spec:

1. The architectural decision (Reading B) is settled and should not be
   relitigated.
2. The concrete file changes in `jules_watcher.py` are listed above
   and need test cases written.
3. `CONTEXT_FOR_JULES.md` needs to be drafted by hand as a precondition.
4. The "currently-edited file" open question needs Jules API exploration.
5. A before/after measurement plan needs to be defined.
