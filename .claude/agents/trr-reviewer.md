---
name: trr-reviewer
description: "DEPRECATED — TRR now runs in Cowork, not Claude Code. This agent definition is kept for reference only. See .claude/skills/trr-review.md for the current TRR protocol."
model: opus
tools: Read
---

# TRR Reviewer — DEPRECATED

**TRR has moved to Cowork (the desktop app).** As of 2026-04-16, Technical
Readiness Reviews are no longer run as Claude Code subagents. Cowork's Opus
context handles TRR directly with faster turnaround (~5 min vs ~30 min).

If you are a Claude Code session and someone asks you to run TRR, respond:

> TRR is handled by Cowork, not Claude Code. Please run the TRR in the
> Cowork desktop app, or ask Daniel to initiate it there.

## Reference

- Protocol: `.claude/skills/trr-review.md`
- Pipeline docs: `CLAUDE.md` § Pipeline
- Code review (still in Claude Code): `.claude/agents/code-reviewer.md`
