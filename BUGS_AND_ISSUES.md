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
| BUG-006 | **react.py narrow error detection** | 2026-03-28 | 🔴 Open | `step.error` only set when `_xibi_error` key present (`react.py` line 337). Tools returning `{"error":"..."}` or `{"status":"error","message":"..."}` silently miss error tracking. Fix: broaden check per step-17 spec Bug 2. |
| BUG-007 | **step.duration_ms excludes tool time** | 2026-03-28 | 🔴 Open | `step.duration_ms` set at Step construction (before dispatch), so tool execution time excluded from trace spans. Fix: add `step.duration_ms = int((time.time() - step_start_time) * 1000)` after `step.tool_output = tool_output`. |
| BUG-008 | **telegram.py uses non-existent self.core.process_query()** | 2026-03-28 | 🔴 Open | `TelegramAdapter._handle_text()` calls `self.core.process_query()` which does not exist. Telegram channel is broken at runtime. Fix: refactor to use `react_run()` directly per step-17 spec Bug 4. |

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
