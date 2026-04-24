"""Regression guard for step-103: no ``bregger.db`` / ``.bregger`` filesystem
references may leak back into ``skills/``.

The five email handlers under ``skills/email/tools/`` previously hardcoded
``bregger.db`` as their SQLite filename; concatenated with the executor's
``_workdir`` of ``~/.xibi`` that yielded ``~/.xibi/data/bregger.db`` — a
file that does not exist — and the handlers silent-no-op'd. Step-103
migrated them all to ``xibi.db``. This test keeps future drift out.

Scope: ``skills/`` only. Other trees (notably ``xibi/`` and ``tests/``)
may still reference ``.bregger`` for env-var names or migration paths;
those are covered by separate spec tracks.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "skills"

# TRR condition 7 preserves the ``~/.bregger`` env-fallback default in
# handler files — so the regression guard narrowly targets the
# ``bregger.db`` SQLite filename literals step-103 actually migrated,
# matching the spec's "Step-specific gates" grep (``grep -rn
# "bregger.db" skills/``).
FORBIDDEN_PATTERNS = (
    re.compile(r'"bregger\.db"'),
    re.compile(r"'bregger\.db'"),
)


def _strip_comments(line: str) -> str:
    """Drop everything from the first ``#`` (naive — fine for this grep check
    since we only look for string literals, and a ``#`` inside a string would
    already make the full-line match noisy)."""
    idx = line.find("#")
    return line[:idx] if idx >= 0 else line


def test_no_bregger_db_references_in_skills():
    offenders: list[str] = []
    for path in SKILLS_DIR.rglob("*.py"):
        for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
            code = _strip_comments(raw)
            if any(p.search(code) for p in FORBIDDEN_PATTERNS):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {raw.strip()}")

    assert not offenders, "Forbidden bregger.db / .bregger literals reintroduced in skills/:\n" + "\n".join(offenders)
