"""Tests for ``scripts/doc_coverage.py``.

Verifies the coverage scanner detects the docstring gaps it should,
respects the baseline mechanism, and exits with the right code in each
mode. The script is run as a subprocess so we exercise the actual CLI
surface, not internal helpers.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "doc_coverage.py"


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke doc_coverage.py with ``args`` and return the completed process."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=cwd or REPO_ROOT,
    )


def _write(path: Path, body: str) -> None:
    """Dedent and write ``body`` to ``path``, parents already exist."""
    path.write_text(textwrap.dedent(body), encoding="utf-8")


@pytest.fixture
def fully_documented(tmp_path: Path) -> Path:
    """A single-file fixture with module + function docstrings present."""
    f = tmp_path / "good.py"
    _write(
        f,
        '''
        """Module-level docstring for the good fixture."""


        def add(x: int, y: int) -> int:
            """Return the sum of x and y."""
            return x + y


        class Greeter:
            """A trivial class fixture."""

            def greet(self, name: str) -> str:
                """Return a greeting addressed to ``name``."""
                return f"hi {name}"
        ''',
    )
    return f


@pytest.fixture
def has_gaps(tmp_path: Path) -> Path:
    """A single-file fixture missing the module and one function docstring."""
    f = tmp_path / "bad.py"
    _write(
        f,
        '''
        def add(x: int, y: int) -> int:
            """Return the sum of x and y."""
            return x + y


        def subtract(x: int, y: int) -> int:
            return x - y


        class Greeter:
            def greet(self, name: str) -> str:
                return f"hi {name}"
        ''',
    )
    return f


def test_fully_documented_file_exits_zero(fully_documented: Path) -> None:
    """A file with module + function docs should exit 0 in explicit mode."""
    result = _run(str(fully_documented))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "summary:" in result.stdout
    assert "GAP" not in result.stdout


def test_file_with_gaps_exits_one(has_gaps: Path) -> None:
    """A file missing docstrings should exit 1 and name the gaps."""
    result = _run(str(has_gaps))
    assert result.returncode == 1, result.stdout + result.stderr
    # Module docstring missing.
    assert "module" in result.stdout
    # ``subtract`` and ``Greeter.greet`` are missing; ``add`` is documented.
    assert "function:subtract" in result.stdout
    assert "function:Greeter.greet" in result.stdout
    assert "function:add" not in result.stdout


def test_overload_stubs_are_skipped(tmp_path: Path) -> None:
    """``@overload`` stubs should not require docstrings."""
    f = tmp_path / "over.py"
    _write(
        f,
        '''
        """Overload fixture."""

        from typing import overload


        @overload
        def cast(x: int) -> int: ...


        @overload
        def cast(x: str) -> str: ...


        def cast(x):
            """Return ``x`` unchanged."""
            return x
        ''',
    )
    result = _run(str(f))
    assert result.returncode == 0, result.stdout + result.stderr


def test_property_setter_pair_is_skipped(tmp_path: Path) -> None:
    """``@<prop>.setter`` should inherit documentation from the getter."""
    f = tmp_path / "prop.py"
    _write(
        f,
        '''
        """Property fixture."""


        class Box:
            """A box with a value."""

            @property
            def value(self) -> int:
                """Return the current value."""
                return self._value

            @value.setter
            def value(self, v: int) -> None:
                self._value = v
        ''',
    )
    result = _run(str(f))
    assert result.returncode == 0, result.stdout + result.stderr


def test_baseline_grandfathers_existing_gaps(tmp_path: Path, has_gaps: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Baseline entries should suppress those gaps from the failure set.

    The default (no-arg) mode reads ``scripts/doc_coverage_baseline.txt``
    from the repo. We invoke the module API directly here so we can
    point at a temporary baseline without mutating the real one.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import importlib

        doc_coverage = importlib.import_module("doc_coverage")
        importlib.reload(doc_coverage)

        # Build the baseline that grandfathers EVERY gap in has_gaps.
        gaps = doc_coverage.scan_file(has_gaps)
        baseline_path = tmp_path / "baseline.txt"
        doc_coverage.write_baseline(gaps, path=baseline_path)
        assert baseline_path.exists()

        # Patch the module to point at our temp baseline + temp file
        # list, then call main() with no args (default-mode path).
        monkeypatch.setattr(doc_coverage, "BASELINE_PATH", baseline_path)
        monkeypatch.setattr(doc_coverage, "iter_xibi_files", lambda: iter([has_gaps]))
        exit_code = doc_coverage.main([])
        assert exit_code == 0, "all gaps are in baseline; expected exit 0"

        # Adding a new gap (write a fresh bad function) should now fail.
        with has_gaps.open("a", encoding="utf-8") as fh:
            fh.write("\n\ndef new_undocumented():\n    return None\n")
        exit_code = doc_coverage.main([])
        assert exit_code == 1, "new gap should not be grandfathered"
    finally:
        sys.path.remove(str(REPO_ROOT / "scripts"))


def test_strict_mode_ignores_baseline(tmp_path: Path, has_gaps: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--strict`` should fail even when every gap is in the baseline."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import importlib

        doc_coverage = importlib.import_module("doc_coverage")
        importlib.reload(doc_coverage)

        gaps = doc_coverage.scan_file(has_gaps)
        baseline_path = tmp_path / "baseline.txt"
        doc_coverage.write_baseline(gaps, path=baseline_path)

        monkeypatch.setattr(doc_coverage, "BASELINE_PATH", baseline_path)
        # Explicit-file form is always strict, but we want to test the
        # --strict default-scan path too.
        monkeypatch.setattr(doc_coverage, "iter_xibi_files", lambda: iter([has_gaps]))
        exit_code = doc_coverage.main(["--strict"])
        assert exit_code == 1, "--strict must not honor baseline"
    finally:
        sys.path.remove(str(REPO_ROOT / "scripts"))


def test_repo_default_scan_passes() -> None:
    """The committed baseline must keep the repo's default scan green.

    This is the actual CI contract: ``python3 scripts/doc_coverage.py``
    with no arguments and the committed baseline must exit 0.
    """
    result = _run()
    assert result.returncode == 0, (
        "default scan should be green via the committed baseline.\n" + result.stdout + result.stderr
    )
