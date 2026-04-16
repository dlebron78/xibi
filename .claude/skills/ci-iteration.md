# CI Iteration

The tight loop between push and green. Claude Code main session handles
this — no subagent needed, no handoff to Cowork.

---

## When to invoke

- You just pushed a branch or a commit to `main`.
- CI is running or has finished.

Trigger: after every `git push`, or user says "check CI on step-X."

---

## Loop

```
1. gh pr checks <PR>           # or: gh run list --branch <branch> --limit 1
2. If pending → wait (poll every 30s for up to 10min)
3. If success → proceed to code review stage
4. If failure → gh run view <run-id> --log-failed
5. Read failure output carefully
6. Classify: test failure, lint, type error, build error, infra
7. Fix in source, commit, push
8. GOTO 1
```

Use `gh` CLI throughout. It's auth'd on the Mac.

---

## Classification + response

| Failure class | How to fix |
|---|---|
| Test failure (assertion) | Read the test, read the code, fix the code (or fix the test if test was wrong per spec). Commit with message `fix: <what>`. |
| Test failure (flake) | First rerun CI. If flakes again, escalate to telegram. |
| Lint/format | Run the formatter locally, commit `style: <fix>`. |
| Type error | Fix the type annotation or the code. Don't add `# type: ignore` casually — only if the type checker is genuinely wrong. |
| Import error / missing dep | Check `pyproject.toml` / `requirements.txt`. If a new dep is needed, add it and commit. |
| Build / environment | Likely infra. Escalate. |
| Migration / DB | Read the migration carefully. If this is a BUG-009 class issue, check `xibi/db/migrations.py` for proper `_safe_add_column` usage. |

---

## Escalation thresholds

Send telegram when:

- **Same failure class 3x in a row.** You tried three fixes for the same
  test/lint/etc and it keeps failing. You're stuck.
- **Flake.** A test fails then passes on rerun. Note it, let user decide
  whether to investigate or quarantine.
- **Infra.** The runner itself is broken, secrets missing, external
  dependency unreachable. Not a code problem.
- **Test contradicts spec.** You find a test that appears to be asserting
  the wrong behavior (e.g., test expects behavior the spec forbids).
  Escalate rather than "fix" by changing the test.

Telegram format:
```
[CI STUCK] step-X — <error class>: <1-line detail>
Last attempt: <commit SHA>. Tried: <list of approaches>.
```

---

## Time budget

If you've been in the CI loop for >45 minutes on a single step without
green, stop and escalate. Implementation that needs that much CI fighting
usually has a deeper issue that telegram escalation or Cowork revision
will solve faster than another fix attempt.

---

## Anti-patterns

- **Silencing tests.** If a test is failing, fix the code, not the test
  (unless the test itself is provably wrong per the spec).
- **Adding `# noqa` or `# type: ignore` to make CI pass.** Only acceptable
  when the tool is genuinely wrong and you can explain why in a comment.
- **Rebasing to "clean up" during CI loop.** Keep the commit history
  honest — "fix CI" commits are fine and will squash-merge later if needed.
- **Force-pushing without checking.** Especially if the branch is shared
  with review subagent context (rare but possible).
