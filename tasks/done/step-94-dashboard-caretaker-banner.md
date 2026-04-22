# step-94: Dashboard caretaker health chip

## Objective

Surface caretaker state at a glance on the existing dashboard index
page (`/`). Add a compact health chip to the existing `#health-chips`
container in `templates/index.html` that shows "last pulse N min ago"
and active-drift count, color-coded green / yellow / red, and links to
`/caretaker` for details. Pure frontend addition — no new backend
endpoints, no new tables, no new config.

This is a step-92 follow-up. v1 Caretaker already emits everything
needed (`/api/caretaker/pulses`, `/api/caretaker/drift`, `/caretaker`
page). This spec just makes caretaker state visible without requiring
Daniel to navigate to `/caretaker` during normal dashboard use.

## User Journey

1. Daniel opens `http://nucbox.local:8082/` (or the Tailscale equivalent).
2. Page renders with the existing header. Within ≤1 second of load,
   the new caretaker chip populates in the `#health-chips` row
   alongside any other chips that live there today.
3. Three visual states:
   - **Green** (clean, recent): `🟢 Caretaker · 2m ago` — most recent
     `caretaker_pulses` row is within 2× pulse interval (≤30 min) and
     `caretaker_drift_state` has zero active rows.
   - **Yellow** (drift): `🟡 Caretaker · 1 drift` — one or more active
     drift rows (`accepted_at IS NULL`). Tooltip on hover lists
     dedup_keys.
   - **Red** (silent): `🔴 Caretaker · silent 47m` — most recent pulse
     is older than 2× pulse interval, or `/api/caretaker/pulses`
     returns an error. This is the meta-monitoring signal: if
     Caretaker itself is dead, the chip says so.
4. Click the chip → navigate to `/caretaker`.
5. Chip auto-refreshes every 60 seconds without a full page reload.

## Real-World Test Scenarios

### Scenario 1: Green state — healthy caretaker

**What you do:** Open `/` on NucBox dashboard. Caretaker service is
running normally; zero active drift.

**What you see:** Chip shows `🟢 Caretaker · Xm ago` where X ≤ 15.

**How you know it worked:** Chrome DevTools Network tab shows
`/api/caretaker/pulses?limit=1` returning 200 with a pulse row; chip
text matches `started_at` delta.

### Scenario 2: Yellow state — active drift

**What you do:** Trigger config drift —
`ssh ... "echo '# drift' >> ~/.xibi/config.json"`. Wait one pulse
cycle (15 min). Refresh `/`.

**What you see:** Chip flips to yellow, reads
`🟡 Caretaker · 1 drift`. Hover tooltip shows
`config_drift:config.json`.

**How you know it worked:** `/api/caretaker/drift` returns one row;
chip count matches.

**Revert:** `xibi caretaker accept-config ~/.xibi/config.json`, wait
one pulse, chip returns to green.

### Scenario 3: Red state — caretaker silent

**What you do:**
`ssh ... "systemctl --user stop xibi-caretaker.service xibi-caretaker.timer"`.
Wait 35 min (2× pulse interval + margin). Refresh `/`.

**What you see:** Chip goes red: `🔴 Caretaker · silent 34m`. Tooltip
says "Last pulse at YYYY-MM-DD HH:MM:SS; threshold 30 min."

**How you know it worked:** Even though Caretaker itself is dead and
cannot telegram from its own dashboard, the chip's red state closes
the visibility loop — you'll notice on the next dashboard open.

**Revert:** `ssh ... "systemctl --user start xibi-caretaker.timer"`.
Wait one pulse, chip returns to green.

### Scenario 4: API 404 — graceful degradation

**What you do:** Deploy step-94 ahead of step-92 (hypothetical; should
not happen given spec gating, but test the degradation anyway). Load
`/`; `/api/caretaker/pulses` returns 404 because endpoints don't exist
yet.

**What you see:** Chip is hidden entirely (not broken, not showing an
error). No console error visible to the user; the fetch catches the
404 and sets `display: none` on the chip.

**How you know it worked:** Rest of dashboard renders normally; no
JS errors break the page.

### Scenario 5: Auto-refresh

**What you do:** Open `/`. Observe chip is green with "2m ago." Leave
the tab open for 16 minutes (one pulse interval + 1 min).

**What you see:** Chip updates in place — "2m ago" → progressively
updates → flips back to "1m ago" or similar after the next pulse
lands. No full page reload; other panels unaffected.

**How you know it worked:** DevTools Network tab shows
`/api/caretaker/pulses?limit=1` firing every 60 seconds.

## Files to Create/Modify

**Modify:**

- `templates/index.html` — (a) add one `<a id="caretaker-chip" ...>`
  element inside the existing `#health-chips` div, initially hidden;
  (b) append a small JS block (inline `<script>` to match current
  conventions — the file already does Tailwind CDN + inline handler
  style) that fetches `/api/caretaker/pulses?limit=1` and
  `/api/caretaker/drift` in parallel on page load, sets chip text and
  color based on state, re-runs every 60 seconds. Keep it ≤60 lines
  of JS.

**Not creating new files:** No new template partial, no new Python,
no new CSS file. Everything inlines into `index.html` to match the
existing pattern (the current file is a single self-contained HTML
document with inline Tailwind + Chart.js + JS).

**Not modifying:** `xibi/dashboard/app.py`. The banner is a pure
consumer of endpoints step-92 already creates. If Claude Code finds
itself editing `app.py`, it has scope-drifted — escalate.

## Database Migration

N/A — pure frontend feature, no schema change.

## Contract

```javascript
// Pseudocode of the chip state resolution logic (implement in JS)
async function refreshCaretakerChip() {
    try {
        const [pulsesRes, driftRes] = await Promise.all([
            fetch('/api/caretaker/pulses?limit=1'),
            fetch('/api/caretaker/drift'),
        ]);
        if (!pulsesRes.ok || !driftRes.ok) {
            // Step-92 not yet deployed, or endpoints broken
            document.getElementById('caretaker-chip').style.display = 'none';
            return;
        }
        const pulses = await pulsesRes.json();
        const drift = await driftRes.json();

        const lastPulse = pulses[0];  // or pulses.pulses[0] depending on step-92's shape
        const ageMin = Math.round((Date.now() - new Date(lastPulse.started_at).getTime()) / 60000);
        const activeDrift = drift.filter(d => d.accepted_at === null).length;

        const chip = document.getElementById('caretaker-chip');
        if (ageMin > 30) {
            chip.className = 'caretaker-chip caretaker-chip-red';
            chip.textContent = `🔴 Caretaker · silent ${ageMin}m`;
        } else if (activeDrift > 0) {
            chip.className = 'caretaker-chip caretaker-chip-yellow';
            chip.textContent = `🟡 Caretaker · ${activeDrift} drift`;
        } else {
            chip.className = 'caretaker-chip caretaker-chip-green';
            chip.textContent = `🟢 Caretaker · ${ageMin}m ago`;
        }
        chip.style.display = '';  // show it
    } catch (err) {
        document.getElementById('caretaker-chip').style.display = 'none';
    }
}

// On load + every 60s
window.addEventListener('load', refreshCaretakerChip);
setInterval(refreshCaretakerChip, 60_000);
```

**API shape assumptions (verify against step-92 implementation before
writing the JS):**

- `GET /api/caretaker/pulses?limit=1` returns a JSON array (or object
  with `pulses` key — step-92 contract does not pin this down; Claude
  Code must check the actual step-92 impl and wire accordingly).
- `GET /api/caretaker/drift` returns a JSON array of drift rows, each
  with at least `dedup_key`, `accepted_at` (ISO string or null),
  `check_name`, `severity`.
- Timestamps are ISO 8601 strings parseable by `new Date()`.
- If either endpoint returns 4xx/5xx, chip hides gracefully.

## Observability

N/A — this spec adds no new spans, logs, or metrics. Caretaker itself
is the observed system; this chip is the operator's view into it.

## Post-Deploy Verification

### Schema verification

N/A — no DB migration.

### Runtime state

Dashboard serves `/` and chip renders:

```bash
ssh dlebron@100.125.95.42 "curl -sS http://localhost:8082/ | grep -c 'caretaker-chip'"
# Expected: ≥ 1 (chip element present in served HTML)
```

Dashboard endpoints still live (no step-92 regression):

```bash
ssh ... "curl -sS http://localhost:8082/api/caretaker/pulses?limit=1 | head -c 80"
ssh ... "curl -sS http://localhost:8082/api/caretaker/drift | head -c 80"
# Expected: JSON in both cases; no 500s.
```

### Observability verification

N/A.

### Failure-path verification

Exercise Scenario 3 on NucBox: stop caretaker, wait 35 min, open `/`,
confirm chip is red with appropriate silence duration. Record the
observed red-state message in the step-94 done-file. Then restart
caretaker and confirm chip returns to green within one pulse cycle.

### Rollback

Pure `git revert <merge sha>` on Mac + `git push origin main`. NucBox
picks up the revert, `templates/index.html` reverts to the pre-step-94
state, chip disappears. No DB, no systemd, no config — nothing to
unwind beyond the file change.

## Constraints

- **No backend changes.** This spec modifies exactly one file:
  `templates/index.html`. If Claude Code finds itself editing
  `xibi/dashboard/app.py`, `xibi/caretaker/*`, or adding a new route,
  it has scope-drifted and must escalate before continuing.
- **No new API endpoints.** Reuse step-92's `/api/caretaker/pulses`
  and `/api/caretaker/drift`. If those endpoints don't provide a
  needed field, that's a step-92 gap — fix there (via a separate
  spec), not by extending step-94.
- **No new dependencies.** Use `fetch()` and vanilla DOM — the page
  already loads Tailwind via CDN and Chart.js; no new libraries.
- **Graceful degradation is mandatory.** If the API returns 4xx/5xx
  or the fetch throws (network error), chip hides — must not render
  as broken or dump an error visible to the user. `catch` block sets
  `display: none`.
- **Auto-refresh at 60s, no backoff.** If the API starts erroring on
  refresh N, chip hides on that refresh and tries again at N+60s. No
  exponential backoff, no retry-after handling. Caretaker's own
  systemd `OnFailure=` hook is the authoritative alerting path; this
  chip is a secondary convenience.
- **No localStorage or sessionStorage.** State is derived from the
  API on every refresh; no client-side persistence.
- **Thresholds match step-92 config.** The "silent" threshold (30
  min in Scenario 3) must be derived from `2 × pulse_interval_min`.
  v1 pulse interval is 15 min, so threshold is 30. If step-92's
  `pulse_interval_min` changes, this threshold must be kept in sync —
  noted in the JS as `const SILENT_THRESHOLD_MIN = 30;` with a
  comment pointing to the step-92 config constant.
- **No coded intelligence.** Chip shows raw facts (pulse age, drift
  count). Does not score, prioritize, or hide findings by type. The
  operator reads the facts and decides.

## Tests Required

No unit tests in this step. The dashboard has no JS test
infrastructure in the repo today; adding one for a 60-line chip is
out of scope. Coverage comes from manual PDV (Scenarios 1, 2, 3 on
NucBox post-deploy).

If the existing dashboard grows any JS test scaffolding between now
and step-94 implementation, the reviewer should note the omission as
a follow-up TODO, not a blocker.

## TRR Checklist

- [ ] **Contract completeness.** JS logic pseudocode is explicit; API
  shape assumptions enumerated; state-resolution rules
  (green/yellow/red) stated in exact thresholds.
- [ ] **RWTS coverage.** Scenarios cover the three visual states plus
  graceful degradation and auto-refresh.
- [ ] **PDV specificity.** Commands are copy-pasteable, expected
  outputs named.
- [ ] **Scope discipline.** Constraints explicitly forbid backend
  changes, new endpoints, new dependencies, localStorage.
- [ ] **Step-92 dependency surfaced.** Spec gating footer names step-92
  as a merge prerequisite; Contract section flags API shape
  verification against the actual step-92 impl as Claude Code's job.
- [ ] **Meta-monitoring integrity preserved.** Red state in the chip
  complements (not replaces) step-92's systemd `OnFailure=` telegram.
  Reviewer must confirm the spec doesn't accidentally make the chip
  load-bearing for Caretaker-death detection.

## Definition of Done

- [ ] `templates/index.html` contains a `#caretaker-chip` element
  inside `#health-chips` and a ≤60-line JS block implementing the
  state-resolution logic above.
- [ ] On `/` page load, chip populates within 1s for the green path.
- [ ] Scenarios 1, 2, 3 validated on NucBox post-deploy; observed
  state messages recorded in step-94 done-file.
- [ ] Scenario 4 (graceful degradation) validated locally by stubbing
  the endpoints to 404 (or running against a pre-step-92 branch).
- [ ] Auto-refresh fires every 60s verified via DevTools Network tab.
- [ ] No changes to `xibi/dashboard/app.py` or any file under
  `xibi/caretaker/`.
- [ ] `grep -rn "localStorage\|sessionStorage" templates/index.html`
  returns zero hits.
- [ ] Rollback proven: revert the merge commit locally, reload `/`,
  confirm chip is gone and page still renders.

---

> **Spec gating:** Step-92 (Caretaker watchdog) merged via PR #100
> on 2026-04-21 (sha `1104f84`); `/api/caretaker/pulses`,
> `/api/caretaker/drift`, the `/caretaker` page, and the
> `caretaker_pulses` / `caretaker_drift_state` tables are all live.
> Step-94 is now eligible for promotion. Parallel to step-93/95/96
> bregger-migration sequence (no ordering dependency).

---

## TRR Record — Opus, 2026-04-21

This TRR was conducted by a fresh Opus context in Cowork with no
draft-authoring history for step-94 in this session. Pre-flight:
local HEAD matches `origin/main` (post-step-93 merge); step-92 is
in `tasks/done/`; `pending/` is empty.

**Verdict:** READY WITH CONDITIONS

**Summary:** The shape of the spec is sound — consume existing
step-92 APIs, render a compact chip, hide gracefully on errors, no
backend changes. Three concrete execution issues need directives
before implementation: (a) the chip cannot live inside `#health-chips`
as specified because the existing `refreshHealth()` rewrites that
container's `innerHTML` every 60s and would clobber the chip; (b)
the API response shapes in the pseudocode are wrong — both endpoints
wrap their arrays in a key (`pulses.pulses[]` and `drift.active[]`);
(c) `dedup.list_active()` despite its name returns all rows including
accepted, so the `accepted_at === null` filter is load-bearing.

**Findings:**

- **[C1] Chip element placed inside `#health-chips` will be wiped
  every 60 seconds.** The spec (Files to Create/Modify, bullet a)
  says to add `<a id="caretaker-chip" ...>` inside the existing
  `#health-chips` div. But `templates/index.html:250-268` implements
  `refreshHealth()` with `container.innerHTML = \`...\``, which
  overwrites the entire contents of `#health-chips` on load and
  every 60 seconds thereafter. The caretaker chip would appear
  briefly, get blown away on the first `refreshHealth()` tick, and
  from then on `document.getElementById('caretaker-chip')` returns
  null, causing `.style.display = 'none'` to throw TypeError. Fix:
  condition 1 below — place the chip in a sibling container, not
  inside `#health-chips`.

- **[C2] API response shapes in pseudocode do not match step-92's
  actual impl.** Verified against `xibi/dashboard/app.py`:
  - L411: `/api/caretaker/pulses` returns `jsonify({"pulses":
    ct.recent_pulses(limit=limit)})` — an object, `pulses` key.
  - L417: `/api/caretaker/drift` returns `jsonify({"active":
    _dedup.list_active(app.config["DB_PATH"])})` — an object,
    `active` key.

  The spec's pseudocode has `const lastPulse = pulses[0]` (should be
  `pulses.pulses[0]`) and `drift.filter(d => d.accepted_at === null)`
  (should be `drift.active.filter(...)`). The `pulses[0]` site has a
  comment flagging the ambiguity; the drift site doesn't. Both must
  be pinned in a condition so Sonnet doesn't guess.

- **[C2] `dedup.list_active()` returns accepted drift rows too.**
  Verified at `xibi/caretaker/dedup.py:100-122`: `list_active` runs
  `SELECT ... FROM caretaker_drift_state ORDER BY first_observed_at DESC`
  with no `WHERE accepted_at IS NULL` clause — every row is returned,
  with `accepted_at` as null or ISO string. The spec's filter
  `d.accepted_at === null` is therefore load-bearing, not decorative.
  If Sonnet "simplifies" it out, the yellow-state count will include
  already-accepted drift forever. Condition 2 covers this.

- **[C3] Tooltip content not specified in pseudocode.** Scenarios 2
  and 3 promise hover tooltips: "Tooltip on hover lists dedup_keys"
  (Scenario 2), "Tooltip says 'Last pulse at YYYY-MM-DD HH:MM:SS;
  threshold 30 min.'" (Scenario 3). The pseudocode only sets
  `textContent` and `className`. Simplest implementation:
  `chip.title = ...` for the HTML `title` attribute. Condition 3.

- **[C3] Scenario 4 framing is hypothetical.** "Deploy step-94 ahead
  of step-92" can't happen now — step-92 is merged. The *real*
  graceful-degradation trigger is endpoint 5xx during a Caretaker
  outage or brief dashboard restart. Implementer should treat
  Scenario 4 as "API unavailable → chip hidden," dropping the
  "pre-step-92 deploy" framing. No condition needed; inline note
  only.

- **[C3] `dedup.list_active` is misnamed — not a TRR finding but
  worth capturing.** Function is documented/named "active" but
  returns every row. This is codebase-wide, not step-94's problem;
  flagging so the "lightweight fix" temptation later doesn't break
  step-94.

**Conditions (READY WITH CONDITIONS):**

1. **Place the caretaker chip in a sibling container, not inside
   `#health-chips`.** In `templates/index.html`, add the chip
   element immediately adjacent to the existing `#health-chips`
   div — e.g., inside the same header flex row but in its own
   wrapper:
   ```html
   <div id="health-chips" class="flex gap-4">
       <!-- Populated by JS -->
   </div>
   <a id="caretaker-chip" href="/caretaker"
      class="bg-slate-800 px-3 py-1 rounded border border-slate-700 text-[10px] font-bold tracking-widest uppercase hidden"
      style="display:none;"
      title="">Caretaker</a>
   ```
   Do NOT place `#caretaker-chip` inside `#health-chips`.
   `refreshHealth()` at `templates/index.html:250-268` uses
   `container.innerHTML = \`...\`` and will overwrite everything
   inside `#health-chips` every 60s. The sibling placement keeps
   the caretaker chip untouched by that refresh.

2. **Use the correct API response shapes in the JS.** Pulses:
   `const pulsesJson = await pulsesRes.json(); const lastPulse =
   pulsesJson.pulses && pulsesJson.pulses[0];` (the endpoint wraps
   the array under the `pulses` key). Drift:
   `const driftJson = await driftRes.json(); const activeDrift =
   (driftJson.active || []).filter(d => d.accepted_at === null).length;`
   The `accepted_at === null` filter is load-bearing — keep it,
   because `dedup.list_active` returns accepted rows too. If
   `lastPulse` is falsy (no pulses ever recorded), treat as red /
   silent with "no pulses yet."

3. **Implement hover tooltips via the `title` attribute.** On
   yellow state: `chip.title = (driftJson.active || []).filter(d =>
   d.accepted_at === null).map(d => d.dedup_key).join('\n');` On
   red state: `chip.title = \`Last pulse at ${lastPulse ?
   lastPulse.started_at : 'never'}; threshold
   ${SILENT_THRESHOLD_MIN} min.\`;` On green: leave blank or set a
   minimal `chip.title = \`Last pulse ${ageMin}m ago.\`;`. This
   satisfies Scenarios 2 and 3's tooltip promises without adding
   any new libraries or CSS.

4. **Reframe Scenario 4's test trigger.** Step-92 is merged, so
   "deploy step-94 ahead of step-92" is no longer reachable. The
   same graceful-degradation behavior is verifiable by temporarily
   stopping the dashboard's caretaker-related dependency (e.g.,
   stop the caretaker service so pulses stop populating — endpoint
   still 200 with empty array) or by pointing the browser at a
   stub server that 404s both endpoints. Implementer should
   verify `chip.style.display = 'none'` is reached via both the
   4xx/5xx path AND the `throw` path (the `catch` block).

**Inline fixes applied during review:**

- "Spec gating" footer updated to reflect post-step-92 reality
  (merged via PR #100, sha `1104f84`; `/api/caretaker/*` endpoints
  and tables live).

**Confidence:**

- Contract: **Medium.** Pseudocode is wrong in two places (see C2)
  but the shape is recoverable with conditions 1–3; no
  architectural rethink.
- Real-World Test Scenarios: **High.** Scenarios 1–3 are concrete
  and exercise each color state; Scenarios 4–5 cover degradation
  and auto-refresh. Condition 4 tidies up Scenario 4's framing.
- Post-Deploy Verification: **High.** `curl` commands + network-tab
  checks + the failure-path exercise on NucBox all have named pass
  signals.
- Observability: **High.** `N/A — pure frontend` is honest.
- Constraints & DoD alignment: **Medium.** DoD bullet on
  "#caretaker-chip inside #health-chips" conflicts with condition
  1; if the implementer follows condition 1, that DoD line should
  read "inside the header flex row, adjacent to `#health-chips`"
  instead. Either condition 1's reword suffices or the DoD bullet
  is updated in the merge commit's final spec-to-done move.
