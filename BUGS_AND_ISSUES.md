# Bregger — Bugs & Issues Tracker

This file tracks active system issues, bugs, and feature requests. 

---

## 🐞 Active Bugs

| ID | Issue | Detected | Status | Notes |
|---|---|---|---|---|
| BUG-001 | **Triple Email Send** | 2026-03-10 | 🔴 Open | Bot sends email immediately even when asked to "draft". Happens because `send_email` is the only tool available. |
| BUG-002 | **Fake Address Send** | 2026-03-10 | 🟡 Pending | Bot attempted to send a real email to `tamara@example.com`. Needs validation gate. |
| BUG-003 | **Filesystem Relative Path** | 2026-03-10 | 🟢 Fixed | `read_file` tools now use `BREGGER_WORKDIR` for relative paths. |
| BUG-004 | **Router Meta-Hallucination** | 2026-03-10 | 🟢 Fixed | Added `capability_check` Tier-1 intent to `KeywordRouter`. Self-referential queries now answered deterministically. |
| BUG-005 | **Content Sharing Loop** | 2026-03-10 | 🟢 Fixed | `generate_report` now collects all keys AND is smarter about missing human messages. No more "*No message provided*" ghosts. |

---

## 📋 Virtual Ledger Migration (Migrated from SQLite)

- **Task**: `Sent 3 emails to [email address or recipient name]` (Done)
- **Bug**: `Sent a real email to a fake address - Tamara@example.com` (Migrated to BUG-002)

---

## 🛠 Feature Requests

- [x] **Draft Support**: `draft_email` tool added.
- [ ] **Validation Layer**: Confirm recipient existence/type before bulk sending.
- [x] **Filesystem Support**: Bregger can read/write/append files.
- [x] **Capability Queries**: Control plane now answers "what tools do you have?" instantly.
