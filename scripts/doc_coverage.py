#!/usr/bin/env python3
"""Documentation coverage checker -- regression gate for the xibi/ tree.

step-126 introduces a documentation pass over a 33-file audit-touched
slice of the codebase. This script is the verification mechanism: it
walks every Python file under ``xibi/`` (or an explicit file list if
given on the command line), parses each one with the stdlib ``ast``
module, and reports any file missing a module-level docstring or any
function/method missing a docstring.

Why a baseline file
-------------------
step-126's narrative scope is 33 audit-touched files (~169 gaps). The
TRR added Condition 1: the script must scan *all* xibi/ files so it
serves as a CI regression gate for files added in future PRs. Those two
goals (close 169 gaps this PR, scan ~530 grandfathered gaps for
regression) only reconcile via a baseline.

``scripts/doc_coverage_baseline.txt`` (one tab-separated
``<path>\\t<gap-key>`` per line, ``#`` comments allowed) records the
gaps that exist on disk at step-126 merge time. Default-mode CI fails
only when a *new* gap appears that is not already in the baseline.
Future spec passes close baseline entries; the file shrinks until it is
empty, at which point ``--strict`` becomes the default.

Usage
-----
::

    python3 scripts/doc_coverage.py
        # Scan all xibi/**/*.py, compare to baseline, exit 0 iff every
        # current gap is grandfathered in the baseline. CI uses this.

    python3 scripts/doc_coverage.py --strict
        # Ignore the baseline; exit 0 only when there are zero gaps.
        # The eventual target state once the baseline is drained.

    python3 scripts/doc_coverage.py --write-baseline
        # Overwrite scripts/doc_coverage_baseline.txt with the current
        # gap set. Used once per spec pass to record what closed.

    python3 scripts/doc_coverage.py FILE [FILE ...]
        # Scan an explicit file list (always strict; baseline ignored).
        # Used by tests and ad-hoc spot checks.

Gap keys
--------
Gap keys are stable across line-number shifts so the baseline does not
churn on unrelated edits.

- ``module`` -- the file has no module-level docstring.
- ``function:<qualname>`` -- the function/method has no docstring.
  ``<qualname>`` is the dotted scope path: ``foo`` for a top-level
  function, ``Bar.foo`` for a method, ``outer.inner`` for a nested
  function. Same convention as ``__qualname__`` minus ``<locals>``.

What counts as documented
-------------------------
- Module: ``ast.get_docstring(tree)`` returns a non-empty string.
- Function / async function / method: ``ast.get_docstring(node)`` returns
  a non-empty string. ``...`` (Ellipsis), bare ``pass``, or any other
  first statement does not count.

What is skipped
---------------
- ``@overload`` stubs (signature-only declarations from ``typing``).
- ``@property.setter`` / ``@<prop>.deleter`` pairs: the ``@property``
  getter carries the contract.

The script never imports the target files (pure AST parsing) so it is
safe to run against modules whose imports have side effects.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
XIBI_ROOT = REPO_ROOT / "xibi"
BASELINE_PATH = REPO_ROOT / "scripts" / "doc_coverage_baseline.txt"


@dataclass(frozen=True)
class Gap:
    """One undocumented item discovered in a file.

    ``rel_path`` is forward-slash separated relative to the repo root so
    baseline lines are portable across platforms. ``key`` is the stable
    gap key (``module`` or ``function:<qualname>``) used for baseline
    matching. ``lineno`` is for human-readable output only.
    """

    rel_path: str
    key: str
    lineno: int

    def baseline_line(self) -> str:
        """Render this gap as a tab-separated baseline file line."""
        return f"{self.rel_path}\t{self.key}"

    def report_line(self) -> str:
        """Render this gap as a human-readable scan report line."""
        return f"GAP  {self.rel_path}  {self.key}  (line {self.lineno})"


# ---------- AST inspection ----------


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Return decorator expressions as dotted strings (best-effort, for filter checks)."""
    names: list[str] = []
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name):
            names.append(dec.id)
        elif isinstance(dec, ast.Attribute):
            parts: list[str] = []
            cur: ast.AST = dec
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
            names.append(".".join(reversed(parts)))
        elif isinstance(dec, ast.Call):
            func = dec.func
            if isinstance(func, ast.Name):
                names.append(func.id)
            elif isinstance(func, ast.Attribute):
                names.append(func.attr)
    return names


def _is_skippable(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True if this function does not require a docstring.

    Skipped: ``@overload`` stubs and property setter/deleter pairs (the
    paired ``@property`` getter is the docstring carrier).
    """
    for name in _decorator_names(node):
        if name == "overload" or name.endswith(".overload"):
            return True
        if name.endswith(".setter") or name.endswith(".deleter"):
            return True
    return False


def _walk_with_scope(
    tree: ast.AST,
) -> Iterator[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]]:
    """Yield every (FunctionDef, qualified_name) under ``tree``.

    Qualified name follows Python's ``__qualname__`` convention but
    omits the ``.<locals>.`` infix so baseline keys stay short and the
    common case (top-level functions and class methods) reads naturally.
    """

    def visit(node: ast.AST, scope: list[str]) -> Iterator[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = ".".join(scope + [child.name])
                yield child, qual
                yield from visit(child, scope + [child.name])
            elif isinstance(child, ast.ClassDef):
                yield from visit(child, scope + [child.name])
            else:
                yield from visit(child, scope)

    yield from visit(tree, [])


def scan_file(path: Path) -> list[Gap]:
    """Return every undocumented item in ``path``.

    Path-related errors (missing file, parse failure) surface as a
    synthetic gap with key ``"error:<reason>"`` so the caller sees them
    in the report but the baseline mechanism does not silently swallow
    them.
    """
    try:
        rel = str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        rel = str(path)
    rel = rel.replace("\\", "/")

    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [Gap(rel, f"error:read:{exc}", 0)]
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [Gap(rel, f"error:parse:{exc}", exc.lineno or 0)]

    gaps: list[Gap] = []
    if not ast.get_docstring(tree):
        gaps.append(Gap(rel, "module", 1))

    for node, qualname in _walk_with_scope(tree):
        if _is_skippable(node):
            continue
        if not ast.get_docstring(node):
            gaps.append(Gap(rel, f"function:{qualname}", node.lineno))

    return gaps


# ---------- File discovery and baseline I/O ----------


def iter_xibi_files() -> Iterator[Path]:
    """Yield every ``*.py`` file under ``xibi/`` in sorted, deterministic order."""
    yield from sorted(XIBI_ROOT.rglob("*.py"))


def load_baseline(path: Path | None = None) -> set[str]:
    """Read the baseline file and return a set of ``"<rel>\\t<key>"`` strings.

    Missing baseline is treated as an empty set -- the script then
    behaves like ``--strict`` for callers that did not pass that flag.
    Comments (``#``) and blank lines are skipped.

    ``path`` defaults to the module-level ``BASELINE_PATH``, looked up
    at call time so tests can monkeypatch it.
    """
    if path is None:
        path = BASELINE_PATH
    if not path.exists():
        return set()
    entries: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        entries.add(line)
    return entries


def write_baseline(gaps: list[Gap], path: Path | None = None) -> None:
    """Overwrite the baseline file with the supplied gaps, sorted for stability.

    ``path`` defaults to the module-level ``BASELINE_PATH`` at call time
    so tests can monkeypatch the destination.
    """
    if path is None:
        path = BASELINE_PATH
    header = (
        "# doc_coverage baseline -- grandfathered docstring gaps.\n"
        "# Generated by `python3 scripts/doc_coverage.py --write-baseline`.\n"
        "# Each line: <relative-path>\\t<gap-key>.\n"
        "# Gap-key: 'module' or 'function:<qualified-name>'.\n"
        "# Remove entries as future passes close gaps; CI fails on any NEW gap.\n"
    )
    lines = sorted({g.baseline_line() for g in gaps})
    path.write_text(header + "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


# ---------- Main entry ----------


def _resolve_targets(explicit: list[str]) -> list[Path]:
    """Return the list of files to scan -- CLI args override the xibi/ default."""
    if explicit:
        return [Path(a).resolve() for a in explicit]
    return list(iter_xibi_files())


def _format_summary(total_files: int, current_gaps: list[Gap]) -> str:
    files_with = len({g.rel_path for g in current_gaps})
    return f"summary: {total_files} files scanned, {files_with} with gaps, {len(current_gaps)} undocumented items"


def main(argv: list[str] | None = None) -> int:
    """Run a coverage scan and return the process exit code.

    Default mode: scan all xibi/ files, exit 0 iff every current gap is
    listed in the baseline. ``--strict``: ignore the baseline, exit 0
    iff zero gaps. ``--write-baseline``: overwrite the baseline with the
    current scan and exit 0. Explicit file arguments override the xibi/
    default and force strict mode.
    """
    parser = argparse.ArgumentParser(description="Check docstring coverage across xibi/.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail on any gap (ignore the baseline).",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Overwrite scripts/doc_coverage_baseline.txt with the current gap set.",
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Explicit files to scan (default: all xibi/**/*.py). Implies --strict.",
    )
    args = parser.parse_args(argv)

    targets = _resolve_targets(args.files)
    explicit_files = bool(args.files)

    current: list[Gap] = []
    for path in targets:
        current.extend(scan_file(path))

    if args.write_baseline:
        if explicit_files:
            print("--write-baseline cannot be combined with explicit files.", file=sys.stderr)
            return 2
        write_baseline(current)
        print(f"wrote {len(current)} entries to {BASELINE_PATH.relative_to(REPO_ROOT)}")
        return 0

    strict = args.strict or explicit_files
    baseline = set() if strict else load_baseline()

    # Partition current gaps into "already grandfathered" and "new".
    new_gaps: list[Gap] = []
    for gap in current:
        if strict or gap.baseline_line() not in baseline:
            new_gaps.append(gap)

    # Print full scan output (one line per gap) for transparency.
    for gap in current:
        print(gap.report_line())

    # In non-strict mode, also report stale baseline entries (entries
    # that no longer correspond to a real gap). Stale entries do not
    # fail CI -- they just tell the operator the baseline is ready to
    # shrink.
    if not strict and baseline:
        current_keys = {g.baseline_line() for g in current}
        stale = sorted(baseline - current_keys)
        if stale:
            print()
            print(f"note: {len(stale)} baseline entries are stale (gap closed):")
            for entry in stale[:20]:
                print(f"  stale: {entry}")
            if len(stale) > 20:
                print(f"  ... and {len(stale) - 20} more")

    print()
    print(_format_summary(len(targets), current))
    if strict:
        print("mode: strict (no baseline applied)")
    else:
        print(f"mode: baseline ({len(baseline)} grandfathered entries)")
        print(f"new gaps not in baseline: {len(new_gaps)}")

    return 0 if not new_gaps else 1


if __name__ == "__main__":
    sys.exit(main())
