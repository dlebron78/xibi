# Xibi — The Vision

## What It Is

**Xibi is an open-source AI agent framework built on two bets: that autonomous agents need structural security, and that intelligence comes from memory — not model size.**

It is not a chatbot. It is not a wrapper around an API. It is a **thinking, acting, remembering system** that monitors your world, takes action through tools, and learns what matters to you. It runs anywhere — a $300 mini PC, a cloud VM, a laptop — because the architecture doesn't depend on where it runs. It depends on *how it thinks*.

## The Problem It Solves

The first wave of AI agents proved the market exists. OpenClaw hit 345K GitHub stars in weeks. People want agents that *do things* — not just answer questions.

But the first wave also proved the architecture is broken. Nine CVEs in four days. A 9.9 CVSS privilege escalation. Agents autonomously making purchases, spamming contacts, exfiltrating data. Nvidia had to build a separate product (NemoClaw) just to sandbox what OpenClaw couldn't secure itself.

The core problem: **giving an LLM shell access and hoping it behaves is not a security model.** It's an absence of one. The more autonomous the agent, the more dangerous this becomes. Prompt injection, hallucinated commands, malicious skills — these aren't edge cases, they're the natural consequence of an architecture that treats the LLM as both the brain and the hands.

**Xibi takes the opposite bet.** The LLM gets full autonomy to think — reason, improvise, chain tools, decide what to do next. But it never touches the shell. It tells Python what it wants, and Python validates and executes through a registry of pre-approved tools with permission tiers.

> The model gets full autonomy to **think**. Python keeps full control over **doing**.

Security isn't a feature you bolt on after adoption. It's the architecture itself.

## The Second Bet: Memory Over Model Size

Most agent frameworks treat the LLM as a stateless reasoning engine. Ask a question, get an answer, forget everything. The "memory" is a conversation buffer that gets truncated.

Xibi treats memory as the primary intelligence layer. A unified signal stream watches everything — emails, calendar events, file changes, API responses, chat messages — and builds a persistent, cross-source model of what's happening in your world.

A good assistant doesn't need to be told what to remember. It **notices**. If you mention "the presentation" three times this week, it knows. If an email arrives about board meeting logistics, it connects the dots and escalates — because it already knows that topic is hot.

This means a small local model backed by Xibi's memory architecture can outperform a frontier cloud model running stateless. Intelligence isn't just reasoning — it's context.

## How Creative Execution Works Without Shell Access

OpenClaw's shell access isn't just a security hole — it's also what makes it flexible. The LLM can improvise: pipe commands together, write one-off scripts, solve problems nobody anticipated. That's real value.

Xibi doesn't sacrifice this. Instead of giving the LLM a raw shell, Xibi provides **structured creativity** through three mechanisms:

1. **Composable tool chains.** The ReAct loop lets the LLM chain any registered tools in any order. It can search the web, extract entities, cross-reference with calendar events, and fire a notification — all in one reasoning pass. The tools are validated; the composition is free.

2. **MCP extensibility.** Any capability can be added as an MCP server — a subprocess that speaks JSON-RPC. The LLM discovers available tools at runtime and uses them creatively. New capabilities don't require code changes to Xibi itself.

3. **Sandboxed code execution (planned).** A constrained execution environment where the LLM can propose code that runs in a validated sandbox — scoped filesystem access, no network, resource limits. The LLM gets creative problem-solving; Python enforces the boundaries. This is the OpenClaw flexibility with structural guarantees.

The principle: maximize the LLM's creative surface area while maintaining an auditable, permission-gated execution boundary.

## The Autonomy-Trust Matrix

A well-rounded agent doesn't just become more capable — it becomes more *trustworthy* as it becomes more capable. OpenClaw proved that autonomy without trust is dangerous.

Xibi tracks progress on two axes:

```
Trust Level
    ^
    |
T3  |                              +-----------+
    |                              | THE GOAL  |
T2  |                 +----------+ +-----------+
    |                 |  Xibi    |
T1  |  +-----------+  +----------+
    |  | Most      |
T0  |  | Agents    |  OpenClaw (high autonomy, low trust)
    |  +-----------+     *
    +-------------------------------------------> Autonomy
       L0          L1          L2          L3          L4
```

### Autonomy Levels (what the agent *can* do):

| Level | Name | Description |
|---|---|---|
| L0 | **Reactive** | Answer questions, execute commands on request |
| L1 | **Monitoring** | Background polling, email triage, rule-based alerts |
| L2 | **Aware** | Cross-references context, connects dots across sources |
| L3 | **Proactive** | Proposes and takes actions based on patterns, gated by approval |
| L4 | **Learning** | Suggests new rules from behavior, self-optimizes routing |

### Trust Levels (why you *should let it*):

| Level | Name | Description |
|---|---|---|
| T0 | **Unverified** | No permission model. LLM output goes directly to execution. |
| T1 | **Gated** | Permission tiers exist. RED actions require confirmation. Audit log. |
| T2 | **Provenance-Aware** | Every action traced to its origin. External content triggers tier bumps. Source tagging on all ingestion paths. |
| T3 | **Adversarial-Resilient** | Prompt injection defenses. Cross-agent isolation. Content scanning on skill inputs. Anomaly detection on action patterns. |

Xibi is currently at approximately **L1-L2 autonomy, T2 trust**. The roadmap advances both simultaneously. A PR that increases autonomy without maintaining trust level is a regression.

## The Architecture

```
┌──────────────────────────────────────────────────────────┐
│                       CHANNELS                            │
│     Telegram  ·  Email  ·  WhatsApp  ·  CLI  ·  API     │
└────────────────────────┬─────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────┐
│                   SIGNAL STREAM                           │
│  Sources (MCP + Native) → Extraction → Intelligence →    │
│  Threads → Observation Cycle                              │
│  Cross-source correlation · Entity resolution · Memory   │
└────────────────────────┬─────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────┐
│                   INTELLIGENCE                            │
│  Tier 1: Regex + Rules (instant)                          │
│  Tier 2: Embedding Similarity (ms)                        │
│  Tier 3: Local LLM (seconds, zero cost)                   │
│  Tier 4: Cloud LLM (on-demand, cost-tracked)              │
│  Graceful degradation: if a tier fails, fall to next      │
└────────────────────────┬─────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────┐
│               EXECUTION (Python Gate)                     │
│  Skill Registry · Permission Tiers (GREEN/YELLOW/RED)    │
│  Source Provenance · Audit Trail · CommandLayer           │
│  Tool Validation · Sandbox (planned)                      │
└────────────────────────┬─────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────┐
│                     MEMORY                                │
│  Unified Signal Table → Threads → Beliefs                 │
│  Working Memory · Active Threads · Long-Term Context     │
│  Observation Cycle → Cross-Source Awareness               │
└──────────────────────────────────────────────────────────┘
```

## Reference Deployments

Xibi's architecture is validated through reference deployments — real use cases that stress-test different capabilities:

**Chief of Staff** (L2 autonomy, T2 trust) — Monitors email, calendar, file changes, and any MCP source. Extracts signals, links them into threads, surfaces what matters. The agent that watches everything and tells you what needs attention. Tests: cross-source correlation, signal intelligence, observation cycle quality, proactive nudges.

**Job Search Assistant** (L1 autonomy, T1 trust) — Polls job boards via MCP, matches against a profile, tracks application threads. Tests: MCP source integration, long-running background tasks, profile-based filtering.

**Tourism Chatbot** (L0 autonomy, T2 trust) — Public-facing bilingual assistant with restricted tool access. Different security boundary from the owner-facing deployments. Tests: multi-agent isolation, permission tier enforcement for untrusted users, RAG over local knowledge base.

These are not the product — they are proof that the architecture generalizes. The same framework handles an owner's private executive assistant and a public-facing chatbot with completely different trust profiles.

## Future Target: Autonomous Development Pipeline

The ultimate test of Xibi's autonomy architecture: an agent that manages its own development. Reviews PRs against architectural criteria. Writes specs for the next feature. Dispatches work to execution agents. Monitors CI results. Merges when quality gates pass.

This isn't hypothetical — a version of this is already running via Cowork scheduled tasks driving the Xibi pipeline. The goal is to bring this capability into Xibi itself: a self-improving agent that maintains code quality and architectural coherence autonomously, demonstrating L3-L4 autonomy with T2-T3 trust.

## The Principles

1. **LLM Thinks, Python Does.** The model is free to reason, improvise, and plan. Python validates and executes. Security is structural, not hopeful.
2. **Local-Capable, Cloud-Capable.** Run on a $300 mini PC or a cloud VM. Local models handle routine work at zero cost. Cloud models are available for complex reasoning. The architecture doesn't care where inference happens.
3. **System Over Model.** If swapping models breaks a feature, the architecture is wrong. The system must work with any model, any size.
4. **Autonomy Requires Trust.** Every increase in autonomous capability must be matched by permission gating, source tracking, and audit trails. Autonomy without trust is a vulnerability.
5. **Memory Is Intelligence.** A small model with rich context outperforms a large model with none. The signal stream, threads, and observation cycle are the primary intelligence layer.
6. **Notice, Don't Ask.** The system passively observes what matters. It should never require you to act like a boss dictating instructions to a notepad.
7. **Open Source, Community-Extensible.** Skills, channels, MCP servers, and model backends are all pluggable. Security review is part of the extension model, not an afterthought.

## Who It's For

Developers and teams who want **real AI agency** — autonomous agents that monitor, reason, and act — without the security posture of "hope the LLM doesn't hallucinate rm -rf." People who've seen what OpenClaw can do and want the same power with an architecture they can actually trust in production.

## The North Star

When Xibi is done, you should be able to:

- Deploy an agent that monitors your email, calendar, files, APIs — any source — and proactively surfaces what matters.
- Trust it with escalating autonomy because every action is permission-gated, source-tracked, and auditable.
- Run it anywhere — mini PC, laptop, cloud VM — because the architecture is environment-agnostic.
- Extend it with community skills and MCP servers, knowing that the security model holds regardless of what plugins are installed.
- Run multiple agents with different trust profiles — a private executive assistant and a public-facing chatbot — in the same framework with strong isolation.
- Never worry about the CVE of the week, because the LLM never had shell access in the first place.
