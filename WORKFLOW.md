# Xibi Dev Workflow

## Spec Gating Rule

**Plan up to two steps ahead locally. Push a spec only when the preceding step is merged.**

```
step-N    [merged]  ← spec was pushed when step-(N-1) merged
step-N+1  [local]   ← spec written, not pushed yet
step-N+2  [local]   ← can sketch, not pushed yet
```

Rationale: specs evolve during implementation. Pushing early means continuous small pushes as edge cases surface. Keep drafts local, push once, push when it's final.

**The trigger to push a spec:** the step before it is merged to main.

## Step Lifecycle

```
draft (local)
  → pushed to tasks/pending/   ← preceding step merged
  → in-progress                ← Jules picks it up
  → tasks/done/                ← PR merged
```

## Definition of Done (per step)

- [ ] All files listed in the spec created or modified
- [ ] All required tests written and passing locally
- [ ] No hardcoded model names — always use `get_model()`
- [ ] PR opened with: summary, test results, any deviations from spec noted
- [ ] Spec moved to `tasks/done/`

## Code Hygiene

- All DB writes best-effort — never raise to caller
- Circuit breaker: `finally: _tables_ensured.add(db_key)` pattern — never retry DDL
- Tracing: every span emission wrapped in `try/except: pass` — tracing must never break the hot path
- Config values via `config.get()` — no hardcoded paths or model strings
