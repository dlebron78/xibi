"""Config-drift check: SHA-256 snapshot compare.

Writes a sidecar ``<path>.sha256`` the first time a watched file is seen.
On subsequent pulses, mismatch between the sidecar and the live hash
produces a Finding. Missing files are skipped silently — the config
drift check is for *unexpected change*, not *existence*.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from xibi.caretaker.config import ConfigDriftConfig
from xibi.caretaker.finding import Finding, Severity


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".sha256")


def snapshot_hash(path: Path) -> str:
    """Write (or overwrite) the ``<path>.sha256`` sidecar. Returns the hash."""
    digest = _hash_file(path)
    _sidecar(path).write_text(digest + "\n", encoding="utf-8")
    return digest


def check(workdir: Path, cfg: ConfigDriftConfig) -> list[Finding]:
    """Inspect every watched path. Missing sidecars are created silently
    on first observation (no Finding). Drift produces a Finding.
    """
    findings: list[Finding] = []
    for raw in cfg.watched_paths:
        path = Path(raw).expanduser()
        if not path.exists():
            continue
        sidecar = _sidecar(path)
        live = _hash_file(path)
        if not sidecar.exists():
            # First observation — establish baseline
            sidecar.write_text(live + "\n", encoding="utf-8")
            continue
        stored = sidecar.read_text(encoding="utf-8").strip()
        if stored == live:
            continue
        findings.append(
            Finding(
                check_name="config_drift",
                severity=Severity.WARNING,
                dedup_key=f"config_drift:{path.name}",
                message=(
                    f"{path} SHA changed\n"
                    f"  was: {stored[:16]}\u2026\n"
                    f"  now: {live[:16]}\u2026\n"
                    f"Resolve: `xibi caretaker accept-config {path}`\n"
                    f"       or revert the change."
                ),
                metadata={
                    "path": str(path),
                    "stored_hash": stored,
                    "live_hash": live,
                },
            )
        )
    return findings
