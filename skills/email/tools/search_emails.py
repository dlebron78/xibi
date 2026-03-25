import sys
import os
import subprocess
import json
import shutil
from typing import Any, Dict, List


def _find_himalaya():
    himalaya_bin = shutil.which("himalaya")
    if not himalaya_bin:
        home = os.path.expanduser("~")
        for path in [f"{home}/.local/bin/himalaya", f"{home}/.cargo/bin/himalaya"]:
            if subprocess.run(["test", "-x", path]).returncode == 0:
                return path
        himalaya_bin = "himalaya"
    return himalaya_bin


# ---------------------------------------------------------------------------
# Keyword scoring — prefer specific terms, deprioritise broad ones
# ---------------------------------------------------------------------------
_BROAD_TERMS = {"email", "emails", "mail", "message", "messages", "update",
                "updates", "notification", "notifications", "alert", "alerts",
                "info", "information", "news", "newsletter"}


def _score_keyword(kw: str) -> float:
    """Higher = more specific.  Longer phrases and capitalised entities win."""
    score = 0.0
    # Multi-word phrases are more specific
    score += len(kw.split()) * 2.0
    # Capitalised words suggest proper nouns / entities
    if any(w[0].isupper() for w in kw.split() if w):
        score += 3.0
    # Penalise known broad terms
    if kw.lower().strip() in _BROAD_TERMS:
        score -= 5.0
    # Longer is generally more specific
    score += min(len(kw) / 10.0, 2.0)
    return score


def _rank_keywords(keywords: List[str], max_count: int = 3) -> List[str]:
    """Score, sort descending, return top max_count."""
    if not keywords:
        return []
    scored = sorted(keywords, key=_score_keyword, reverse=True)
    return scored[:max_count]


# ---------------------------------------------------------------------------
# Probe builder — OR within fields, AND across fields
# ---------------------------------------------------------------------------
MAX_PROBES = 3


def _build_probes(params: Dict) -> List[str]:
    """Convert intent slots into himalaya query strings.

    Strategy:
      - OR within a field  → one probe per keyword in that field
      - AND across fields  → each probe combines keywords from different fields
      - Subject + sender probes first, body only as fallback
      - Max 3 probes to bound latency
    """
    subject_kws = _rank_keywords(params.get("subject_keywords") or [])
    from_kws    = _rank_keywords(params.get("from_keywords") or [])
    body_kws    = _rank_keywords(params.get("body_keywords") or [])
    after_date  = params.get("after_date")

    probes = []

    # --- Primary probes: subject keywords × from keywords ---
    if subject_kws and from_kws:
        for s_kw in subject_kws:
            for f_kw in from_kws:
                parts = [f'subject "{s_kw}"', f'from "{f_kw}"']
                if after_date:
                    parts.append(f'after {after_date}')
                probes.append(" and ".join(parts))
                if len(probes) >= MAX_PROBES:
                    return probes

    # --- Subject-only probes (no from constraint) ---
    elif subject_kws:
        for s_kw in subject_kws:
            parts = [f'subject "{s_kw}"']
            if after_date:
                parts.append(f'after {after_date}')
            probes.append(" and ".join(parts))
            if len(probes) >= MAX_PROBES:
                return probes

    # --- From-only probes ---
    elif from_kws:
        for f_kw in from_kws:
            parts = [f'from "{f_kw}"']
            if after_date:
                parts.append(f'after {after_date}')
            probes.append(" and ".join(parts))
            if len(probes) >= MAX_PROBES:
                return probes

    # --- Body probes (lowest priority — expensive server-side) ---
    if len(probes) < MAX_PROBES and body_kws:
        for b_kw in body_kws:
            parts = [f'body "{b_kw}"']
            if after_date:
                parts.append(f'after {after_date}')
            probes.append(" and ".join(parts))
            if len(probes) >= MAX_PROBES:
                return probes

    # --- Fallback: date-only ---
    if not probes and after_date:
        probes.append(f'after {after_date}')

    return probes


# ---------------------------------------------------------------------------
# Probe execution — run each query, merge + dedup by ID
# ---------------------------------------------------------------------------

def _run_himalaya_query(himalaya_bin: str, query_str: str) -> List[Dict]:
    """Execute a single himalaya envelope query, return parsed results."""
    try:
        cmd = [himalaya_bin, "--quiet", "--output", "json", "envelope", "list", query_str]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)

        if result.returncode != 0 or (not result.stdout.strip() and result.stderr.strip()):
            return []

        if not result.stdout.strip():
            return []

        envelopes = json.loads(result.stdout)
        results = []
        for env in envelopes:
            sender = env.get("from", {})
            if isinstance(sender, dict):
                from_name = sender.get("name") or ""
                from_addr = sender.get("addr") or ""
            else:
                from_name = ""
                from_addr = str(sender)

            # Pre-format for LLM — avoids "Unknown" when model doesn't parse raw fields
            if from_name and from_addr:
                from_display = f"{from_name} <{from_addr}>"
            elif from_addr:
                from_display = from_addr
            elif from_name:
                from_display = from_name
            else:
                from_display = "Unknown"

            results.append({
                "id": env.get("id"),
                "from": from_display,
                "subject": env.get("subject"),
                "date": env.get("date")
            })
        return results

    except (json.JSONDecodeError, subprocess.TimeoutExpired, Exception):
        return []


def _execute_probes(himalaya_bin: str, probes: List[str], limit: int) -> tuple:
    """Run probes sequentially, merge results, dedup by envelope ID.

    Returns (results_list, probes_executed_list).
    """
    seen_ids = set()
    results = []
    probes_run = []

    for query in probes:
        hits = _run_himalaya_query(himalaya_bin, query)
        probes_run.append({"query": query, "hits": len(hits)})
        for email in hits:
            eid = email.get("id")
            if eid and eid not in seen_ids:
                seen_ids.add(eid)
                results.append(email)
            if len(results) >= limit:
                return results, probes_run

    return results, probes_run


# ---------------------------------------------------------------------------
# Main entry point — backward-compatible with old string params
# ---------------------------------------------------------------------------

def run(params: Dict) -> Dict[str, Any]:
    """Search emails using intent slots.

    New schema (preferred):
      subject_keywords: list[str]   — ANY match returns email
      body_keywords:    list[str]   — ANY match returns email
      from_keywords:    list[str]   — ANY match returns email
      relative_time:    str         — semantic date token
      unread_only:      bool        — (reserved, not yet wired)
      limit:            int         — max results (default 10)

    Legacy schema (backward-compatible):
      from_query:     str
      subject_query:  str
      body_query:     str
      after_date:     str (YYYY-MM-DD)
      before_date:    str (YYYY-MM-DD)
    """

    # --- Backward compatibility: convert old string params to arrays ---
    # Guard with isinstance to avoid crashing if the model passes a list for subject_query etc.
    for old_key, new_key in [("subject_query", "subject_keywords"),
                              ("from_query", "from_keywords"),
                              ("body_query", "body_keywords")]:
        val = params.get(old_key)
        if val is not None:
            if isinstance(val, list):
                # Model passed list-style to an old field — promote directly
                params.setdefault(new_key, val)
            elif isinstance(val, str) and val:
                params.setdefault(new_key, [val])

    # Ensure all keyword fields are lists, even if the LLM passed a bare string
    for key in ["subject_keywords", "from_keywords", "body_keywords"]:
        val = params.get(key)
        if isinstance(val, str) and val:
            params[key] = [val]

    subject_kws = params.get("subject_keywords", [])
    from_kws = params.get("from_keywords", [])
    body_kws = params.get("body_keywords", [])
    after_date = params.get("after_date")
    limit = params.get("limit", 10)

    if not any([subject_kws, from_kws, body_kws, after_date]):
        return {"status": "error", "message": "At least one search parameter must be provided."}

    # Build and execute retrieval probes
    probes = _build_probes(params)

    if not probes:
        return {"status": "error", "message": "Could not construct any search probes from the given parameters."}

    himalaya_bin = _find_himalaya()
    results, probes_run = _execute_probes(himalaya_bin, probes, limit)

    if not results:
        return {
            "status": "success",
            "count": 0,
            "probes": probes_run,
            "emails": [],
            "message": "No emails matched your search."
        }

    return {
        "status": "success",
        "count": len(results),
        "probes": probes_run,
        "emails": results[:limit]
    }
