"""JulesWatcher — polls Jules sessions for questions and auto-answers them.

Runs inside the heartbeat tick. For each recent session in AWAITING_USER_FEEDBACK:
  1. Fetches all activities (paginated) to find agentMessaged entries
  2. Uses an LLM call with the session spec as context to generate an answer
  3. Replies to Jules via :sendMessage
  4. Broadcasts a summary to Telegram so Daniel can see what was answered
  5. If the LLM cannot produce a confident answer, broadcasts the raw question instead
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

JULES_API = "https://jules.googleapis.com/v1alpha"
# Only look at sessions from the last N days
HISTORY_WINDOW_DAYS = 7
# State file tracking which activity IDs we've already responded to
_STATE_FILENAME = "responded_activities.json"


class JulesWatcher:
    def __init__(
        self,
        api_key: str,
        history_file: Path,
        llm: Any,
        broadcast_fn: Callable[[str], None],
        state_dir: Path | None = None,
    ) -> None:
        self.api_key = api_key
        self.history_file = Path(history_file)
        self.llm = llm
        self.broadcast = broadcast_fn
        self.state_dir = state_dir or (Path.home() / ".jules_trigger_state")
        self.state_file = self.state_dir / _STATE_FILENAME

    # ------------------------------------------------------------------ #
    # Public                                                               #
    # ------------------------------------------------------------------ #

    def poll(self) -> None:
        """Check all recent sessions; auto-answer any pending Jules questions."""
        sessions = self._load_recent_sessions()
        if not sessions:
            return

        responded = self._load_state()
        changed = False

        for entry in sessions:
            session_id = entry["session"].split("/")[-1]
            try:
                state = self._get_session_state(session_id)
                if state != "AWAITING_USER_FEEDBACK":
                    continue

                task_name = entry.get("task", session_id)
                question, activity_id, spec = self._find_pending_question(session_id, responded)
                if not question or not activity_id:
                    continue

                logger.info("Jules question on %s (activity %s)", task_name, activity_id)

                answer = self._generate_answer(question, spec, task_name)
                if not answer:
                    # Couldn't generate — surface to Daniel
                    self.broadcast(f"❓ **Jules needs your input on {task_name}:**\n\n{question[:600]}")
                    # Mark as seen so we don't spam
                    responded[activity_id] = {
                        "session_id": session_id,
                        "ts": datetime.utcnow().isoformat(),
                        "escalated": True,
                    }
                    changed = True
                    continue

                # Reply to Jules
                self._send_message(session_id, answer)
                logger.info("Replied to Jules on %s: %s", task_name, answer[:120])

                # Broadcast summary to Telegram
                self.broadcast(f"🤖 **Auto-answered Jules on {task_name}**\n\nQ: {question[:300]}\n\nA: {answer[:300]}")

                responded[activity_id] = {
                    "session_id": session_id,
                    "ts": datetime.utcnow().isoformat(),
                    "answer": answer[:500],
                }
                changed = True

            except Exception as e:
                logger.warning("JulesWatcher error for session %s: %s", session_id, e, exc_info=True)

        if changed:
            self._save_state(responded)

    # ------------------------------------------------------------------ #
    # Jules API helpers                                                    #
    # ------------------------------------------------------------------ #

    def _api_get(self, path: str) -> dict[Any, Any]:
        url = f"{JULES_API}/{path}"
        req = urllib.request.Request(url, headers={"X-Goog-Api-Key": self.api_key})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                res = json.load(r)
                return res if isinstance(res, dict) else {}
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Jules API {path} → HTTP {e.code}") from e

    def _get_session_state(self, session_id: str) -> str:
        data = self._api_get(f"sessions/{session_id}")
        state = data.get("state")
        return str(state) if state is not None else ""

    def _get_session_spec(self, session_id: str) -> str:
        """Return the original task prompt (spec) from the session."""
        data = self._api_get(f"sessions/{session_id}")
        prompt = data.get("prompt")
        return str(prompt) if prompt is not None else ""

    def _get_all_activities(self, session_id: str) -> list[dict[Any, Any]]:
        """Fetch all activity pages for a session."""
        activities: list[dict[Any, Any]] = []
        page_token: str | None = None
        while True:
            path = f"sessions/{session_id}/activities"
            if page_token:
                path += f"?pageToken={page_token}"
            data = self._api_get(path)
            activities.extend(data.get("activities", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return activities

    def _find_pending_question(self, session_id: str, responded: dict) -> tuple[str | None, str | None, str]:
        """Find the latest unanswered agentMessaged activity.

        Returns (question_text, activity_id, spec) or (None, None, "").
        """
        activities = self._get_all_activities(session_id)
        spec = ""

        # Find last agentMessaged not yet responded to
        question = None
        activity_id = None
        for a in activities:
            if "agentMessaged" in a:
                aid = a.get("id", "")
                if aid and aid not in responded:
                    question = a["agentMessaged"].get("agentMessage", "")
                    activity_id = aid

        if question and activity_id:
            # Lazy-load spec only when needed
            try:
                spec = self._get_session_spec(session_id)
            except Exception:
                spec = ""

        return question, activity_id, spec

    def _send_message(self, session_id: str, message: str) -> None:
        url = f"{JULES_API}/sessions/{session_id}:sendMessage"
        payload = json.dumps({"prompt": message}).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "X-Goog-Api-Key": self.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()

    # ------------------------------------------------------------------ #
    # LLM answer generation                                               #
    # ------------------------------------------------------------------ #

    def _generate_answer(self, question: str, spec: str, task_name: str) -> str | None:
        """Use an LLM to answer Jules' question based on the task spec.

        Returns None if the answer can't be determined (escalate to user).
        """
        spec_section = f"\n\n---\nTask spec for {task_name}:\n{spec[:3000]}" if spec else ""

        prompt = (
            "You are reviewing work by Jules, an AI coding agent. "
            "Jules has asked a clarifying question while implementing a task. "
            "Answer the question concisely and directly based on the task spec provided. "
            "If the answer is genuinely unclear from the spec and requires human judgment, "
            "reply with exactly: ESCALATE"
            f"{spec_section}"
            f"\n\n---\nJules asks:\n{question}\n\n"
            "Your answer (be direct and specific, 1-3 sentences):"
        )

        try:
            response = self.llm.generate(prompt, max_tokens=300)
            if not response:
                return None
            response = response.strip()
            if response.upper() == "ESCALATE":
                return None
            return str(response)
        except Exception as e:
            logger.warning("LLM answer generation failed: %s", e)
            return None

    # ------------------------------------------------------------------ #
    # State persistence                                                    #
    # ------------------------------------------------------------------ #

    def _load_state(self) -> dict[Any, Any]:
        if not self.state_file.exists():
            return {}
        try:
            data = json.loads(self.state_file.read_text())
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_state(self, state: dict[Any, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, indent=2))

    def _load_recent_sessions(self) -> list[dict[Any, Any]]:
        """Read history.jsonl, return sessions from the last HISTORY_WINDOW_DAYS days."""
        if not self.history_file.exists():
            return []
        cutoff = datetime.utcnow() - timedelta(days=HISTORY_WINDOW_DAYS)
        sessions = []
        try:
            for line in self.history_file.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts_str = entry.get("ts", "")
                    if ts_str:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).replace(tzinfo=None)
                        if ts >= cutoff:
                            sessions.append(entry)
                    else:
                        sessions.append(entry)  # no ts — include
                except Exception:
                    continue
        except Exception as e:
            logger.warning("Failed to read Jules history: %s", e)
        return sessions
