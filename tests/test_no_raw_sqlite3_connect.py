"""Regression test: every connection inside ``xibi/`` flows through
``open_db()``.

step-120 routes all 29 known raw ``sqlite3.connect()`` call sites through
``xibi.db.open_db`` so that PRAGMAs (foreign_keys=ON, WAL, busy_timeout) are
enforced once at the choke point rather than re-applied per call. This grep
test guards that invariant: any new raw call into ``sqlite3.connect`` from a
file outside ``EXEMPT_FILES`` will fail this test.

EXEMPT_FILES is a module-level constant — adding a new bypass requires
editing this constant in a separate change, which surfaces the exemption
during code review (TRR condition 5).
"""

from __future__ import annotations

import re
from pathlib import Path

# Hard-coded exempt list — adding a new bypass is a conscious, reviewable
# act. Each entry must include a justification in the source file's
# comment/docstring explaining why the site bypasses open_db().
#
# Exempt rationale (see in-source comments):
#   xibi/db/__init__.py    — IS open_db().
#   xibi/db/migrations.py  — bootstraps the DB itself; runs before
#                            connection-level PRAGMAs are meaningful.
#   xibi/db/schema_check.py — uses :memory: + read-only file URI; both
#                             are incompatible with WAL/foreign_keys
#                             PRAGMAs.
EXEMPT_FILES: frozenset[str] = frozenset(
    {
        "xibi/db/__init__.py",
        "xibi/db/migrations.py",
        "xibi/db/schema_check.py",
    }
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_XIBI_PKG = _REPO_ROOT / "xibi"

# Match `sqlite3.connect(` as a real call (word boundary), not in a string
# literal that happens to contain the phrase. We strip simple ``"..."`` /
# ``'...'`` substrings before matching to keep docstring mentions from
# tripping the test.
_CONNECT_RE = re.compile(r"\bsqlite3\.connect\s*\(")
_DBLQUOTE_STRING = re.compile(r'"[^"\n]*"')
_SGLQUOTE_STRING = re.compile(r"'[^'\n]*'")
_TPLQUOTE_STRING = re.compile(r'"""[\s\S]*?"""', re.MULTILINE)


def _strip_strings(line: str) -> str:
    """Remove simple string literals from a line before grepping.

    Keeps the test from firing on ``f"... sqlite3.connect ..."`` style
    docstring mentions while still catching real call sites.
    """
    line = _TPLQUOTE_STRING.sub("", line)
    line = _DBLQUOTE_STRING.sub("", line)
    line = _SGLQUOTE_STRING.sub("", line)
    return line


def _strip_line_comment(line: str) -> str:
    """Drop everything after the first ``#`` outside a string."""
    # We've already stripped strings, so the first ``#`` in the surviving
    # text is a real comment marker.
    idx = line.find("#")
    return line if idx < 0 else line[:idx]


def test_no_raw_sqlite3_connect_outside_exempt_list() -> None:
    """Fail if any non-exempt ``xibi/*.py`` file calls ``sqlite3.connect`` directly."""
    offenders: list[str] = []
    for py_file in _XIBI_PKG.rglob("*.py"):
        rel = py_file.relative_to(_REPO_ROOT).as_posix()
        if rel in EXEMPT_FILES:
            continue
        text = py_file.read_text()
        # Drop triple-quoted blocks first so docstrings don't trigger the
        # match. Done over the whole file to handle multi-line docstrings.
        text = _TPLQUOTE_STRING.sub("", text)
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = _strip_strings(line)
            stripped = _strip_line_comment(stripped)
            if _CONNECT_RE.search(stripped):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Raw sqlite3.connect() found outside EXEMPT_FILES. Route the call "
        "through xibi.db.open_db() instead. If a genuine exemption is "
        "required, add the file to EXEMPT_FILES in this test (separate "
        "change, reviewable). Offenders:\n  " + "\n  ".join(offenders)
    )


def test_exempt_files_actually_exist() -> None:
    """Guard against drift: every entry in EXEMPT_FILES must point at a real file."""
    for rel in EXEMPT_FILES:
        path = _REPO_ROOT / rel
        assert path.is_file(), f"EXEMPT_FILES entry {rel!r} does not exist"
