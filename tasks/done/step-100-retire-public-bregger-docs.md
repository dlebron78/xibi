# step-100: Retire public/bregger_*.md — final migration cleanup

## Architecture Reference
- Closes the bregger→xibi migration at the documentation layer. Step-99
  (merged 2026-04-22) completed the code migration; this spec completes
  the public-facing architecture docs.
- `public/xibi_roadmap.md:695` explicitly tracks this as an open Phase 4
  TODO: `- [ ] Clean or archive old public/bregger_*.md docs (superseded
  by xibi_*.md)`. This spec completes that checkbox.
- After step-100 lands, `ls public/bregger_*.md` returns empty. The only
  remaining bregger references in the repo are (a) historical records
  under `CHANGELOG.md`, `ARCHITECTURE_REVIEW.md`, `reviews/`, and
  `tasks/done/`, and (b) internal bregger references inside `xibi_*.md`
  (58 total — out of scope for this spec; separate future cleanup).

## Objective

Retire the six `public/bregger_*.md` files via per-file disposition:

- **DELETE (4 files, ~1,030 lines)** — content superseded or concepts
  dropped: `bregger_architecture.md`, `bregger_reflection_loop.md`,
  `bregger_tier_escalation.md`, `bregger_urt.md`.
- **RENAME (1 file, 170 lines, no content change)** — `bregger_vision.md`
  → `xibi_vision.md`. Body already titled "Xibi — The Vision"; this is a
  pure filename correction.
- **RENAME + CONTENT REFRESH (1 file, 308 lines)** — `bregger_task_layer.md`
  → `xibi_task_layer.md`. Schema section matches live code
  (`xibi/db/migrations.py:210`); prose needs "Bregger" → "Xibi"
  substitution and a few architectural-term corrections.
- **FIXUP (3 small edits in 2 unrelated files)** — broken inbound links
  that point at soon-to-be-renamed/deleted targets.

Net change: ~1,030 lines deleted, 2 files renamed, 3 small inbound-link
fixups. No code changes. No schema changes. No prompts, no LLM surface.

## User Journey

Operator-facing, zero user surface.

1. **Trigger:** merge to `main` → NucBox auto-deploy pulls (no service
   restart is actually necessary since nothing in `public/` is imported
   by runtime; deploy is a no-op for runtime).
2. **Interaction:** none — `public/` is documentation, not code.
3. **Outcome:** `ls public/bregger_*.md` returns ENOENT. `ls
   public/xibi_vision.md public/xibi_task_layer.md` both succeed. Inbound
   links in `public/review_criteria.md` resolve to the renamed files.
   `public/xibi_roadmap.md:695` shows a checked box.
4. **Verification:** see Post-Deploy Verification — three file-existence
   checks and one grep over SSH.

## Real-World Test Scenarios

### Scenario 1: Happy path — renames and deletions land atomically

**What you do:** On the feature branch locally, after implementation:
```
ls public/bregger_*.md 2>&1
ls public/xibi_vision.md public/xibi_task_layer.md
git log --name-status -1 | head -30
```

**What Roberto does:** ripgrep/ls walk the working tree; git shows the
commit's file list.

**What you see:**
```
ls: cannot access 'public/bregger_*.md': No such file or directory
public/xibi_task_layer.md
public/xibi_vision.md
commit <sha>
  D  public/bregger_architecture.md
  D  public/bregger_reflection_loop.md
  D  public/bregger_tier_escalation.md
  D  public/bregger_urt.md
  R  public/bregger_vision.md -> public/xibi_vision.md
  R  public/bregger_task_layer.md -> public/xibi_task_layer.md
  M  public/xibi_task_layer.md   (content-refresh diff on the rename)
  M  public/review_criteria.md
  M  public/xibi_roadmap.md
  M  public/xibi_signal_intelligence.md
```

**How you know it worked:** zero `bregger_*.md` entries present; two `R`
(rename) lines for vision + task_layer; four `D` (delete) lines for the
retired four; three `M` (modify) entries for the inbound-link fixups.

### Scenario 2: Guardrail — no more bregger-named docs in public/

**What you do:**
```
ls public/bregger_*.md 2>&1
find public/ -name 'bregger_*'
```

**What Roberto does:** ls and find both walk `public/`.

**What you see:**
```
ls: cannot access 'public/bregger_*.md': No such file or directory
# (empty output from find)
```

**How you know it worked:** exit code 2 from ls, zero lines from find.

### Scenario 3: Inbound-link audit — review_criteria.md references resolve

**What you do:**
```
grep -n "bregger_.*\.md" public/*.md
```

**What Roberto does:** grep walks every public/ .md file.

**What you see:**
```
# (empty output)
```
OR only non-top-level references if any legitimately-historical pointer
survives (none are expected after this step).

**How you know it worked:** zero hits within `public/*.md` — all
references either repointed to `xibi_*.md` or removed where the target
was already broken.

### Scenario 4: Task layer rename preserves live schema accuracy

**What you do:**
```
diff <(sed -n '/^### Schema/,/^## Relationship/p' public/xibi_task_layer.md) \
     <(sed -n '/^### Schema/,/^## Relationship/p' public/bregger_task_layer.md)
```
— except this is a post-rename check, so compare the renamed file against
the historical content via git:
```
git show HEAD~1:public/bregger_task_layer.md > /tmp/old_task_layer.md
diff <(sed -n '/CREATE TABLE tasks/,/^```$/p' /tmp/old_task_layer.md) \
     <(sed -n '/CREATE TABLE tasks/,/^```$/p' public/xibi_task_layer.md)
```

**What Roberto does:** `diff` compares the schema block (which must remain
verbatim — it matches live migration code).

**What you see:** `(empty — no diff)` or only whitespace changes. The
schema block must be byte-identical before/after rename.

**How you know it worked:** zero content-diff inside the schema fence.
Prose edits outside that fence are expected; schema edits are not.

### Scenario 5: Roadmap checkbox toggled

**What you do:**
```
grep -n "bregger_\*.md docs" public/xibi_roadmap.md
```

**What Roberto does:** grep finds the Phase 4 checklist line.

**What you see:**
```
695:- [x] Clean or archive old `public/bregger_*.md` docs (superseded by `xibi_*.md`)
```

**How you know it worked:** checkbox is `[x]`, not `[ ]`.

## Files to Create/Modify

**DELETE (4 files):**
- `public/bregger_architecture.md` (230 lines) — "local-first AI
  personal operator" framing is stale per `feedback_vision_framing.md`;
  UniversalRecord details describe a concept not present in xibi code
  (grep confirmed); superseded by `public/xibi_architecture.md`.
- `public/bregger_reflection_loop.md` (222 lines) — concept evolved into
  the Observation Cycle documented in `public/xibi_architecture.md`
  (sections at lines 298, 327, 358 of that file).
- `public/bregger_tier_escalation.md` (508 lines) — `min_tier` /
  tier-escalation concept dropped; replaced by the role/specialty
  architecture in `public/xibi_architecture.md` ("Roles, Not Models",
  "Effort Levels", "Specialty Model Dispatch" sections). Grep of xibi
  code confirms no `min_tier` usage.
- `public/bregger_urt.md` (69 lines) — URT/UniversalRecord concept
  dropped; no `UniversalRecord` or `universal_record` references in xibi
  code.

**RENAME (pure, no content change):**
- `public/bregger_vision.md` → `public/xibi_vision.md` (170 lines).
  Document body already starts with "# Xibi — The Vision"; content
  reflects current Xibi positioning (L1-L2/T2, security + memory bets,
  reference deployments). Use `git mv` so history follows.

**RENAME + CONTENT REFRESH:**
- `public/bregger_task_layer.md` → `public/xibi_task_layer.md` (308
  lines). The `CREATE TABLE tasks (...)` block at roughly line 25 is
  byte-identical to `xibi/db/migrations.py:210` — keep it verbatim. The
  prose edits below are the full content-refresh scope:
  - Replace `Bregger` / `bregger` → `Xibi` / `xibi` globally in this
    file (case-preserving).
  - Leave the ReAct-loop / Memory / Heartbeat concepts as-is — they are
    current xibi concepts.
  - No other structural changes. Do NOT add new sections, do NOT
    reorganize, do NOT tighten the prose. Minimum-touch refresh.

**FIXUP inbound links (3 edits across 2 files):**
- `public/review_criteria.md:98` — change `bregger_vision.md` →
  `xibi_vision.md`.
- `public/review_criteria.md:147` — change `bregger_vision.md` →
  `xibi_vision.md`.
- `public/xibi_signal_intelligence.md:3` — currently references
  `bregger_roadmap_v2.md`, which does not exist in the repo (pre-existing
  broken link). Change to `public/xibi_roadmap.md`.

**CHECKBOX TOGGLE (1 edit):**
- `public/xibi_roadmap.md:695` — change `- [ ]` to `- [x]` on the
  line that reads `Clean or archive old public/bregger_*.md docs
  (superseded by xibi_*.md)`.

No other files in `xibi/`, `tests/`, `skills/`, `scripts/`, `systemd/`,
or root-level `.md` files need to change.

## Database Migration

N/A — documentation-only step. No schema, no code, no data.

## Contract

N/A — no new function signatures, no new classes, no new config keys.

Inverse contract (the *removed* docs) for audit purposes:

| Removed doc | Disposition | Content fate |
|---|---|---|
| `bregger_architecture.md` | DELETE | Superseded by `xibi_architecture.md` (97KB, comprehensive) |
| `bregger_reflection_loop.md` | DELETE | Concept evolved into Observation Cycle, in `xibi_architecture.md` |
| `bregger_tier_escalation.md` | DELETE | Tier escalation concept dropped; role/specialty architecture replaces it in `xibi_architecture.md` |
| `bregger_urt.md` | DELETE | URT concept dropped; no xibi code uses UniversalRecord |
| `bregger_vision.md` | RENAME | Becomes `xibi_vision.md`; content unchanged, already current |
| `bregger_task_layer.md` | RENAME + REFRESH | Becomes `xibi_task_layer.md`; schema verbatim, prose Bregger→Xibi |

## Observability

1. **Trace integration:** N/A — documentation change, not runtime.
2. **Log coverage:** N/A — no log lines added, removed, or changed.
3. **Dashboard/query surface:** unchanged.
4. **Failure visibility:** N/A — docs don't fail. The only post-merge
   "failure" mode is an external reader hitting a broken
   `public/bregger_*.md` URL via a commit-pinned GitHub link. That's an
   expected consequence of deletion; not a bug.

## Post-Deploy Verification

### Schema / migration (DB state)

N/A — zero schema or data changes. `SCHEMA_VERSION` unchanged.

### Runtime state (services, endpoints, agent behavior)

- No service restarts required by this change. Still sanity-check
  nothing got unexpectedly restarted:
  ```
  ssh dlebron@100.125.95.42 "for svc in \$(grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh); do systemctl --user show \"\$svc\" -p NRestarts --value; done"
  ```
  Expected: all values `0` or unchanged from pre-merge baseline (i.e.
  nothing flapped because of a doc-only merge).

- Files actually gone from deployed checkout:
  ```
  ssh dlebron@100.125.95.42 "ls ~/xibi/public/bregger_*.md 2>&1"
  ```
  Expected: `ls: cannot access '/home/dlebron/xibi/public/bregger_*.md': No such file or directory`

- Renamed files present in deployed checkout:
  ```
  ssh dlebron@100.125.95.42 "ls ~/xibi/public/xibi_vision.md ~/xibi/public/xibi_task_layer.md"
  ```
  Expected: both paths listed with no error.

- Inbound-link audit is clean on NucBox:
  ```
  ssh dlebron@100.125.95.42 "grep -n 'bregger_.*\\.md' ~/xibi/public/*.md | grep -v CHANGELOG"
  ```
  Expected: empty output.

- Roadmap checkbox was flipped on NucBox:
  ```
  ssh dlebron@100.125.95.42 "grep -n 'bregger_\\*.md docs' ~/xibi/public/xibi_roadmap.md"
  ```
  Expected: `695:- [x] Clean or archive old \`public/bregger_*.md\` docs (superseded by \`xibi_*.md\`)`

### Observability — the feature actually emits what the spec promised

N/A — no spans, no log lines added.

### Failure-path exercise

N/A in the runtime sense. The closest analog is someone clicking a
GitHub-pinned link to `public/bregger_architecture.md` after merge:
they get GitHub's "This file was deleted" page. That's expected
behavior from a deletion and not something to exercise pre-deploy.

### Rollback

- **If any check above fails**, revert with:
  ```
  cd ~/Documents/Xibi
  git revert <step-100-merge-sha> --no-edit
  git push origin main
  ```
  NucBox auto-deploy pulls the revert. All six bregger_*.md files are
  restored; review_criteria.md and xibi_roadmap.md edits revert.
- **Escalation:** telegram `[DEPLOY VERIFY FAIL] step-100 — <1-line
  what failed>` (unlikely to fire; this is a doc-only step).

## Constraints

- **Do NOT touch `CHANGELOG.md`, `ARCHITECTURE_REVIEW.md`, `reviews/`,
  `tasks/done/`.** These contain historical entries mentioning
  `bregger_*.md` paths in their roles as records of what happened.
  Preserving them is correct. Step-99's constraint block established
  this pattern; this spec honors it.
- **Do NOT modify internal bregger references inside `xibi_*.md`.**
  There are 65 of them across `xibi_architecture.md` (22),
  `xibi_multistep_loop.md` (8), `xibi_roadmap.md` (32),
  `xibi_signal_intelligence.md` (3). Many of these are contextually
  correct (e.g., describing what legacy code was called during
  migration). A grep-and-replace pass is a separate future step and
  requires per-hit judgment that isn't in scope here.
- **Do NOT touch `scripts/xibi_cutover.sh`, `xibi_rollback.sh`,
  `xibi_config_migrate.{py,sh}`** — step-99 already established these
  are one-time cutover tooling, out of scope.
- **Content refresh on `xibi_task_layer.md` is minimum-touch.** Replace
  "Bregger" → "Xibi" globally. Do NOT rewrite sections, do NOT add new
  prose, do NOT reorganize. If a section needs substantive revision,
  stop and escalate per rule #8 — that's a different spec.
- **Schema block in `xibi_task_layer.md` is BYTE-IDENTICAL** to the
  removed `bregger_task_layer.md`'s schema block. Verify via `diff` on
  that fenced region post-rename. The schema is load-bearing
  documentation of `xibi/db/migrations.py:210`.
- **Depends on:** step-99 (merged 2026-04-22). Step-99's constraint
  block explicitly deferred public/bregger_*.md cleanup to this step.

## Tests Required

- `pytest tests/` green (no code changes; this is a sanity check that
  the doc-only PR doesn't accidentally touch test files).
- No new tests needed. Tests don't reference `public/*.md` files.

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages — N/A, no code.
- [ ] No coded intelligence — N/A, no prompts, no logic.
- [ ] No LLM content injected into scratchpad — N/A.
- [ ] Input validation — N/A, no new inputs.
- [ ] All acceptance criteria traceable through the codebase.
- [ ] Real-world test scenarios walkable end-to-end.
- [ ] Post-Deploy Verification section present; every subsection filled
      with a concrete runnable command (or explicit `N/A — <reason>`).
- [ ] Every Post-Deploy Verification check names its exact expected
      output.
- [ ] Failure-path exercise present (N/A justified — doc-only).
- [ ] Rollback is a concrete command; escalation telegram shape filled.

**Step-specific gates:**
- [ ] Reviewer ran `grep -rn "bregger_.*\.md" --include="*.md" public/`
      and confirmed the only post-step-100 hits would be historical
      (CHANGELOG-style) entries, which are explicitly out of scope.
- [ ] Reviewer verified the 4 deletion targets describe concepts not
      present in current xibi code:
      - URT/UniversalRecord: `grep -rn "UniversalRecord" xibi/ skills/`
        returns empty.
      - tier escalation: `grep -rn "min_tier" xibi/ skills/` returns
        empty.
      - reflection loop: superseded by Observation Cycle (verify the
        concept is documented in `xibi_architecture.md`).
      - bregger architecture doc: superseded by xibi_architecture.md
        (97KB, comprehensive).
- [ ] Reviewer verified `bregger_task_layer.md`'s schema block matches
      `xibi/db/migrations.py:210` (the 15-column `CREATE TABLE tasks`
      definition). Columns, types, and defaults must match; whitespace
      and inline comment annotations are expected differences between
      the doc and the Python source. If they diverge substantively,
      this is a prior-drift bug,
      not a step-100 problem — flag it but don't block the spec.
- [ ] Reviewer verified `bregger_vision.md` body already says "Xibi",
      not "Bregger" (i.e., the rename really is pure).
- [ ] Reviewer confirmed `CHANGELOG.md`, `ARCHITECTURE_REVIEW.md`,
      `reviews/`, `tasks/done/` are NOT modified — historical surface
      preserved.
- [ ] Reviewer verified that the inbound-link edits (review_criteria.md
      x2, xibi_signal_intelligence.md x1) are the ONLY inbound links to
      bregger_*.md from within `public/*.md` (not counting the docs
      being renamed/deleted themselves).

## Definition of Done

- [ ] 4 files deleted: `bregger_architecture.md`,
      `bregger_reflection_loop.md`, `bregger_tier_escalation.md`,
      `bregger_urt.md`.
- [ ] 2 files renamed via `git mv`:
      `bregger_vision.md` → `xibi_vision.md`,
      `bregger_task_layer.md` → `xibi_task_layer.md`.
- [ ] `xibi_task_layer.md` content-refreshed (Bregger → Xibi), schema
      block byte-identical to pre-rename.
- [ ] `public/review_criteria.md:98` and `:147` point to
      `xibi_vision.md`.
- [ ] `public/xibi_signal_intelligence.md:3` points to
      `public/xibi_roadmap.md`.
- [ ] `public/xibi_roadmap.md:695` checkbox toggled to `[x]`.
- [ ] `ls public/bregger_*.md` returns ENOENT.
- [ ] `grep -rn "bregger_.*\.md" --include="*.md" public/` returns
      empty (within `public/*.md`; CHANGELOG and reviews/ are
      out-of-scope and may still match).
- [ ] `pytest tests/` green.
- [ ] Deployed to NucBox; Post-Deploy Verification all-pass.
- [ ] PR opened with summary + CI test results.

---
> **Spec gating:** Standard flow. Cowork TRRs; `xs-promote step-100`
> when ready. Not Fast-TRR eligible (touches ~1,030 lines of deletions
> plus a content-refresh that requires prose judgment, exceeding the
> ~30-line ceiling). See `WORKFLOW.md`.

---

## TRR Record — Opus, 2026-04-22
**Verdict:** READY WITH CONDITIONS

**Summary:** Doc-only cleanup closing the bregger→xibi migration at the
public/ documentation layer. Spec is well-structured — per-file disposition
table, inverse contract, line-accurate inbound-link inventory, concrete
SSH-based Post-Deploy Verification with expected outputs, and a real
rollback. All quantitative claims hold up (1,029 lines across the four
DELETE targets vs "~1,030"; bregger_vision.md body already titled "Xibi —
The Vision" with zero "Bregger" mentions, confirming pure rename;
UniversalRecord / min_tier / URT grep empty in `xibi/` and `skills/`; the
three inbound-link line anchors at review_criteria.md:98, :147, and
xibi_signal_intelligence.md:3 all match verbatim; bregger_roadmap_v2.md
confirmed absent; scripts/xibi_{cutover,rollback,config_migrate}.* exist
and are correctly scoped out). The schema block in bregger_task_layer.md
has the same 15 columns, types, defaults, and order as
`xibi/db/migrations.py:210` — load-bearing claim confirmed.

Five pillars: **Contract** N/A (no code); **Real-World Tests** five
scenarios, all runnable from a dev checkout, schema-byte-identity check
(Scenario 4) correctly uses `git show HEAD~1:` to compare across the
rename; **Post-Deploy Verification** strong for a doc-only step —
service-restart sanity grep, file-existence checks, inbound-link grep,
roadmap checkbox grep, concrete revert command, escalation telegram;
**Observability** appropriately N/A with inspection justification;
**Constraints & DoD** alignment tight — CHANGELOG / ARCHITECTURE_REVIEW
/ reviews/ / tasks/done/ and scripts/xibi_cutover.* explicitly preserved
(mirroring step-99's pattern), minimum-touch prose refresh well-bounded,
scope-drift escape hatch present.

**Findings:**

- **C2 — Inbound-link grep gates promise "empty" but one line will
  always survive.** `[must-address]` Scenario 3, Post-Deploy Verification's
  inbound-link grep (line 278 of spec), and the DoD bullet (line 406)
  all claim `grep -n "bregger_.*\.md" public/*.md` returns *empty*
  after step-100 lands. But the very checklist line the spec toggles
  at `xibi_roadmap.md:695` is itself:
  `- [ ] Clean or archive old \`public/bregger_*.md\` docs (superseded
  by \`xibi_*.md\`)` — toggling the checkbox to `[x]` leaves the
  `public/bregger_*.md` substring intact and the grep will match that
  line. As written, the three verification gates fail the spec they
  describe. See Condition 1 below.

- **C3 — Adjacent checklist line at 696 is out of scope but similar.**
  Line 696 reads `- [ ] Legacy \`bregger_*.py\` files removed or
  clearly marked (Step 4 should handle this)`. Different pattern
  (`*.py` not `*.md`), so it does NOT match the spec's `bregger_.*\.md`
  grep — no action needed, flagged only to prevent the implementer
  from accidentally toggling it while editing 695.

- **C3 — "verbatim" schema claim softened (inline-fixed).** The Tests
  Required / TRR Checklist item asserted "18-column `CREATE TABLE
  tasks` definition... verbatim" against `xibi/db/migrations.py:210`.
  Actual count is 15 columns, and "verbatim" overstates it — the md
  uses `CREATE TABLE tasks (` (no `IF NOT EXISTS`) and adds inline
  annotations like `-- "Renew afya.fit domains on Namecheap"`, which
  are expected differences between doc and code. Corrected inline.

- **C3 — "58 total" stale (inline-fixed).** Internal bregger mentions
  inside `public/xibi_*.md` total 65, not 58 (xibi_roadmap.md drifted
  from 25 → 32). Out-of-scope content unchanged; corrected the count
  in Constraints for future-auditor accuracy.

**Conditions (READY WITH CONDITIONS):**

1. **Reword `public/xibi_roadmap.md:695` to remove the `bregger_*.md`
   substring at the same time as toggling the checkbox.** Replace the
   full line:
   `- [ ] Clean or archive old \`public/bregger_*.md\` docs (superseded by \`xibi_*.md\`)`
   with:
   `- [x] Old deprecated \`public/\` design docs removed, superseded by the current \`xibi_*.md\` set (step-100)`
   This preserves the historical completion marker (the `[x]`) while
   letting the three "empty output" verification gates (Scenario 3,
   Post-Deploy inbound-link grep, DoD inbound-link bullet) pass as
   written. Do NOT amend those three gates to tolerate a 1-line match;
   rewording the source line is the cleaner single-edit fix.

**Inline fixes applied during review:**

- Corrected "18-column... verbatim" → "15-column... columns, types,
  and defaults must match; whitespace and inline comment annotations
  are expected differences" in the TRR Checklist schema-verification
  bullet (around spec line 376–380).
- Corrected the out-of-scope bregger-mention tally in Constraints
  (around spec line 320–324) from "58 total (22/8/25/3)" to
  "65 total (22/8/32/3)."

**Confidence:** High on Contract scope (trivial — no code surface).
High on Real-World Tests. High on Post-Deploy Verification (concrete
commands, concrete rollback). High on Constraints & DoD alignment.
Medium only on the grep-gate wording (driving Condition 1).

**Independence:** This TRR was conducted by a fresh Opus context in
Cowork with no draft-authoring history for step-100. The spec file is
currently untracked on disk; this TRR Record is the first content this
session has written into it beyond the two inline fixes noted above.
