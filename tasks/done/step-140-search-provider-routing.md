# Step 140: Stop reporting total search-engine failure as success, and point the manifest at a search tool that works

## Architecture Reference
- Code under change: `skills/search/tools/search_searxng.py` (the zero-result return at
  `:42-47`) and `skills/search/manifest.json` (three tool descriptions at `:20`, `:43`,
  and the `search_tavily` entry). Repo paths — `~/.xibi/skills` on the NucBox is a
  symlink to `~/xibi/skills`, so repo and runtime are the same inodes.
- Unchanged and healthy: `skills/search/tools/search_tavily.py`.
- Origin: 2026-09-17 live incident. `session_turns` row `2026-09-18T00:33:50` —
  query "Who are the nba champions?", `tools_called=["search_searxng","search_searxng",
  "search_searxng"]`, `exit_reason=finish`, answer *"I am unable to find the list of NBA
  champions right now… every attempt returned no results."*
- Epic: `tasks/EPIC-chief-of-staff.md`.

## Objective
Two defects combine to make a working search tool unreachable.

**1. The tool lies about infrastructure failure.** `search_searxng.py:42-47` returns
`status: "success"` with `message: "No results found."` when SearXNG's response carries a
non-empty `unresponsive_engines`. A total outage is reported as a successful search that
happened to match nothing, so the model cannot distinguish "the web has no answer" from
"this tool is broken."

**2. The manifest tells the model not to switch.** Verbatim, today:

| tool | description (`manifest.json`) |
|---|---|
| `search_searxng` `:20` | "…**This is the primary search tool** — no API key, no rate limits… Use this for any world-state or current information request." |
| `search_tavily` | "Fallback web search via Tavily API. **Use only when search_searxng is unavailable.**" |
| `read_page` `:43` | "**Use this after search_searxng** when you need to read the full content of a page…" |

Three descriptions point at the broken tool, and Tavily is gated on SearXNG being
"unavailable" — which, because of defect 1, it never reports itself to be. The model
behaved correctly on false information: it retried the primary tool three times.

**Measured on the NucBox, 2026-09-17:**
```
searxng container   up 3 days, HTTP 200, JSON format enabled — healthy
engine status       brave: "too many requests" | duckduckgo: "CAPTCHA"
                    google: "access denied"    | startpage: "Suspended: CAPTCHA"
result              number_of_results: 0 for every query tested, incl. gibberish
search_tavily       HTTP 200, valid key, correct synthesised answer, ~1 s
```

**Do not re-attempt engine tuning.** On 2026-09-17 `mojeek`, `qwant`, `bing` and `yep`
were enabled in the container and reverted the same night: mojeek returned 0 results and
bing returned ten confidently *wrong* results per query (HBO Max pages for an NBA query;
"download Google Chrome" for "capital of Portugal"). Garbage is worse than nothing — the
model will summarise it into a confident falsehood. Settings were restored from an
in-volume backup and verified back to a clean zero. The container is out of scope here.

## User Journey
1. **Trigger:** Daniel asks a question needing the live web — "who won the 2026 NBA
   finals?"
2. **Interaction:** The model reads the manifest, reaches for the tool described as
   primary, and gets an answer. If it calls SearXNG while every engine is down, the tool
   returns an explicit error naming the dead engines, and the model chooses again.
3. **Outcome:** A correct answer in one or two steps, not three retries ending in an
   apology.
4. **Verification:** `session_turns` shows `exit_reason='finish'`, a non-empty answer,
   and at most one `search_searxng` call.

## Real-World Test Scenarios

### Scenario 1: Happy path — a current-events question
**What Daniel does:** asks Roberto "who won the 2026 NBA finals?"
**What Roberto does:** calls a search tool once, answers "The New York Knicks…"
**What you see:**
```
$ ssh dlebron@100.125.95.42 "python3 -c \"import sqlite3;\
c=sqlite3.connect('/home/dlebron/.xibi/data/xibi.db');\
print(list(c.execute('SELECT exit_reason,tools_called,length(answer) FROM session_turns ORDER BY rowid DESC LIMIT 1')))\""
[('finish', '[\"search_tavily\"]', 212)]
```
`search_searxng` appearing at most once is also a pass; three times is a fail.

### Scenario 2: Secondary path — SearXNG is healthy again
Engines recover; a query returns real results. `search_searxng` returns
`status: "success"` with results, unchanged from today. Nothing about this step degrades
the local path when it works.

### Scenario 3: Error / edge case — a query with genuinely no matches
**What Daniel does:** asks for something with no web presence.
**What Roberto does:** reports honestly that nothing was found.
**What you see:** `status: "success"`, `message: "No results found."` — the *unchanged*
branch. Distinguishing this from Scenario 2's failure mode is the entire point of the
step; if both return `error`, the change is wrong.

## Contract
```python
# skills/search/tools/search_searxng.py — replaces the zero-result return at :42-47
dead = data.get("unresponsive_engines", [])     # verified shape: [[engine, reason], ...]

if not raw_results and dead:
    logger.warning("search.engines_unavailable engines=%s", [e[0] for e in dead])
    return {
        "status": "error",
        "message": (
            f"SearXNG returned no results because {len(dead)} of its engines failed: "
            f"{', '.join(e[0] for e in dead)}. This is a tool outage, not an empty "
            f"result set."
        ),
        "data": {"query": query, "unresponsive_engines": dead, "source": "searxng"},
    }

if not raw_results:                              # unchanged
    return {"status": "success", "message": "No results found.", "data": {...}}
```

The message states **what failed**. It names no alternative tool: which tool to use next
is the manifest's job, and hard-coding a tool name here would put provider preference in
Python — forbidden by this spec's own Constraints and by CLAUDE.md rule 5. It would also
dead-end when `TAVILY_API_KEY` is absent, since `search_tavily.py:69-75` errors in that
case.

```jsonc
// skills/search/manifest.json — three descriptions
"search_tavily":  "Web search via the Tavily API. Use this for world-state and current
                   information requests. Returns a synthesised answer when Tavily
                   provides one. Requires TAVILY_API_KEY in secrets.env."
"search_searxng": "Web search using the locally hosted SearXNG instance — private, no
                   API key. Its upstream engines may be blocked, in which case it
                   returns an error naming them; prefer search_tavily for general
                   queries."
"read_page":      "Fetch and extract the main text content from a URL using trafilatura.
                   Use this after a web search when you need the full content of a page."
```
`read_page` loses its `search_searxng` reference — it is search-tool agnostic.

No retry loop, fallback chain, or provider-selection branch is added in Python. The tool
reports state; the model chooses.

## Files to Create/Modify
- `skills/search/tools/search_searxng.py` — the branch above, plus a module `logger`.
- `skills/search/manifest.json` — three descriptions.
- `tests/test_search_tools.py` — **new** (no test file currently covers these tools;
  `tests/test_web_search_source.py` is heartbeat-extractor coverage and unrelated).

## Database Migration
None.

## Observability
`logging.warning("search.engines_unavailable engines=%s", …)`.

Log key deliberately **not** `provider_down`: `xibi/errors.py:12` already defines
`PROVIDER_DOWN = "provider_down"` meaning *LLM provider unreachable*, and
`tasks/backlog/step-137-ollama-health-check-false-down.md` uses unscoped counts of that
bare string as its success metric. A second meaning would contaminate it.

**Known limitation, not addressed here:** `cmd_telegram` (`xibi/__main__.py:32`) never
calls `logging.basicConfig` — only `cmd_heartbeat` does (`:102`). The warning therefore
reaches journald through `logging.lastResort`, which emits the bare message with no level
name and no logger name. PDV below greps for the message text, not for "WARNING".
Fixing telegram's logging setup is real but out of scope; it belongs with the
failure-visibility work already tracked.

## Post-Deploy Verification

### Runtime state
```
# 1. the error branch, against the live outage
ssh dlebron@100.125.95.42 "cd ~/xibi && python3 -c \"
import importlib.util,os,json
p=os.path.expanduser('~/xibi/skills/search/tools/search_searxng.py')
s=importlib.util.spec_from_file_location('m',p); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
r=m.run({'query':'capital of Portugal'})
print(r['status']); print(r['message'])\""
# expect: error
#         SearXNG returned no results because 4 of its engines failed: brave, duckduckgo, ...

# 2. end-to-end — ask Roberto a current-events question from Telegram, then:
ssh dlebron@100.125.95.42 "python3 -c \"import sqlite3;\
c=sqlite3.connect('/home/dlebron/.xibi/data/xibi.db');\
print(list(c.execute('SELECT exit_reason,tools_called,length(answer) FROM session_turns ORDER BY rowid DESC LIMIT 1')))\""
# expect exit_reason='finish', non-empty answer, search_searxng appearing at most once
```

### Observability
```
ssh dlebron@100.125.95.42 "journalctl --user -u xibi-telegram --since '10 min ago' --no-pager | grep -c 'search.engines_unavailable'"
# expect >= 1 after PDV 1 has run through the telegram path
```
Note the unit is a **user** unit and the grep is on the message text, per the limitation
above.

### Failure-path exercise
Deterministic, in-process — the live outage is a third-party CAPTCHA state that clears on
its own schedule and must not be the fixture. In `tests/test_search_tools.py`,
monkeypatch `urllib.request.urlopen` to return each of three canned SearXNG payloads:

| fixture | `results` | `unresponsive_engines` | expected |
|---|---|---|---|
| A — total outage | `[]` | 4 entries | `status="error"`, message names all four |
| B — genuine empty | `[]` | `[]` | `status="success"`, "No results found." |
| C — healthy | 3 entries | `[]` | `status="success"`, results passed through |

Fixture B is the branch that cannot be produced against the live instance today; this is
why it is a stub and not a PDV step.

### Rollback
```
cd ~/Documents/Xibi && git revert <merge-sha> && git push origin main
# deploy watcher pulls within 30s; confirm the manifest is back:
ssh dlebron@100.125.95.42 "python3 -c \"import json;\
d=json.load(open('/home/dlebron/xibi/skills/search/manifest.json'));\
print([t['description'][:40] for t in d['tools'] if t['name']=='search_tavily'])\""
# expect the old 'Fallback web search via Tavily API...' text
```
No schema, no config, no state.

## KPIs (7 days after merge)
| Metric | Baseline (2026-09-17, measured) | Target |
|---|---|---|
| Web-search turns ending "unable to find" | 1 of 1 observed | 0 |
| `search_searxng` calls per web-search turn | 3 | ≤ 1 |
| Total engine failure reported as `success` | always | never |
| `search_tavily` invocations in `session_turns` | 0 of 256 rows | > 0 |

The last row is the honest baseline: Tavily has **never** been invoked through the
executor in production. Its health rests on a manual call, not on production evidence.
A reviewer should weigh whether that warrants an explicit smoke test before merge.

## Constraints
- Do not touch the SearXNG container, its `settings.yml`, or its engine set.
- Do not delete `search_searxng`; it is the private path and is expected to work again.
- Do not name a specific alternative tool in Python. Provider preference lives in the
  manifest descriptions only.
- Do not use `provider_down` as the log key.
- `manifest.json:4-15` carries `control_plane_triggers` that hard-route
  `^look up (.+)$` and `^search for (.+)$` to `search_searxng`. These are **inert** —
  `ControlPlaneRouter._register_defaults` (`control_plane.py:45-68`) registers only
  hard-coded patterns and nothing in `xibi/` reads `control_plane_triggers`. Leave them
  alone; note their existence so a future reader does not mistake them for live routing.

## Tests Required
1. Fixture A → `status == "error"`; message names every dead engine; `data` carries
   `unresponsive_engines`.
2. Fixture B → `status == "success"`, `"No results found."` — the distinction that makes
   this step worth shipping.
3. Fixture C → unchanged shape and content.
4. Network exception → existing error path unchanged.
5. The error message contains no tool name — asserts the Constraint mechanically, so a
   later edit cannot quietly reintroduce coded routing.
6. `search_tavily` answer mode returns `data["answer"]` with `source="tavily_answer"`;
   snippet mode returns `data["snippets"]` with `source="tavily_snippets"`
   (`search_tavily.py:47-48`) — **not** `data["results"]`. Covers `_slim_results`'
   truncation path, which is the branch with actual logic.

## Definition of Done
- [ ] Error branch implemented exactly as the Contract states; no tool name in Python.
- [ ] Three manifest descriptions updated.
- [ ] `search.engines_unavailable` warning emitted.
- [ ] `tests/test_search_tools.py` created; all 6 tests pass.
- [ ] PDV runtime, observability, failure-path and rollback executed and recorded.
- [ ] `tasks/ARCHITECTURE-TRACKER.md` updated in the merge commit.

## Note on hotfix eligibility (CLAUDE.md rule 8)
Correctly in the spec lane: the manifest edit is an LLM-facing surface change and is
explicitly NOT hotfix-eligible. The status-shape correction alone would qualify as
intent-restoring, but it is bundled here under one rollback; splitting it is a reviewer's
call.

## TRR Checklist
To be completed by an independent Opus session in Cowork. This spec's author must not
conduct its review.

## TRR Record — Opus, 2026-09-19

**Verdict:** READY WITH CONDITIONS

**Summary:** The diagnosis is correct and every load-bearing claim survived live
re-verification on the NucBox — the tool really does report a total engine outage
as success, the `unresponsive_engines` shape the Contract assumes is exactly
right, and manifest descriptions really do reach the model verbatim, so the
routing premise is well founded rather than hopeful. What needs work is the
verification half: the Observability check cannot fire on a *successful* deploy,
nothing proves the manifest reloaded into the running process, two of the six
required tests are written in ways that would pass for the wrong reason, and the
step converts a success-shaped return into an error-shaped one without accounting
for the ReAct loop's three-consecutive-error exit. Every finding renders as an
implementation directive; none needs a spec rewrite.

Verified live (no finding — recorded so the next reader does not re-do it):

- **Defect 1 reproduced.** Live SearXNG returns `number_of_results: 0` with
  `unresponsive_engines = [["brave","too many requests"], ["duckduckgo","CAPTCHA"],
  ["google","access denied"], ["startpage","CAPTCHA"]]`, and the deployed
  `skills/search/tools/search_searxng.py` returns
  `{"status":"success","message":"No results found."}` against it. The
  `[[engine, reason], ...]` shape the Contract assumes (`:92`) is correct.
- **Descriptions reach the model unmodified.** `~/.xibi/config.json` sets no
  `react_format`, so `xibi/channels/telegram.py:640` selects `json`, which routes
  through `_build_tools_block` (`xibi/react.py:652-676` — `description` copied
  verbatim into the OBSERVATION/ACTION JSON) and is appended whole at
  `xibi/react.py:266`. The native path (`_build_native_tools`,
  `xibi/react.py:526-554`) is equally verbatim. Neither truncates. The routing
  mechanism is sound.
- **Rule 5 is satisfied.** Provider preference lives only in manifest
  descriptions, which are prompt surface, and the Contract message (`:98-102`)
  names no alternative tool. No if/else provider selection enters Python.
- **Log key is clean.** `search.engines_unavailable` has zero collisions
  tree-wide; `PROVIDER_DOWN` is confirmed at `xibi/errors.py:12`.
- **The logging limitation is real and the grep still works.** `cmd_telegram`
  (`xibi/__main__.py:32`) never calls `basicConfig`, only `cmd_heartbeat` (`:102`)
  does, and no other module configures logging for that process. A module logger
  with no config, run in a subprocess on the NucBox, emitted the bare
  `search.engines_unavailable engines=[...]` to stderr, and
  `systemd/xibi-telegram.service` sets `StandardError=journal`.
- **`control_plane_triggers` are inert**, as claimed: `_register_defaults`
  (`xibi/routing/control_plane.py:45-68`) registers only hard-coded patterns and
  nothing under `xibi/` reads the key.
- **Every PDV command is literally runnable.** I executed the read-only ones
  verbatim; the nested `ssh`/`python3 -c` quoting is correct, including the
  backslash-newline continuations. `session_turns` carries the incident row
  exactly as the Architecture Reference cites it.
- **The KPI section's open question is answered: no pre-merge smoke test is
  needed.** `TAVILY_API_KEY` is present and non-placeholder in
  `~/.xibi/secrets.env` and reaches the running `xibi-telegram` process
  (`EnvironmentFile=%h/.xibi/secrets.env`). A live `search_tavily.run()` returned
  `status:"success"` with `data = {answer, query, source:"tavily_answer"}` in
  about a second, confirming the Tests Required #6 answer-mode claim and
  `search_tavily.py:47-48`. The "0 of 256 rows" baseline is now 0 of 257.

**Findings:**

- **[C2] PDV → Observability: the check cannot fire on a successful deploy, and
  its stated trigger is wrong (spec `:178-184`).** The note says "expect >= 1
  after PDV 1 has run through the telegram path," but PDV 1 (`:161-167`) is a
  standalone `importlib` load over ssh — it never enters `xibi-telegram`, and its
  stderr goes to the ssh session, not the unit's journal. I ran both: the journal
  grep returned 0. The only thing that writes that line to `xibi-telegram` is a
  Telegram turn in which the model *chooses* `search_searxng` — precisely what the
  new manifest discourages. The better the routing works, the more likely this
  check reads 0 and a healthy deploy is recorded as failed. Fix: condition 1.

- **[C2] Nothing verifies the manifest half actually deployed (spec `:157-176`).**
  `SkillRegistry._load()` (`xibi/skills/registry.py:24-41`) reads manifests once
  inside `__init__` (`:22`), and `cmd_telegram` builds the registry once per
  process (`xibi/__main__.py:55`). Only a restart picks up new descriptions.
  `scripts/deploy.sh:337-347` does restart `xibi-telegram.service` (listed at
  `:16`), but the spec's Runtime-state block has neither the restart-timestamp
  check the template mandates nor any check that the deployed manifest text is the
  new one — the only manifest read in the whole spec sits in the *Rollback* block
  (`:204-207`), which runs only after something has already failed. An
  un-restarted unit would serve the old manifest with the new Python, and every
  PDV check as written would still pass. Fix: condition 2.

- **[C2] Failure-path fixtures under-specified; the obvious stub silently bypasses
  the new branch (spec `:186-198`).** The tool consumes the response as
  `with urllib.request.urlopen(...) as response: json.loads(response.read().decode("utf-8"))`
  (`skills/search/tools/search_searxng.py:36-37`). I monkeypatched `urlopen` on the
  NucBox: an object exposing `__enter__`/`__exit__` and `read() -> bytes` reaches
  the zero-result branch correctly, but returning the payload dict directly raises
  and lands on `except Exception` (`:38-39`), returning
  `{"status":"error","message":"SearXNG request failed: 'dict' object does not
  support the context manager protocol"}`. Fixture A's `status == "error"`
  assertion then passes for entirely the wrong reason. Fix: condition 3.

- **[C2] Tests Required #5 is not literally satisfiable (spec `:242-243`).** "The
  error message contains no tool name" — but the Contract message (`:98-102`)
  opens with "SearXNG", the tool is named `search_searxng`, and `data["source"]`
  is `"searxng"` (`:103`). A straightforward
  `assert "searxng" not in msg.lower()` fails against a correct implementation,
  and the implementer will either weaken the test into uselessness or "fix" the
  Contract message. Fix: condition 4.

- **[C2] Turning this tool into an error source changes the ReAct exit path, and
  the acceptance criteria do not account for it (spec `:60-61`, `:75`, `:175`).**
  `xibi/react.py:1435-1437` increments `consecutive_errors` whenever
  `tool_output.get("status") == "error"`; at three it calls
  `_build_partial_answer` (`:1438`), which skips every step whose output is
  `status:"error"` (`:470-476`) and therefore returns `None` for an all-search
  failure, giving `exit_reason="error"` with `answer=""` (`:1439-1445`).
  `xibi/channels/telegram.py:643-653` then sends
  `ReActResult.user_facing_failure_message()` → "Something went wrong. Please try
  again." (`xibi/types.py:67-84`), and `session_turns.answer` is stored empty. On
  the sub-path where the model keeps retrying SearXNG, this step replaces today's
  truthful "every attempt returned no results" apology with a generic one, and
  Scenario 1 / PDV 2's `exit_reason='finish'` plus non-empty answer would fail
  with no stated diagnosis or escalation. Fix: condition 5.

- **[C3] `logger.warning` (`:95`) vs `logging.warning` (`:143`) are not
  interchangeable here.** `logging.warning()` is the module-level function, which
  calls `basicConfig()` when the root logger has no handlers — and
  `xibi-telegram` deliberately has none. Using it would install a root
  StreamHandler and change log formatting for every other logger in that process,
  a side effect well outside this step's scope. Fix: condition 6.

- **[C3] The manifest preference is permanent with no revisit trigger (spec
  `:121-124`, `:225`).** Constraints keep `search_searxng` because it "is expected
  to work again," but the new description hard-wires "prefer search_tavily for
  general queries" with no follow-on step, KPI, or condition to flip it back on
  recovery — and Tavily is the metered path while SearXNG is free. Fix:
  condition 7(b).

- **[C3] `"access": "operator"` on the tool being promoted is left unexamined
  (`skills/search/manifest.json:106`).** It is inert today — nothing under `xibi/`
  reads the `access` key, and `xibi/tools.py:18` sets `DEFAULT_TIER = GREEN` with
  `search_tavily` absent from `TOOL_TIERS` — so the routing does work. But the
  spec goes out of its way to document the inert `control_plane_triggers`
  (`:229-233`) and misses this one on the very entry it repoints the model at.
  Fix: condition 7(a).

- **[C3] Tests #6 under-claims its coverage; Rollback and one DoD line are
  imprecise.** (a) Asserting the two `source` values exercises neither
  `_SNIPPET_CHARS` (300) nor `_MAX_TOTAL_CHARS` (1000) in
  `skills/search/tools/search_tavily.py:12-51`, so #6 (`:244-247`) does not cover
  "the branch with actual logic" as claimed, and the spec never says how the test
  gets past the API-key guard at `:67-75`. (b) `git revert <merge-sha>`
  (`:202`) assumes a merge commit, but CLAUDE.md mandates `git merge --ff-only`,
  so a multi-commit branch needs a range revert; the Rollback block also omits the
  `[DEPLOY VERIFY FAIL]` escalation shape and gate-consequence line the template
  requires. (c) DoD `:254` says rollback is "executed and recorded" — it cannot be
  executed on a successful deploy. Also `control_plane.py:45-68` (`:231`) should
  read `xibi/routing/control_plane.py:45-68`. Fix: condition 8.

**Conditions (READY WITH CONDITIONS):**

1. Replace the PDV → Observability check with a durable span check plus a forced
   trigger. After deploy, send Roberto a Telegram message that names the tool
   explicitly (e.g. "use search_searxng to look up the capital of Portugal"),
   then run verbatim:
   ```
   ssh dlebron@100.125.95.42 "python3 -c \"import sqlite3;\
   c=sqlite3.connect('/home/dlebron/.xibi/data/xibi.db');\
   print(list(c.execute('SELECT COUNT(*) FROM spans WHERE operation=? AND attributes LIKE ?', ('tool.dispatch','%SearXNG returned no results because%'))))\""
   ```
   Pass = count >= 1. `xibi/executor.py:240-248` writes
   `output_preview = str(output_text)[:400]` into the `tool.dispatch` span, so the
   new message lands there in full and survives journald rotation. Keep the
   existing `journalctl ... grep -c 'search.engines_unavailable'` as a secondary
   check and record a 0 there as informational, not a deploy failure. (Verified on
   the NucBox: this query returns `[(0,)]` today and `[(3,)]` when the LIKE
   pattern is the current `'%No results found.%'` text, so the wiring is proven.)

2. Add two Runtime-state PDV checks ahead of the end-to-end one. (a)
   `ssh dlebron@100.125.95.42 "systemctl --user show xibi-telegram.service --property=ActiveEnterTimestamp --value"`
   — pass = a timestamp later than this step's commit date on `origin/main`. (b)
   `ssh dlebron@100.125.95.42 "python3 -c \"import json;d=json.load(open('/home/dlebron/xibi/skills/search/manifest.json'));print([t['description'][:40] for t in d['tools'] if t['name']=='search_tavily'])\""`
   — pass = the new `Web search via the Tavily API. …` text. Both are required
   because `SkillRegistry._load()` (`xibi/skills/registry.py:22,24-41`) caches
   descriptions at process start; without the restart check, an un-restarted unit
   serves the old manifest with the new Python and every other check still passes.

3. In `tests/test_search_tools.py`, define the `urlopen` fixture as a class with
   `__enter__` returning `self`, `__exit__` returning `False`, and `read()`
   returning `json.dumps(payload).encode("utf-8")`; monkeypatch
   `urllib.request.urlopen` (the module attribute, which
   `search_searxng.py:36` resolves at call time). Load the tool with
   `importlib.util.spec_from_file_location`, following the existing pattern at
   `tests/test_react_subagent.py:55`. Add a guard assertion to Fixture A that
   `"SearXNG request failed" not in result["message"]`, so a malformed fixture
   fails loudly instead of slipping through `search_searxng.py:38-39`.

4. Implement Tests Required #5 as: collect every tool `name` in
   `skills/*/manifest.json` except `search_searxng`, and assert that none of them
   appears in `result["message"]`. Do not assert the absence of
   "searxng"/"SearXNG" — the Contract message names the failing provider by
   design, and `data["source"]` is `"searxng"`.

5. Record Scenario 1 / PDV 2 pass-fail explicitly. PASS = `exit_reason='finish'`,
   non-empty answer, and at most one `search_searxng` entry in `tools_called`.
   FAIL = `exit_reason='error'` with an empty answer and three `search_searxng`
   entries — that is the `xibi/react.py:1435-1456` three-consecutive-error exit,
   which surfaces as "Something went wrong. Please try again." via
   `xibi/channels/telegram.py:650-651`; escalate
   `[DEPLOY VERIFY FAIL] step-140 — model still retrying searxng after manifest
   change; routing not taking` and roll back. Do not accept the degraded exit as a
   pass on the grounds that the tool "correctly reported an error." Add a seventh
   test asserting Fixture A's `status` key sits at the top level of the returned
   dict (not nested under `data`), since that is the key `xibi/react.py:1435`
   reads.

6. In `skills/search/tools/search_searxng.py`, add `import logging` and a
   module-level `logger = logging.getLogger(__name__)`, and emit
   `logger.warning("search.engines_unavailable engines=%s", [e[0] for e in dead])`.
   Do not call `logging.warning(...)` — the module-level function calls
   `basicConfig()` when the root logger has no handlers, which inside
   `xibi-telegram` would install a root StreamHandler and change formatting for
   every other logger in that process.

7. Add two lines of context to the PR body (no code change): (a)
   `skills/search/manifest.json:106` keeps `"access": "operator"` on
   `search_tavily`; it is inert — no module under `xibi/` reads the `access` key
   and `xibi/tools.py:18` sets `DEFAULT_TIER = GREEN` with `search_tavily` absent
   from `TOOL_TIERS` — leave it as-is; (b) the "prefer search_tavily" wording in
   the `search_searxng` description is an outage-driven preference with no
   automatic expiry and should be revisited when the SearXNG engine set recovers.

8. Apply three verification corrections during implementation. (a) Extend Tests
   Required #6 with one `_slim_results` case whose single snippet exceeds 300
   characters and one whose combined entries exceed 1000, asserting truncation at
   `_SNIPPET_CHARS` and the `_MAX_TOTAL_CHARS` break
   (`skills/search/tools/search_tavily.py:37,41`); call `_slim_results` directly
   rather than `run()`, which is gated on `TAVILY_API_KEY` at `:67-75`. (b) In the
   PR body, record the rollback as
   `git revert --no-commit <first-sha>..<last-sha> && git commit` for a
   multi-commit branch (CLAUDE.md mandates `--ff-only`, so there is no merge
   commit to revert), and add the escalation line
   `[DEPLOY VERIFY FAIL] step-140 — <1-line what failed>` plus the gate
   consequence. (c) Treat the DoD line "PDV runtime, observability, failure-path
   and rollback executed and recorded" as satisfied by executing the runtime and
   observability checks and pasting the rollback commands verbatim into the PR —
   do not execute the rollback unless a check fails.

**Inline fixes applied during review:** none. The spec body was not modified; this
Record is purely additive.

**Confidence:**
- Contract: High — the error shape, the `unresponsive_engines` structure, the
  description-delivery path and the "no tool name in Python" constraint were all
  verified against live code and the live instance.
- Tests: Medium — six tests are the right set, but #5 is unsatisfiable as worded,
  the monkeypatch fixture shape is unspecified in a way I demonstrated is a real
  trap, and #6 does not cover the branch it claims. Conditions 3, 4 and 8(a)
  close it.
- PDV: Medium — the commands run verbatim and have named signals, but the
  observability check is untriggerable as written and the manifest half has no
  deployed-state proof. Conditions 1 and 2 close it.
- Observability: Medium — the log line and its limitation are accurate and
  empirically confirmed, but the section answers only the template's question 2
  (no span, dashboard, or failure-visibility answer) and the promised line has no
  reliable verification command until condition 1 is applied.
- Constraints/DoD: High — constraints are tight and correct, the rule 8 hotfix
  assessment is right (the manifest edit is an LLM-facing surface change and is
  not hotfix-eligible), and the DoD items are mechanically verifiable apart from
  the rollback wording in 8(c).

**Independence:** This TRR was conducted by a fresh Opus context in Cowork with no
draft-authoring history for step-140.
