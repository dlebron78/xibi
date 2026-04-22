"""step-91: regression guards against service-list drift in deploy.sh.

The spec's C8 condition: deploy.sh must declare LONG_RUNNING_SERVICES exactly
once, and neither restart loop nor health-check loop may contain literal
service names. If someone adds a new hardcoded reference, these tests fail.
"""
from __future__ import annotations

import re
from pathlib import Path


DEPLOY_SH = Path(__file__).resolve().parent.parent / "scripts" / "deploy.sh"
SCRIPT_TEXT = DEPLOY_SH.read_text()


def test_long_running_services_declared_exactly_once() -> None:
    matches = re.findall(r"^LONG_RUNNING_SERVICES=", SCRIPT_TEXT, flags=re.MULTILINE)
    assert len(matches) == 1, (
        f"expected exactly one LONG_RUNNING_SERVICES= declaration, found {len(matches)}"
    )


def test_long_running_services_initial_value_covers_known_units() -> None:
    match = re.search(
        r'^LONG_RUNNING_SERVICES="([^"]+)"', SCRIPT_TEXT, flags=re.MULTILINE
    )
    assert match is not None, "LONG_RUNNING_SERVICES= declaration not found or malformed"
    services = set(match.group(1).split())
    # step-97 removed xibi-caretaker.service — it is a timer-triggered oneshot
    # owned by xibi-caretaker.timer, not a long-running service. The restart
    # loop should not handle it.
    assert services == {
        "xibi-heartbeat.service",
        "xibi-telegram.service",
        "xibi-dashboard.service",
    }, f"initial LONG_RUNNING_SERVICES diverged from the known set: {services}"


def test_no_hardcoded_service_names_outside_declaration() -> None:
    """No restart/health-check code path may contain `xibi-*.service` literals.

    The whole point of LONG_RUNNING_SERVICES is a single source of truth for
    the restart loop — any stray literal there is drift bait.

    Exempt contexts (not restart-loop code paths):
      - Comment lines (documentation; cannot drift silently).
      - The stale-detection ALLOW_LIST in sync_units() — step-97 declares
        xibi-deploy.{service,timer} as bootstrap-only units by design; the
        literal is the contract and cannot be folded into LONG_RUNNING_SERVICES
        (different concern).
    """
    offenders = []
    for lineno, line in enumerate(SCRIPT_TEXT.splitlines(), start=1):
        stripped = line.strip()
        if line.startswith("LONG_RUNNING_SERVICES="):
            continue
        if stripped.startswith("#"):
            continue
        if "ALLOW_LIST=" in line:
            continue
        if re.search(r"xibi-[a-z]+\.service", line):
            offenders.append(f"line {lineno}: {line.rstrip()}")
    assert not offenders, (
        "hardcoded xibi-*.service literal(s) in restart/health-check code path:\n  "
        + "\n  ".join(offenders)
    )
