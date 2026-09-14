# Step 136: Repo hygiene — fossil docs, tracked data, README

## Architecture Reference
- Governing plan: `~/Documents/Dev Docs/Xibi/RFC-source-agnostic-xibi.md` (no section — this step changes no architecture)
- Status: `tasks/ARCHITECTURE-TRACKER.md`
- Origin: **Xibi Pruning Audit, 2026-09-14** (Cowork) — step 1 of 5. Decision recorded there: prune, do not rewrite. The next pruning step (dead-code deletion), then one-skill-system, then the shadow-path cutover are separate specs and depend on this one only for a clean tree.
- Revision: v2, 2026-09-14 — reworked in Cowork after TRR v1 (NOT READY). The v1 Record and a directive-by-directive rework log are at the end of this file.

## Objective
Remove everything in the repo that is not code, tests, deploy config, or a living document, so that a fresh session — human or Claude — opens the repo and finds one map instead of six. Concretely: delete 12 root planning docs and 9 `public/` docs that nothing references, stop tracking 533 files of review output and a 5-file test artifact (`MagicMock/`, committed once in `68e1167` on 2026-04-03 and never regenerated since), stop the NucBox nightly cron from committing into the repo, carry the one live invariant from `GEMINI.md` into `CLAUDE.md` before deleting it, and write the README (currently 0 bytes). **Zero runtime behavior changes.** No Python under `xibi/` is touched.

## User Journey
1. **Trigger:** Daniel (or a fresh Cowork/Claude Code session) opens `~/Documents/Xibi` to start the next cleanup spec.
2. **Interaction:** `ls` shows five `.md` files at root. README says what Xibi is, what runs in production, what every top-level directory is, and where the living design docs are. CLAUDE.md is the only process doc.
3. **Outcome:** No contradictory architecture docs to read; no 4 MB of review logs in the tree; `git status` stays clean the morning after the nightly pressure test.
4. **Verification:** `ls *.md *.txt` returns exactly `BUGS_AND_ISSUES.md CLAUDE.md DEPLOY.md README.md SECURITY.md`; `git ls-files | wc -l` prints exactly `687` (1246 today − 559 deleted); NucBox `git status --short | grep -v '^??'` is empty the morning after the cron runs.

## Real-World Test Scenarios

### Scenario 1: Fresh clone reads as one map (happy path)
**What you do:**
```
git clone <origin> /tmp/xibi-136 && cd /tmp/xibi-136 && ls *.md *.txt public/ reviews/ MagicMock/ 2>&1; git ls-files | wc -l
```
**What happens:** nothing runs; this is a tree check.

**What you see:**
```
BUGS_AND_ISSUES.md  CLAUDE.md  DEPLOY.md  README.md  SECURITY.md
public/: review_criteria.md  xibi_architecture.md  xibi_roadmap.md  xibi_signal_intelligence.md  xibi_vision.md
ls: reviews/: No such file or directory
ls: MagicMock/: No such file or directory
687
```
**How you know it worked:** exact listing above; `wc -c README.md` > 2000.

### Scenario 2: The `MagicMock/` artifact is gone and the suite does not bring it back
**What you do:**
```
cd /tmp/xibi-136 && test ! -d MagicMock && python3 -m pytest -q && test ! -d MagicMock && git status --short | wc -l
```
**What happens:** `MagicMock/` was a one-time artifact committed in `68e1167` (all five files carry the same 2026-04-03 timestamp; `git status` has shown no new id-named files since). `git rm -r --cached MagicMock/ && rm -rf MagicMock/` plus the existing `.gitignore:74` entry is the entire fix. No test is modified.

**What you see:** the suite passes and the last line is `0`.
**How you know it worked:** no `MagicMock/` before or after the run; tree clean. (If a `MagicMock/` directory *does* appear after the run, that is new information — TRR v1 established by static analysis that no current caller passes an unnamed mock's `.config` to a path constructor; a regression here is a finding for the next pruning step, not a reason to widen this one.)

### Scenario 3: Nightly pressure test writes outside the repo
**What you do:** on NucBox, after re-running the installer:
```
cd ~/xibi && python3 scripts/dev_pressure_test.py --verbose >/dev/null 2>&1; ls -t ~/.xibi/reviews/test-runs | head -1; git status --short | grep -v '^??' | wc -l; git status --short | grep -c '^?? reviews/'
```
**What happens:** `dev_pressure_test.py` default `--report-dir` is now `~/.xibi/reviews/test-runs`; the cron line no longer contains `git add`/`git commit`/`git push`.

**What you see:** a report file named with today's date, then `0`, then `0`.
**How you know it worked:** report exists outside the repo; nothing modified; nothing new under `reviews/`.

### Scenario 4: Deploy pull conflict (failure path, exercised in a scratch clone on the Mac — never on NucBox)
**What you do:** in a scratch clone at the pre-136 commit, dirty a tracked review file, then pull the 136 merge:
```
git clone <origin> /tmp/xibi-pre && cd /tmp/xibi-pre && git checkout <pre-136-sha> && echo x >> reviews/daily/2026-03-25-0033.md && git pull origin main
```
**What you see:** `error: Your local changes to the following files would be overwritten by merge: reviews/daily/2026-03-25-0033.md` — the branch NucBox's `deploy.sh:329` (plain `git pull`) would take if the box had a *modified tracked* file under `reviews/`, firing the `⚠️ Deploy failed — git pull error` telegram at `:331`.
**How you know it worked:** the error reproduces; `git stash && git pull origin main` then succeeds in the scratch clone. This scenario only demonstrates the failure shape. The production procedure is the pre-deploy gate in Post-Deploy Verification, which is different: on NucBox the 113 untracked files do not block the pull, and `git stash -u` must **never** be used there (it would hide the box-only dev scripts).

## Files to Create/Modify

**Delete (root docs — 0 live references; the only references are inside `tasks/done/` history, stale worktrees, and each other):**
`GEMINI.md`, `LOCAL_AI_AGENT_ARCHITECTURE.txt`, `system_architecture_summary.txt`, `ARCHITECTURE_REVIEW.md`, `xibi_roadmap_v3.md`, `HANDOFF.md`, `PIPELINE.md`, `WORKFLOW.md`, `BACKLOG.md`, `CHANGELOG.md`, `COWORK_AUDIT_2026-03-29.md`, `COWORK_AUDIT_OPERATIONAL_2026-03-30.md`

**Delete (`public/` — 0 live references outside this spec; all last touched 2026-03-24 or 04-22):**
`public/comparison_final.md`, `public/email_skill_spec.md`, `public/openclaw_optimization.md`, `public/phase_0_plan.md`, `public/ray_delta.md`, `public/skill_adaptation.md`, `public/task.md`, `public/xibi_multistep_loop.md`, `public/xibi_task_layer.md`

**Keep (`public/` — each has ≥ 1 live reference, so the same rule that deletes the nine above keeps these five; the next pruning step re-evaluates them against the RFC):**
- `public/xibi_architecture.md` — referenced by `SECURITY.md`, the spec template, `public/xibi_roadmap.md`.
- `public/xibi_roadmap.md` — referenced by the spec template and `public/xibi_architecture.md`.
- `public/review_criteria.md` — the TRR Vision-check criteria; referenced by `tasks/README.md`'s TRR protocol.
- `public/xibi_vision.md` — the Autonomy-Trust Matrix (L1–L2 autonomy, T2 trust) that `review_criteria.md:98` and `:147` cite and that every TRR's Vision check is made against. **Decision (TRR v1 F2): keep, not move or delete** — it has a live reference in a kept file, so deleting it would have violated this spec's own rule; moving it now creates work for a file whose fate belongs to the next step's evidence pass.
- `public/xibi_signal_intelligence.md` — referenced by `public/xibi_architecture.md:5` (companion docs). Same rule.

**Stop tracking (delete from tree; history keeps them):**
- `reviews/` — 450 `daily/` + 82 `test-runs/` + 1 top-level = 533 files, 4.1 MB. `git rm -r reviews/`.
- `MagicMock/` — 5 files. `git rm -r --cached MagicMock/ && rm -rf MagicMock/` (already in `.gitignore:74`).

**Modify:**
- `.gitignore` — append the block in Contract (`reviews/`, `.claude/worktrees/`, `.claude/settings.local.json`, `architecture/`). None of the four is currently tracked, so these are pure ignores — no `--cached` removal needed.
- `CLAUDE.md` — two additions, nothing else changes: (a) a new hard rule **9. Python precomputes, roles reason** carrying `GEMINI.md` Rule 24 verbatim (text in Contract) — this is the one invariant in `GEMINI.md` with no other home; (b) a new `## Code hygiene` section carrying the four bullets from `WORKFLOW.md` verbatim (DB writes best-effort; `finally: _tables_ensured.add(db_key)` circuit-breaker; tracing wrapped in `try/except: pass`; config via `config.get()`).
- `tasks/templates/task-spec.md:304` and `tasks/backlog/step-87b-schema-reconciliation.md:270` — replace `> See \`WORKFLOW.md\`.` with `> See \`CLAUDE.md\` § Git workflow.` (path fix only; no spec prose).
- `public/xibi_roadmap.md:3` — delete both the `**Backlog:** \`BACKLOG.md\`.` fragment and the `**Antigravity instructions:** \`GEMINI.md\`.` fragment; the line becomes `> **Architecture:** \`public/xibi_architecture.md\`. **Security:** \`SECURITY.md\`.`
- `public/xibi_roadmap.md:214` — replace `(see GEMINI.md Rule 24)` with `(see CLAUDE.md hard rule 9)`; the rest of the line stays.
- `scripts/install_dev_cron.sh` — `CRON_JOB` becomes exactly:
  `55 1 * * * cd ${XIBI_DIR} && git pull origin main -q && systemctl --user restart xibi-telegram xibi-heartbeat && sleep 5 && /usr/bin/python3 scripts/dev_pressure_test.py --report-dir ${HOME}/.xibi/reviews/test-runs >> ${LOG_DIR}/pressure-test.log 2>&1  # ${CRON_MARKER}`
  Header comment line 9 "2. Test results are committed to the repo so Cowork can read them" → "2. Reports are written to ~/.xibi/reviews/test-runs (outside the repo)". Line 56 `echo "Reports: ..."` updated to the same path.
- `scripts/dev_pressure_test.py:978` — `default="reviews/test-runs"` → `default="~/.xibi/reviews/test-runs"` (line 999 already applies `.expanduser()`; `os` is imported at line 24; `report_dir.mkdir(parents=True, exist_ok=True)` already exists at line 937). Docstring lines 14 **and** 16 updated to the new path.
- `README.md` — replace the empty file with the content in **Contract** below, verbatim.

**Local-only (not in the PR; Claude Code runs these on the Mac before opening it):**
- Worktrees: all six entries in `git worktree list` are already marked `prunable`, and all three branches they point at (`hotfix/email-workdir-default`, `hotfix/priority-context-cap-and-compression`, `claude/amazing-mcclintock-5ec190`) are merged into `main` (verified 2026-09-14) — nothing is lost. Run `git worktree prune`, then `rm -rf .claude/worktrees/` for any leftover directories. (`git worktree remove --force <path>` errors on prunable entries; don't use it.)
- `rm -rf architecture/` — untracked frozen snapshot (2026-05-04). Cowork archived both files byte-identical at `~/Documents/Dev Docs/Xibi/archive/2026-05-04-codebase-map/` and repointed the RFC's `Depends on:` line at the archive on 2026-09-14 (done; outside the repo).

## Database Migration
None — no schema changes. Section intentionally omitted.

## Contract

**CLAUDE.md hard rule 9** (append after rule 8, verbatim; source `GEMINI.md:121–127`, path updated to the current package):

```markdown
9. **Python precomputes, roles reason.** Models never receive raw inputs
   that require inference to interpret. Before any prompt is assembled,
   Python resolves:
   - **Temporal expressions** → absolute date strings. `"next Tuesday"`
     becomes `"Tuesday, April 1, 2026"`; `"last week"` becomes
     `after_date=2026-03-17`. The model sees resolved values, never
     computes them.
   - **Conditional injection** → date context is only added when the user
     message contains temporal language (today, tomorrow, weekday names,
     etc.). No temporal words = no date block = the model cannot apply
     phantom date filters.
   - **Active threads, pinned topics** → pre-queried from SQLite and
     formatted before the prompt is built.

   Precomputation utilities live in `xibi/utils/` (e.g.
   `xibi/utils/time.py::parse_semantic_datetime`). No precomputation logic
   belongs in prompt templates, role-calling code, or tool implementations.
   If you find yourself asking a role to "figure out what date last Tuesday
   was" — stop. Python does that.
```

**README.md** (verbatim):

```markdown
# Xibi

Secure autonomous agent framework (formerly Project Ray). It reads your inbox, calendar and contacts, decides what matters, and talks to you over Telegram — with every external input passing through a trust gate before any model sees it. Architecture choices lean toward security and intelligence, not cost or local-first ideology. The chief-of-staff, job-search and tourism deployments are reference deployments, not product goals.

## What runs in production

NucBox K12 (Ubuntu 24.04), user-level systemd. `scripts/deploy.sh` watches `origin/main`, pulls, syncs the units in `systemd/`, and restarts services.

| Unit | Job | Entry point |
|---|---|---|
| `xibi-heartbeat.service` | Polls email / calendar / contacts, extracts signals, runs sweeps and the review cycle | `python3 -m xibi … heartbeat` |
| `xibi-telegram.service` | The chat surface — routes messages to skills and the ReAct loop | `python3 -m xibi … telegram` |
| `xibi-caretaker.timer` | Self-checks: service silence, schema/config drift, provider health, review freshness | `python3 -m xibi … caretaker run` |
| `xibi-dashboard.service` | Operator dashboard | `run_dashboard.py` |
| `xibi-oauth-callback.service` | Google OAuth callback receiver | `python3 -m xibi.oauth.server` |
| `xibi-ci-watch.timer` | Polls CI status | `scripts/ci-watch.sh` |

Local inference is Ollama on the NucBox iGPU; the review tier uses cloud models. Model selection always goes through `get_model()` — never hardcode a model name.

## Repo map

- `xibi/` — the package. `heartbeat/` (signal pipeline), `channels/` + `react.py` (chat), `security/` (trust gate, sanitizer, risk grader), `subagent/`, `skills/` (the Python skill registry and handlers the Telegram surface uses), `db/` (SQLite + migrations), `caretaker/`, `dashboard/`, `routing/`, `utils/` (precomputation — see CLAUDE.md hard rule 9).
- `tests/` — pytest suite. `python3 -m pytest -q`.
- `scripts/` — `deploy.sh` (NucBox watcher), model benchmark, nightly dev pressure test.
- `systemd/` — the unit files above.
- `templates/` — HTML templates for the dashboard (index, caretaker, subagents).
- `skills/` — manifest-based skill packs (calendar, checklists, email, filesystem, memory, nudge, reminders, search) from an earlier skill format. Not the same thing as `xibi/skills/`. Whether anything still loads these is settled by the one-skill-system pruning step.
- `domains/` — domain agent packs (`career-ops`, `test-echo`): an `agent.yml` plus markdown skills, loaded from `<workdir>/domains` by the subagent skill. `career-ops` has produced real output on the NucBox.
- `public/` — earlier design docs. Five remain (`xibi_architecture`, `xibi_roadmap`, `review_criteria`, `xibi_vision`, `xibi_signal_intelligence`) because live files still cite them; the next pruning step re-evaluates each against the RFC.
- `tasks/` — the spec pipeline: `backlog/` → `pending/` → `done/`; `ARCHITECTURE-TRACKER.md` is the living execution status; `EPIC-*.md` group specs; `README.md` is the TRR protocol; `templates/task-spec.md` is the spec template.
- `.claude/skills/` — TRR, code-review and CI-iteration protocols.

## How work flows

Specs are written in Cowork, reviewed (TRR) in a fresh Cowork session, promoted and implemented in Claude Code, merged locally with `--ff-only`, and auto-deployed when `origin/main` moves. The full protocol is `CLAUDE.md`. Incidents: `BUGS_AND_ISSUES.md`. Deploy and box access: `DEPLOY.md`. Security model: `SECURITY.md`.

## Design docs

The living architecture plan is `~/Documents/Dev Docs/Xibi/RFC-source-agnostic-xibi.md` (outside the repo). Execution status against it: `tasks/ARCHITECTURE-TRACKER.md`. The five files in `public/` are the earlier design baseline the TRR Vision check still reads. Any other architecture write-up you find in git history is a snapshot of an earlier design, not a source of truth.
```

**`.gitignore` additions** (exact lines, appended under a `# step-136` comment):
```
# step-136: review output, editor worktrees, frozen snapshots
reviews/
.claude/worktrees/
.claude/settings.local.json
architecture/
```

**Deleted-file list** used by the verification loop (`DELETED`, 21 names):
`GEMINI.md LOCAL_AI_AGENT_ARCHITECTURE.txt system_architecture_summary.txt ARCHITECTURE_REVIEW.md xibi_roadmap_v3.md HANDOFF.md PIPELINE.md WORKFLOW.md BACKLOG.md CHANGELOG.md COWORK_AUDIT_2026-03-29.md COWORK_AUDIT_OPERATIONAL_2026-03-30.md comparison_final.md email_skill_spec.md openclaw_optimization.md phase_0_plan.md ray_delta.md skill_adaptation.md task.md xibi_multistep_loop.md xibi_task_layer.md`

**No-dangling-reference invariant:** after the change, for every name in `DELETED`:
```
grep -rl --exclude-dir=.git --exclude-dir=.venv --exclude-dir=done --exclude-dir=__pycache__ --exclude-dir=worktrees -F "$name" .
```
must return nothing except this spec file (`tasks/backlog/` until promoted, then `tasks/done/step-136-*.md`, which is excluded). Verified achievable on 2026-09-14: with the five `public/` keepers retained, the only surviving reference was `public/xibi_roadmap.md:3` (`GEMINI.md`), which the edit above removes. (`tasks/done/` is history and may keep stale references.)

**Exact tracked-file count after the change:** 1246 − 12 − 9 − 533 − 5 = **687**. `README.md` is already tracked (0 bytes), so writing it does not change the count.

## Observability
1. **Trace integration:** N/A — no runtime code path is added or changed.
2. **Log coverage:** N/A — the only new log output is the nightly pressure test writing to `~/.xibi/reviews/test-runs/` instead of the repo (already logged to `~/xibi/logs/pressure-test.log`).
3. **Dashboard/query surface:** N/A.
4. **Failure visibility:** the one way this step fails in production is the NucBox deploy pull refusing to overwrite a modified tracked file under `reviews/`. `deploy.sh:331` already sends `⚠️ Deploy failed — git pull error on NucBox` to Telegram in that case. The pre-deploy gate below exists so that never fires.

## Post-Deploy Verification

### Pre-deploy gate (run on NucBox BEFORE the merge to `origin/main`)
Checked 2026-09-14: the box has **113 untracked files, 0 modified tracked files, 0 local commits** (`git status --short | cut -c1-2 | sort | uniq -c` → `113 ??`). The nightly cron's `git commit` never fired, so its reports accumulated as untracked files under `reviews/test-runs/`. Untracked files do not block `git pull` (only modified tracked files do), so the deploy itself is safe — but they would leave a stray `reviews/` directory behind and fail the tree check below. Run, in order:

1. Confirm there are still no modified tracked files (the only thing that can break the pull):
   ```
   ssh dlebron@100.125.95.42 "cd ~/xibi && git status --short | grep -v '^??' | wc -l && git log --oneline origin/main..HEAD | wc -l"
   ```
   Expected: `0` and `0`. If the first is non-zero: `git stash` (tracked changes only — **never `-u`**, which would also hide the dev scripts listed below), then note the stash in the deploy telegram thread. If the second is non-zero: `git log --oneline origin/main..HEAD` — every line must be `chore: nightly dev pressure test …`; if so `git branch nucbox-cron-backup && git reset --hard origin/main`; if any other commit appears, **stop** and telegram `[DEPLOY VERIFY FAIL] step-136 — NucBox has non-cron local commits: <first line>`.
2. Relocate the untracked review reports to their new home (the same path the updated cron writes to), so `reviews/` can disappear cleanly on pull:
   ```
   ssh dlebron@100.125.95.42 "mkdir -p ~/.xibi/reviews/test-runs ~/.xibi/reviews/daily && cd ~/xibi && (mv reviews/test-runs/* ~/.xibi/reviews/test-runs/ 2>/dev/null; mv reviews/daily/* ~/.xibi/reviews/daily/ 2>/dev/null; true) && git status --short | grep -c '^?? reviews/'"
   ```
   Expected: `0` (nothing untracked remains under `reviews/`; the tracked files are still there until the pull deletes them).
3. Leave the other untracked items alone — they are out of scope for this step and must not be stashed or deleted by it: `benchmarks/`, `patches/`, `run_dashboard.py.bak`, `domains/career-ops/output/*.md`, `scripts/{dump_system_prompt,show_prompt_clean,smoke,trace_timeline}.py`, `scripts/smoke_results/`. Each is a commit-or-delete decision for the drift audit (parked; see Constraints).

### Schema / migration (DB state)
N/A — no schema or data changes.

### Runtime state
- Deploy landed and services restarted (standard checks):
  ```
  ssh dlebron@100.125.95.42 "grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh | tr ' ' '\n' | sort"
  ssh dlebron@100.125.95.42 "systemctl --user list-units --state=active 'xibi-*.service' --no-legend | awk '{print \$1}' | sort"
  ```
  Expected: identical lists.
- Tree matches the PR on the box:
  ```
  ssh dlebron@100.125.95.42 "cd ~/xibi && git status --short | grep -v '^??' | wc -l && git ls-files | wc -l && git ls-files reviews MagicMock | wc -l && ls *.md *.txt 2>/dev/null | tr '\n' ' ' && echo && test ! -d reviews && test ! -d MagicMock && echo TREE_OK"
  ```
  Expected: `0` (no modified tracked files — pre-existing untracked dev files are allowed), `687`, `0`, then `BUGS_AND_ISSUES.md CLAUDE.md DEPLOY.md README.md SECURITY.md`, then `TREE_OK`.
- Cron re-installed without the git chain (operator step — the crontab is not managed by `deploy.sh`):
  ```
  ssh dlebron@100.125.95.42 "bash ~/xibi/scripts/install_dev_cron.sh >/dev/null && crontab -l | grep dev_pressure_test | grep -c 'git add'"
  ```
  Expected: `0` (grep -c prints 0; the shell exit code will be 1 — that is the pass signal here).
- Next-morning check (after the 01:55 run following the deploy):
  ```
  ssh dlebron@100.125.95.42 "ls -t ~/.xibi/reviews/test-runs | head -1; cd ~/xibi && git status --short | grep -v '^??' | wc -l; git status --short | grep -c '^?? reviews/'"
  ```
  Expected: a filename containing today's date, then `0`, then `0`.

### Observability
N/A — no spans or log lines were promised.

### Failure-path exercise
Exercised pre-merge on the Mac (Scenario 4), not on NucBox: a modified tracked file under `reviews/` makes `git pull` refuse with `Your local changes … would be overwritten by merge`. That is the branch `deploy.sh:329–331` takes. The production defense is the pre-deploy gate (step 1 proves there are no such files; step 2 relocates the untracked ones). Deliberately inducing it on the production checkout would block a real deploy for no additional information.

### Rollback
- Revert the merge commit on the Mac and push:
  ```
  cd ~/Documents/Xibi && git revert --no-edit <merge-sha> && git push origin main
  ```
  This restores every deleted doc, re-tracks `reviews/` and `MagicMock/` from history, and restores the old cron installer. Then on NucBox: `bash ~/xibi/scripts/install_dev_cron.sh` to put the old cron line back.
- **Escalation:** telegram `[DEPLOY VERIFY FAIL] step-136 — <1-line what failed>`
- **Gate consequence:** the next pruning step is not promoted until this is green.

## Constraints
- No files under `xibi/` are modified. No prompts, no schema, no LLM-facing surface. If implementation finds it needs one, **stop** — that is scope drift, not a condition to absorb.
- **Named scope for the next pruning step (dead-code deletion), so it is not lost:** `xibi/react.py.bak` and `xibi/session.py.bak` (tracked fossils — same class as this step's targets, excluded only by the no-`xibi/` rule above); the five kept `public/` files (re-evaluate each against the RFC; `xibi_architecture.md` is byte-identical to its Dev Docs copy, `xibi_signal_intelligence.md` differs by 1 byte, `xibi_vision.md` has no copy outside the repo).
- The 27 uncommitted files under `tasks/backlog/notes/` are **not** touched by this step — Cowork triages them separately.
- Test-DB artifacts at repo root (`test_*.db-*`, `nonexistent.db*`) are gitignored clutter created by tests running in CWD; cleaning the test suite to use `tmp_path` throughout is a follow-on, not this step. `rm -f test_*.db* nonexistent.db*` locally is fine.
- NucBox carries ~11 untracked non-review items (dev scripts written on the box, `benchmarks/`, `patches/`, `run_dashboard.py.bak`, `domains/career-ops/output/`). This step does not touch them. They are evidence for two later decisions: (a) `domains/career-ops` has real output files on the box, so `domains/` is *used* in production — input to the one-skill-system step's "decide domains/"; (b) the dev scripts are a commit-or-delete pass for a drift-audit note, not this spec.
- The nightly cron still restarts `xibi-telegram` and `xibi-heartbeat` at 01:55 and shares the production Ollama. That is a known problem (2026-06-23 incident) and a separate decision; this step only stops it committing to git.
- Depends on: nothing. step-135 is merged; `pending/` is empty.

## Tests Required
- Full existing suite green: `python3 -m pytest -q`.
- `test ! -d MagicMock && python3 -m pytest -q && test ! -d MagicMock` passes from a clean tree.
- The no-dangling-reference loop over `DELETED` returns nothing (except this spec file).
- `git ls-files | wc -l` prints `687`.
- `bash -n scripts/install_dev_cron.sh` (syntax) passes; `python3 scripts/dev_pressure_test.py --help` shows the new default report dir.

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages — nothing added to bregger files (N/A: no code under `xibi/` is touched)
- [ ] No coded intelligence (N/A)
- [ ] No LLM content injected into scratchpad (N/A)
- [ ] All acceptance criteria traceable through the codebase
- [ ] Real-world test scenarios walkable end-to-end
- [ ] Post-Deploy Verification present; every subsection filled or justified N/A
- [ ] Every PDV check names exact expected output
- [ ] Failure-path exercise present
- [ ] Rollback is a concrete command; escalation shape filled

**Step-specific gates:**
- [ ] Reviewer re-runs the live-reference grep for every name in `DELETED` (excluding `.git`, `.venv`, `tasks/done`, `.claude/worktrees`, `reviews`, `__pycache__`, and this spec) and gets nothing; and confirms each of the five kept `public/` files has ≥ 1 live reference.
- [ ] Reviewer confirms `scripts/deploy.sh:329` is a plain `git pull` (not `reset --hard`), which is why the pre-deploy gate is mandatory.
- [ ] Reviewer confirms `.gitignore:74` already ignores `MagicMock/` and that `git log -- MagicMock` shows a single commit (`68e1167`).
- [ ] Reviewer confirms the CLAUDE.md hard-rule-9 text matches `GEMINI.md:121–127` apart from the `xibi/utils.py` → `xibi/utils/` path update, and that `xibi/utils/time.py::parse_semantic_datetime` exists.
- [ ] Reviewer confirms 1246 − 559 = 687 against `git ls-files | wc -l` on the reviewed commit.

## Definition of Done
- [ ] All deletions, untrackings and modifications above applied in one PR on branch `step-136-repo-hygiene`
- [ ] `python3 -m pytest -q` green locally and in CI
- [ ] No-dangling-reference loop returns nothing
- [ ] `git ls-files | wc -l` == 687 on the PR head
- [ ] README.md and CLAUDE.md additions match Contract verbatim
- [ ] Pre-deploy gate run on NucBox and passed before `git merge --ff-only` + push
- [ ] PDV "Runtime state" checks pass; next-morning check scheduled
- [ ] `tasks/ARCHITECTURE-TRACKER.md` gets one line under a new `## Pruning (audit 2026-09-14)` table: `step-136 | DONE | <one-line evidence>` — as part of the merge commit
- [ ] Spec moved to `tasks/done/` in the merge commit

---
> **Spec gating:** promote only when `tasks/pending/` is empty (it is). The next pruning step stays in `backlog/` until this merges.

---

## TRR Record — Opus, 2026-09-14

**Verdict: NOT READY**

### Summary
The mechanical core of this spec is accurate and unusually well-evidenced: every file
path, line number, count and command I re-ran checks out (see *Verified* below). It
fails the gate on three things that are not mechanical — one wrong root-cause diagnosis
that makes a named deliverable unimplementable (F1), one deletion that removes the only
live copy of the document this pipeline's own Vision-check gate depends on (F2), and a
set of surviving references that make the spec's own no-dangling-reference invariant
unpassable as written (F3). F2 is a scope call for Daniel, which is what puts this at
NOT READY rather than READY WITH CONDITIONS.

### Findings

**F1 — BLOCKING — The `MagicMock/` root cause is wrong; the prescribed fix cannot be applied.**
`tests/test_react_handles.py::mock_config` (line 14) is a plain dict:
`return {"db_path": ":memory:", "models": {...}}`. It is not a Mock. There is no
`config.get` to attach a `side_effect` to, and it already yields a real string for
`db_path`, so it cannot be the producer of `MagicMock/mock.config/<id>`. The spec's
Files-to-Modify entry (line 85) and Scenario 2's mechanism (line 40) both describe an
edit that has no target.

Further, the premise that anything "keeps recreating" `MagicMock/` (Objective, line 9)
does not hold on the evidence:
- All 5 files carry the identical mtime `2026-04-15 19:27` and come from one commit,
  `68e1167 Implement onboarding tools: xibi init, doctor, and skill test`.
- `git status --short MagicMock` is empty — no new id-named files have appeared since,
  and every id gets a fresh name per run, so a regenerating suite would show untracked
  additions.
- Mechanism confirmed: `os.fspath(MagicMock().config)` → `MagicMock/mock.config/<id>`,
  i.e. the producer is an *unnamed* MagicMock's `.config` child reaching a path
  constructor. Every current caller sets it explicitly — `tests/test_cli_init.py`
  (5 sites), `tests/test_cli_doctor.py` (5 sites) and `tests/test_heartbeat_resilience.py:176`
  all set `args.config = None`; `tests/test_cli_skill_test.py` leaves it unset but
  `cmd_skill_test` (`xibi/cli/skill_test.py:25`) only reads `args.name`.

`MagicMock/` is a committed fossil from April, not live pollution. `git rm -r --cached
MagicMock/ && rm -rf MagicMock/` plus the existing `.gitignore:74` is the entire fix.

**F2 — BLOCKING (scope decision for Daniel) — deleting `public/xibi_vision.md` removes the
only live copy of the Autonomy-Trust Matrix.**
`tasks/README.md` requires every TRR's Vision check to be made against "local-capable,
security-first, **L1-L2 autonomy, T2 trust**, reference deployments, opposite-of-OpenClaw
posture." A repo-wide grep finds that framework in exactly two live files:
`public/xibi_vision.md` (the definition) and `public/review_criteria.md` (which cites it
at `:98` and `:147`) — and `review_criteria.md` is one of the three files this spec
explicitly **keeps**. There is no copy in `~/Documents/Dev Docs/Xibi/` (unlike
`xibi_architecture.md`, which is byte-identical there, and `xibi_signal_intelligence.md`,
which differs by 1 byte). The new README then tells readers that any architecture doc
found in git history "is a snapshot of an earlier design, not a source of truth" — which
closes the history escape hatch on purpose.
Net effect: step-136 would delete the vision baseline that step-137's own TRR is required
to review against. Deletion is defensible only as a deliberate call.

**F3 — BLOCKING — three live references survive; the spec's own invariant cannot pass.**
Re-ran the Contract's loop (line 151) over all 23 `DELETED` names, excluding
`.git/.venv/tasks/done/__pycache__/reviews/worktrees`. Everything resolves to
deleted-file-to-deleted-file except:
1. `public/xibi_architecture.md:5` → "**Companion docs:** `xibi_signal_intelligence.md` …" (kept file → deleted file)
2. `public/review_criteria.md:98` and `:147` → `xibi_vision.md` (kept file → deleted file; see F2)
3. `public/xibi_roadmap.md:3` → still contains "**Antigravity instructions:** `GEMINI.md`."
   The spec (line 80) removes only the `**Backlog:** \`BACKLOG.md\`.` fragment from that
   same line and leaves the `GEMINI.md` pointer standing.
Definition of Done item "No-dangling-reference loop returns nothing" is therefore
unachievable as the spec is written.

**F4 — HIGH — the spec gives two opposite recovery instructions.**
Scenario 4 (line 61) and Failure-path exercise (line 208) both prescribe
`git stash -u && git pull`, and line 61 calls it "the recovery procedure in Post-Deploy
Verification." The pre-deploy gate (line 170) says the opposite in bold: "do NOT use
`-u`, which would also hide the dev scripts listed below." An implementer following
Scenario 4 on the NucBox would stash the 11 untracked dev artifacts the spec spends a
Constraint protecting. Scenario 4 is a scratch-clone exercise on the Mac where `-u` is
harmless, but it must stop advertising itself as the production recovery.

**F5 — MEDIUM — `CLAUDE.md hard rule 5` is the wrong citation, and Rule 24 has no home.**
`GEMINI.md:121` Rule 24 is "Python precomputes, roles reason" — temporal expressions
resolved to absolute dates, conditional date injection, threads pre-queried from SQLite.
`CLAUDE.md` hard rule 5 is "**No coded intelligence.**" Unrelated. Nothing in `CLAUDE.md`
covers date arithmetic or pre-resolution (grepped: no "date arithmetic", "utils.py",
"pre-resolved", "resolved values"). So line 80's rewrite of `public/xibi_roadmap.md:214`
would plant a wrong cross-reference, and deleting `GEMINI.md` retires a live invariant
that has no replacement. Same shape, lower stakes: `WORKFLOW.md`'s DoD bullet "No
hardcoded model names — always use `get_model()`" is not in `CLAUDE.md` either — the new
README does carry it, so that one is covered.

**F6 — MEDIUM — the README omits four tracked top-level directories.**
The Repo map lists `xibi/`, `tests/`, `scripts/`, `systemd/`, `tasks/`, `.claude/skills/`.
Missing: `skills/` (50 tracked files), `domains/` (20 — and per the 2026-09-14 NucBox
check, `domains/career-ops/output/` holds real production output), `templates/` (3),
`public/` (14 — which this same spec keeps three of and hands to step-137). The map also
names `xibi/skills/` while a separate root `skills/` exists, so a fresh reader cannot
tell them apart. For a file whose stated purpose is "one map instead of six", four
unlisted directories is the defect the step exists to remove.

**F7 — LOW — two tracked fossils are excluded by the Constraint.**
`xibi/react.py.bak` and `xibi/session.py.bak` are tracked. They are exactly this step's
target class, but "No files under `xibi/` are modified" puts them out of scope. That is
the right call for 136; name them explicitly as step-137 scope so they are not lost.

**F8 — LOW — `rm -rf architecture/` orphans a pointer in the governing RFC.**
`RFC-source-agnostic-xibi.md` (status ACTIVE) line 5 reads "**Depends on:**
architecture/CODEBASE_MAP.md (Phases 1-21), architecture/CODEBASE_DEEP_READ.md". I
verified both archived copies at `~/Documents/Dev Docs/Xibi/archive/2026-05-04-codebase-map/`
are **byte-identical** to the live ones, so the delete is safe — but the RFC's pointer
needs repointing at the archive in the same change.

**F9 — LOW — second stale path in the pressure-test docstring.**
Spec updates `scripts/dev_pressure_test.py:14`. Line 16 also says "Output: Markdown report
written to `reviews/test-runs/`". Update both. (Note `:999` already applies
`.expanduser()`, so `default="~/.xibi/reviews/test-runs"` as a plain string works; the
spec's `os.path.expanduser()` is also fine.)

**F10 — LOW — two small pipeline/command inaccuracies.**
(a) The footer and "Gate consequence" both gate on step-137, which does not yet exist as a
spec — `tasks/backlog/` holds only `step-87b`, `step-132`, `step-134`, `step-136` and
`dashboard-punchlist.md`. Phrase it as the next pruning step, not a file.
(b) All 6 entries in `git worktree list` are marked `prunable`. `git worktree remove
--force <path>` errors on a worktree whose directory is already gone; `git worktree prune`
alone clears those. Try `remove` and fall back to `prune`.

### Verified during this review (no defect found)
- All 12 root docs and all 11 `public/` docs on the delete lists exist. Root `.md`/`.txt`
  is 17 files today → exactly the 5 named in User Journey §4 after deletion.
- `git ls-files`: 1246 total; `reviews/` 533; `MagicMock/` 5; `reviews/` is 4.1 MB.
  Deletion total = 12 + 11 + 533 + 5 = **561**, so "drops by ≥ 560" holds — *by one file*.
  See KPI note below.
- `scripts/deploy.sh:329` is a plain `git pull origin "$BRANCH" --quiet`, with the
  `⚠️ Deploy failed — git pull error` telegram at `:331`. Pre-deploy gate is correctly
  mandatory.
- `.gitignore:74` is `MagicMock/`. `.claude/settings.local.json` and `.claude/worktrees/`
  are **not** tracked, so the two new ignore lines are pure ignores — no `--cached`
  removal needed.
- `scripts/dev_pressure_test.py`: `:14` docstring, `:24` `import os`, `:937`
  `report_dir.mkdir(parents=True, exist_ok=True)`, `:978` `default="reviews/test-runs"` —
  all exactly as the spec states.
- `scripts/install_dev_cron.sh`: `CRON_JOB` at `:20`, header bullet at `:9`, `echo
  "Reports: …"` at `:56` — all three match; the replacement line is the current one minus
  the `git add`/`commit`/`push` chain plus `--report-dir`.
- `WORKFLOW.md` `## Code Hygiene` is exactly the 4 bullets quoted in the spec.
  `CLAUDE.md` has a `## Git workflow` section (`:177`), so the new pointer target exists.
- `tasks/templates/task-spec.md:304` and `tasks/backlog/step-87b-schema-reconciliation.md:270`
  are both exactly `> See \`WORKFLOW.md\`.`
- `xibi/react.py:1090` is `config.get("db_path") or Path.home() / ".xibi" / "data" / "xibi.db"`,
  `Path()`-wrapped at `:1091` — the truthy-mock mechanism the spec describes is real, it is
  just not reachable from the fixture the spec names (F1).
- README's production claims all hold: 6 named systemd units exist in `systemd/`;
  `.claude/skills/` contains `trr-review.md`, `code-review.md`, `ci-iteration.md`;
  `scripts/ci-watch.sh`, `xibi/oauth/server.py`, `run_dashboard.py` all present.
- `architecture/` is untracked (0 tracked files) and archived byte-identical (F8).
- `tasks/pending/` is empty — the spec's promotion gate holds.

### KPI note
The DoD's "`git ls-files | wc -l` drops by ≥ 560" has exactly 1 file of headroom (561).
If F2 is resolved by keeping `xibi_vision.md`, the drop is 560 — still passing. If
`xibi_signal_intelligence.md` is also kept, it is 559 and the KPI **fails on a correct
implementation**. Restate it as an exact expected number derived from the final delete
lists (tracked count 1246 → 685 as currently specified), not a threshold.

### Rework directives (mechanical — apply, then re-TRR in a fresh session)
1. Delete the `tests/test_react_handles.py` change from Files to Modify, and rewrite
   Scenario 2 as a tree check only: `git rm -r --cached MagicMock/ && rm -rf MagicMock/`,
   then `test ! -d MagicMock`. Drop "keeps recreating" from the Objective; say "a tracked
   artifact committed once in 68e1167 (2026-04-15) and never regenerated since."
   Remove the third step-specific TRR gate's fixture clause.
2. Add to Files to Modify: `public/xibi_architecture.md:5` — drop
   `` `xibi_signal_intelligence.md` (Phase 2.5–2.6 data model), `` from the Companion docs
   line. Extend the `public/xibi_roadmap.md:3` edit to remove the
   `**Antigravity instructions:** \`GEMINI.md\`.` fragment as well as the Backlog one.
3. Replace the `public/xibi_roadmap.md:214` citation with the rule's actual content
   ("Invariant: Python resolves temporal expressions, conditional date injection and
   thread pre-queries before any prompt is assembled") rather than a pointer to
   `CLAUDE.md` hard rule 5 — or add Rule 24 to CLAUDE.md as a new hard rule and cite that.
   Whichever, the citation must resolve after GEMINI.md is gone.
4. Strike `-u` from Scenario 4 (line 61) and the Failure-path exercise (line 208), and
   delete the clause claiming it is the PDV recovery procedure.
5. Add `skills/`, `domains/`, `templates/` and `public/` to the README Repo map, and
   disambiguate root `skills/` from `xibi/skills/`.
6. Add `xibi/react.py.bak` and `xibi/session.py.bak` to the Constraints as named step-137
   scope. Add repointing the RFC's `Depends on:` line at the archive path. Add
   `dev_pressure_test.py:16`. Replace the step-137 file references with "the next pruning
   step". Note the `git worktree remove` → `prune` fallback.
7. Restate the DoD KPI as an exact count (see KPI note).

### Decision required from Daniel (blocks re-TRR)
**`public/xibi_vision.md` — delete, keep, or move?** See F2. Three options: keep it in
`public/` (smallest change, leaves one more doc at the root of the pruning problem); move
it to `~/Documents/Dev Docs/Xibi/` alongside the RFC and repoint `review_criteria.md`
(consistent with the README's "living design docs live outside the repo" story); or
delete it and accept that the Vision check in `tasks/README.md` loses its written
baseline.

### Inline fixes applied during review
None. Verdict is NOT READY; the spec goes back for rework rather than absorbing edits.

### Confidence
- Mechanical claims (paths, line numbers, counts, commands): **high** — every one
  re-executed against the working tree at `ff1f0dd`.
- F1 (MagicMock non-regeneration): **high** on the fixture being the wrong target
  (read directly) and on the April-only mtimes; **medium** on "no test in the suite
  regenerates it" — the macOS `.venv` cannot execute in the Cowork Linux VM, so this is
  static analysis plus `git status` evidence, not a suite run. The implementer should run
  `python3 -m pytest -q && test ! -d MagicMock` from a clean tree on the Mac as the
  confirming check; it is already in Tests Required.
- F2/F3 (reference graph): **high** — full repo grep over all 23 `DELETED` names, plus a
  cross-check against `~/Documents/Dev Docs/Xibi/` for duplicate copies.
- Vision check: **medium-high** — read the governing RFC's framing and confirmed the
  README's positioning paragraph matches it; the OpenClaw posture survives in the kept
  `public/xibi_architecture.md`.
- Pipeline check: **high** — `pending/` empty, no later backlog spec overlaps this one.

## Rework log — v2, 2026-09-14 (Cowork, Fable; author session, not reviewer)
Applied against TRR v1 directives 1–7 above. Awaiting a fresh-session re-TRR.
1. F1 — `tests/test_react_handles.py` change removed; Scenario 2 is a tree check; Objective says "committed once in 68e1167, never regenerated"; TRR gate 3 now checks the single-commit history instead of a fixture.
2. F2/F3 — **Decision: keep `public/xibi_vision.md` and `public/xibi_signal_intelligence.md`** (both have live references in kept files; the spec's own rule keeps them). `public/` delete list is now 9. With both kept, F3 items 1 and 2 dissolve; item 3 fixed by removing the `GEMINI.md` fragment from `xibi_roadmap.md:3` alongside the `BACKLOG.md` one. Invariant re-verified achievable.
3. F5 — `GEMINI.md` Rule 24 carried into `CLAUDE.md` as hard rule 9 (verbatim text in Contract, `xibi/utils.py` → `xibi/utils/`); `xibi_roadmap.md:214` cites hard rule 9.
4. F4 — `-u` struck from Scenario 4 and the Failure-path exercise; the "recovery procedure in PDV" claim removed; Scenario 4 now states explicitly that it is not the production procedure.
5. F6 — README Repo map adds `templates/`, `skills/` (disambiguated from `xibi/skills/`), `domains/`, `public/`, `utils/`.
6. F7/F8/F9/F10 — `.bak` files named as next-step scope; RFC `Depends on:` repointed at the archive (done in Dev Docs 2026-09-14); `dev_pressure_test.py:16` added; "step-137" replaced by "the next pruning step" throughout; worktree procedure is `prune` + `rm -rf` (all six prunable, all three branches verified merged).
7. KPI — restated as exact: `git ls-files | wc -l` == 687 (1246 − 12 − 9 − 533 − 5).


---

## TRR Record — Opus, 2026-09-14 (v2)

**Verdict: READY WITH CONDITIONS**

### Summary
The v2 rework closes all ten v1 findings and I re-verified each one against the working
tree at `ff1f0dd`. The no-dangling-reference invariant now passes: re-running the loop
over all 21 `DELETED` names, the only surviving references from kept files are exactly
the three the spec edits (`public/xibi_roadmap.md:3` ×2, `tasks/templates/task-spec.md:304`,
`step-87b:270`). Five defects remain. All five are mechanical — a wrong constant, a
justification that isn't true, an inverted local procedure, a rollback command the merge
policy makes impossible, and one Contract gap — and every one renders as an implementation
directive. No scope call is required from Daniel, which is what separates this from v1.

### Findings

**F11 — C2 — the exact tracked-file count is 688, not 687.**
`tasks/backlog/step-136-repo-hygiene.md` is **untracked today** (`git ls-files tasks/backlog/`
returns only `dashboard-punchlist.md`, 9 `notes/` files and `step-87b`). `xs-promote` commits
the spec into `tasks/pending/` and pushes to `origin/main` **before** the feature branch is
cut, so the branch point is 1247 tracked files, not 1246. 1247 − 559 = **688**. The wrong
constant appears five times: User Journey §4, Scenario 1's expected output, Contract
("Exact tracked-file count"), PDV → Runtime state, and DoD. Deletion arithmetic itself is
correct and verified: 12 root (all tracked) + 9 `public/` (all tracked) + 533 `reviews/` +
5 `MagicMock/` = 559.

**F12 — C2 — `public/review_criteria.md` has zero live references after this step; the stated
justification does not exist.**
Files-to-Keep (line 76) says it is "referenced by `tasks/README.md`'s TRR protocol."
`tasks/README.md` never names the file — its Vision check (`:85–88`) states the criteria
inline and says only "Read the latest vision docs." The **only** file in the repo that
references `public/review_criteria.md` is `PIPELINE.md` (`:22`, `:193` — the 7-gate vision
audit), and `PIPELINE.md` is on this step's delete list. So the spec's own keep rule
("≥ 1 live reference") is not met for this file, and its own step-specific TRR gate
(line 88) cannot pass. Confirmed the live process docs do **not** depend on it:
`.claude/skills/{trr-review,code-review,ci-iteration}.md` reference neither `PIPELINE.md`
nor the 7 gates, so deleting `PIPELINE.md` is otherwise safe. Cascade note: `xibi_vision.md`'s
only live reference is `review_criteria.md:98/:147`, so orphaning one orphans both.

**F13 — C2 — the worktree procedure is inverted; "prunable" is an artifact of the review VM.**
All six entries show `prunable` when `git worktree list` is run from the Cowork Linux VM
**only because** their registered paths (`/Users/dlebron/Documents/Xibi/.claude/worktrees/…`)
do not resolve inside that VM. On the Mac all six directories exist — 126 MB, contents
readable right now through the mount, newest file 2026-05-22 — and all six admin entries are
live in `.git/worktrees/`. Consequences of following the spec literally on the Mac:
`git worktree prune` prunes **nothing** (git sees six live worktrees), then
`rm -rf .claude/worktrees/` leaves six stale admin entries that `git worktree list` keeps
reporting. The spec's warning "`git worktree remove --force` errors on prunable entries;
don't use it" is backwards for the machine the command runs on. Safety is otherwise
confirmed: all six HEADs (`7b267d5 90089e7 e99afed 8081b05 40a9cfa 25ba93f`) are ancestors
of `main`, so no unmerged commit is at risk.

**F14 — C2 — Rollback names a merge sha that `--ff-only` never creates.**
Rollback says `git revert --no-edit <merge-sha>`. CLAUDE.md § Git workflow merges with
`git merge --ff-only`, which produces no merge commit. If implementation lands as several
commits there is nothing single to revert.

**F15 — C2 — the CLAUDE.md `## Code hygiene` addition has no Contract text and no placement.**
Files-to-Modify (line 86) promises two CLAUDE.md additions; Contract gives verbatim text for
hard rule 9 only. DoD then requires "CLAUDE.md additions match Contract verbatim" — for the
hygiene section there is nothing to match against, and no insertion point is specified.
(Source verified: `WORKFLOW.md:34–40` is exactly the four bullets the spec paraphrases.)

**F16 — C3 — README repo map still omits `.github/`.** Tracked (`.github/workflows/ci.yml`)
and load-bearing — it is the PR gate the CI-iteration stage polls. Same class as v1's F6,
which the rework otherwise closed (`skills/` 50, `domains/` 20, `templates/` 3, `public/` 14
all now listed and counts confirmed).

**F17 — C3 — `public/xibi_roadmap.md:214` keeps a dead path.** The spec replaces the
`GEMINI.md Rule 24` citation and says "the rest of the line stays" — but the rest of the line
reads "`xibi/utils.py` runs first", and `xibi/utils.py` no longer exists (it is the package
`xibi/utils/`). The spec fixes exactly this path in the CLAUDE.md copy and leaves it stale in
a kept file.

**F18 — C3 — Contract hard-rule-9 text is not byte-verbatim `GEMINI.md` Rule 24.** Beyond the
declared `xibi/utils.py` → `xibi/utils/` update: `. "last week"` → `; "last week"`,
"= model cannot" → "= the model cannot", "All precomputation utilities" → "Precomputation
utilities", plus re-wrapping. Harmless — the Contract block is the authoritative text to
insert — but step-specific TRR gate 4 asks for a match that does not literally hold.

**F19 — C3 — Scenario 2's timestamp claim.** "All five files carry the same 2026-04-03
timestamp" is the *commit* date of `68e1167` (verified: single commit, 2026-04-03). On-disk
mtimes are checkout times. The claim is not load-bearing for any check.

### Conditions

1. **The expected tracked-file count is 688, not 687** — everywhere it appears (User Journey
   §4, Scenario 1, Contract, PDV → Runtime state, DoD). This supersedes the `687` in the spec
   body; do not edit the body. At branch creation run `git ls-files | wc -l` and record it as
   `BASE`; the expected post-change count is `BASE − 559`. If `BASE` is not 1247, stop and
   surface `[PIPELINE] step-136 — branch-point tracked count is <N>, expected 1247` before
   implementing. (If `xs-promote` refuses because the spec is untracked, that is rule 7 — stop
   and telegram; do not hand-promote.)
2. **Give `public/review_criteria.md` a live reference in a kept file.** In `tasks/README.md`,
   in the Vision-check bullet (currently `:85–88`), change "Read the latest vision docs, not
   just this spec's framing." to "Read the latest vision docs, not just this spec's framing —
   the seven vision-alignment gates are in `public/review_criteria.md`." Add
   `tasks/README.md` to the PR's changed-file list. This is the only new file this condition
   touches; do not migrate or restructure anything else in `tasks/`.
3. **Reverse the worktree procedure and verify before deleting.** On the Mac, run in this
   order: `git worktree list --porcelain` to confirm the six paths exist; for each, confirm
   it is clean with `git -C .claude/worktrees/<name> --no-optional-locks status --short`
   (any output → stop and surface `[PIPELINE] step-136 — dirty worktree <name>`); then
   `rm -rf .claude/worktrees/`; then `git worktree prune`; then confirm `git worktree list`
   shows only the main checkout. Ignore the spec's "already prunable" claim and its
   "don't use `git worktree remove`" note — both are artifacts of the review VM.
4. **Land the implementation as exactly one commit** on `step-136-repo-hygiene` (squash before
   merging). Read Rollback's `<merge-sha>` as that commit's sha — `git rev-parse
   step-136-repo-hygiene`, captured before the `--ff-only` merge — since the merge creates no
   merge commit. The rest of the Rollback procedure stands unchanged.
5. **Copy `WORKFLOW.md:34–40` verbatim** (the `## Code Hygiene` heading's four bullets, text
   unchanged) into `CLAUDE.md` as a new `## Code hygiene` section inserted immediately after
   the `## Git workflow` section (`CLAUDE.md:177`) and before `## Spec lifecycle` (`:215`).
   Do this **before** deleting `WORKFLOW.md`. Treat those four bullets as the Contract text
   the DoD's "match Contract verbatim" item refers to.
6. **Add to the README repo map**, in the same list style, after the `.claude/skills/` bullet:
   `` - `.github/` — GitHub Actions CI workflows; the PR gate the CI-iteration stage polls. ``
7. **In the `public/xibi_roadmap.md:214` edit, also replace `` `xibi/utils.py` `` with
   `` `xibi/utils/` ``** — same one-token fix the spec already applies to the CLAUDE.md copy.

### Verified during this review (no defect found)
- **Invariant passes.** Full loop over all 21 `DELETED` names (excluding `.git`, `.venv`,
  `tasks/done`, `__pycache__`, `worktrees`, `reviews`, this spec), discarding
  deleted→deleted edges: survivors are `GEMINI.md` and `BACKLOG.md` ← `public/xibi_roadmap.md:3`
  and `WORKFLOW.md` ← `task-spec.md:304` + `step-87b:270`. All three are edits in the spec.
- Root `.md`/`.txt` is 17 today → exactly the 5 named after deletion. All 12 root and all 9
  `public/` delete targets are tracked and present. `public/` is 14 tracked → 5 keepers.
- Keeper references re-checked one by one from *kept* files only: `xibi_architecture.md`
  ← `SECURITY.md`, `task-spec.md`, `public/xibi_roadmap.md`; `xibi_roadmap.md` ←
  `task-spec.md`, `xibi_architecture.md`, `xibi_signal_intelligence.md`;
  `xibi_signal_intelligence.md` ← `xibi_architecture.md:5`; `xibi_vision.md` ←
  `review_criteria.md`. Four of five hold; the fifth is F12.
- **Zero runtime behavior change confirmed by grep, not assertion:** no `.py`, `.sh`,
  `.service`, `.timer` or template file resolves a `reviews/` path. The four `xibi/` modules
  containing the string "reviews" (`observation.py`, `dashboard/app.py`,
  `heartbeat/review_cycle.py`, `heartbeat/poller.py`) use it as English prose / "previews".
  Deleting `reviews/` cannot affect the review cycle.
- **Cron-window race is benign** (not stated in the spec, worth recording): between deploy and
  the operator re-running `install_dev_cron.sh`, the old cron line still runs. Traced the
  chain — `git add reviews/test-runs/` fails (path gone, now gitignored), so the `||` branch
  reaches `git commit` with nothing staged, which also fails, and `git push` never runs. No
  bad commit, no push to `main`. Next-morning PDV check still reads `0`/`0`.
- `scripts/deploy.sh:329` is `git pull origin "$BRANCH" --quiet`, telegram at `:331`.
  `sync_units` (`:39–115`) does keep `~/.config/systemd/user/` in lockstep with `systemd/`,
  so the README's deploy sentence is accurate.
- `.gitignore:74` is `MagicMock/`. `git log -- MagicMock/` is a single commit, `68e1167`,
  2026-04-03. `.claude/settings.local.json` and `.claude/worktrees/` are untracked → the new
  ignore lines need no `--cached` removal. `architecture/` untracked.
- `scripts/dev_pressure_test.py`: `:14` and `:16` docstring paths, `:24` `import os`, `:937`
  `mkdir(parents=True, exist_ok=True)`, `:978` `default="reviews/test-runs"`, `:999`
  `.expanduser()` — all exactly as specified.
- `scripts/install_dev_cron.sh`: `CRON_JOB` `:20`, header bullet `:9`, `echo "Reports: …"`
  `:56` — all three match; the replacement line is the current one minus the
  `git add`/`commit`/`push` chain plus `--report-dir`.
- `CLAUDE.md` has 8 hard rules (`:41–89`) so rule 9 appends cleanly; `## Git workflow` exists
  at `:177` so the repointed `> See CLAUDE.md § Git workflow` resolves. `xibi/utils/` is a
  package and `xibi/utils/time.py:6` defines `parse_semantic_datetime`.
- README production claims: all 6 named units exist in `systemd/`; `run_dashboard.py`,
  `xibi/oauth/server.py`, `scripts/ci-watch.sh` present. New README is ~3.9 KB, so Scenario 1's
  `wc -c README.md > 2000` passes.
- `xibi/react.py.bak` and `xibi/session.py.bak` are tracked and correctly named as next-step
  scope. `tasks/pending/` is empty; `tasks/ARCHITECTURE-TRACKER.md` has no Pruning table yet,
  so the DoD's new table is genuinely new.

### Inline fixes applied during review
None. F11's constant appears in five places including a DoD item, and F12 adds a file to the
change set — both are past the "trivial text edit" bar, so they are conditions instead. Per
CLAUDE.md rule 7 the conditions govern; the spec body is not edited to absorb them.

### Confidence
- Mechanical claims (paths, line numbers, counts, commands): **high** — every one re-executed
  against `ff1f0dd`.
- F11 (count): **high** on the spec being untracked and on 559; **medium-high** on 688, which
  assumes `xs-promote` commits the spec before the branch is cut — condition 1 makes the check
  self-verifying either way.
- F12 (reference graph): **high** — full-repo grep, plus confirming `.claude/skills/` does not
  depend on `PIPELINE.md`.
- F13 (worktrees): **high** on the six directories and admin entries existing and on all six
  HEADs being ancestors of `main`; **medium** on them being clean — I cannot run `git status`
  inside them from the Cowork VM, which is why condition 3 makes the implementer check.
- Runtime-inertness of the `reviews/` deletion: **high** — grep over `xibi/`, `scripts/`,
  `systemd/`, `templates/`, `run_dashboard.py`.
- Suite behavior after `MagicMock/` removal: **medium** — static analysis plus single-commit
  history; the Mac-side `pytest -q && test ! -d MagicMock` in Tests Required is the confirming
  run.

**Independence:** this TRR was conducted by a fresh Opus context in Cowork with no
draft-authoring history for step-136, and by a different session from the one that produced
the v1 Record.
