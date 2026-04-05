# step-49 — Job Search Signal Intelligence (Phase D, Step 1)

> **Depends on:** step-48 (Multi-Source Framework — merged)
> **Blocks:** Phase D Step 2 (job search goals, preference learning, outreach skills)
> **Scope:** Wire JobSpy poll results through signal intelligence, deduplicate job signals,
> create job threads, implement job-specific extractor, validate end-to-end job → thread → nudge.

---

## Why This Step Exists

Step-48 connected JobSpy as a polling source and built the generic signal extractor that
sets `needs_llm_extraction: True` for MCP tool results. But the generic extractor is a
placeholder — it produces undifferentiated signals with no job-specific structure.

The job search use case needs:
1. **Job deduplication** — same job posting should not create a new signal on every tick
2. **Job-specific signal structure** — role, company, location, salary are first-class fields
3. **Thread-per-company model** — all signals from "Stripe" belong to one thread
4. **Relevance gating** — not every job in the JobSpy results is worth surfacing; roles must
   match a configured search profile
5. **End-to-end validation** — a new job match should produce a nudge to Telegram

Without this step, job signals pile up as raw text blobs with no dedup, no structure, and
no connection to the observation cycle's action layer.

---

## Gap 1: Job Signal Extractor

### Problem

`extract_generic_signals()` produces one signal per poll call with `raw: <entire result>`.
For job search, one poll call returns N job listings. Each listing needs its own signal
with structured fields so signal intelligence can deduplicate and thread-match.

### Fix

Register a `"jobs"` extractor in `SignalExtractorRegistry`:

```python
@SignalExtractorRegistry.register("jobs")
def extract_job_signals(source: str, data: Any, context: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract one signal per job listing from JobSpy MCP tool results.

    data is expected to be the result dict from search_jobs:
    {
        "status": "ok",
        "result": "<text>",
        "structured": {   # from step-48 MCP structured content
            "jobs": [
                {
                    "id": "<unique job ID>",
                    "title": "Product Manager",
                    "company": "Stripe",
                    "location": "Miami, FL",
                    "salary_min": 130000,
                    "salary_max": 160000,
                    "url": "<job posting URL>",
                    "posted_at": "2026-04-03",
                    "description_snippet": "<first 300 chars>"
                },
                ...
            ]
        }
    }

    If structured content is absent (server does not support it), fall back to
    parsing the text result with a fast-role LLM call (flagged with
    needs_llm_extraction=True and extractor_hint="jobs").
    """
    structured = None
    if isinstance(data, dict):
        structured = data.get("structured")

    if not structured or "jobs" not in structured:
        # Fallback: generic extraction with extractor hint for signal intelligence
        return [{
            "source": source,
            "type": "job_batch",
            "raw": data.get("result", str(data)) if isinstance(data, dict) else str(data),
            "needs_llm_extraction": True,
            "extractor_hint": "jobs",
            "content_preview": "Job search results (unstructured)",
        }]

    signals = []
    for job in structured.get("jobs", []):
        job_id = str(job.get("id", ""))
        company = _normalize_company(job.get("company", "Unknown Company"))
        title = job.get("title", "Unknown Role")
        location = job.get("location", "")
        salary_range = ""
        if job.get("salary_min") and job.get("salary_max"):
            salary_range = f"${job['salary_min']:,}–${job['salary_max']:,}"

        signals.append({
            "source": source,
            "type": "job_listing",
            "entity_text": company,
            "entity_type": "company",
            "topic_hint": f"{title} at {company}",
            "content_preview": f"{title} | {company} | {location}{' | ' + salary_range if salary_range else ''}",
            "ref_id": job_id,
            "ref_source": "jobspy",
            "metadata": {
                "job": job,
                "title": title,
                "company": company,
                "location": location,
                "salary_min": job.get("salary_min"),
                "salary_max": job.get("salary_max"),
                "url": job.get("url", ""),
                "posted_at": job.get("posted_at", ""),
            },
        })
    return signals


def _normalize_company(name: str) -> str:
    """Normalize company name for thread matching."""
    for suffix in [", Inc.", " Inc.", " LLC", " Ltd.", " Corp.", ", Corp.", " Co.", " AG", " SE", " PLC"]:
        name = name.replace(suffix, "")
    return name.strip()
```

Update config.example.json heartbeat source entry for jobs:
```json
{
    "name": "jobs",
    "type": "mcp",
    "server": "jobspy",
    "tool": "search_jobs",
    "args": {"query": "product manager Miami", "results_wanted": 10},
    "interval_minutes": 60,
    "signal_extractor": "jobs"
}
```

**Files:** `xibi/heartbeat/extractors.py` (add jobs extractor + _normalize_company), `config.example.json` (update jobs source entry signal_extractor field)

---

## Gap 2: Job Signal Deduplication

### Problem

The current signal dedup in signal intelligence is: same `(source, ref_id)` within a
tick window = skip. But job search polls every 60 minutes, and the same job listing
will reappear in every poll until it is no longer available. Without dedup, the same PM
role at Stripe creates a signal every hour for days.

### Fix

Add a dedup check in `xibi/signal_intelligence.py`:

```python
def is_duplicate_signal(
    ref_source: str, ref_id: str, db_path: Path, window_hours: int = 72
) -> bool:
    """
    Return True if a signal with this (ref_source, ref_id) was logged within window_hours.
    Default 72h window prevents job listing signal spam across multiple poll cycles.
    """
    if not ref_id:
        return False
    with open_db(db_path) as conn:
        cutoff = (datetime.utcnow() - timedelta(hours=window_hours)).isoformat()
        row = conn.execute(
            "SELECT id FROM signals WHERE ref_source = ? AND ref_id = ? AND timestamp > ?",
            (ref_source, ref_id, cutoff),
        ).fetchone()
        return row is not None
```

Integrate in `HeartbeatPoller.async_tick()` for non-email sources before logging:

```python
# In the standard signal logging path for non-email sources:
from xibi.signal_intelligence import is_duplicate_signal

for sig in raw_signals:
    if sig.get("ref_id") and is_duplicate_signal(
        sig.get("ref_source", ""), sig["ref_id"], self.db_path
    ):
        logger.debug(f"Dedup skip: {sig['ref_id']} from {sig.get('ref_source')}")
        continue
    self.rules.log_signal_with_conn(
        conn,
        source=sig["source"],
        topic_hint=sig.get("topic_hint"),
        entity_text=sig.get("entity_text"),
        entity_type=sig.get("entity_type", "unknown"),
        content_preview=sig.get("content_preview", ""),
        ref_id=sig.get("ref_id"),
        ref_source=sig.get("ref_source"),
    )
```

**Files:** `xibi/signal_intelligence.py` (add is_duplicate_signal), `xibi/heartbeat/poller.py` (integrate dedup in non-email signal path)

---

## Gap 3: Job Search Profile Configuration

### Problem

JobSpy's `search_jobs` takes a `query` parameter. Currently hardcoded as `"software engineer"`
in config.example.json. Daniel needs to configure target roles, locations, and salary
expectations without editing JSON every time.

### Fix

Add a `job_search` section to `config.example.json` (alongside the heartbeat config):

```json
{
    "job_search": {
        "enabled": true,
        "profiles": [
            {
                "name": "pm_miami",
                "query": "product manager",
                "location": "Miami, FL",
                "salary_min": 120000,
                "interval_minutes": 60
            }
        ]
    }
}
```

`SourcePoller` reads this to build dynamic args for JobSpy sources:

```python
# In SourcePoller._poll_source(), before dispatching MCP call:
if source.get("server") == "jobspy":
    job_profiles = self.config.get("job_search", {}).get("profiles", [])
    if job_profiles:
        profile = job_profiles[0]  # Multi-profile support is Phase D Step 2
        args = {
            "query": f"{profile['query']} {profile.get('location', '')}".strip(),
            "results_wanted": source.get("args", {}).get("results_wanted", 10),
        }
    else:
        args = source.get("args", {})
else:
    args = source.get("args", {})
```

**Files:** `xibi/heartbeat/source_poller.py` (profile-driven args for jobspy), `config.example.json` (add job_search section)

---

## Gap 4: Thread-Per-Company Model (Normalization Only)

### Problem

Job signals from different roles at the same company should thread together. If Stripe
posts 3 different PM roles, they should all go into a "Stripe" thread because signal
intelligence keys threads on entity_text. "Stripe, Inc." and "Stripe" must normalize
to the same string.

### Fix

The `_normalize_company()` function in Gap 1 handles this. No additional threading
logic needed — signal intelligence already threads by entity_text. The extractor's
job is consistent normalization.

**Files:** (covered in Gap 1)

---

## Gap 5: End-to-End Job Pipeline Validation

### Validation Scenarios

**Scenario A: New job match → notification**
1. JobSpy returns a PM role at Stripe in Miami, salary $140k
2. Tick → jobs extractor returns signal with entity_text="Stripe", ref_id=<job_id>
3. Dedup check: ref_id not in signals table → allow
4. Signal logged; signal intelligence creates or updates Stripe thread
5. Observation cycle → nudge: "New PM role at Stripe (Miami, $130k–$160k). Want me to pull the full posting?"

**Scenario B: Repeated job listing (dedup)**
1. Same Stripe PM role appears in next poll (60 min later)
2. Dedup: ref_id exists in signals within 72h → skip
3. No duplicate signal, no second nudge

**Scenario C: Multiple roles at same company**
1. Stripe posts 3 different PM roles across 3 polls
2. All get entity_text="Stripe" → all thread to the same Stripe thread
3. Observation cycle notices pattern → nudge: "3 open PM roles at Stripe"

**Acceptance criteria:**
- Scenario A: Telegram notification with title, company, salary range
- Scenario B: No duplicate signal (verify signal count in DB)
- Scenario C: All 3 signals in same thread (verify by thread_id)
- Jobs extractor handles both structured and unstructured JobSpy responses
- Dedup window default is 72h and configurable

**Files:** No new code — manual testing only

---

## What This Step Does NOT Include

- Job preference learning (matching against stated criteria). Phase D Step 2.
- Job application tracking (CRM-style). Phase D Step 3.
- Outreach skills (cold emails to hiring managers). Phase D Step 4.
- Multi-profile simultaneous search. Phase D Step 2.
- Resume matching. Phase D Step 2.

---

## Files to Create / Modify

| File | Change |
|------|--------|
| `xibi/heartbeat/extractors.py` | Add `jobs` extractor with structured/unstructured branches; add `_normalize_company()` |
| `xibi/heartbeat/source_poller.py` | Profile-driven args for JobSpy source |
| `xibi/signal_intelligence.py` | Add `is_duplicate_signal(ref_source, ref_id, db_path, window_hours=72)` |
| `xibi/heartbeat/poller.py` | Integrate dedup check in non-email signal path before logging |
| `config.example.json` | Update jobs source `signal_extractor` to `"jobs"`; add `job_search` profile section |
| `tests/test_job_extractor.py` | **New** — 6 job signal extraction tests |
| `tests/test_signal_dedup.py` | **New** — 5 signal deduplication tests |
| `tests/test_poller_job_profile.py` | **New** — 3 profile-driven source args tests |

---

## Test Plan

### Job Signal Extractor (6 tests)

1. `test_jobs_extractor_structured` — structured result → one signal per job, correct fields (entity_text=normalized company, topic_hint, ref_id, metadata.url)
2. `test_jobs_extractor_unstructured_fallback` — no structured content → single signal with needs_llm_extraction=True, extractor_hint="jobs"
3. `test_jobs_extractor_empty_jobs_list` — structured but empty jobs array → empty signals list
4. `test_jobs_extractor_normalizes_company_name` — "Stripe, Inc." → entity_text="Stripe"
5. `test_jobs_extractor_salary_range_included` — salary_min + salary_max → appears in content_preview
6. `test_jobs_extractor_missing_fields_handled` — job dict missing url/salary/posted_at → no KeyError, graceful defaults

### Signal Deduplication (5 tests)

7. `test_is_duplicate_signal_false_when_not_in_db` — new ref_id → returns False
8. `test_is_duplicate_signal_true_when_in_db` — existing ref_id within window → returns True
9. `test_is_duplicate_signal_false_when_expired` — existing ref_id outside window → returns False
10. `test_duplicate_signal_skipped_in_tick` — tick with duplicate job signal → signal not re-logged (verify signal count unchanged)
11. `test_dedup_ref_id_empty_passes_through` — empty ref_id → is_duplicate returns False (no dedup on un-identified signals)

### Profile-Driven Source Args (3 tests)

12. `test_poller_uses_job_search_profile_args` — job_search profile in config → query built from profile.query + profile.location
13. `test_poller_falls_back_to_source_args_without_profile` — empty profiles list → uses source.args unchanged
14. `test_poller_non_jobspy_source_unaffected` — profile config present but source.server != "jobspy" → uses source.args unchanged

---

## Notes for Jules

- The jobs extractor's structured branch relies on JobSpy returning MCP structured content (the `structuredContent` field from step-48). If JobSpy does not implement this, the fallback path runs. Both paths must be tested.
- Company normalization is intentionally simple — strip common legal suffixes. Do not use fuzzy matching or LLM calls for this. Simple string cleanup is sufficient. List of suffixes to strip: ", Inc.", " Inc.", " LLC", " Ltd.", " Corp.", ", Corp.", " Co.", " AG", " SE", " PLC".
- `is_duplicate_signal` queries by `ref_source` + `ref_id`, not by source name. `ref_id` is the stable identifier across polls (the job posting ID from JobSpy). `ref_source="jobspy"` scopes the dedup to job signals only — it does not interfere with email dedup which uses `ref_source="email"`.
- Profile-driven args: only override args when `server == "jobspy"`. Other MCP sources use their configured args as-is. Do not generalize this to a generic arg-override framework.
- The 72h dedup window is the default. Job listings typically cycle every 24-72h. 72h prevents signal spam while still catching reposts after a listing expires and reappears.
- Dedup happens BEFORE logging. Check → skip or log. Do not log then check.
- The thread-per-company model emerges from entity_text matching in signal intelligence. The extractor only needs to produce consistent normalized entity_text. Signal intelligence handles the actual threading. Do not implement threading logic in the extractor.
- Use `open_db()` from `xibi/db/__init__.py` in `is_duplicate_signal`. No bare `sqlite3.connect()`.
- `is_duplicate_signal` must not raise — catch all DB exceptions, log with exc_info=True, and return False (assume not duplicate if DB fails, to avoid blocking new signals on a DB error).
