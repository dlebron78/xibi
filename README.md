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
- `.github/` — GitHub Actions CI workflows; the PR gate the CI-iteration stage polls.

## How work flows

Specs are written in Cowork, reviewed (TRR) in a fresh Cowork session, promoted and implemented in Claude Code, merged locally with `--ff-only`, and auto-deployed when `origin/main` moves. The full protocol is `CLAUDE.md`. Incidents: `BUGS_AND_ISSUES.md`. Deploy and box access: `DEPLOY.md`. Security model: `SECURITY.md`.

## Design docs

The living architecture plan is `~/Documents/Dev Docs/Xibi/RFC-source-agnostic-xibi.md` (outside the repo). Execution status against it: `tasks/ARCHITECTURE-TRACKER.md`. The five files in `public/` are the earlier design baseline the TRR Vision check still reads. Any other architecture write-up you find in git history is a snapshot of an earlier design, not a source of truth.
