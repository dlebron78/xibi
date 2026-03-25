# Ray Lite → Full Ray: Complete Delta

## Orchestration & Routing

| Feature | Ray Lite | Full Ray |
|---|---|---|
| Intent routing | Regex only | Regex → Embedding similarity → LLM classification (cascade) |
| Unknown intent handling | Falls back to raw Ollama chat | Classifies urgency/type, routes accordingly |
| Multi-step reasoning | ❌ | ✅ LLM plans multi-step tasks, Python executes each step |
| Sub-agents | ❌ | ✅ Specialized agents per domain (email, properties, tasks) |
| LLM role | Polish only (optional) | Analyst + planner for complex/unknown intents |

## Memory & Learning

| Feature | Ray Lite | Full Ray |
|---|---|---|
| Chat history | SQLite flat log (2,000 char limit) | SQLite + vector index (searchable by meaning) |
| User facts | `[SAVE:]` tag extraction | Same + structured categories, confidence decay |
| Interaction log | ❌ | ✅ Every Q→A pair saved with outcome tracking |
| Gap detection | ❌ | ✅ Detects when host had to reply manually → training signal |
| Pattern surfacing | ❌ | ✅ Weekly LLM batch analysis of clustered interactions |
| Automation proposals | ❌ | ✅ "I've seen this 5x — want me to auto-reply?" |
| Confidence scoring | ❌ | ✅ Tracks if replies worked, adjusts routing thresholds |
| Embedding model | ❌ not used | ✅ `nomic-embed-text` for semantic similarity |
| Semantic search | ❌ | ✅ Match new messages to past answers by meaning, not text |

## Channels & Ingestion

| Feature | Ray Lite | Full Ray |
|---|---|---|
| Telegram | ✅ | ✅ |
| Email (himalaya) | ✅ | ✅ |
| WhatsApp | ❌ | ✅ (via Business API or bridge) |
| iMessage | ❌ | ✅ (Mac only, AppleScript) |
| SMS | ❌ | ✅ (Twilio or similar) |
| Airbnb messages | ❌ | ✅ via email forwarding (Airbnb sends email per message) |
| Multi-channel routing | n/a | ✅ unified message queue, channel-aware replies |

## Skills & Extensibility

| Feature | Ray Lite | Full Ray |
|---|---|---|
| Adding new tools | Edit main Python file | Drop-in `SKILL.md` + handler file |
| Domain knowledge base | ❌ | ✅ Per-entity FAQ store (e.g., per Airbnb property) |
| Browser automation | ❌ | ✅ Headless Chromium for web tasks |
| Calendar integration | ❌ | ✅ Check-in/out awareness |
| TTS / voice output | ❌ | ✅ Optional (HomePod, speaker) |
| Plugin system | ❌ all-in-one-file | ✅ Modular, loadable at startup |

## Safety & Approval

| Feature | Ray Lite | Full Ray |
|---|---|---|
| Tool allowlist | Hard-coded set in Python | Config-driven, per-agent, per-domain |
| Approval flow | ❌ blocking or auto | ✅ Telegram inline buttons (tap to approve/reject) |
| Audit log | ❌ | ✅ Every automated action logged with reason |
| Confidence threshold gates | ❌ | ✅ Low-confidence → escalate, don't guess |
| Escalation policy | All-or-nothing (alert or ignore) | ✅ Tiered: auto → suggest → alert → escalate |

## Deployment & Ops

| Feature | Ray Lite | Full Ray |
|---|---|---|
| Infra | Single Python process | Single Python process (same — no Docker required) |
| Config | Env vars | Env vars + YAML/JSON config per domain |
| Multi-user / multi-property | ❌ one user | ✅ Per-user, per-property context isolation |
| Heartbeat | Basic — email check + nudge | ✅ Smart — quiet hours, urgency classification, calendar-aware |
| Health monitoring | `/status` command | ✅ Self-reported + periodic Telegram pings |

---

## Summary

Ray Lite is **~40% of Full Ray**. The core Telegram + email + Ollama foundation is solid. The main engineering work is:

1. **Embeddings** — `nomic-embed-text` + similarity search (~50 lines)
2. **Interaction log** — SQLite table for every Q→A pair + outcome
3. **LLM classification** — structured label call, not open-ended chat (~30 lines)
4. **Pattern job** — weekly batch analysis + Telegram approval flow (~150 lines)
5. **Skill loader** — modular tool registration at startup
6. **Channel adapters** — abstract Telegram code, add WhatsApp/email ingestion

Everything else (multi-property, confidence scoring, calendar) is additive on top of those foundations.
