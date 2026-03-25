# UniversalRecord — Bregger Standard Retrieval View

> Standardize the view layer, not the ontology.
> URT is for retrieved/readable records, not actions.

---

## Core Fields (Always Present)

| Field | Type | Meaning |
|---|---|---|
| `id` | str | Unique per source |
| `source` | str | Origin: `email`, `calendar`, `web`, `ledger`, `whatsapp`, `imessage`, `file` |
| `record_type` | str | Semantic class: `message`, `event`, `memory`, `page`, `file`, `contact` |
| `title` | str | Email subject, event name, page headline, note label |
| `body` | str | Content, truncated to token budget by Python |
| `author` | str | `"Name <addr>"` or `"self"` |
| `sent_at` | str | ISO 8601. When the thing was created **in its source** |

## Extended Fields (Include When Available)

| Field | Type | When Used |
|---|---|---|
| `recipients` | str[] | Email to/cc, event attendees |
| `location` | str | Address, room, Zoom link |
| `starts_at` | str | Event/meeting start |
| `ends_at` | str | Event/meeting end |
| `due_at` | str | Task deadline, offer expiry |
| `status` | str | `read`, `unread`, `accepted`, `declined`, `pending`, `done` |
| `url` | str | Source link |
| `stored_at` | str | When Bregger cached this (Ledger items only) |

## Meta (Escape Hatch)

```json
"meta": {"flags": ["Seen"], "has_attachment": true, "cc": ["boss@co.com"]}
```

A field graduates from `meta` to extended when 3+ sources use it.

## Nullability

| Layer | Unknown value |
|---|---|
| Internal Python | `None` |
| Model-facing JSON | `""` (empty string) |

---

## Non-Goals

- URT is **not** the canonical storage model. SQLite rows, himalaya JSON, and API responses stay native internally
- URT is **not** required for action responses. `{status, message, data}` is fine for mutations
- URT is **not** a mandatory retrofit project. Apply when touching existing tools, require for new ones

---

## Implementation Path

1. **Now**: Apply to `summarize_email` and `recall` (already in progress)
2. **Forward**: Require URT for new retrieval tools (calendar, WhatsApp, etc.)
3. **Retrofit**: Only when touching existing tools for other reasons

---

## Backlog (Parked)

- **UniversalAction** — standardized mutation response envelope. Build when action responses cause actual confusion
- **UniversalTraceEvent** — standardized observability envelope. Build when traces table or logging needs a rethink
