# Voice / quick-add for checklists

> **Status:** Parked note (not a spec).
> **Parked:** 2026-04-09 during step-65 brainstorm.
> **Source:** step-65 open question Q5.
> **Promote when:** A voice / natural-input adapter layer is being
> specced independently. Quick-add is a client of that layer, not a
> feature of the checklist primitive.

## Why this is out of scope for step-65

"Say 'Xibi, add milk to the grocery list' and have it land as a
checklist item" sounds like a checklist feature, but it isn't. It's an
input-ingestion feature that happens to write to checklists. The
actual work is on the input side: speech-to-text, wake word handling,
an intent parser that distinguishes "add to a checklist" from "add to
a note" from "add to a calendar event," and an ambiguity-resolution
flow when the parse is uncertain.

If step-65 grew voice support directly, every other Xibi primitive
(notes, calendar, memory) would need its own voice support too, and
the voice-handling logic would end up copy-pasted everywhere. The
right factoring is one voice adapter that routes parsed intents to
the right downstream primitive.

## What step-65 does that makes this possible later

Step-65 exposes `update_checklist_item` and `create_checklist_template`
as tools that go through the existing ReAct loop. A voice adapter,
when it lands, parses the user's utterance into a tool call and runs
it through ReAct like any other request. No changes to the checklist
tables or tools are needed — voice is a new *surface* on top of the
same tool surface, not a new primitive inside the checklist module.

The Telegram free-text routing already in step-65 scope ("done with
the email one" → ReAct → `update_checklist_item` with a label hint)
is the dry run for the same pattern. If the Telegram path works well,
the voice path is the same pipeline with a different input source.
