"""Entry point for xibi-dashboard.service.

The systemd unit (``systemd/xibi-dashboard.service``) invokes this script
directly. It builds the Flask app via ``create_app(DashboardConfig(...))``
and binds to ``127.0.0.1:8081`` (localhost only). The dashboard has no
authentication on HTML routes — only the ``/api/*`` surface is gated by
the ``X-API-Key`` header — so the bind address must remain localhost.
Do NOT change ``host`` to ``0.0.0.0`` (or any non-loopback address)
without a security review.

The dashboard's database path defaults to ``~/.xibi/data/xibi.db``
(the same default used by ``xibi/react.py``).
"""

from __future__ import annotations

from pathlib import Path

from xibi.dashboard.app import DashboardConfig, create_app


def main() -> None:
    """Build the dashboard Flask app and serve it on 127.0.0.1:8081."""
    db_path = Path.home() / ".xibi" / "data" / "xibi.db"
    app = create_app(DashboardConfig(db_path=db_path))
    # Loopback only — see module docstring. Flask exits cleanly on SIGTERM.
    app.run(host="127.0.0.1", port=8081)


if __name__ == "__main__":
    main()
