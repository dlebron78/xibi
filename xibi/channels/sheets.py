"""
Google Sheets export channel.

Writes structured rows (currently job listings) to a Google Sheet so external
consumers (e.g. another Claude project) can read Xibi's outputs without
touching the SQLite database.

Auth model: service account key file. The target sheet must be shared (Editor)
with the service account's client_email. This avoids touching the existing
Calendar OAuth credentials and gives a clean isolation boundary — if the key
leaks, only that one sheet is exposed.

Config (in profile/config.json):
    "sheets_export": {
        "enabled": true,
        "credentials_path": "~/.xibi/secrets/sheets-service-account.json",
        "jobs": {
            "spreadsheet_id": "1AbC...XyZ",
            "worksheet": "Jobs",
            "dedupe_by": "url"
        }
    }
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


JOB_COLUMNS = [
    "exported_at",
    "title",
    "company",
    "location",
    "remote",
    "salary_min",
    "salary_max",
    "url",
    "posted_at",
    "source",
    "search_profile",
    "ref_id",
]


class SheetsExportError(Exception):
    """Raised when the Sheets export pipeline fails in a recoverable way."""


class SheetsExporter:
    """Thin wrapper around gspread for appending job rows to a Google Sheet."""

    def __init__(self, config: dict[str, Any]):
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", False))
        self._client = None
        self._jobs_ws = None
        self._dedupe_cache: set[str] = set()
        self._dedupe_loaded = False

    # ---------- lazy init ----------

    def _ensure_client(self) -> None:
        if self._client is not None:
            return
        try:
            import gspread  # type: ignore
            from google.oauth2.service_account import Credentials  # type: ignore
        except ImportError as e:
            raise SheetsExportError(
                "gspread / google-auth not installed. Run: "
                "pip install gspread google-auth"
            ) from e

        creds_path = self.config.get("credentials_path")
        if not creds_path:
            raise SheetsExportError("sheets_export.credentials_path is required")
        creds_path = os.path.expanduser(creds_path)
        if not Path(creds_path).exists():
            raise SheetsExportError(f"Service account key not found: {creds_path}")

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.file",
        ]
        creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
        self._client = gspread.authorize(creds)

    def _ensure_jobs_worksheet(self) -> Any:
        if self._jobs_ws is not None:
            return self._jobs_ws
        self._ensure_client()
        jobs_cfg = self.config.get("jobs", {}) or {}
        sheet_id = jobs_cfg.get("spreadsheet_id")
        ws_name = jobs_cfg.get("worksheet", "Jobs")
        if not sheet_id:
            raise SheetsExportError("sheets_export.jobs.spreadsheet_id is required")

        sh = self._client.open_by_key(sheet_id)  # type: ignore[union-attr]
        try:
            ws = sh.worksheet(ws_name)
        except Exception:
            ws = sh.add_worksheet(title=ws_name, rows=1000, cols=len(JOB_COLUMNS))
            ws.append_row(JOB_COLUMNS, value_input_option="USER_ENTERED")

        # Ensure header row exists
        first_row = ws.row_values(1)
        if not first_row:
            ws.append_row(JOB_COLUMNS, value_input_option="USER_ENTERED")

        self._jobs_ws = ws
        return ws

    def _load_dedupe_cache(self) -> None:
        if self._dedupe_loaded:
            return
        try:
            ws = self._ensure_jobs_worksheet()
            jobs_cfg = self.config.get("jobs", {}) or {}
            dedupe_by = jobs_cfg.get("dedupe_by", "url")
            if dedupe_by not in JOB_COLUMNS:
                self._dedupe_loaded = True
                return
            col_idx = JOB_COLUMNS.index(dedupe_by) + 1
            existing = ws.col_values(col_idx)[1:]  # skip header
            self._dedupe_cache = {v.strip() for v in existing if v and v.strip()}
        except Exception as e:
            logger.warning("Sheets dedupe cache load failed: %s", e)
        finally:
            self._dedupe_loaded = True

    # ---------- public API ----------

    def export_job_signals(
        self,
        job_signals: list[dict[str, Any]],
        search_profile: str | None = None,
    ) -> int:
        """
        Append job rows to the configured Google Sheet.

        Returns number of rows actually appended (after dedupe).
        Silently no-ops if not enabled.
        """
        if not self.enabled or not job_signals:
            return 0

        try:
            ws = self._ensure_jobs_worksheet()
            self._load_dedupe_cache()
        except SheetsExportError as e:
            logger.warning("Sheets export disabled this run: %s", e)
            return 0
        except Exception as e:
            logger.warning("Sheets export init failed: %s", e, exc_info=True)
            return 0

        jobs_cfg = self.config.get("jobs", {}) or {}
        dedupe_by = jobs_cfg.get("dedupe_by", "url")
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

        rows: list[list[Any]] = []
        for sig in job_signals:
            meta = sig.get("metadata", {}) or {}
            job = meta.get("job", {}) or {}
            url = meta.get("url") or job.get("url") or ""
            title = meta.get("title") or job.get("title") or ""
            company = meta.get("company") or job.get("company") or ""
            location = meta.get("location") or job.get("location") or ""
            row_dict = {
                "exported_at": now_iso,
                "title": title,
                "company": company,
                "location": location,
                "remote": job.get("is_remote") or job.get("remote") or "",
                "salary_min": meta.get("salary_min") or job.get("salary_min") or "",
                "salary_max": meta.get("salary_max") or job.get("salary_max") or "",
                "url": url,
                "posted_at": meta.get("posted_at") or job.get("posted_at") or "",
                "source": sig.get("source", ""),
                "search_profile": search_profile or "",
                "ref_id": sig.get("ref_id", ""),
            }
            dedupe_val = str(row_dict.get(dedupe_by, "")).strip()
            if dedupe_val and dedupe_val in self._dedupe_cache:
                continue
            if dedupe_val:
                self._dedupe_cache.add(dedupe_val)
            rows.append([row_dict[c] for c in JOB_COLUMNS])

        if not rows:
            logger.debug("Sheets export: 0 new rows after dedupe")
            return 0

        try:
            ws.append_rows(rows, value_input_option="USER_ENTERED")
            logger.info("Sheets export: appended %d job rows", len(rows))
            return len(rows)
        except Exception as e:
            logger.warning("Sheets export append failed: %s", e, exc_info=True)
            return 0
