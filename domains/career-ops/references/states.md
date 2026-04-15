# Application States

Canonical status values for the track skill. No freeform statuses are accepted.

## Status Values

| Status | Meaning | Next Valid Statuses |
|--------|---------|---------------------|
| `Evaluated` | Posting has been scored by the evaluate skill. No application submitted yet. | Applied, Discarded, SKIP |
| `Applied` | Application submitted (online form, email, or referral). | Responded, Rejected, Discarded |
| `Responded` | Recruiter or hiring team has made contact (email, phone, LinkedIn). | Interview, Rejected, Discarded |
| `Interview` | At least one interview scheduled or completed. May have sub-stages (phone screen, technical, panel, final). | Offer, Rejected, Discarded |
| `Offer` | Written or verbal offer received. Negotiation may be in progress. | (terminal — Accepted, Declined are logged as notes, not separate statuses) |
| `Rejected` | Application or candidacy rejected at any stage. | (terminal) |
| `Discarded` | Daniel decided not to proceed — posting was a poor fit, company fell off radar, or role was withdrawn. Not a rejection from the company. | (terminal) |
| `SKIP` | Evaluated and immediately determined not worth applying to (score < 2.0 or explicitly unwanted). | (terminal) |

## Rules

1. Status transitions must follow the allowed paths in the table above. You cannot jump from `Evaluated` directly to `Interview`.
2. `Rejected` and `Discarded` are both terminal but semantically distinct: `Rejected` = company said no; `Discarded` = Daniel chose not to proceed.
3. `SKIP` is set automatically by the triage skill when a posting scores below 2.0. It can also be set manually.
4. When updating status to `Interview`, include a `stage` field in the entry: `"phone_screen"`, `"technical"`, `"panel"`, `"final"`, or `"other"`.
5. Status values are case-sensitive and must match exactly as shown above.

## Entry Schema

Each application entry in the tracker must contain:

```json
{
  "id": "unique string (company_slug-role_slug-YYYYMM)",
  "company": "Company Name",
  "role": "Job Title",
  "url": "Job posting URL or null",
  "status": "Evaluated",
  "score": 3.8,
  "updated_at": "ISO 8601 datetime",
  "notes": "Optional free text",
  "stage": null
}
```
