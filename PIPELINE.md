# Xibi Autonomous Development Pipeline

> **What this is:** The protocol for how Jules (async coding agent), Cowork (architecture + review), and GitHub Actions (deterministic CI) work together to build Xibi autonomously.
>
> **Who reads this:** Jules reads task specs from `tasks/pending/`. Cowork reads completion reports from `tasks/done/`. GitHub Actions runs `.github/workflows/ci.yml`. Daniel reviews daily summaries and breaks ties.

---

## Roles

### Jules (Think Role — Builder)
- Picks up task specs from `tasks/pending/`
- Implements against the architecture doc (`public/xibi_architecture.md`)
- Writes unit tests for every module it creates or modifies
- Runs tests in its cloud VM before opening a PR
- Opens a PR with: summary of changes, test results, any deviations from the spec
- Does NOT modify files in `public/` (architecture docs are Cowork's domain)
- Does NOT merge its own PRs

### Cowork (Review Role — Architect)
- Generates task specs from the roadmap (`public/xibi_roadmap.md`)
- Reviews PRs against the architecture doc AND the vision alignment gates (`public/review_criteria.md`) — every review must address all 7 gates by number with file/line citations. A review that says "aligns with vision" without gate-by-gate analysis is incomplete.
- Approves, requests changes, or comments with specific guidance
- On merge: updates roadmap status, generates next task spec, drops it in `tasks/pending/`
- Runs daily review: reads all activity from last 24 hours, writes summary to `reviews/daily/`
- Maintains architecture docs, roadmap, backlog, and this pipeline doc

#### Cowork Model Protocol

Cowork switches models based on the cognitive weight of the task. Use `/model` to switch mid-session.

| Task | Model |
|---|---|
| PR code review | **Opus** |
| Architecture review / design decisions | **Opus** |
| Backlog prioritization / triage | **Opus** |
| Writing task specs | Sonnet |
| Updating roadmap / docs | Sonnet |
| Backlog editing (adding, grooming) | Sonnet |
| Daily review summaries | Sonnet |
| Routine file updates | Sonnet |

**Rule:** When in doubt, if it requires judgment → Opus. If it requires writing → Sonnet.

Switch command: `/model claude-opus-4-6` (Opus) · `/model claude-sonnet-4-6` (Sonnet)

### GitHub Actions (Reflex Layer — Deterministic CI)
- Runs on every PR and push to `main`
- Lint (ruff), type check (mypy), unit tests (pytest)
- Tests must pass before PR can be reviewed
- No inference, no judgment — binary pass/fail

### Daniel (Executive — Tiebreaker)
- Reviews daily summaries (designed to be skimmable in 2 minutes)
- Approves PRs that touch core orchestration (Steps 4-5, 8)
- Breaks ties when Jules and Cowork disagree on approach
- Sets strategic direction — architecture changes, new phases, priority shifts
- Can pause the pipeline at any time by adding a `HOLD` file to `tasks/`

---

## Directory Structure

```
xibi/
├── tasks/
│   ├── pending/           # Task specs waiting for Jules
│   │   └── step-01.md     # Next task for Jules to pick up
│   ├── in-progress/       # Jules moves here when starting work
│   ├── done/              # Jules drops completion report here
│   │   └── step-01.md     # Completion summary + PR link
│   └── templates/
│       └── task-spec.md   # Template for task specs
├── reviews/
│   └── daily/             # Cowork's daily review summaries
│       └── 2026-03-24.md
├── .github/
│   └── workflows/
│       └── ci.yml         # Lint + type check + test runner
├── xibi/               # The Python package (Jules writes here)
│   ├── __init__.py
│   ├── router.py          # Step 1
│   ├── ...
├── tests/                 # Test suite (Jules writes here)
│   ├── test_router.py     # Step 1 tests
│   ├── ...
├── public/                # Architecture docs (Cowork writes here)
│   ├── xibi_architecture.md
│   ├── xibi_roadmap.md
│   └── ...
├── config.json            # System config (Step 2)
├── profile.json           # Deployment config (Step 2)
├── GEMINI.md              # Instructions for Jules/Antigravity
├── PIPELINE.md            # This file
└── BACKLOG.md             # Feature backlog
```

---

## Task Spec Format

Every task spec in `tasks/pending/` follows this structure:

```markdown
# Step N: [Title]

## Architecture Reference
- Design doc: `public/xibi_architecture.md` section [X]
- Roadmap: `public/xibi_roadmap.md` Step N

## Objective
[One paragraph: what this step delivers and why it matters]

## Files to Create/Modify
- `xibi/[file].py` — [what it does]
- `tests/test_[file].py` — [what it tests]

## Contract
[Exact function signatures, class interfaces, config schema — the "what" not the "how"]

## Constraints
- [Hard requirements: no hardcoded model names, must use get_model(), etc.]
- [Dependencies: requires Step N-1 to be merged]

## Tests Required
- [Specific test cases that must pass]

## Definition of Done
- [ ] All files created/modified as listed
- [ ] All tests pass locally
- [ ] No hardcoded model names anywhere in new code
- [ ] PR opened with summary + test results + any deviations noted
```

---

## The Loop

### Happy Path (fully autonomous)

```
1. Cowork generates task spec → tasks/pending/step-N.md
       ↓
2. Jules picks up spec → moves to tasks/in-progress/
       ↓
3. Jules implements → writes tests → runs tests in cloud VM
       ↓
4. Jules opens PR → drops completion report in tasks/done/step-N.md
       ↓
5. GitHub Actions runs CI (lint, type check, tests)
       ↓
6. Cowork reviews PR against architecture doc
       → Approve → merge → update roadmap → generate step N+1 spec → back to 1
       → Request changes → Jules picks up review comments → amends PR → back to 5
       ↓
7. Daily review: Cowork summarizes all activity → reviews/daily/YYYY-MM-DD.md
```

### Failure Modes

**Jules tests fail in cloud VM:**
Jules does NOT open a PR. Instead, drops a bug report in `tasks/done/step-N.md` with:
- Which tests failed and why
- What it tried to fix
- Where it got stuck
Cowork triages: is this a spec problem (Cowork fixes spec) or an implementation problem (Cowork adds guidance, Jules retries)?

**CI fails on PR:**
Jules reads the CI failure output and pushes a fix commit. If it fails 3 times on the same issue, it drops a bug report and Cowork triages.

**Cowork requests changes on PR:**
Jules reads the review comments, amends the PR, pushes. Normal code review loop.

**Architecture conflict detected:**
Cowork flags the conflict in review comments, marks the PR as `blocked`, and writes a resolution note. Daniel breaks the tie if needed.

**Pipeline halt:**
Daniel drops a `HOLD` file in `tasks/`. Cowork stops generating new specs. Jules finishes its current PR but doesn't pick up new work. Remove `HOLD` to resume.

---

## Daily Review Protocol (Cowork Scheduled Task)

Runs once per day. Output goes to `reviews/daily/YYYY-MM-DD.md`.

**What it checks:**
1. PRs merged in last 24 hours — diff summary, architecture alignment
2. PRs open — status, blockers, age
3. CI failures — pattern analysis (same test failing repeatedly?)
4. Task pipeline health — any specs stuck in pending > 24 hours?
5. Roadmap progress — which step are we on, projected completion
6. Architecture drift — any merged code that doesn't match the design doc?
7. Vision gate audit — for each merged PR, confirm all 7 gates from `public/review_criteria.md` were addressed in the PR review. Flag any PR that was merged with an incomplete vision alignment section.

**Output format:**
```markdown
# Daily Review — YYYY-MM-DD

## Summary
[2-3 sentences: what happened today, overall health]

## PRs Merged
- #N: [title] — [1-line assessment: clean / minor deviation / needs follow-up]

## Open Items
- [anything that needs Daniel's attention]

## Pipeline Health
- Current step: N
- Tasks pending: X | in-progress: X | done today: X
- CI status: passing / failing (details)

## Architecture Notes
- [any drift, concerns, or design decisions that surfaced]
```

---

## Environment Protocol

Xibi uses a `profile.json` `environment` field to prevent dev/test work from touching real services.

### Environment Tiers

| Tier | `environment` | Channels | Sends | LLM Calls | When |
|---|---|---|---|---|---|
| **unit** | n/a | Mocked in conftest.py | Never | Never | `pytest tests/` — CI and local |
| **integration** | `"test"` | Mocked providers, synthetic fixtures | Dry-run only (logged, not sent) | Mocked | `pytest -m integration` |
| **live-local** | `"dev"` | Real Ollama, mock channels | Dry-run only | Real local, mock cloud | Manual: `XIBI_ENV=dev pytest -m live` |
| **live-cloud** | `"dev"` | Real providers, mock channels | Dry-run only | Real local + cloud | Manual: rare, cost-aware |
| **production** | `"production"` | Real everything | Real sends | Real everything | NucBox deployment only |

### Safety Rules

1. **`dry_run_sends: true` is the default.** Every channel adapter checks `profile.dev_overrides.dry_run_sends` before any outbound action (send email, post message, etc.). When true, the adapter logs what it *would* send (recipient, subject, body hash) but does not execute. This is the "no generic Jake emails" rule.
2. **`mock_channels: true` disables all inbound channel polling.** No real emails fetched, no real Telegram messages read. Tests use synthetic fixtures from `tests/fixtures/`.
3. **`test_recipient`** in dev overrides: if a send does execute in dev (e.g., testing the send path), it routes to `dev@example.com` — never to a real address.
4. **Cloud API calls in tests use mocks.** The only exception is `pytest -m live` which is NEVER run in CI — only manually by Daniel to sanity-check a real provider path.
5. **CI runs unit + integration only.** No network calls, no real providers, no cost.

### Config Detection

```python
# In any channel adapter or outbound tool:
def _should_send(profile: dict) -> bool:
    env = profile.get("environment", "dev")
    dry_run = profile.get("dev_overrides", {}).get("dry_run_sends", True)
    if env == "production" and not dry_run:
        return True
    return False
```

Jules must implement this pattern in every outbound tool starting from Step 3.

### Test Fixtures

```
tests/
├── fixtures/
│   ├── emails/           # Synthetic email JSON (no real addresses)
│   │   ├── simple.json
│   │   ├── with_cc.json
│   │   └── phishing_attempt.json
│   ├── configs/           # Test config variants
│   │   ├── valid.json
│   │   ├── circular_fallback.json
│   │   └── missing_provider.json
│   └── signals/           # Synthetic signals for observation cycle tests
├── conftest.py            # Shared fixtures, mock providers
```

All test data uses `@example.com` addresses, synthetic names, and placeholder content. Zero PII. See `SECURITY.md`.

---

## Self-Testing Protocol

Jules writes tests as part of every task. But the testing strategy has layers:

### Layer 1: Unit Tests (Jules writes, CI runs)
- Every function in `xibi/` has a corresponding test in `tests/`
- Jules writes tests as part of the task spec — not as an afterthought
- CI runs the full suite on every PR

### Layer 2: Contract Tests (Jules writes, CI runs)
- Tool schemas: valid params execute, invalid params return error
- `get_model()` contract: fallback resolution, provider failure handling
- Config validation: missing fields, invalid combinations, type errors

### Layer 3: Integration Tests (Jules writes, triggered on merge to main)
- Full path tests: chat → fast extraction → think reasoning → tool call → response
- Heartbeat tick: email ingestion → signal extraction → thread matching
- Observation cycle: signal dump → review role → tool calls → watermark advance

### Layer 4: Smoke Tests (Jules writes, manual trigger)
- `XIBI_MOCK_ROUTER=1` mode: mock all LLM calls, test the full pipeline end-to-end
- Dev mode: run Xibi against synthetic email fixtures, verify correct classification/nudging
- Used for validating complete steps before marking them done

**CLI chat (available from Step 4 onward):**

The CLI channel adapter (`xibi/channels/cli.py`) is the primary smoke test tool — stdin/stdout, no Telegram bot required. Jules uses it for end-to-end smoke tests. Cowork uses it to verify behavior during PR reviews on Steps 4+.

```bash
# Interactive — chat with Xibi directly in the terminal
python -m xibi chat

# Scripted — automated conversation with assertions, exits non-zero on failure
XIBI_MOCK_ROUTER=1 python -m xibi chat --script tests/scripts/basic_email_triage.json

# Live local — real Ollama, mocked channels, no real sends
XIBI_ENV=dev python -m xibi chat
```

Scripted conversation scripts live in `tests/scripts/`. Format:
```json
{
  "description": "Basic email triage smoke test",
  "env": { "XIBI_MOCK_ROUTER": "1", "XIBI_ENV": "test" },
  "turns": [
    { "user": "what emails do I have?", "assert_contains": ["email"] },
    { "user": "summarize the first one", "assert_tool_called": "read_email" },
    { "user": "reply and say I'll follow up next week",
      "assert_tool_called": "reply_email",
      "assert_dry_run": true }
  ]
}
```

`assert_dry_run: true` verifies the send path was reached but `dry_run_sends` intercepted it — no emails sent. Scripts with `XIBI_MOCK_ROUTER=1` are runnable in CI as extended smoke tests.

**Carry-forward:** `inject_scripted_steps()` and `BREGGER_MOCK_ROUTER` exist in legacy `bregger_core.py`. Step 4 formalizes and replaces this pattern with the CLI runner.

### Layer 5: Architecture Compliance (Cowork reviews)
- No hardcoded model names in any file under `xibi/`
- All tool calls go through the schema validation gate
- No role-to-role direct communication (everything goes through Python)
- Config changes don't require code changes
- **Precomputation invariant:** Prompts contain pre-resolved values — no raw relative time expressions, no "figure out what date last Tuesday was." All temporal resolution, thread context, and topic injection happens in `xibi/utils.py` before the prompt is assembled.

---

## Bug Lifecycle

```
Bug detected (by tests, CI, Cowork review, or Radiant once live)
    ↓
Cowork triages:
    → Spec problem? → Fix spec, re-queue task
    → Implementation bug? → Write bug-fix task spec → tasks/pending/bugfix-NNN.md
    → Architecture issue? → Update architecture doc first, then generate fix specs
    ↓
Jules picks up bugfix spec → same PR flow as feature work
    ↓
Cowork verifies fix in PR review → merge
```

Bug-fix task specs follow the same template but include:
- **Reproduction:** exact test case or steps that demonstrate the bug
- **Root cause analysis:** Cowork's assessment of why it happened
- **Fix scope:** which files should change and which should NOT

---

## Bootstrapping

To get the pipeline running from zero:

1. **Daniel:** Create private `xibi` repo on GitHub. Push current Project_Ray contents.
2. **Cowork:** Create `tasks/`, `reviews/`, `tests/` directories. Drop Step 1 spec in `tasks/pending/`.
3. **Cowork:** Create `.github/workflows/ci.yml` — lint + test runner.
4. **Cowork:** Set up daily review scheduled task.
5. **Daniel:** Connect Jules to the `xibi` repo. Point it at `tasks/pending/step-01.md`.
6. **Pipeline is live.** Jules builds Step 1. Cowork reviews. Loop begins.

---

## Escape Hatches

- **`HOLD` file:** Drop in `tasks/` to pause the pipeline. Remove to resume.
- **Manual task specs:** Daniel or Cowork can write one-off specs for hotfixes or experiments.
- **Direct Antigravity work:** For debugging on NucBox, Daniel can use Antigravity directly. Changes get committed and Cowork reconciles in next daily review.
- **Architecture override:** If Daniel makes a design decision that contradicts the architecture doc, Cowork updates the doc first, then reconciles any in-flight specs.
