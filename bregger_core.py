import os
import sys
import re
import json
import sqlite3
import calendar
import time
import urllib.request
import urllib.error
import uuid
import importlib.util
import threading
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from collections import deque, defaultdict
from dataclasses import dataclass, field
from bregger_utils import (
    inference_lock,
    normalize_topic as _normalize_topic,
    get_active_threads as _get_active_threads_shared,
    get_pinned_topics as _get_pinned_topics_shared,
    ensure_signals_schema,
)

try:
    import psutil as _psutil

    _PSUTIL_OK = True
except ImportError:
    _psutil = None
    _PSUTIL_OK = False

from bregger_shadow import ShadowMatcher


# ---------------------------------------------------------------------------
# Token sink — thread-local storage for LLM token metadata
# Written by OllamaProvider / GeminiProvider after every generate() call.
# Read by the ReAct loop to capture per-step token cost without changing
# the generate() return type.
# ---------------------------------------------------------------------------
_token_sink = threading.local()


def _clear_token_sink():
    """Reset the token sink before each generate() call."""
    _token_sink.prompt_tokens = 0
    _token_sink.response_tokens = 0
    _token_sink.tok_per_sec = 0.0
    _token_sink.error = None


def _read_token_sink() -> dict:
    """Read token metadata written by the last generate() call."""
    return {
        "prompt_tokens": getattr(_token_sink, "prompt_tokens", 0),
        "response_tokens": getattr(_token_sink, "response_tokens", 0),
        "tok_per_sec": getattr(_token_sink, "tok_per_sec", 0.0),
        "token_error": getattr(_token_sink, "error", None),
    }


# ---------------------------------------------------------------------------
# Step — reflectable ReAct scratchpad entry
# ---------------------------------------------------------------------------


@dataclass
class Step:
    """One step in a ReAct reasoning loop."""

    step_num: int
    thought: str = ""
    tool: str = ""
    tool_input: Dict = field(default_factory=dict)
    tool_output: Dict = field(default_factory=dict)
    raw_prompt: Optional[str] = None
    signal: Optional[Dict] = None  # Extracted topic/entity
    duration_ms: int = 0
    timestamp: str = ""
    escalated: bool = False
    escalation_source: str = ""
    parse_warning: Optional[str] = None  # Set when generate_step had to recover from a parse error

    def full_text(self) -> str:
        """Full detail for recent steps injected into next prompt."""
        out = str(self.tool_output)
        if len(out) > 800:
            out = out[:800] + "... [truncated]"
        return (
            f"Step {self.step_num}:\n"
            f"  Thought: {self.thought}\n"
            f"  Signal: {json.dumps(self.signal, separators=(',', ':')) if self.signal else 'null'}\n"
            f"  Action: {self.tool}\n"
            f"  Input: {json.dumps(self.tool_input, separators=(',', ':'))}\n"
            f"  Output: {out}"
        )

    def one_line_summary(self) -> str:
        """Compressed one-liner for older steps."""
        input_summary = json.dumps(self.tool_input, separators=(",", ":"))[:60]
        output_hint = ""
        if self.tool_output.get("status") == "error":
            output_hint = f"ERROR: {self.tool_output.get('message', '?')[:60]}"
        elif self.tool_output.get("content"):
            output_hint = str(self.tool_output["content"])[:80]
        elif self.tool_output.get("message"):
            output_hint = str(self.tool_output["message"])[:80]
        else:
            output_hint = str(self.tool_output)[:80]
        return f"Step {self.step_num}: {self.tool}({input_summary}) → {output_hint}"


# ---------------------------------------------------------------------------
# ReAct helper functions
# ---------------------------------------------------------------------------


def compress_scratchpad(scratchpad: List["Step"], current_step: int) -> str:
    """Progressive compression: full detail for recent 2 steps, one-liners for older."""
    result = []
    for step in scratchpad:
        if step.step_num >= current_step - 1:
            result.append(step.full_text())
        else:
            result.append(step.one_line_summary())
    return "\n\n".join(result) if result else "No steps taken yet."


def is_repeat(step: "Step", scratchpad: List["Step"]) -> bool:
    """Return True if this step closely duplicates a previous one (>60% word overlap)."""
    curr_words = set(json.dumps(step.tool_input).lower().split())
    if not curr_words:
        return False
    for prev in scratchpad:
        if prev.tool != step.tool:
            continue
        prev_words = set(json.dumps(prev.tool_input).lower().split())
        if not prev_words:
            continue
        overlap = len(curr_words & prev_words) / max(len(curr_words), len(prev_words))
        if overlap > 0.6:
            return True
    return False


_CONFIRMATION_RE = re.compile(
    r"\b(yes|yeah|yep|yup|confirm|go\s+ahead|do\s+it|sure|ok|okay|proceed|send\s+it)\b", re.IGNORECASE
)
_NEGATION_RE = re.compile(r"^\s*(no\b|nope|nah|never|cancel|stop|don't|do\s+not|don't)\b", re.IGNORECASE)


def _resolve_relative_time(token: str) -> Optional[str]:
    """Resolve a semantic time token into a YYYY-MM-DD string.
    Supported:
    - 1w_ago, 2w_ago, ... 6w_ago
    - 1mo_ago, 2mo_ago, 3mo_ago, 6mo_ago
    - 1yr_ago
    - today, yesterday
    - this_monday, next_monday, ... (this/next + weekday)
    """
    if not token or not isinstance(token, str):
        return None

    token = token.lower().strip().replace(" ", "_").replace(".", "")
    # Robustness: strip common model 'hallucination' headers
    token = token.replace("after_date=", "").replace("before_date=", "").replace("relative_time=", "")
    now = datetime.now()

    # Simple day offsets
    if token == "today":
        return now.strftime("%Y-%m-%d")
    if token == "yesterday":
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")

    # Weeks ago: 6w_ago
    match = re.match(r"(\d+)w(_?ago)?", token)
    if match:
        weeks = int(match.group(1))
        return (now - timedelta(weeks=weeks)).strftime("%Y-%m-%d")

    # Months ago: 2mo_ago, 2m_ago
    match = re.match(r"(\d+)m(o)?(_?ago)?", token)
    if match:
        # Avoid confusion with 'm' for minutes if we ever add time
        months = int(match.group(1))
        year = now.year
        month = now.month - months
        while month < 1:
            month += 12
            year -= 1
        day = min(now.day, 28)
        return datetime(year, month, day).strftime("%Y-%m-%d")

    # Year ago: 1yr_ago, 1y_ago
    match = re.match(r"(\d+)y(r)?(_?ago)?", token)
    if match:
        years = int(match.group(1))
        try:
            return now.replace(year=now.year - years).strftime("%Y-%m-%d")
        except ValueError:  # Leap year
            return (now - timedelta(days=365 * years)).strftime("%Y-%m-%d")

    # This/Next Weekday: this_monday, next_friday
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for i, w in enumerate(weekdays):
        if w in token:
            target_weekday = i
            days_ahead = target_weekday - now.weekday()
            if "next" in token:
                days_ahead += 7
            elif "this" in token and days_ahead < 0:
                # If it's Saturday and they say 'this monday', they likely meant next monday
                # or the monday that just passed. In a 'this' context, we usually look forward.
                days_ahead += 7
            return (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    # If it's already YYYY-MM-DD, return as-is
    if re.match(r"^\d{4}-\d{2}-\d{2}$", token):
        return token

    return None


def is_confirmation(text: str) -> bool:
    """Return True if the user's message is a confirmation of a pending action."""
    t = text.strip()
    if _NEGATION_RE.match(t):
        return False
    return bool(_CONFIRMATION_RE.search(t))


class MessageModeClassifier:
    """Categorizes incoming messages into command, conversation, drafting, or confirmation."""

    def classify(self, message: str, state: dict) -> str:
        """
        Returns: 'command' | 'conversation' | 'drafting' | 'confirmation'
        state keys: pending_action, draft_active, last_turn_type
        """
        message_lower = message.lower().strip()
        tokens = message_lower.split()
        num_tokens = len(tokens)

        scores = {"command": 0, "conversation": 0, "drafting": 0, "confirmation": 0}

        # 1. State-based signals (Highest Weight)
        if state.get("pending_action"):
            # If we have a pending action, it's almost certainly a confirmation or cancellation
            scores["confirmation"] += 10

        if state.get("draft_active"):
            # If a draft is currently being edited/composed
            scores["drafting"] += 8

        if state.get("last_turn_type") == "clarifying_question":
            # If the bot just asked a question, user is likely replying conversationally
            scores["conversation"] += 5

        # 2. Heuristic signals
        # Command leaning
        if num_tokens <= 6:
            scores["command"] += 3

        domain_nouns = {"inbox", "email", "mail", "search", "weather", "calendar", "event", "remind", "note", "task"}
        if any(token in domain_nouns for token in tokens):
            scores["command"] += 3

        # Conversation leaning
        if num_tokens >= 15:
            scores["conversation"] += 4

        narrative_starts = ("i ", "when i", "we ", "they ", "it was", "as i")
        if any(message_lower.startswith(start) for start in narrative_starts):
            scores["conversation"] += 5

        narrative_markers = {"i decided", "i was", "i thought", "i wanted", "i noticed", "i am"}
        if any(marker in message_lower for marker in narrative_markers):
            scores["conversation"] += 5

        if "?" in message:
            scores["conversation"] += 2

        # Confirmation/Negation markers
        if is_confirmation(message):
            scores["confirmation"] += 8
        if _NEGATION_RE.match(message_lower):
            scores["confirmation"] += 8

        # 3. Decision
        mode = max(scores, key=scores.get)

        # Safe fallback: if the 'winning' score is too low, or if it's a tie, default to conversation
        max_score = scores[mode]
        # If it's a tie among top categories, default to 'conversation' for safety
        winners = [m for m, s in scores.items() if s == max_score and s > 0]

        if max_score < 3 or len(winners) > 1:
            return "conversation"

        return mode


class SkillRegistry:
    """Dynamically loads and manages Bregger skills."""

    def __init__(self, skills_dir: str):
        self.skills_dir = Path(skills_dir)
        self.skills = {}
        self.load_skills()

    def load_skills(self):
        """Scan skills_dir for manifest.json files and register them."""
        if not self.skills_dir.exists():
            return

        for manifest_path in self.skills_dir.glob("*/manifest.json"):
            try:
                with open(manifest_path, "r") as f:
                    manifest = json.load(f)

                skill_name = manifest.get("name")
                if skill_name:
                    self.skills[skill_name] = {"manifest": manifest, "path": manifest_path.parent}
                    print(f"📦 Loaded skill: {skill_name}", flush=True)
            except Exception as e:
                print(f"⚠️ Error loading skill from {manifest_path}: {e}", flush=True)

    def get_skill_manifests(self) -> List[Dict[str, Any]]:
        """Return all skill manifests for the LLM to use during Planning."""
        return [skill["manifest"] for skill in self.skills.values()]

    def validate_manifests(self):
        """Startup check for manifest health. Emits warnings, does not block."""
        valid_output_types = {"raw", "synthesis", "action"}
        for name, info in self.skills.items():
            manifest = info["manifest"]
            # 1. Basic properties
            if not manifest.get("name") or not manifest.get("description"):
                print(f"⚠️  Manifest {name}: Missing 'name' or 'description'", flush=True)

            # 2. Tools validation
            for tool in manifest.get("tools", []):
                tname = tool.get("name", "unknown")
                if not tool.get("description"):
                    print(f"⚠️  Skill {name}, Tool {tname}: Missing description", flush=True)

                ot = tool.get("output_type")
                if ot not in valid_output_types:
                    print(f"⚠️  Skill {name}, Tool {tname}: Invalid output_type '{ot}'", flush=True)

                if tool.get("risk") == "irreversible" and ot != "action":
                    print(
                        f"⚠️  Skill {name}, Tool {tname}: 'irreversible' risk requires output_type 'action'", flush=True
                    )

    def get_tool_min_tier(self, skill_name: str, tool_name: str) -> int:
        """Helper to check if a tool requires a minimum inference tier."""
        skill = self.skills.get(skill_name)
        if not skill:
            return 1
        for tool in skill["manifest"].get("tools", []):
            if tool.get("name") == tool_name:
                return tool.get("min_tier", 1)
        return 1

    def get_tool_meta(self, skill_name: str, tool_name: str) -> Optional[Dict[str, Any]]:
        """Return the full tool manifest dict for a given skill+tool, or None."""
        skill = self.skills.get(skill_name)
        if not skill:
            return None
        for tool in skill["manifest"].get("tools", []):
            if tool.get("name") == tool_name:
                return tool
        return None


class LLMProvider:
    """Base class for LLM backends (Ollama, Gemini, etc.)."""

    def generate(self, prompt: str, system: str = "", json_format: bool = False) -> str:
        raise NotImplementedError


class OllamaProvider(LLMProvider):
    """Ollama-specific implementation via urllib."""

    def __init__(self, base_url: str, model: str):
        self.base_url = base_url
        self.model = model

    def generate(self, prompt: str, system: str = "", json_format: bool = False) -> str:
        payload = {
            "model": self.model,
            "prompt": f"{system}\n{prompt}" if system else prompt,
            "stream": False,
            "options": {"num_ctx": 8192},
        }
        if json_format:
            payload["format"] = "json"
        # Disable thinking trace for reasoning models (Qwen 3.5+).
        # Sending think:false suppresses <think> blocks so the LLM returns
        # a direct answer — matching the no_think benchmark mode.
        # Non-reasoning models (Gemma, Mistral) ignore this field safely.
        if "qwen" in self.model.lower():
            payload["think"] = False

        url = f"{self.base_url}/api/generate"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        _clear_token_sink()
        with inference_lock:
            try:
                with urllib.request.urlopen(req, timeout=120) as response:
                    res_data = json.loads(response.read().decode("utf-8"))
                    prompt_tokens = res_data.get("prompt_eval_count", 0)
                    response_tokens = res_data.get("eval_count", 0)
                    # eval_duration is in nanoseconds; convert to tok/s
                    eval_ns = res_data.get("eval_duration", 0)
                    tok_per_sec = round(response_tokens / (eval_ns / 1e9), 1) if eval_ns > 0 else 0.0
                    _token_sink.prompt_tokens = prompt_tokens
                    _token_sink.response_tokens = response_tokens
                    _token_sink.tok_per_sec = tok_per_sec
                    print(
                        f"📊 TOKENS: prompt={prompt_tokens} | response={response_tokens} | {tok_per_sec} tok/s",
                        flush=True,
                    )
                    return res_data.get("response", "").strip()
            except urllib.error.HTTPError as e:
                print(
                    f"🔴 OLLAMA DEBUG: HTTPError {e.code} | URL: {url} | Model: {self.model} | Payload size: {len(data)} bytes | Reason: {e.reason}",
                    flush=True,
                )
                try:
                    body = e.read().decode("utf-8")[:500]
                    print(f"🔴 OLLAMA DEBUG: Response body: {body}", flush=True)
                except Exception:
                    pass
                # Track GPU hang events in signals table (Rule 15 — observability)
                if e.code == 500:
                    try:
                        import sqlite3 as _sqlite3, os as _os

                        _workdir = _os.environ.get("BREGGER_WORKDIR", _os.path.expanduser("~/.bregger"))
                        _db = _os.path.join(_workdir, "data", "bregger.db")
                        with _sqlite3.connect(_db, timeout=2) as _conn:
                            _conn.execute(
                                "INSERT INTO signals (source, topic_hint, entity_text, entity_type, content_preview) "
                                "VALUES (?, ?, ?, ?, ?)",
                                (
                                    "ollama",
                                    "gpu_hang",
                                    self.model,
                                    "error",
                                    f"HTTP 500 | payload={len(data)}b | reason={e.reason}",
                                ),
                            )
                        print(f"📈 [gpu_hang] Logged crash event for model={self.model}", flush=True)
                    except Exception as _sig_err:
                        print(f"⚠️ [gpu_hang] Failed to log signal: {_sig_err}", flush=True)
                raise


class GeminiProvider(LLMProvider):
    """Gemini-specific implementation via urllib (Google AI Studio)."""

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    def generate(self, prompt: str, system: str = "", json_format: bool = False) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"

        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": 2048,
                "temperature": 0.1,
            },
        }
        if system:
            payload["system_instruction"] = {"parts": [{"text": system}]}

        if json_format:
            payload["generationConfig"]["responseMimeType"] = "application/json"

        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}
        )

        with inference_lock:
            with urllib.request.urlopen(req, timeout=120) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                try:
                    # Gemini response path: candidates[0].content.parts[0].text
                    return res_data["candidates"][0]["content"]["parts"][0]["text"].strip()
                except (KeyError, IndexError):
                    error_msg = res_data.get("error", {}).get("message", "Unknown Gemini error")
                    raise Exception(f"Gemini API Error: {error_msg}")


class BreggerRouter:
    """Handles LLM communication to generate Plans (The 'P' in P-D-A-R)."""

    SYSTEM_PROMPT = """You are {assistant_name}. {assistant_persona}
Your job is to take user input and generate a JSON PLAN based on available skills.

USER CONTEXT:
{context}

RULES:
1. ONLY output raw JSON. No markdown, no conversational text.
2. NEVER "confirm" or "promise" an action without actually running a tool. If the user asks for information, USE A TOOL.
3. If no skill matches, use the "fallback" intent.
4. ALWAYS include "intent", "skill", "tool", and "parameters" keys.
5. If no skill is used, set "skill": "none" and "tool": "none".
6. For ANY query requiring real-time info (sports, news, current events), ALWAYS use the "search" skill.
7. DO NOT include schema definitions. Provide ONLY the values.
8. ALWAYS respond to the user's CURRENT input. History is context, not a queue of pending tasks.

Available Skills:
{manifests}
"""

    REACT_SYSTEM_PROMPT = """You are Bregger, a local-first AI agent running on the user's machine. You have direct access to their workspace files, email, memory, and tools.

When you need information, LOOK IT UP. Do not ask the user for things you can find yourself.

You solve tasks step by step using tools. Think → Act → Observe → Repeat until done.

## RESPONSE FORMAT (follow exactly)

Thought: <your reasoning about what to do next>
Action: <tool_name OR "finish">
Action Input: <JSON object with parameters>

To complete a task:
Thought: I have everything the user needs.
Action: finish
Action Input: {{"final_answer": "your response to the user"}}

## RULES (ranked by importance)

1. LOOK BEFORE YOU LEAP
   Before any action tool, call an observation tool first.
   - Don't know a filename → list_files
   - Don't know a person's email → recall
   - Don't know what's in a file → read_file
   - Not sure about anything → recall
   NEVER guess what you can look up.

2. IRREVERSIBLE ACTIONS NEED CONFIRMATION
   Before send_email or any tool that cannot be undone:
   - Do NOT send directly.
   - Use finish to present a draft to the user.
   - Ask "Should I send this?"

3. GATHER CONTEXT BEFORE CREATING CONTENT
   Before drafting emails, documents, or creative content:
   - Recall the recipient's details (name, email)
   - Recall relevant business/product context
   - Do NOT draft from assumptions. Look up what you can.

4. WHEN STUCK
   If you don't know how to proceed:
   - Do NOT call a random tool.
   - Use finish and explain what you need.

5. FOCUS ON THE CURRENT REQUEST ONLY
   ORIGINAL REQUEST is your ONLY task. RECENT CONVERSATION is
   background context — completed prior turns. Do not continue,
   revisit, or answer from them. If the answer appears to be in
   conversation history, verify it with a tool anyway — the data
   may be stale. ACTIVE THREADS show what topics the user has
   been focused on this week — use them to prioritize and add
   context, but do not proactively surface them unless asked.

## TOOLS

OBSERVATION TOOLS (use these FIRST):
{observation_tools}

ACTION TOOLS (use AFTER observing):
{action_tools}

CONTROL:
  - ask_user: Pause and ask the user a question. Use when you need confirmation, clarification, or a decision before proceeding. The task will resume when the user replies. Parameters: `{"question": "What you want to ask"}`
  - schedule: Schedule a future reminder or task. Bregger will proactively nudge the user at the right time. Parameters: `{"goal": "What to do", "due": "Semantic time token (e.g. 'tomorrow_1400', 'tuesday_0930') OR ISO 8601 string", "urgency": "normal|critical|low"}`
  - finish: Respond to the user conversationally. For answers or status updates. Do NOT use finish to compose emails, documents, or long content — use the action tools. Parameters: `{"final_answer": "your response"}`

## CURRENT CONTEXT

{date_context}

ORIGINAL REQUEST: {user_message}

{conversation_history}

What is your next step?"""

    REACT_TOOL_OVERRIDES = {
        "list_files": {
            "category": "observation",
            "description": "List files in the workspace. Use when you need to find a file but don't know the exact name.",
        },
        "recall": {
            "category": "observation",
            "description": "Look up people, contacts, facts, or context from memory. Use when you need someone's email, business details, or stored information.",
        },
        "read_file": {
            "category": "observation",
            "description": "Read the contents of a specific file. Requires the exact filepath from list_files.",
        },
        "read_email": {
            "category": "observation",
            "description": "Read a specific email by ID. Use to inspect email content before replying.",
        },
        "search_emails": {
            "category": "observation",
            "description": "Search emails by sender, subject, or time range. Returns email summaries.",
        },
        "list_unread": {
            "category": "observation",
            "description": "List unread emails. Returns summaries of recent unread messages.",
        },
        "draft_email": {
            "category": "action",
            "description": "Compose and save a new email or edit an existing draft. Use this whenever you need to WRITE an email. Do NOT write email content inside finish/final_answer.",
        },
        "send_email": {
            "category": "action",
            "description": "Send an email. REQUIRES: recipient email from recall (never guess). REQUIRES: user confirmation via finish first (Rule 2).",
        },
        "reply_email": {
            "category": "action",
            "description": "Reply to a previously-read email. REQUIRES: you must have used read_email earlier in this session. Do NOT use for new emails.",
        },
        "search_searxng": {
            "category": "action",
            "description": "Search the web for external information. Use only for facts not in your workspace or memory.",
        },
        "remember": {"category": "action", "description": "Store a fact, preference, or rule in long-term memory."},
    }

    def __init__(self, config: Dict):
        self.config = config
        self.llm_conf = config.get("llm", {})

        # Primary local provider (Ollama)
        ollama_base = self.llm_conf.get("base_url", "http://localhost:11434")
        ollama_model = self.llm_conf.get("chat_model") or self.llm_conf.get("model", "llama3.2:latest")

        self.providers = {"ollama": OllamaProvider(ollama_base, ollama_model)}

        # Gemini provider (if key present in env)
        gemini_key = os.environ.get("GOOGLE_API_KEY")
        if gemini_key:
            gemini_model = self.llm_conf.get("tier4_model", "gemini-1.5-flash")
            self.providers["gemini"] = GeminiProvider(gemini_key, gemini_model)

        self.default_provider = "ollama"

    def _get_provider(self, name: str = None) -> LLMProvider:
        """Return the requested provider, falling back to ollama."""
        name = name or self.default_provider
        return self.providers.get(name, self.providers["ollama"])

    def generate_plan(
        self,
        user_input: str,
        manifests: List[Dict],
        context: str = "",
        assistant_name: str = "Bregger",
        assistant_persona: str = "",
    ) -> Dict:
        """Call LLM to generate a structured plan."""
        prompt = f"User Input: {user_input}\n\nOutput JSON Plan:"
        system = self.SYSTEM_PROMPT.format(
            assistant_name=assistant_name,
            assistant_persona=assistant_persona,
            manifests=json.dumps(manifests, indent=2),
            context=context,
        )

        try:
            provider = self._get_provider()
            response = provider.generate(prompt, system=system, json_format=True)
            return json.loads(response)
        except Exception as e:
            return {"intent": "error", "error": str(e)}

    def generate_step(
        self,
        original_request: str,
        scratchpad: List[Step],
        manifests: List[Dict],
        context: str = "",
        step_num: int = 1,
        assistant_name: str = "Bregger",
        assistant_persona: str = "",
    ) -> Step:
        """Generate one ReAct step: Thought → Action → Action Input.

        Calls Ollama with the ReAct prompt and parses the text response.
        Falls back to a finish step on parse error.
        """
        # Build compact tool list from manifests
        # Skip tools marked operational=false (setup/config tools belong outside the loop)
        obs_lines = []
        act_lines = []
        for m in manifests:
            for t in m.get("tools", []):
                if t.get("operational", True) is False or t.get("access") == "operator":
                    continue

                # Check for override
                override = self.REACT_TOOL_OVERRIDES.get(t["name"], {})
                desc = override.get("description", t.get("description", ""))
                category = override.get("category", "action")

                # Handle both schema formats:
                # - New (flat list):  "parameters": [{"name": "filepath", ...}]
                # - Old (JSON Schema): "parameters": {"type": "object", "properties": {...}}
                raw_params = t.get("parameters", [])
                if isinstance(raw_params, list):
                    param_names = [p.get("name", "") for p in raw_params if isinstance(p, dict)]
                elif isinstance(raw_params, dict):
                    # Old-style JSON Schema: extract from properties dict
                    props = raw_params.get("properties", {})
                    param_names = list(props.keys()) if isinstance(props, dict) else []
                else:
                    param_names = []

                param_str = f"({', '.join(param_names)})" if param_names else "()"
                line = f"- {t['name']}{param_str}: {desc}"

                if category == "observation":
                    obs_lines.append(line)
                else:
                    act_lines.append(line)

        obs_list = "\n".join(obs_lines) or "No observation tools."
        act_list = "\n".join(act_lines) or "No action tools."

        scratchpad_text = self._compress_scratchpad(scratchpad, step_num)

        date_context = self._resolve_temporal_context(original_request)

        # Build combined context block
        ctx_parts = []
        ctx_body = context or "No user context available."
        ctx_parts.append(f"USER CONTEXT (do NOT use a tool if the answer is here):\n{ctx_body}")

        if scratchpad_text:
            ctx_parts.append(f"PROGRESS SO FAR:\n{scratchpad_text}")

        convo_history = "\n\n".join(ctx_parts)

        prompt = self.REACT_SYSTEM_PROMPT
        prompt = prompt.replace("{observation_tools}", obs_list)
        prompt = prompt.replace("{action_tools}", act_list)
        prompt = prompt.replace("{date_context}", date_context)
        prompt = prompt.replace("{user_message}", original_request)
        prompt = prompt.replace("{conversation_history}", convo_history)
        # Unescape {{ -> { and }} -> } (Python .format() escape sequences used in the template)
        prompt = prompt.replace("{{", "{").replace("}}", "}")

        # Full prompt logging — always on during platform build
        import os as _os

        print(f"📋 PROMPT [{len(prompt)} chars] context=[{len(context)} chars]", flush=True)
        if _os.environ.get("BREGGER_DEBUG") == "1":
            print(f"📄 FULL PROMPT:\n{prompt}\n---END PROMPT---", flush=True)

        try:
            provider = self._get_provider()
            raw_response = provider.generate(prompt, json_format=False)
            print(f"🔬 RAW STEP {step_num} [{len(raw_response)} chars]:\n{raw_response[:600]}\n---", flush=True)
        except Exception as e:
            import traceback

            print(f"🔴 GENERATE_STEP EXCEPTION:", flush=True)
            traceback.print_exc()
            return Step(
                step_num=step_num,
                thought=f"LLM call failed: {e}",
                tool="finish",
                tool_input={"final_answer": f"I encountered a technical error: {e}"},
            )

        thought_m = re.search(r"Thought:\s*(.+?)(?=\nAction:|\Z)", raw_response, re.DOTALL)
        thought = thought_m.group(1).strip() if thought_m else raw_response[:200]

        # Signal is no longer requested in the ReAct prompt. Default to None.
        signal = None

        action_m = re.search(r"Action:\s*([a-zA-Z0-9_]+)", raw_response)
        input_m = re.search(r"Action Input:\s*(\{.*?\}|\[.*?\])", raw_response, re.DOTALL)

        action = action_m.group(1).strip().lower() if action_m else "finish"
        if not action_m and "Action Input:" in raw_response:
            print(
                "⚠️ PARSE WARNING: No 'Action:' line found but 'Action Input:' exists. Falling back to finish.",
                flush=True,
            )
        tool_input = {}

        # Truncation guard: if Action Input exists but has no valid JSON closure, treat as truncated
        raw_input_m = re.search(r"Action Input:\s*(\{.+)", raw_response, re.DOTALL)
        if raw_input_m and not input_m:
            _warn_msg = f"truncation_guard: {raw_input_m.group(1)[:80]}"
            print(f"⚠️ PARSE WARNING: Truncation guard fired for: {raw_input_m.group(1)[:50]}...", flush=True)
            s = Step(
                step_num=step_num,
                thought=thought,
                tool="finish",
                tool_input={},
                tool_output={
                    "system_note": (
                        "Your previous response was truncated and the JSON could not be parsed. "
                        "Retry with a shorter, simpler Action Input."
                    )
                },
                parse_warning=_warn_msg,
            )
            return s

        _parse_warning: Optional[str] = None
        if input_m:
            try:
                tool_input = json.loads(input_m.group(1))
            except Exception as e:
                print(
                    f"⚠️ PARSE WARNING: Action Input JSON failed to parse: {e} | Text: {input_m.group(1)[:150]}",
                    flush=True,
                )

                # Robust fallback: Try to extract final_answer from raw broken JSON if it exists
                # Extensive multi-line text (like drafts) can break JSON parsing. Use best-effort fallback.
                fallback_m = re.search(r'"final_answer"\s*:\s*"(.*)"', input_m.group(1), re.DOTALL)
                if fallback_m:
                    tool_input = {"final_answer": fallback_m.group(1).strip()}
                    _parse_warning = f"json_parse_recovered: {str(e)[:120]}"
                    print("🔧 PARSE RECOVERY: Extracted final_answer from broken JSON.", flush=True)
                else:
                    tool_input = {"raw": input_m.group(1)[:500]}
                    _parse_warning = f"json_parse_unrecovered: {str(e)[:120]}"
        else:
            # Try shorthand 'Input:' before falling back to thought (model occasionally omits 'Action')
            scoped_input_m = re.search(r"^Input:\s*(\{.*?\})", raw_response, re.MULTILINE | re.DOTALL)
            if scoped_input_m:
                try:
                    tool_input = json.loads(scoped_input_m.group(1))
                    print(
                        f"⚠️ PARSE WARNING: Used 'Input:' shorthand fallback for action '{action}'. Params: {str(tool_input)[:100]}",
                        flush=True,
                    )
                except Exception:
                    print(
                        f"⚠️ PARSE WARNING: 'Input:' fallback found but JSON failed for action '{action}'. Defaulting to empty dict.",
                        flush=True,
                    )
                    tool_input = {}
            elif action == "finish":
                # Only use thought as last-resort fallback when there is truly no JSON answer block
                tool_input = {"final_answer": thought}
            else:
                print(
                    f"⚠️ PARSE WARNING: Action is '{action}' but no Action Input found. Defaulting to empty dict.",
                    flush=True,
                )
                tool_input = {}

        print(f"🔧 PARSED: tool={action} params={str(tool_input)[:150]}", flush=True)

        return Step(
            step_num=step_num,
            thought=thought,
            tool=action,
            tool_input=tool_input,
            signal=signal,
            raw_prompt=prompt,
            timestamp=datetime.now().isoformat(),
            parse_warning=_parse_warning,
        )

    def _compress_scratchpad(self, scratchpad: List["Step"], current_step: int) -> str:
        """Progressive compression with intelligent data densification instead of blind truncation."""
        result = []
        for step in scratchpad:
            # First, pull out the raw output data and densify it
            _RESERVED = {"status", "message"}
            raw_data = {k: v for k, v in step.tool_output.items() if k not in _RESERVED}
            densified_out = self._densify_data(raw_data, step.tool) if raw_data else "No supplementary data."

            # Reconstruct the tool output dict cleanly for the LLM
            clean_output = {"status": step.tool_output.get("status", "success")}
            if "message" in step.tool_output:
                clean_output["message"] = step.tool_output["message"]
            if densified_out != "No supplementary data.":
                clean_output["data"] = densified_out

            out_str = str(clean_output)
            # Safe truncation just in case, but densification should have made it very compact
            if len(out_str) > 3000:
                out_str = out_str[:3000] + "... [truncated]"

            if step.step_num >= current_step - 1:
                # Full step
                text = (
                    f"Step {step.step_num}:\n"
                    f"  Thought: {step.thought}\n"
                    f"  Action: {step.tool}\n"
                    f"  Input: {json.dumps(step.tool_input, separators=(',', ':'))}\n"
                    f"  Output: {out_str}"
                )
                result.append(text)
            else:
                # One-liner
                input_summary = json.dumps(step.tool_input, separators=(",", ":"))[:60]
                output_hint = out_str[:80]
                result.append(f"Step {step.step_num}: {step.tool}({input_summary}) → {output_hint}")

        return "\n\n".join(result) if result else "No steps taken yet."

    def _resolve_temporal_context(self, user_input: str) -> str:
        """
        Injects a static baseline of relative dates plus dynamically resolved
        temporal expressions found in the user input.

        Nothing is injected unless the user's message contains temporal language.
        This prevents the model from autonomously applying date filters on
        non-time-related requests (e.g. "open the email from Miranda").
        """
        now = datetime.now()
        text = user_input.lower()

        WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

        # Temporal trigger check — only inject if the user message contains time-related language
        _STATIC_TRIGGERS = {
            "today",
            "tonight",
            "now",
            "tomorrow",
            "yesterday",
            "this morning",
            "this afternoon",
            "this evening",
            "this week",
            "last week",
            "next week",
            "this weekend",
            "next weekend",
            "this month",
            "last month",
            "next month",
        }
        has_temporal = any(t in text for t in _STATIC_TRIGGERS) or any(day in text for day in WEEKDAYS)

        if not has_temporal:
            return ""  # No date context injected — model won't apply phantom date filters

        # 1. Static Baseline (Only injected when temporal language is detected)
        date_format = "%A, %B %-d, %Y"
        if sys.platform == "win32":
            date_format = "%A, %B %#d, %Y"

        base = {
            "CURRENT DATE": now.strftime(date_format),
            "YESTERDAY": (now - timedelta(days=1)).strftime(date_format),
            "TOMORROW": (now + timedelta(days=1)).strftime(date_format),
            "NEXT WEEK": (now + timedelta(weeks=1)).strftime(date_format),
            "THIS WEEKEND": (now + timedelta(days=(5 - now.weekday()) % 7 or 7)).strftime(date_format),
        }

        # 2. Dynamic Resolution (re + calendar)
        resolved = {}

        # next <weekday>
        m = re.search(r"next (monday|tuesday|wednesday|thursday|friday|saturday|sunday)", text)
        if m:
            day_name = m.group(1)
            target = WEEKDAYS.index(day_name)
            days_ahead = (target - now.weekday()) % 7 or 7
            resolved[f"NEXT {day_name.upper()}"] = (now + timedelta(days=days_ahead)).strftime(date_format)

        # last <weekday> of the month
        m = re.search(r"last (monday|tuesday|wednesday|thursday|friday|saturday|sunday) of (?:the )?month", text)
        if m:
            day_name = m.group(1)
            target = WEEKDAYS.index(day_name)
            _, last_day = calendar.monthrange(now.year, now.month)
            d = datetime(now.year, now.month, last_day)
            while d.weekday() != target:
                d -= timedelta(days=1)
            resolved[f"LAST {day_name.upper()} OF THE MONTH"] = d.strftime(date_format)

        # next month
        if "next month" in text:
            m = now.month % 12 + 1
            y = now.year + (1 if now.month == 12 else 0)
            _, last_in_next = calendar.monthrange(y, m)
            resolved["NEXT MONTH"] = datetime(y, m, min(now.day, last_in_next)).strftime(date_format)

        # Combine
        lines = [f"{k}: {v}" for k, v in base.items()]
        if resolved:
            lines.append("RESOLVED FROM REQUEST:")
            for k, v in resolved.items():
                lines.append(f"  {k}: {v}")

        return "\n".join(lines)

    def _densify_data(self, data: Any, tool: str) -> str:
        """
        Convert potentially bulky JSON tool output into lean Markdown/text.
        Reduces 'JSON tax' (tokens) for CPU-bound LLMs.
        """
        if not data or data == "No supplementary data.":
            return str(data)

        # 1. Email densification (list_unread)
        if tool == "list_unread":
            inner = data.get("data") if isinstance(data, dict) and "data" in data else data
            if isinstance(inner, list):
                from datetime import datetime, timezone

                def _friendly_date(date_str: str) -> str:
                    if not date_str:
                        return ""
                    try:
                        # dt will have tzinfo if fromisoformat handles it
                        dt = datetime.fromisoformat(date_str)
                        # Local current time on the NucBox
                        now = datetime.now().astimezone()  # aware, local time
                        # Convert email date to NucBox local timezone
                        dt_local = (
                            dt.astimezone() if dt.tzinfo else dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
                        )

                        # Generate local time string
                        hour = dt_local.strftime("%I").lstrip("0") or "12"
                        minute = dt_local.strftime("%M")
                        period = dt_local.strftime("%p").lower()
                        time_str = f"{hour}:{minute}{period}"

                        # Calendar-day logic
                        if dt_local.date() == now.date():
                            return f"Today {time_str}"
                        elif dt_local.date() == (now - timedelta(days=1)).date():
                            return f"Yesterday {time_str}"

                        # Fallback to relative days/weeks
                        delta = now - dt_local
                        days_ago = delta.days
                        if days_ago < 7:
                            return f"{dt_local.strftime('%A')} ({days_ago}d ago)"
                        elif days_ago < 60:
                            weeks = days_ago // 7
                            return f"{dt_local.strftime('%b')} {dt_local.day} ({weeks}w ago)"
                        else:
                            return f"{dt_local.strftime('%b')} {dt_local.day}"
                    except Exception as e:
                        print(f"⚠️ [date_format] Failed to parse date '{date_str}': {e}", flush=True)
                        return date_str

                lines = []
                for email in inner:
                    subject = email.get("subject", "No Subject")
                    sender = email.get("from", {}).get("name") or email.get("from", {}).get("addr", "Unknown")
                    date = _friendly_date(email.get("date", ""))
                    lines.append(f"📧 {sender} — {date}\n   {subject}")

                if not lines:
                    return "📬 No unread emails"

                # Summary header with total count
                displayed = len(inner)
                total_unread = data.get("total_unread", displayed) if isinstance(data, dict) else displayed
                if total_unread > displayed:
                    header = f"📬 {displayed} of {total_unread} unread"
                else:
                    header = f"📬 {total_unread} unread"

                return header + "\n\n" + "\n\n".join(lines)

        # 2. Email search densification (search_emails)
        if tool == "search_emails" and isinstance(data, dict):
            emails = data.get("emails", [])
            query = data.get("query", "")
            if not emails:
                return f"No emails found matching query: {query}"
            lines = [f"Found {len(emails)} email(s) for: {query}"]
            for email in emails:
                sender = email.get("from", "Unknown")
                # Simplify "Last, First (Org) <email>" → "First Last"
                if "<" in sender:
                    sender = sender.split("<")[0].strip().rstrip(",").strip()
                subject = email.get("subject", "No Subject")
                date_str = email.get("date", "")
                try:
                    from datetime import datetime

                    dt = datetime.fromisoformat(date_str)
                    dt_local = dt.astimezone() if dt.tzinfo else dt
                    friendly = f"{dt_local.strftime('%b')} {dt_local.day}"
                except Exception as e:
                    print(f"⚠️ [search_date_format] Failed to parse date '{date_str}': {e}", flush=True)
                    friendly = date_str[:10] if date_str else ""
                lines.append(f"- [{email.get('id')}] {sender}: {subject} ({friendly})")
            return "\n".join(lines)

        # 3. Search densification (search)
        if tool == "search" and isinstance(data, dict):
            if data.get("answer"):
                return f"SYNTHESIZED ANSWER: {data['answer']}"
            if data.get("snippets"):
                return "\n".join([f"- {s}" for s in data["snippets"]])
            return str(data)

        # 3. Memory densification (recall)
        if tool == "recall" and isinstance(data, list):
            lines = []
            for item in data:
                content = item.get("content", "")
                tag = item.get("tag", "note")
                ts = item.get("timestamp", "")
                lines.append(f"- [{tag}] {content} ({ts})")
            return "\n".join(lines) if lines else "No memories found."

        # 4. Email body densification (summarize_email)
        if tool == "summarize_email" and isinstance(data, dict):
            # Detect nesting (summarize_email tool returns findings inside "data")
            inner = data.get("data") if "data" in data else data

            subject = inner.get("subject", "")
            sender = inner.get("from", "")
            body = inner.get("body", "")

            # Strip MIME-style headers if present.
            body_text = body.strip()
            if body_text:
                import re
                from urllib.parse import urlparse

                parts = re.split(r"\r?\n\r?\n", body_text, 1)
                if len(parts) > 1 and ": " in parts[0] and len(parts[0]) < 1000:
                    body_text = parts[1].strip()

                def _shorten_url(match):
                    url = match.group(0)
                    try:
                        parse_url = url if url.startswith("http") else f"http://{url}"
                        domain = urlparse(parse_url).netloc
                        if domain.startswith("www."):
                            domain = domain[4:]
                        if domain:
                            return f"[LINK: {domain}]"
                    except:
                        pass
                    return "[LINK]"

                # Robust HTML and URL stripping
                # 0. Remove <style>, <script>, and HTML comment blocks entirely first.
                #    Critical for MJML/SES marketing emails whose inline CSS can be 10-20KB,
                #    which would consume the entire 4000-char token budget before any content.
                body_text = re.sub(r"<style[^>]*>.*?</style>", " ", body_text, flags=re.DOTALL | re.IGNORECASE)
                body_text = re.sub(r"<script[^>]*>.*?</script>", " ", body_text, flags=re.DOTALL | re.IGNORECASE)
                body_text = re.sub(r"<!--.*?-->", " ", body_text, flags=re.DOTALL)
                # 1. Remove remaining HTML tags
                body_text = re.sub(r"<[^>]+>", " ", body_text)
                # 2. Shorten URLs to include only their domain
                body_text = re.sub(r'https?://[^\s<>"]+|www\.[^\s<>"]+', _shorten_url, body_text)
                # 3. Collapse horizontal whitespace but PRESERVE single newlines for readability.
                #    Collapse 3+ newlines into 2 (paragraph break).
                body_text = re.sub(r"[^\S\r\n]+", " ", body_text)
                body_text = re.sub(r"\n{3,}", "\n\n", body_text)
                body_text = body_text.strip()
            else:
                body_text = "(no body)"

            lines = []
            if subject:
                lines.append(f"Subject: {subject}")
            if sender:
                lines.append(f"From: {sender}")
            lines.append("")
            lines.append(body_text[:4000])  # Increased to 4000, now packed with pure text
            return "\n".join(lines)

        # Fallback: compact JSON (no whitespace)
        try:
            return json.dumps(data, separators=(",", ":"))
        except:
            return str(data)

    def generate_report(self, user_input: str, plan: Dict, results: Any, context: str = "") -> str:
        """Call Ollama to format tool results into a nice message (The 'R' in P-D-A-R)."""
        # Defensive guard: if results is not a dict (e.g. string from a buggy path), wrap it
        if not isinstance(results, dict):
            results = {"status": "success", "content": str(results)}
        status = results.get("status", "success")
        msg = results.get("message")
        output_type = plan.get("output_type", "synthesis")  # Default: synthesize

        # ── Raw passthrough — skip Ollama entirely ───────────────────
        if output_type == "raw":
            content = results.get("content") or results.get("items") or results.get("data")
            if content:
                return str(content)
            return msg or "No content returned."

        # ── Action confirmation — silent success, no narration ───────
        if output_type == "action":
            if status == "error":
                return f"❌ {msg or 'Something went wrong.'}"
            return f"✅ {msg or 'Done.'}"

        # ── Synthesis — route through LLM (existing behaviour) ───────
        # Collect ALL payload keys — tools may use any key (data, content, items, draft, etc.)
        _RESERVED = {"status", "message"}
        raw_data = {k: v for k, v in results.items() if k not in _RESERVED}

        # Densify data before sending to LLM
        tool_name = plan.get("tool", "none")
        densified_data = self._densify_data(raw_data, tool_name) if raw_data else "No supplementary data."

        # Construct specific prompt for the 'Report' phase
        # Note: If msg is None, we don't mention it to avoid LLM confusion
        msg_context = f"Tool Message: {msg}\n" if msg else ""

        prompt = (
            f"RAW TOOL DATA (FACTS):\n{densified_data}\n\n"
            f"User Input: {user_input}\n"
            f"Action Taken: {tool_name}\n"
            f"Status: {status}\n"
            f"{msg_context}"
            f"REPORTING GUIDELINES:\n"
            f"1. Base your answer entirely on the 'RAW TOOL DATA' or 'Tool Message' provided above.\n"
            f"2. Synthesize the raw data into a friendly, proactive 1-2 sentence summary.\n"
            f"3. Present information conversationally. Avoid bulleted lists unless explicitly requested by the user.\n"
            f"4. If a tool failed, summarize the results professionally and suggest a fix if configuration led to the failure.\n"
            f"5. Maintain a concise and helpful tone.\n\n"
            f"User Context (for reference only):\n{context}"
        )

        try:
            provider = self._get_provider()
            text = provider.generate(prompt, json_format=False)

            # Standardize: remove repeated legacy prefixes (case-insensitive)
            text = re.sub(r"^((Bregger|Assistant|AI|System):\s*)+", "", text, flags=re.IGNORECASE)
            return text
        except Exception as e:
            import traceback

            print(f"🔴 GENERATE_REPORT EXCEPTION:", flush=True)
            traceback.print_exc()
            return f"Action complete: {results.get('message', str(results))}"


class BreggerExecutive:
    """Validates and executes tools (The 'D' and 'A' in P-D-A-R)."""

    def __init__(self, registry: SkillRegistry):
        self.registry = registry

    def validate_plan(self, plan: Dict) -> bool:
        """Check if the plan matches registered skills and tools."""
        intent = plan.get("intent")
        skill_name = plan.get("skill")
        tool_name = plan.get("tool")

        # Treat missing or 'fallback' skill as "none"
        if skill_name is None or skill_name.lower() in ["none", "fallback", "null"]:
            skill_name = "none"
            plan["skill"] = "none"

        # Allow plans that don't need skills (greetings, fallbacks handled by router)
        if skill_name == "none":
            return True

        if skill_name not in self.registry.skills:
            # Fallback: check if the 'skill_name' provided is actually a tool name
            for s_name, s_info in self.registry.skills.items():
                if any(t["name"] == skill_name for t in s_info["manifest"].get("tools", [])):
                    skill_name = s_name
                    plan["skill"] = s_name
                    break
            else:
                return False

        manifest = self.registry.skills[skill_name]["manifest"]
        tool_exists = any(t["name"] == tool_name for t in manifest.get("tools", []))
        return tool_exists

    def execute_plan(self, plan: Dict, beliefs: Optional[Dict] = None) -> Any:
        """Dynamically load and run the skill's Python code."""
        skill_name = plan.get("skill")
        tool_name = plan.get("tool")
        # Create a copy to prevent mutating the ReAct step memory by reference.
        # execute_plan() injects _workdir, beliefs etc. into params — without a copy,
        # those injections bleed back into step.tool_input and appear in the scratchpad.
        params = dict(plan.get("parameters", {}))

        if skill_name not in self.registry.skills:
            if skill_name == "none":
                return {"status": "success", "message": "No action required."}
            return {"status": "error", "message": f"Skill '{skill_name}' not found in registry."}

        skill_path = self.registry.skills[skill_name]["path"]
        tool_file = skill_path / "tools" / f"{tool_name}.py"

        if not tool_file.exists():
            return {"status": "error", "message": f"Tool implementation file missing: {tool_file}"}

        # Inject workdir so skills never need to rely on BREGGER_WORKDIR env var
        if hasattr(self, "workdir") and self.workdir:
            params["_workdir"] = str(self.workdir)

        # Inject beliefs (Reference Data) as context
        if beliefs:
            # Flatten or pass as-is? Let's pass as-is so tools can access anything
            for k, v in beliefs.items():
                if k not in params:  # Don't overwrite explicit params
                    params[k] = v

        # Resolve Semantic Time Tokens
        # If any parameter looks like a time token, resolve it.
        # Specifically look for 'relative_time', or params ending in '_date'
        rel_time = params.get("relative_time")
        if rel_time:
            resolved = _resolve_relative_time(rel_time)
            if resolved:
                # Map to 'after_date' by default for email search tools
                if "after_date" not in params:
                    params["after_date"] = resolved

        # Check other date fields for tokens (e.g. after_date: "2w_ago")
        for k in list(params.keys()):
            if k.endswith("_date") or k in ["since", "before"]:
                val = params[k]
                if isinstance(val, str) and not re.match(r"^\d{4}-\d{2}-\d{2}$", val):
                    resolved = _resolve_relative_time(val)
                    if resolved:
                        params[k] = resolved

        # Safety Net (Option C): Inverted Range Protection
        # If the model or resolver flipped the dates, swap them so the tool doesn't fail.
        after = params.get("after_date")
        before = params.get("before_date")
        if after and before and re.match(r"^\d{4}-\d{2}-\d{2}$", after) and re.match(r"^\d{4}-\d{2}-\d{2}$", before):
            if after > before:
                # Range inverted (e.g. after Mar 14, before Jan 14). Swap.
                params["after_date"], params["before_date"] = before, after

        # Clean empty strings from search commands to keep himalaya happy
        for k in ["from_query", "subject_query", "body_query"]:
            if params.get(k) == "":
                params.pop(k)

        try:
            # Add the skill's tools directory to sys.path so tools can import sibling files
            # (e.g. `from _google_auth import gcal_request`). This removes the need for
            # `sys.path.insert` hacks inside the tool scripts themselves.
            tools_dir = str(skill_path / "tools")
            path_injected = False
            if tools_dir not in sys.path:
                sys.path.insert(0, tools_dir)
                path_injected = True

            try:
                # Dynamic import
                spec = importlib.util.spec_from_file_location(tool_name, tool_file)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                if hasattr(module, "run"):
                    return module.run(params)
                else:
                    return {"status": "error", "message": f"Tool '{tool_name}' missing 'run' function."}
            finally:
                if path_injected:
                    sys.path.remove(tools_dir)
        except Exception as e:
            return {"status": "error", "message": f"Execution error: {str(e)}"}


class MockRouter:
    """A deterministic router for testing without a live LLM."""

    def __init__(self):
        self._step_sequence: List[Step] = []  # scripted steps for ReAct tests
        self._step_idx = 0
        self.providers = {}
        self.default_provider = "mock"

    def _get_provider(self, name=None):
        return self

    def generate(self, prompt, system="", json_format=False):
        return "MOCK SUMMARY: This is a compacted history summary."

    def set_step_sequence(self, steps: List[Step]):
        """Inject a scripted step sequence for ReAct loop tests."""
        self._step_sequence = steps
        self._step_idx = 0

    def generate_plan(self, user_input: str, manifests: List[Dict], context: str = "") -> Dict:
        user_input = user_input.lower()
        if "email" in user_input:
            return {"skill": "email", "tool": "list_unread", "parameters": {"count": 3}, "intent": "check_email"}
        # A simple greeting context check for mock
        if "hi" in user_input and "Dan" in context:
            return {"intent": "greet", "skill": "none", "tool": "none"}

        return {"intent": "fallback", "skill": "none", "tool": "none"}

    def generate_step(
        self,
        original_request: str,
        scratchpad: List[Step],
        manifests: List[Dict],
        context: str = "",
        step_num: int = 1,
        assistant_name: str = "Bregger",
        assistant_persona: str = "",
    ) -> Step:
        """Return the next scripted step, or a finish step if sequence exhausted."""
        if self._step_idx < len(self._step_sequence):
            step = self._step_sequence[self._step_idx]
            step.step_num = step_num
            self._step_idx += 1
            return step
        # Default: finish
        return Step(
            step_num=step_num,
            thought="Task complete.",
            tool="finish",
            tool_input={"final_answer": "Done."},
        )

    def generate_report(self, user_input: str, plan: Dict, results: Any, context: str = "") -> str:
        """Mock renderer."""
        if plan.get("intent") == "greet":
            # Guard against missing startup context

            # Guard against missing startup context
            if "startup: " in context:
                startup_line = context.split("startup: ")[1]
                startup_name = startup_line.split("\n")[0]
            else:
                startup_name = "your project"
            return f"Hello Dan! I see you are working on {startup_name}."
        return f"[Mock Report] Context: {context[:30]}... Results: {results.get('message', str(results))}"


class Caretaker:
    """The proactive layer of Bregger that nudges the user based on context."""

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def check_pulse(self) -> Optional[Dict[str, Any]]:
        """Scan traces and beliefs to find reasons to nudge the user."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Find recent failed traces (last 1 hour)
                cursor = conn.execute("""
                    SELECT intent, plan FROM traces 
                    WHERE status='failed' 
                    AND created_at > datetime('now', '-1 hour')
                    LIMIT 1
                """)
                failed = cursor.fetchone()
                if failed:
                    return {
                        "type": "recovery",
                        "reason": f"Last action '{failed[0]}' failed. Should I try a different approach?",
                        "meta": failed[1],
                    }
        except Exception as e:
            print(f"⚠️ Caretaker error: {e}")
        return None


# ---------------------------------------------------------------------------
# Control Plane — deterministic fast path (5 locked intents, read-only)
# ---------------------------------------------------------------------------

# Domain nouns that disqualify bare "search ___" from matching search_web
_DOMAIN_NOUNS = {
    "email",
    "emails",
    "inbox",
    "mail",
    "task",
    "tasks",
    "reminder",
    "reminders",
    "weather",
    "forecast",
    "clima",
}


def normalize_input(text: str) -> str:
    """Lowercase, collapse whitespace, strip outer punctuation."""
    t = (text or "").strip().lower()
    t = re.sub(r"[^\w\s]", " ", t)  # punctuation → space
    t = re.sub(r"\s+", " ", t).strip()
    return t


class KeywordRouter:
    """Deterministic intent matcher.  Returns an intent object or None.

    Rules:
      • only HIGH confidence matches are returned
      • if entity extraction is incomplete → return None (fail closed)
      • read-only intents only — no writes, no sends
    """

    def __init__(self):
        self._patterns = self._build_patterns()

    def register(self, regex: str, intent: str, extractor=None):
        """Add a new pattern to the router at runtime."""
        self._patterns.append((re.compile(regex), intent, extractor))

    # ---- pattern definitions ------------------------------------------------

    @staticmethod
    def _build_patterns():
        patterns = []

        def _add(regex: str, intent: str, extractor=None):
            patterns.append((re.compile(regex), intent, extractor))

        # --- greeting -----------------------------------------------
        _add(
            r"^(hi|hello|hey|howdy|sup|yo|hola|greetings|good\s+morning|good\s+afternoon|good\s+evening)[.!?\s]*$",
            "greet",
        )

        # --- capability_check -------------------------------------------------
        _add(
            r"\b(what|which|do you have|can you|do you support|list)\b.*\b(tool|tools|skill|skills|capability|capabilities)\b",
            "capability_check",
        )
        _add(
            r"\b(do you see|do you know about|are you aware of)\b.*\b(tool|skill|filesystem|email|memory|search)\b",
            "capability_check",
        )

        # --- status_check -----------------------------------------------------
        _add(r"^(status|ping|are you up|system status|health check)$", "status_check")

        # --- reset -----------------------------------------------------------
        _add(r"^(reset|clear|forget|restart|/reset|/clear)$", "reset")

        # --- clear traces ----------------------------------------------------
        _add(r"^clear traces$", "clear_traces")

        # --- identity updates -------------------------------------------------
        def _extract_name(m):
            name = m.group("name").strip()
            if len(name.split()) > 4:
                return None
            return {"name": name}

        _add(r"^your name (is|should be|will be) (?P<name>.+)$", "update_assistant_name", _extract_name)
        _add(r"^call yourself (?P<name>.+)$", "update_assistant_name", _extract_name)
        _add(r"^my name (is|should be|will be) (?P<name>.+)$", "update_user_name", _extract_name)
        _add(r"^call me (?P<name>.+)$", "update_user_name", _extract_name)

        # --- read_traces -----------------------------------------------------
        def _extract_traces(m):
            limit = m.group("limit")
            try:
                limit = int(limit.strip()) if limit and limit.strip() else 5
            except ValueError:
                limit = 5
            return {"limit": limit}

        _add(r"^traces(?:\s+(?P<limit>\d+))?$", "read_traces", _extract_traces)

        return patterns

    # ---- public API ---------------------------------------------------------

    def match(self, raw_text: str) -> Optional[Dict[str, Any]]:
        """Return an intent object or None."""
        text = normalize_input(raw_text)
        if not text:
            return None

        for regex, intent, extractor in self._patterns:
            m = regex.search(text)
            if m:
                entities = {}
                if extractor is not None:
                    entities = extractor(m)
                    if entities is None:
                        continue  # fail closed — extraction incomplete
                return {
                    "intent": intent,
                    "confidence": "high",
                    "entities": entities,
                    "matched_pattern": regex.pattern,
                }
        return None


class IntentMapper:
    """Converts a normalized intent object into an execution plan."""

    _MAP = {
        # Only trivial, unambiguous intents stay in Tier 1.
        "status_check": None,
        "capability_check": None,
        "greet": None,
        "reset": None,
        "update_assistant_name": None,
        "update_user_name": None,
        "read_traces": None,
    }

    def __init__(self, registry=None):
        self._dynamic_map = {}
        self.registry = registry

    def register(self, intent: str, skill: str, tool: str):
        """Register a dynamic intent mapping."""
        self._dynamic_map[intent] = {"skill": skill, "tool": tool}

    def to_plan(self, intent_obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        intent = intent_obj["intent"]

        # Check static map first
        if intent in self._MAP:
            # Existing static logic...
            if intent == "status_check":
                return {
                    "intent": "status_check",
                    "skill": "none",
                    "tool": "none",
                    "parameters": {},
                    "routed_by": "control_plane",
                }
            if intent == "capability_check":
                return {
                    "intent": "capability_check",
                    "skill": "none",
                    "tool": "none",
                    "parameters": {},
                    "routed_by": "control_plane",
                }
            if intent == "reset":
                return {
                    "intent": "reset",
                    "skill": "none",
                    "tool": "none",
                    "parameters": {},
                    "routed_by": "control_plane",
                }
            if intent == "greet":
                return {
                    "intent": "greet",
                    "skill": "none",
                    "tool": "none",
                    "parameters": {},
                    "routed_by": "control_plane",
                }
            if intent == "update_assistant_name":
                return {
                    "intent": "update_assistant_name",
                    "skill": "none",
                    "tool": "none",
                    "parameters": {"assistant_name": intent_obj["entities"]["name"]},
                    "routed_by": "control_plane",
                }
            if intent == "update_user_name":
                return {
                    "intent": "update_user_name",
                    "skill": "none",
                    "tool": "none",
                    "parameters": {"user_name": intent_obj["entities"]["name"]},
                    "routed_by": "control_plane",
                }
            if intent == "read_traces":
                return {
                    "intent": "read_traces",
                    "skill": "none",
                    "tool": "none",
                    "parameters": {"limit": intent_obj["entities"]["limit"]},
                    "routed_by": "control_plane",
                }

        # Check dynamic map
        if intent in self._dynamic_map:
            mapping = self._dynamic_map[intent]

            # min_tier check (Phase 9): If tool requires Tier 3+, skip Tier 1 path
            if self.registry:
                min_tier = self.registry.get_tool_min_tier(mapping["skill"], mapping["tool"])
                if min_tier >= 3:
                    return None

            # Capture regex groups as parameters
            params = intent_obj.get("entities", {})
            # Look up output_type from registry so generate_report honours it
            output_type = "synthesis"
            if self.registry:
                tool_meta = self.registry.get_tool_meta(mapping["skill"], mapping["tool"])
                if tool_meta:
                    output_type = tool_meta.get("output_type", "synthesis")
            return {
                "intent": intent,
                "skill": mapping["skill"],
                "tool": mapping["tool"],
                "parameters": params,
                "output_type": output_type,
                "routed_by": "control_plane",
            }

        return None


class BreggerCore:
    """The central engine for P-D-A-R."""

    def __init__(self, config_path: str):
        with open(config_path, "r") as f:
            self.config = json.load(f)
        # Priority: config["assistant"]["workdir"] > BREGGER_WORKDIR env var > ~/.bregger
        env_workdir = os.environ.get("BREGGER_WORKDIR")
        self.workdir = Path(
            self.config.get("assistant", {}).get("workdir", env_workdir or os.path.expanduser("~/.bregger"))
        )
        self.db_path = self.workdir / "data" / "bregger.db"
        self.registry = SkillRegistry(str(self.workdir / "skills"))

        self.shadow_matcher = ShadowMatcher()
        self.shadow_matcher.load_manifests(str(self.workdir / "skills"))

        self._ensure_conversation_history_table()
        self._ensure_signals_table()
        self._ensure_tasks_table()
        self._ensure_pinned_topics_table()
        self._ensure_traces_table_migration()
        self._ensure_beliefs_table_migration()
        self._ensure_ledger_table_migration()

        self._load_secrets()

        # Use MockRouter if env var is set
        if os.environ.get("BREGGER_MOCK_ROUTER") == "1":
            self.router = MockRouter()
        else:
            self.router = BreggerRouter(self.config)

        self.executive = BreggerExecutive(self.registry)
        self.executive.workdir = self.workdir
        self.caretaker = Caretaker(self.db_path)

        # Skill Contract (Phase 9): Registry-aware mapping and validation
        self.control_plane = KeywordRouter()
        self.intent_mapper = IntentMapper(self.registry)
        self.registry.validate_manifests()

        # Register dynamic triggers from manifests
        self._register_skill_triggers()

        # Memory Layer Pattern (GEMINI Rule 10)
        self._tool_cache = deque(maxlen=3)
        self._belief_cache = {}
        self._prewarm_memory()

        # ReAct state
        self._pending_action: Optional[Dict] = None  # confirmation gate
        self.step_callback = None  # injected by channel adapter
        self._inference_active = False  # contention guard

        # Email pagination state (Rule 10 — RAM, not DB, session-scoped)
        self._cached_unread: list = []  # full unread list from last list_unread fetch
        self._email_page_offset: int = 0  # current page position

        # Message Mode Classifer (Phase 9 Prerequisite)
        self.classifier = MessageModeClassifier()
        self._last_mode = "conversation"
        self._last_turn_type = "none"

    def _load_secrets(self):
        """Load environment variables from secrets.env if it exists."""
        secrets_path = (
            Path(os.environ.get("XIBI_DEPLOY_DIR", os.path.join(os.path.expanduser("~"), "bregger_deployment")))
            / "secrets.env"
        )
        if secrets_path.exists():
            for line in secrets_path.read_text().splitlines():
                if line.startswith("export "):
                    key_val = line.replace("export ", "").split("=", 1)
                    if len(key_val) == 2:
                        os.environ[key_val[0]] = key_val[1].strip('"').strip("'")

    def _register_skill_triggers(self):
        """Scan loaded skill manifests for control_plane_triggers and register them."""
        for skill_name, skill_info in self.registry.skills.items():
            manifest = skill_info.get("manifest", {})
            triggers = manifest.get("control_plane_triggers", [])
            if not isinstance(triggers, list):
                continue

            for trigger in triggers:
                regex = trigger.get("regex")
                intent = trigger.get("intent")
                tool = trigger.get("tool")
                # Allow trigger to override the skill (e.g. "none" for inline handlers)
                mapped_skill = trigger.get("skill", skill_name)

                if regex and intent and tool:
                    # Special-case: intents that need regex group extraction
                    extractor = None
                    if intent == "email_open":
                        # Capture the first numeric group as `num`
                        extractor = lambda m: {"num": m.group(1)} if m.group(1) else None
                    # Register regex with router
                    self.control_plane.register(regex, intent, extractor)
                    # Register mapping with intent mapper
                    self.intent_mapper.register(intent, mapped_skill, tool)
                    print(f"🎛️ Registered contract trigger: {intent} ({mapped_skill}:{tool})", flush=True)

        # Also load user-defined shortcuts from the Ledger (Phase 1.5)
        try:
            with sqlite3.connect(self.db_path) as conn:
                try:
                    cursor = conn.execute("SELECT content, entity FROM ledger WHERE category='shortcut'")
                    for phrase, action_str in cursor.fetchall():
                        # action_str format expected: "skill_name:tool_name"
                        if phrase and action_str and ":" in action_str:
                            skill_name, tool = action_str.split(":", 1)
                            # Create an exact-match lowercase regex for the phrase
                            safe_phrase = re.escape(phrase.strip().lower())
                            regex = f"^{safe_phrase}[.!?\\s]*$"
                            intent = f"shortcut_{skill_name}_{tool}"

                            self.control_plane.register(regex, intent)
                            self.intent_mapper.register(intent, skill_name, tool)
                            print(f"🎛️ Registered user shortcut: '{phrase}' → {skill_name}:{tool}", flush=True)
                except sqlite3.OperationalError:
                    pass  # Ledger not set up yet
        except Exception as e:
            print(f"⚠️ Failed to load user shortcuts: {e}", flush=True)

    def _ensure_conversation_history_table(self):
        """Create the conversation_history table if it doesn't exist."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS conversation_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_message TEXT NOT NULL,
                        bot_response TEXT NOT NULL,
                        mode TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                # Migration: Add mode column if it doesn't exist
                try:
                    conn.execute("ALTER TABLE conversation_history ADD COLUMN mode TEXT")
                except sqlite3.OperationalError:
                    pass  # Already exists
        except Exception as e:
            print(f"⚠️ Error creating conversation_history table: {e}", flush=True)

    def _ensure_signals_table(self):
        """Create the signals table if it doesn't exist.

        Delegates to ensure_signals_schema() in bregger_utils — single source
        of truth for the signals schema across core and heartbeat.
        """
        ensure_signals_schema(self.db_path)

    def _create_task(
        self,
        goal: str,
        exit_type: str,
        urgency: str,
        due: Optional[str],
        context_compressed: str,
        scratchpad_json: str,
        trace_id: str,
    ) -> str:
        """Create a new task. ask_user tasks get awaiting_reply (single active slot)."""
        import uuid

        task_id = str(uuid.uuid4())
        trace_id = trace_id or str(uuid.uuid4())
        if exit_type == "schedule":
            status = "scheduled"
        elif exit_type == "ask_user":
            status = "awaiting_reply"
        else:
            status = "paused"
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Single Active Slot: demote any existing awaiting_reply task
                if status == "awaiting_reply":
                    conn.execute(
                        "UPDATE tasks SET status='paused', updated_at=CURRENT_TIMESTAMP WHERE status='awaiting_reply'"
                    )
                conn.execute(
                    """
                    INSERT INTO tasks (id, goal, status, exit_type, urgency, due, context_compressed, scratchpad_json, trace_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (task_id, goal, status, exit_type, urgency, due, context_compressed, scratchpad_json, trace_id),
                )
            return task_id
        except Exception as e:
            print(f"⚠️ Failed to create task: {e}", flush=True)
            return ""

    def _resume_task(self, task_id: str, user_input: str) -> str:
        """Resume a task by restoring its scratchpad and re-entering the process_query loop."""
        import json
        import dataclasses

        task = None
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if row:
                    task = dict(row)
        except Exception as e:
            print(f"⚠️ Failed to load task {task_id}: {e}", flush=True)

        if not task:
            return "Sorry, I couldn't find that pending task."

        # Build a synthetic scratchpad step representing "Progress So Far"
        pseudo_step = Step(
            step_num=1,
            thought="Resuming paused task.",
            tool="resume_task",
            tool_input={"user_reply": user_input},
            tool_output={
                "system_note": f"RESUMED TASK.\\nPROGRESS SO FAR:\\n{task.get('context_compressed', '')}\\n\\nUser replied: {user_input}"
            },
        )

        # Mark as done so it doesn't get resumed again
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("UPDATE tasks SET status='done', updated_at=CURRENT_TIMESTAMP WHERE id=?", (task_id,))
                # Mark proposed signals as confirmed
                conn.execute("UPDATE signals SET proposal_status='confirmed' WHERE proposal_status='proposed'")
        except Exception as e:
            print(f"⚠️ Failed to mark task {task_id} done: {e}", flush=True)

        print(f"🔄 Resuming task {task_id} with user reply: {user_input}", flush=True)
        original_trace = task.get("trace_id", "local_resume")
        resume_trace = f"{original_trace}_resume"
        self.log_trace(
            resume_trace,
            "task_resumed",
            {"task_id": task_id, "original_trace": original_trace, "goal": task.get("goal", "")[:100]},
        )
        return self._process_query_internal(
            task.get("goal", ""), force_react=True, _resume_scratchpad=[pseudo_step], trace_id=resume_trace
        )

    def _get_awaiting_task(self) -> Optional[dict]:
        """Get the single task in awaiting_reply state (the active slot)."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tasks WHERE status='awaiting_reply' LIMIT 1").fetchone()
                return dict(row) if row else None
        except Exception as e:
            print(f"⚠️ Failed to get awaiting task: {e}", flush=True)
            return None

    def _cancel_task(self, task_id: str) -> None:
        """Cancel a task and free the active slot."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("UPDATE tasks SET status='cancelled', updated_at=CURRENT_TIMESTAMP WHERE id=?", (task_id,))
                # Dismiss proposed signals so reflection doesn't re-propose
                conn.execute(
                    "UPDATE signals SET proposal_status='dismissed', dismissed_at=CURRENT_TIMESTAMP "
                    "WHERE proposal_status='proposed'"
                )
            print(f"🚫 Task {task_id} cancelled.", flush=True)
        except Exception as e:
            print(f"⚠️ Failed to cancel task {task_id}: {e}", flush=True)

    def _expire_stale_tasks(self):
        """Called by heartbeat to expire old tasks."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE tasks SET status='expired', updated_at=CURRENT_TIMESTAMP WHERE status='paused' AND updated_at < datetime('now', '-7 days')"
                )
                # Expire stale awaiting_reply tasks (24h — slot shouldn't be held forever)
                conn.execute(
                    "UPDATE tasks SET status='expired', updated_at=CURRENT_TIMESTAMP WHERE status='awaiting_reply' AND updated_at < datetime('now', '-1 day')"
                )
        except Exception as e:
            pass

    def _ensure_tasks_table(self):
        """Create the tasks table for the Task Layer."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS tasks (
                        id TEXT PRIMARY KEY,
                        goal TEXT NOT NULL,
                        status TEXT DEFAULT 'open',
                        exit_type TEXT,
                        urgency TEXT DEFAULT 'normal',
                        due DATETIME,
                        trigger TEXT,
                        nudge_count INTEGER DEFAULT 0,
                        last_nudged_at DATETIME,
                        context_compressed TEXT,
                        scratchpad_json TEXT,
                        origin TEXT DEFAULT 'user',
                        trace_id TEXT NOT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
        except Exception as e:
            print(f"⚠️ Failed to ensure tasks table: {e}", flush=True)

    def _ensure_pinned_topics_table(self):
        """Create the pinned_topics table if it doesn't exist."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS pinned_topics (
                        topic TEXT PRIMARY KEY,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
        except Exception as e:
            print(f"⚠️ Failed to ensure pinned_topics table: {e}", flush=True)

    def _ensure_traces_table_migration(self):
        """Add observability columns to traces table if they don't exist."""
        new_columns = [
            ("steps_detail", "TEXT"),
            ("route", "TEXT"),
            ("model", "TEXT"),
            ("raw_prompt", "TEXT"),
            ("started_at", "TEXT"),
            ("total_ms", "INTEGER"),
            ("step_count", "INTEGER"),
            ("total_prompt_tokens", "INTEGER"),
            ("total_response_tokens", "INTEGER"),
            ("overall_tok_per_sec", "REAL"),
            ("final_answer_length", "INTEGER"),
            ("ram_start_pct", "REAL"),
            ("ram_end_pct", "REAL"),
            ("proc_rss_mb", "REAL"),
            ("tier2_shadow", "TEXT"),
        ]
        try:
            with sqlite3.connect(self.db_path) as conn:
                existing = {row[1] for row in conn.execute("PRAGMA table_info(traces)").fetchall()}
                for col_name, col_type in new_columns:
                    if col_name not in existing:
                        conn.execute(f"ALTER TABLE traces ADD COLUMN {col_name} {col_type}")
                        print(f"✅ Migrated traces table: added {col_name}", flush=True)

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS shadow_phrases (
                        phrase TEXT,
                        tool TEXT,
                        hits INTEGER DEFAULT 0,
                        correct INTEGER DEFAULT 0,
                        last_seen DATETIME,
                        source TEXT DEFAULT 'manifest',
                        PRIMARY KEY (phrase, tool)
                    )
                """)
        except Exception as e:
            print(f"⚠️ Failed to migrate traces/shadow table: {e}", flush=True)

    def _ensure_beliefs_table_migration(self):
        """Migrate beliefs table to bi-temporal schema (valid_from/valid_until, no UNIQUE on key)."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Check if migration is needed by looking for the valid_until column
                cols = [row[1] for row in conn.execute("PRAGMA table_info(beliefs)").fetchall()]
                if "valid_until" in cols:
                    return  # Already migrated

                # SQLite cannot ALTER constraints, so we recreate the table
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS beliefs_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        key TEXT,
                        value TEXT,
                        type TEXT,
                        visibility TEXT,
                        metadata TEXT,
                        valid_from DATETIME DEFAULT CURRENT_TIMESTAMP,
                        valid_until DATETIME,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );

                    INSERT INTO beliefs_new (id, key, value, type, visibility, metadata, updated_at)
                        SELECT id, key, value, type, visibility, metadata, updated_at
                        FROM beliefs;

                    DROP TABLE beliefs;

                    ALTER TABLE beliefs_new RENAME TO beliefs;
                """)
                print(
                    "✅ Migrated beliefs table: bi-temporal schema (valid_from, valid_until, UNIQUE removed)",
                    flush=True,
                )
        except Exception as e:
            print(f"⚠️ Failed to migrate beliefs table: {e}", flush=True)

    def _ensure_ledger_table_migration(self):
        """Add decay_days column to ledger table if it doesn't exist."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                existing = {row[1] for row in conn.execute("PRAGMA table_info(ledger)").fetchall()}
                if "decay_days" not in existing:
                    conn.execute("ALTER TABLE ledger ADD COLUMN decay_days INTEGER")
                    print("✅ Migrated ledger table: added decay_days", flush=True)
        except Exception as e:
            print(f"⚠️ Failed to migrate ledger table: {e}", flush=True)

    def _log_signal(self, source, topic_hint, entity_text, entity_type, content_preview, ref_id, ref_source):
        """Insert a signal into the signals table."""
        try:
            # Truncate content_preview to 280 chars
            preview = (content_preview[:277] + "...") if len(content_preview) > 280 else content_preview
            with sqlite3.connect(self.db_path) as conn:
                # Dedup check: skip if same source+ref_id was logged today
                if ref_id:
                    cursor = conn.execute(
                        "SELECT 1 FROM signals WHERE source = ? AND ref_id = ? AND date(timestamp) = date('now')",
                        (source, str(ref_id)),
                    )
                    if cursor.fetchone():
                        return

                conn.execute(
                    """
                    INSERT INTO signals (source, topic_hint, entity_text, entity_type, content_preview, ref_id, ref_source, env)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'production')
                """,
                    (source, topic_hint, entity_text, entity_type, preview, str(ref_id), ref_source),
                )
        except Exception as e:
            print(f"⚠️ Failed to log signal: {e}", flush=True)

    def _get_active_threads(self, days=7):
        """Discover active threads from cross-channel signal recurrence."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                query = """
                    SELECT topic_hint, COUNT(*) as mentions, 
                           COUNT(DISTINCT source) as channels,
                           MAX(timestamp) as last_seen
                    FROM signals
                    WHERE topic_hint IS NOT NULL
                      AND timestamp > datetime('now', ?)
                    GROUP BY topic_hint
                    HAVING COUNT(*) > 1
                    ORDER BY channels DESC, last_seen DESC
                    LIMIT 5
                """
                return [dict(row) for row in conn.execute(query, (f"-{days} days",)).fetchall()]
        except Exception as e:
            print(f"⚠️ Failed to get active threads: {e}", flush=True)
            return []

    def _extract_passive_memory(self, user_input: str, final_answer: str, scratchpad: List[Step]):
        """Async LLM pass to extract and persist durable facts to the Ledger."""
        pm_id = f"pm_{str(uuid.uuid4())[:8]}"
        self.log_trace(pm_id, "passive_memory", {"status": "started"})

        # Brief yield to let the main thread's response reach the user first
        time.sleep(1)

        try:
            tools_used = [s.tool for s in scratchpad] if scratchpad else []

            # 2. Build extraction prompt (memory + signal in one call)
            prompt = f"""You are an extraction utility. Scan the conversation exchange below and produce TWO sections.

SECTION 1 — DURABLE FACTS
Extract concrete facts about the user worth remembering long-term.
Categories allowed: contact, preference, deadline, commitment, fact, interest.
Rules:
- "interest": only extract if enthusiasm is explicit, not filler like "cool" or "nice".
- DO NOT extract transient intents, generic greetings, or questions.

SECTION 2 — SIGNAL
Extract the main topic of this conversation turn (1-3 words) and any named entity (person, company, project).

If nothing is worth persisting in EITHER section, return exactly: NONE

EXCHANGE:
User said: {user_input}
Bot replied: {final_answer}
Tools used: {", ".join(tools_used)}

Respond ONLY with valid JSON. Format:
{{
  "facts": [
    {{"category": "preference", "content": "Prefers morning meetings", "entity": "user"}}
  ],
  "signal": {{
    "topic": "morning meetings",
    "entity_text": null,
    "entity_type": null
  }}
}}
If no durable facts, set "facts" to []. If no clear topic, set "signal" to null.
"""
            # 3. Call LLM directly (bypass routing).
            # Note: provider.generate() acquires inference_lock internally (RLock),
            # so this background thread serializes behind active chat without
            # needing a separate outer lock here.
            provider = self.router._get_provider()
            response = provider.generate(prompt, system="You are an extraction utility that only outputs JSON or NONE.")

            if response.strip() == "NONE":
                self.update_trace(pm_id, {"status": "skipped", "reason": "NONE"}, "completed")
                return

            # 4. Parse JSON (new combined format: {facts: [...], signal: {...}})
            try:
                json_str = response.strip()
                if json_str.startswith("```json"):
                    json_str = json_str[7:]
                if json_str.startswith("```"):
                    json_str = json_str[3:]
                if json_str.endswith("```"):
                    json_str = json_str[:-3]

                parsed = json.loads(json_str.strip())

                # Handle both old format (list) and new format (dict with facts/signal)
                if isinstance(parsed, list):
                    extracted = parsed
                    signal_data = None
                elif isinstance(parsed, dict):
                    extracted = parsed.get("facts", [])
                    signal_data = parsed.get("signal")
                    if not isinstance(extracted, list):
                        extracted = [extracted] if extracted else []
                else:
                    extracted = [parsed]
                    signal_data = None
            except Exception as e:
                self.update_trace(pm_id, {"status": "failed", "error": f"decode error: {e}"}, "failed")
                return

            # 4b. Log chat signal (Phase 1.75 Fix 2)
            if signal_data and signal_data.get("topic"):
                from bregger_utils import normalize_topic as _normalize_topic

                raw_topic = "_".join(signal_data["topic"].lower().split()[:3])
                topic = _normalize_topic(raw_topic) or raw_topic
                self._log_signal(
                    source="chat",
                    topic_hint=topic,
                    entity_text=signal_data.get("entity_text"),
                    entity_type=signal_data.get("entity_type"),
                    content_preview=user_input[:280],
                    ref_id=pm_id,
                    ref_source="passive_memory",
                )
                print(f"🧠 [passive_memory] Chat signal logged: topic={topic}", flush=True)

            # 5. Dedup and Insert
            inserted = 0
            with sqlite3.connect(self.db_path) as conn:
                for item in extracted:
                    category = item.get("category")
                    content = item.get("content")
                    entity = item.get("entity", "")
                    if not category or not content:
                        continue

                    # Dedup check for ledger items (7 days)
                    if category not in ["preference", "fact", "contact"]:
                        cursor = conn.execute(
                            "SELECT 1 FROM ledger WHERE entity = ? AND category = ? AND content LIKE ? AND created_at > datetime('now', '-7 days')",
                            (entity, category, f"%{content[:20]}%"),
                        )
                        if cursor.fetchone():
                            continue
                    else:
                        # Dedup check for beliefs
                        key = entity if entity else content[:50]
                        cursor = conn.execute(
                            "SELECT 1 FROM beliefs WHERE key = ? AND value = ? AND valid_until IS NULL", (key, content)
                        )
                        if cursor.fetchone():
                            continue

                    plan = {
                        "skill": "memory",
                        "tool": "remember",
                        "parameters": {"category": category, "content": content, "entity": entity},
                    }
                    self.executive.execute_plan(plan, beliefs=self._belief_cache)
                    inserted += 1

            self.update_trace(
                pm_id, {"status": "success", "extracted": len(extracted), "inserted": inserted}, "completed"
            )

        except Exception as e:
            print(f"⚠️ [passive_memory] Extraction failed: {e}", flush=True)
            self.update_trace(pm_id, {"status": "failed", "error": str(e)}, "failed")

    def _log_conversation(self, user_message: str, bot_response: str):
        """Persist a conversation turn to the history table."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO conversation_history (user_message, bot_response, mode) VALUES (?, ?, ?)",
                    (user_message[:1000], bot_response[:2000], self._last_mode),
                )
        except Exception as e:
            print(f"⚠️ Error logging conversation: {e}", flush=True)

    def reset_state(self):
        """Clear ephemeral state and wipe conversation history."""
        self._pending_action = None
        self._tool_cache.clear()
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM conversation_history")
        except Exception as e:
            print(f"⚠️ Error resetting history: {e}", flush=True)
            print(f"⚠️ Error logging conversation: {e}", flush=True)

    def _prewarm_memory(self):
        """Initial load of stable reference data and recent traces into RAM."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                # 1. Pre-warm traces (Working Memory)
                cursor = conn.execute("""
                    SELECT intent, status, created_at FROM traces 
                    WHERE status = 'completed' AND intent != 'fallback' AND intent != 'none'
                    ORDER BY created_at DESC LIMIT 3
                """)
                for row in reversed(cursor.fetchall()):
                    self._tool_cache.append({"intent": row[0], "status": row[1], "timestamp": row[2]})

                # 2. Pre-warm beliefs (Reference Data)
                # Load both user-facing and system-level configuration beliefs, filtering out invalidated ones
                cursor = conn.execute(
                    "SELECT key, value FROM beliefs WHERE visibility IN ('user', 'system') AND valid_until IS NULL"
                )
                for key, value in cursor.fetchall():
                    self._belief_cache[key] = value
        except Exception as e:
            print(f"⚠️ Memory pre-warm error: {e}", flush=True)

    def _get_recent_history(self, n: int = 10) -> str:
        """Fetch the last N conversation turns as a formatted string."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT user_message, bot_response FROM conversation_history "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (n,),
                )
                rows = cursor.fetchall()
            if not rows:
                return ""
            # Reverse so oldest is first (chronological order for the LLM)
            turns = []
            for user_msg, bot_resp in reversed(rows):
                turns.append(f"User: {user_msg}")
                turns.append(f"Bregger: {bot_resp}")
            return "\n".join(turns)
        except Exception as e:
            import traceback

            print(f"⚠️ [_prewarm_memory] Failed to load history into RAM: {e}", flush=True)
            traceback.print_exc()
            return ""

    def _get_recent_traces(self) -> str:
        """Fetch the last N successful tool executions from RAM."""
        if not self._tool_cache:
            return ""

        lines = []
        for trace in self._tool_cache:
            created_at = trace["timestamp"]
            ts = created_at.split(" ")[1][:5] if " " in created_at else created_at
            lines.append(f"- {ts}: {trace['intent']} ({trace['status']})")
        return "\n".join(lines)

    def _compact_history(self, history_rows: List[tuple]) -> str:
        """
        Compress session history when it grows long to prevent syntax drift and context overflow.
        Keeps the last 2 turns verbatim, summarizes the rest if above threshold.
        """
        if not history_rows:
            return ""

        # Estimates tokens roughly as chars/4
        def _estimate_tokens(text: str) -> int:
            return len(text) // 4

        raw_turns = []
        for user_msg, bot_resp in reversed(history_rows):
            raw_turns.append(f"User: {user_msg}\nBregger: {bot_resp}")

        full_history = "\n".join(raw_turns)
        threshold = self.config.get("loop", {}).get("conversation_history_compaction_threshold", 2000)

        if _estimate_tokens(full_history) <= threshold:
            return full_history

        # Split: last 2 turns (verbatim) vs older turns (summarized)
        recent_rows = history_rows[:2]  # history_rows is most-recent-first
        older_rows = history_rows[2:]

        if not older_rows:
            return full_history

        recent_verbatim = "\n".join(reversed([f"User: {u}\nBregger: {b}" for u, b in recent_rows]))
        older_text = "\n".join(reversed([f"User: {u}\nBregger: {b}" for u, b in older_rows]))

        compaction_prompt = (
            "Summarize the following conversation history into a concise SESSION SUMMARY. "
            "Focus on: CURRENT GOAL, KEY FACTS extracted, and STATUS of tasks. "
            "IMPORTANT: Preserve ALL IDs (email IDs, draft IDs, etc.) exactly as they appear. "
            "Respond ONLY with the summary.\n\n"
            f"HISTORY TO SUMMARIZE:\n{older_text}"
        )

        try:
            # Use the local provider for compaction
            if not hasattr(self.router, "_get_provider"):
                return full_history

            summary = self.router._get_provider().generate(
                compaction_prompt, system="You are a context compaction utility."
            )
            print(
                f"📉 History compacted ({_estimate_tokens(full_history)} -> {_estimate_tokens(summary)} tokens)",
                flush=True,
            )
            return f"SESSION SUMMARY:\n{summary}\n\n{recent_verbatim}"
        except Exception as e:
            print(f"⚠️ History compaction failed: {e}. Falling back to raw history.", flush=True)
            return full_history

    def _get_active_threads_context(self) -> str:
        """Format active signal threads + pinned topics for prompt injection.

        Phase 2.1: Delegates to bregger_utils.get_active_threads() and
        get_pinned_topics() — the single source of truth shared with the
        heartbeat. Cheap SQL + Python aggregation, no LLM cost.
        """
        try:
            threads = _get_active_threads_shared(self.db_path)
            pinned = _get_pinned_topics_shared(self.db_path)
            pinned_names_lower = {p["topic"].lower() for p in pinned}

            lines = []
            for p in pinned:
                lines.append(f"- [PINNED] {p['topic']}")
            for t in threads:
                if t["topic"].lower() in pinned_names_lower:
                    continue  # Already listed as pinned
                sources = "+".join(t["sources"])
                lines.append(f"- {t['topic']} ({t['count']} signals via {sources})")

            if not lines:
                return ""
            return "ACTIVE THREADS (topics the user has been focused on this week):\n" + "\n".join(lines)

        except Exception as e:
            print(f"⚠️ [_get_active_threads_context] Error: {e}", flush=True)
            return ""

    def _get_user_context(self) -> str:
        """Fetch user beliefs and recent conversation history as context string.

        Conversation history is token-budgeted (not turn-budgeted).
        Budget: 1,500 tokens ≈ 6,000 chars at ~4 chars/token.
        Most-recent-first truncation; reversed so LLM sees chronological order.
        """
        context_parts = []
        # Pull from RAM cache (Reference Data)
        for key, value in self._belief_cache.items():
            label = key.replace("user_", "")
            context_parts.append(f"{label}: {value}")

        # Inject current time and identity for grounding
        context_parts.append(f"current_time: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

        # 1b. Active Threads (Phase 2.1 — cross-channel awareness)
        threads_ctx = self._get_active_threads_context()
        if threads_ctx:
            context_parts.append(f"\n{threads_ctx}")

        # 2. Token-budgeted and compacted conversation history
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT user_message, bot_response FROM conversation_history "
                    "ORDER BY created_at DESC, id DESC LIMIT 10"
                )
                rows = cursor.fetchall()  # most-recent-first

            if rows:
                history = self._compact_history(rows)
                if history:
                    context_parts.append(f"\nRECENT CONVERSATION:\n{history}")
        except Exception as e:
            print(f"⚠️ Error building history context: {e}", flush=True)

        # 3. Inject recent background events (Ledger Category 'background_event')
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Get events from last 8 hours
                cursor = conn.execute(
                    "SELECT content FROM ledger WHERE category='background_event' "
                    "AND created_at > datetime('now', '-8 hours') "
                    "ORDER BY created_at DESC LIMIT 1"
                )
                events = [row[0] for row in cursor.fetchall()]
                if events:
                    # Clean up triple backticks if they exist to avoid nesting issues
                    cleaned_events = [e.replace("```", "'''") for e in events]
                    events_str = "\n".join([f"- {e}" for e in cleaned_events])
                    context_parts.append(f"\nRECENT BACKGROUND ACTIVITY (RECAPS/ALERTS):\n{events_str}")
        except Exception as e:
            import traceback

            print(f"⚠️ [_get_user_context] Failed to load background events: {e}", flush=True)
            traceback.print_exc()

        # Inject recent tool executions (Working Memory from RAM)
        traces = self._get_recent_traces()
        if traces:
            context_parts.append(f"\nRECENT TOOLS PERFORMED:\n{traces}")

        return "\n".join(context_parts)

    def process_query(self, user_input: str, force_react: bool = False):
        """Execute the full P-D-A-R loop with Control Plane fast path.

        Args:
            user_input: The user's message.
            force_react: If True, skip mode classification and the Control Plane
                entirely, routing directly to the ReAct loop. Used by the
                reasoning benchmark to guarantee multi-step execution.
        """
        self._inference_active = True
        try:
            return self._process_query_internal(user_input, force_react=force_react)
        finally:
            self._inference_active = False

    def _process_query_internal(
        self,
        user_input: str,
        force_react: bool = False,
        _resume_scratchpad: Optional[List["Step"]] = None,
        trace_id: Optional[str] = None,
    ):
        trace_id = trace_id or str(uuid.uuid4())
        t0 = time.time()
        context = self._get_user_context()
        normalized = normalize_input(user_input)

        # ── Message Mode Classification ──────────────────────────────
        # Prerequisite for safe routing and Phase 2 signal quality
        # Skip when force_react=True (benchmark mode — go straight to ReAct)
        if force_react:
            mode = "react"
            intent_obj = None
            print(f"🧩 MODE: react (force_react)", flush=True)
        else:
            mode = self.classifier.classify(
                user_input,
                {
                    "pending_action": self._pending_action,
                    "draft_active": self._pending_action.get("intent") == "draft_email"
                    if self._pending_action
                    else False,
                    "last_turn_type": self._last_turn_type,
                },
            )
            self._last_mode = mode
            print(f"🧩 MODE: {mode} (scorecard match)", flush=True)

        # ── Control Plane (Tier 1) ──────────────────────────────────
        # ONLY fire Control Plane if we are in 'command' mode or there is no state conflict
        intent_obj = None
        if not force_react and mode == "command":
            intent_obj = self.control_plane.match(user_input)

        if intent_obj is not None:
            plan = self.intent_mapper.to_plan(intent_obj)
            if plan is not None:
                latency_ms = int((time.time() - t0) * 1000)
                self._log_control_plane_metric(trace_id, user_input, normalized, intent_obj, latency_ms)

                # CLEAR PENDING ACTION if we match a new intent in the Control Plane
                # (Unless the intent is already handled by the gate, but Control Plane takes precedence)
                if self._pending_action:
                    self._pending_action = None

                self.log_trace(trace_id, plan["intent"], plan)

                # Inline handlers — no skill execution needed
                if plan.get("skill") == "none":
                    self.update_trace(trace_id, {}, "completed")
                    if plan.get("intent") == "capability_check":
                        report = self._format_capabilities()
                    elif plan.get("intent") == "greet":
                        name = self._belief_cache.get("user_name", "")
                        greeting = (
                            f"Hey {name}! What can I help you with?" if name else "Hey! What can I help you with?"
                        )
                        report = greeting
                    elif plan.get("intent") == "reset":
                        self.reset_state()
                        report = "State cleared. I've forgotten our recent conversation and any pending actions. How can I help you start fresh?"
                    elif plan.get("intent") == "update_assistant_name":
                        new_name = plan["parameters"]["assistant_name"]
                        self._pending_action = plan
                        report = f"So you want to change my name to {new_name}? (yes/no)"
                    elif plan.get("intent") == "update_user_name":
                        new_name = plan["parameters"]["user_name"]
                        self._pending_action = plan
                        report = f"So you want to change your name to {new_name}? (yes/no)"
                    elif plan.get("intent") == "read_traces":
                        limit = plan["parameters"].get("limit", 5)
                        try:
                            with sqlite3.connect(self.db_path) as conn:
                                # Safe fetch: check if steps_detail column exists
                                try:
                                    cursor = conn.execute(
                                        "SELECT intent, plan, status, created_at, act_results, steps_detail, "
                                        "ram_start_pct, ram_end_pct, proc_rss_mb "
                                        "FROM traces ORDER BY rowid DESC LIMIT ?",
                                        (limit,),
                                    )
                                    rows = cursor.fetchall()
                                except sqlite3.OperationalError:
                                    cursor = conn.execute(
                                        "SELECT intent, plan, status, created_at, act_results, NULL as steps_detail, NULL, NULL, NULL FROM traces ORDER BY rowid DESC LIMIT ?",
                                        (limit,),
                                    )
                                    rows = cursor.fetchall()

                            if not rows:
                                report = "No logs found in the trace database."
                            else:
                                report = f"**Last {len(rows)} Execution Traces:**\n\n"
                                for i, row in enumerate(rows, 1):
                                    (
                                        t_intent,
                                        t_plan_json,
                                        t_status,
                                        t_created_at,
                                        t_act_results_json,
                                        t_steps_detail_json,
                                        t_ram_start,
                                        t_ram_end,
                                        t_proc_rss,
                                    ) = row
                                    t_plan_dict = {}
                                    if t_plan_json:
                                        try:
                                            t_plan_dict = json.loads(t_plan_json)
                                        except:
                                            t_plan_dict = {"raw": t_plan_json}

                                    report += f"**{i}. {t_intent}** (`{t_created_at}`)\n"
                                    report += f"• **Status**: `{t_status}`\n"

                                    if t_steps_detail_json:
                                        try:
                                            t_steps = json.loads(t_steps_detail_json)
                                            if isinstance(t_steps, list) and t_steps:
                                                total_ms = sum(s.get("ms", 0) for s in t_steps)
                                                total_p = sum(s.get("prompt_tokens", 0) for s in t_steps)
                                                total_r = sum(s.get("response_tokens", 0) for s in t_steps)
                                                avg_tps = round(
                                                    sum(s.get("tok_per_sec", 0.0) for s in t_steps) / len(t_steps), 1
                                                )
                                                ram_info = ""
                                                if t_ram_start is not None and t_ram_end is not None:
                                                    ram_info = f" | **RAM**: {t_ram_start:.0f}%→{t_ram_end:.0f}%"
                                                    if t_proc_rss:
                                                        ram_info += f" (proc {t_proc_rss:.0f}MB)"
                                                report += f"• **Steps**: {len(t_steps)} | **Time**: {total_ms}ms | **Tokens**: {total_p}p + {total_r}r @ {avg_tps} tok/s{ram_info}\n"
                                                report += "• **Steps Detail**:\n"
                                                for step in t_steps:
                                                    step_tool = step.get("tool", "unknown")
                                                    step_dur = step.get("ms", 0)
                                                    step_out = step.get("output_hint", "")
                                                    step_thought = step.get("thought", "")
                                                    step_error = step.get("error")
                                                    step_p = step.get("prompt_tokens", 0)
                                                    step_r = step.get("response_tokens", 0)
                                                    step_tps = step.get("tok_per_sec", 0.0)
                                                    step_out = step_out[:60] + "..." if len(step_out) > 60 else step_out
                                                    tok_info = (
                                                        f" [{step_p}p+{step_r}r @ {step_tps}t/s]" if step_p else ""
                                                    )
                                                    report += f"  ↳ *Step {step.get('step')}*: `{step_tool}` ({step_dur}ms){tok_info}\n"
                                                    if step_thought:
                                                        thought_short = (
                                                            step_thought[:120] + "…"
                                                            if len(step_thought) > 120
                                                            else step_thought
                                                        )
                                                        report += f"    💭 _{thought_short}_\n"
                                                    if step.get("probes"):
                                                        for p in step["probes"]:
                                                            report += f"    🔍 `{p.get('q', '')}` → {p.get('hits', 0)} hit(s)\n"
                                                    if step_error:
                                                        report += f"    ❌ **Error**: {step_error[:100]}\n"
                                                    report += f"    → {step_out}\n"
                                        except:
                                            pass

                                    report += "\n"
                        except Exception as e:
                            report = f"Error reading traces: {e}"
                    elif plan.get("intent") == "clear_traces":
                        try:
                            with sqlite3.connect(self.db_path) as conn:
                                # Wipe raw_prompt to free up disk space but keep the metrics and latency history
                                conn.execute("UPDATE traces SET raw_prompt = NULL")
                            report = "✅ All raw prompt logs have been cleared to free up space. Execution metrics and summary traces were preserved."
                        except Exception as e:
                            report = f"⚠️ Failed to clear traces: {e}"
                    elif plan.get("intent") == "email_more":
                        from skills.email.tools.list_unread import format_page, PAGE_SIZE

                        if not self._cached_unread:
                            report = '📬 No email list cached. Say "check email" first.'
                        else:
                            next_offset = self._email_page_offset + PAGE_SIZE
                            if next_offset >= len(self._cached_unread):
                                report = "📬 You're all caught up — no more unread emails."
                            else:
                                self._email_page_offset = next_offset
                                page = self._cached_unread[next_offset : next_offset + PAGE_SIZE]
                                report = format_page(page, next_offset, len(self._cached_unread))

                    elif plan.get("intent") == "email_open":
                        num = plan["parameters"].get("num")
                        if not self._cached_unread:
                            report = '📬 No email list cached. Say "check email" first.'
                        elif num is None:
                            report = "Which email? Reply with a number from the list."
                        else:
                            idx = int(num) - 1
                            page_start = self._email_page_offset
                            page_end = self._email_page_offset + PAGE_SIZE
                            if idx < 0 or idx >= len(self._cached_unread):
                                report = f'⚠️ Email {num} not found. Say "check email" to refresh the list.'
                            else:
                                email = self._cached_unread[idx]
                                email_id = str(email.get("id", ""))
                                subject = email.get("subject", "")
                                sender = email.get("from", {}).get("name") or email.get("from", {}).get("addr", "")
                                # Execute summarize_email via the executive
                                read_plan = {
                                    "skill": "email",
                                    "tool": "summarize_email",
                                    "parameters": {"email_id": email_id, "subject_query": subject},
                                    "output_type": "synthesis",
                                }
                                if self.executive.validate_plan(read_plan):
                                    read_results = self.executive.execute_plan(read_plan, beliefs=self._belief_cache)
                                    report = self.router.generate_report(
                                        user_input, read_plan, read_results, context=context
                                    )
                                else:
                                    report = f"⚠️ Couldn't open email from {sender}."
                    else:
                        report = self._format_status()

                    self._log_conversation(user_input, report)

                    # If the report is a question, mark it as clarifying
                    if report.strip().endswith("?") or "(yes/no)" in report:
                        self._last_turn_type = "clarifying_question"
                    else:
                        self._last_turn_type = "tool_result"

                    return report

                # Validate & Execute
                if self.executive.validate_plan(plan):
                    t_exec_start = time.time()
                    results = self.executive.execute_plan(plan, beliefs=self._belief_cache)
                    duration_ms = int((time.time() - t_exec_start) * 1000)

                    # Cache email list for pagination ("more" / "open N")
                    if plan.get("tool") == "list_unread" and results.get("status") == "success":
                        self._cached_unread = results.get("emails", [])
                        self._email_page_offset = 0

                    report = self.router.generate_report(user_input, plan, results, context=context)
                    status = "completed" if results.get("status") != "error" else "failed"

                    output_summary = str(results)[:80] + "..." if len(str(results)) > 80 else str(results)
                    if "status" in results:
                        output_summary = f"[{results['status']}] {output_summary}"

                    step_telemetry = [
                        {
                            "step": 1,
                            "tool": plan.get("tool", "unknown"),
                            "input": plan.get("parameters", {}),
                            "output_hint": output_summary,
                            "ms": duration_ms,
                        }
                    ]

                    self.update_trace(trace_id, results, status, steps_detail=step_telemetry)
                    self._log_conversation(user_input, report)
                    self._last_turn_type = "tool_result"
                    return report

        # ── Pending Action Gate ─────────────────────────────────────
        # Executes a stored irreversible plan if the user confirms it.
        if self._pending_action is not None:
            if is_confirmation(user_input):
                plan = self._pending_action
                self._pending_action = None

                # Special case: send_email confirmation calls send_smtp() directly
                # instead of re-executing run() (which would only return another preview).
                if plan.get("tool") == "send_email":
                    smtp_payload = plan.get("_smtp_payload") or plan.get("parameters", {})
                    try:
                        from skills.email.tools.send_email import send_smtp

                        results = send_smtp(smtp_payload)
                        # Flip Ledger status to sent if we have a draft_id
                        if results.get("status") == "success":
                            draft_id = smtp_payload.get("draft_id")
                            if draft_id:
                                try:
                                    with sqlite3.connect(self.db_path) as conn:
                                        conn.execute("UPDATE ledger SET status='sent' WHERE id=?", (draft_id,))
                                except Exception as e:
                                    print(f"⚠️ [ledger_update] Failed to mark draft as sent: {e}", flush=True)
                    except Exception as e:
                        results = {"status": "error", "message": f"Failed to send: {e}"}
                elif plan.get("intent") == "update_assistant_name":
                    new_name = plan["parameters"]["assistant_name"]
                    try:
                        with sqlite3.connect(self.db_path) as conn:
                            conn.execute(
                                "INSERT OR REPLACE INTO beliefs (key, value, type, visibility) VALUES (?, ?, ?, ?)",
                                ("assistant_name", new_name, "user_request", "system"),
                            )
                        self._prewarm_memory()  # Refresh cache
                        results = {"status": "success", "message": f"My name is now {new_name}."}
                    except Exception as e:
                        results = {"status": "error", "message": f"Failed to update name: {e}"}
                elif plan.get("intent") == "update_user_name":
                    new_name = plan["parameters"]["user_name"]
                    try:
                        with sqlite3.connect(self.db_path) as conn:
                            conn.execute(
                                "INSERT OR REPLACE INTO beliefs (key, value, type, visibility) VALUES (?, ?, ?, ?)",
                                ("user_name", new_name, "user_request", "system"),
                            )
                        self._prewarm_memory()  # Refresh cache
                        results = {"status": "success", "message": f"Your name is now {new_name}."}
                    except Exception as e:
                        results = {"status": "error", "message": f"Failed to update name: {e}"}
                else:
                    t_exec_start = time.time()
                    if "parameters" not in plan:
                        plan["parameters"] = {}
                    plan["parameters"]["_confirmed"] = True
                    results = self.executive.execute_plan(plan, beliefs=self._belief_cache)

                report = self.router.generate_report(user_input, plan, results, context=context)
                status = "completed" if results.get("status") != "error" else "failed"

                # We reuse the t_exec_start or default to 0 ms for internal updates
                duration_ms = int((time.time() - t_exec_start) * 1000) if "t_exec_start" in locals() else 0
                output_summary = str(results)[:80] + "..." if len(str(results)) > 80 else str(results)
                if "status" in results:
                    output_summary = f"[{results['status']}] {output_summary}"

                step_telemetry = [
                    {
                        "step": 1,
                        "tool": plan.get("tool", "unknown"),
                        "input": plan.get("parameters", {}),
                        "output_hint": output_summary,
                        "ms": duration_ms,
                    }
                ]

                self.update_trace(trace_id, results, status, steps_detail=step_telemetry)
                self._log_conversation(user_input, report)
                self._last_turn_type = "tool_result"
                return report
            else:
                # Discarded
                report = "Got it. Action cancelled."

                # Implicit discard: if rejecting a send_email, flip Ledger status to discarded
                if self._pending_action and self._pending_action.get("tool") == "send_email":
                    draft_id = self._pending_action.get("_smtp_payload", {}).get("draft_id")
                    if draft_id:
                        try:
                            with sqlite3.connect(self.db_path) as conn:
                                conn.execute("UPDATE ledger SET status='discarded' WHERE id=?", (draft_id,))
                        except Exception as e:
                            print(f"⚠️ [ledger_discard] Failed to mark draft as discarded: {e}", flush=True)

                self._pending_action = None  # Any non-confirmation clears the gate
                self._log_conversation(user_input, report)
                self._last_turn_type = "tool_result"
                return report

        # ── ReAct Loop (Tier 2 — Multi-Step) ────────────────────────
        manifests = self.registry.get_skill_manifests()
        loop_conf = self.config.get("loop", {})
        MAX_STEPS = loop_conf.get("max_steps", 12)
        MAX_SECS = loop_conf.get("max_secs", 60)

        # Save and guarantee restore of provider after loop
        orig_provider = self.router.default_provider

        # Escalation Check (Tier 4) — protect local model against context overflow
        escalation_conf = self.config.get("escalation", {})
        threshold_pct = escalation_conf.get("threshold_pct", 70)  # Default 70%

        # Estimate context tokens (rough chars/4)
        # bulk of context is history + tools summary (~4k typically)
        tools_summary = self._available_tools_summary()
        approx_tokens = (len(context) + len(user_input) + len(str(tools_summary)) + 2000) // 4

        num_ctx = 8192  # Baseline for gemma2:9b on this machine
        if approx_tokens > (num_ctx * threshold_pct / 100) and self.router.providers.get("gemini"):
            print(
                f"🚀 [escalation] Prompt context (~{approx_tokens} tokens) exceeds {threshold_pct}% of budget. Escalating to Gemini.",
                flush=True,
            )
            self.router.default_provider = "gemini"

        scratchpad: List[Step] = _resume_scratchpad or []
        consecutive_errors = 0
        retry_counts = defaultdict(int)
        step_telemetry = []  # Collect lightweight step data for the DB

        # Insert the initial trace row for this ReAct request.
        # update_trace() later fills in results — but it's an UPDATE, so the row
        # must exist first. Without this INSERT, all ReAct traces silently disappear.
        # If it's a resume trace, _resume_task already inserted the row.
        if trace_id != "local" and not trace_id.endswith("_resume"):
            self.log_trace(trace_id, "react", {"user_input": user_input[:200]})

        # Shadow Tier 2 — observe only
        _shadow_prediction = self.shadow_matcher.match(user_input)
        if _shadow_prediction:
            print(
                f"👻 Shadow predicts: {_shadow_prediction['predicted_tool']} ({_shadow_prediction['score']:.2f})",
                flush=True,
            )

        # Snapshot system resources at request start
        _ram_start_pct = _psutil.virtual_memory().percent if _PSUTIL_OK else None
        _proc_rss_mb = round(_psutil.Process().memory_info().rss / 1024 / 1024, 1) if _PSUTIL_OK else None

        t_start = time.time()
        for step_num in range(1, MAX_STEPS + 1):
            if (time.time() - t_start) > MAX_SECS:
                break

            # 1. Generate next step
            t_step = time.time()
            assistant_name = self._belief_cache.get("assistant_name", "Bregger")
            assistant_persona = self._belief_cache.get("assistant_persona", "")

            try:
                step = self.router.generate_step(
                    original_request=user_input,
                    scratchpad=scratchpad,
                    manifests=manifests,
                    context=context,
                    step_num=step_num,
                    assistant_name=assistant_name,
                    assistant_persona=assistant_persona,
                )
                _step_tokens = _read_token_sink()
            except Exception as e:
                import traceback

                traceback.print_exc()
                print(f"⚠️ ReAct generation failure: {e}", flush=True)
                self.log_trace(
                    f"{trace_id}_parse_err",
                    "react_parse_failure",
                    {"step": step_num, "error": str(e)[:200], "user_input": user_input[:100]},
                )
                break

            # Log parse warnings to the trace DB so they appear in the dashboard
            if step.parse_warning:
                print(f"📋 [parse_warn] step={step_num} warn={step.parse_warning}", flush=True)
                self.log_trace(
                    f"{trace_id}_parse_{step_num}",
                    "react_parse_warning",
                    {
                        "step": step_num,
                        "warning": step.parse_warning,
                        "tool": step.tool,
                        "user_input": user_input[:100],
                    },
                )

            # 2. Step visibility callback (typing indicator per step)
            if callable(self.step_callback) and step.tool != "finish":
                try:
                    self.step_callback(f"🔍 Step {step_num}: {step.thought[:80]}…")
                except Exception as e:
                    print(f"⚠️ [step_callback] Callback raised: {e}", flush=True)

            # 3. END LOOP EXITS (finish, ask_user, schedule)
            if step.tool in ("finish", "ask_user", "schedule"):
                # Python-level persistence gate (Case 1):
                last_step = scratchpad[-1] if scratchpad else None
                if (
                    step.tool == "finish"
                    and last_step
                    and (last_step.tool_output.get("status") == "error" or last_step.tool_output.get("error"))
                ):
                    # Inject a retry signal and keep looping
                    step.tool_output = {
                        "system_note": (
                            f"The previous '{last_step.tool}' call failed. "
                            "You must attempt a different approach before finishing."
                        )
                    }
                    scratchpad.append(step)
                    continue

                if step.tool == "finish":
                    final_answer = step.tool_input.get("final_answer") or step.thought
                elif step.tool == "ask_user":
                    question = step.tool_input.get("question") or step.thought
                    final_answer = question
                    try:
                        import dataclasses

                        scratchpad_json = json.dumps([dataclasses.asdict(s) for s in scratchpad])
                    except Exception:
                        scratchpad_json = "[]"
                    context_compressed = self.router._compress_scratchpad(scratchpad, step_num)
                    self._create_task(
                        goal=user_input,
                        exit_type="ask_user",
                        urgency="normal",
                        due=None,
                        context_compressed=context_compressed,
                        scratchpad_json=scratchpad_json,
                        trace_id=trace_id,
                    )
                elif step.tool == "schedule":
                    goal = step.tool_input.get("goal") or "Reminder"
                    raw_due = step.tool_input.get("due")

                    if not raw_due:
                        step.tool_output = {"status": "error", "message": "The 'due' parameter is required."}
                        scratchpad.append(step)
                        continue

                    try:
                        from bregger_utils import parse_semantic_datetime

                        user_tz = os.environ.get("BREGGER_TZ", "America/New_York")
                        due = parse_semantic_datetime(raw_due, user_tz).isoformat()
                    except Exception as e:
                        step.tool_output = {
                            "status": "error",
                            "message": f"Invalid due date format: '{raw_due}'. Try 'tomorrow_1400' or similar.",
                        }
                        print(f"⚠️ PARSE WARNING: schedule date invalid '{raw_due}': {e}", flush=True)
                        scratchpad.append(step)
                        continue

                    urgency = step.tool_input.get("urgency", "normal")
                    final_answer = f"Got it. I'll remind you about: {goal}"
                    try:
                        import dataclasses

                        scratchpad_json = json.dumps([dataclasses.asdict(s) for s in scratchpad])
                    except Exception:
                        scratchpad_json = "[]"
                    context_compressed = self.router._compress_scratchpad(scratchpad, step_num)
                    self._create_task(
                        goal=goal,
                        exit_type="schedule",
                        urgency=urgency,
                        due=due,
                        context_compressed=context_compressed,
                        scratchpad_json=scratchpad_json,
                        trace_id=trace_id,
                    )

                self.log_trace(f"{trace_id}_{step.tool}", f"react_{step.tool}", {"steps": step_num})
                if trace_id != "local":
                    _total_ms = int((time.time() - t_start) * 1000)
                    _p_tok = sum(s.get("prompt_tokens", 0) for s in step_telemetry)
                    _r_tok = sum(s.get("response_tokens", 0) for s in step_telemetry)
                    _overall_tps = round(_r_tok / (_total_ms / 1000), 1) if _total_ms > 0 else 0.0
                    _ram_end_pct = _psutil.virtual_memory().percent if _PSUTIL_OK else None
                    _req_meta = {
                        "route": "react",
                        "model": getattr(self.router, "default_provider", "ollama"),
                        "raw_prompt": scratchpad[0].raw_prompt if scratchpad else step.raw_prompt,
                        "started_at": datetime.fromtimestamp(t_start).isoformat(),
                        "total_ms": _total_ms,
                        "step_count": step_num,
                        "total_prompt_tokens": _p_tok,
                        "total_response_tokens": _r_tok,
                        "overall_tok_per_sec": _overall_tps,
                        "final_answer_length": len(final_answer) if final_answer else 0,
                        "ram_start_pct": _ram_start_pct,
                        "ram_end_pct": _ram_end_pct,
                        "proc_rss_mb": _proc_rss_mb,
                    }
                    self.update_trace(
                        trace_id, {"steps": step_num}, "completed", steps_detail=step_telemetry, request_meta=_req_meta
                    )

                    if _shadow_prediction:
                        actual_tools = [s["tool"] for s in step_telemetry if s.get("tool")]
                        match = _shadow_prediction["predicted_tool"] in actual_tools
                        tier2_data = {
                            "predicted_tool": _shadow_prediction["predicted_tool"],
                            "score": _shadow_prediction["score"],
                            "phrase_matched": _shadow_prediction["phrase_matched"],
                            "actual_tools": actual_tools,
                            "match": match,
                        }
                        self._update_trace_shadow(trace_id, tier2_data)

                self._log_conversation(user_input, final_answer)
                self._last_turn_type = "tool_result"

                # ── Log Signal ──────────────────────────────────────
                # Scan scratchpad for first non-null signal (topic/entity info)
                # LLMs often emit the subject label in Step 1 but Signal: null in finish steps.
                found_sig = next((s.signal for s in scratchpad if s.signal), None)
                if not found_sig and step.signal:
                    found_sig = step.signal

                self._log_signal(
                    source="chat",
                    topic_hint=found_sig.get("topic") if found_sig else None,
                    entity_text=found_sig.get("entity") if found_sig else None,
                    entity_type=found_sig.get("entity_type") if found_sig else None,
                    content_preview=user_input,
                    ref_id=trace_id,
                    ref_source="traces",
                )

                # Passive Memory extraction (L2 — Aware)
                should_extract = True
                if len(scratchpad) > 0:
                    tools_used = [s.tool for s in scratchpad]
                    read_only = {"account_info", "recall", "list_unread"}
                    if set(tools_used).issubset(read_only):
                        should_extract = False

                if should_extract:
                    threading.Thread(
                        target=self._extract_passive_memory, args=(user_input, final_answer, scratchpad), daemon=True
                    ).start()

                return final_answer

            # 4. Unknown tool guard
            tool_meta = self._get_tool_meta(step.tool)
            if tool_meta is None:
                step.tool_output = {
                    "error": f"Tool '{step.tool}' not found.",
                    "available_tools": self._available_tools_summary(),
                    "instruction": "Choose a tool from the available list above, or use finish.",
                }
                scratchpad.append(step)
                consecutive_errors += 1
                continue

            # 5. Layer 1 escalation: min_tier routing
            current_tier = 3
            min_tier = tool_meta.get("min_tier", 3)
            cloud_cfg = self.config.get("cloud", {})
            if min_tier > current_tier and cloud_cfg.get("enabled"):
                # Cloud escalation — V1 stub (cloud not yet wired)
                step.tool_output = {
                    "error": "Cloud escalation not yet enabled.",
                    "instruction": "Try a different approach using local tools only.",
                }
                scratchpad.append(step)
                continue

            # 6. Input validation
            validation_error = self._validate_tool_input(step.tool, step.tool_input, tool_meta)
            if validation_error:
                step.tool_output = validation_error
                scratchpad.append(step)
                consecutive_errors += 1
                continue

            # 7. Execute tool
            plan = {
                "skill": tool_meta["skill"],
                "tool": step.tool,
                "parameters": step.tool_input,
                "output_type": tool_meta.get("output_type", "synthesis"),
                "risk": tool_meta.get("risk", "safe"),
            }
            if self.executive.validate_plan(plan):
                step.tool_output = self.executive.execute_plan(plan, beliefs=self._belief_cache)
            else:
                step.tool_output = {"status": "error", "message": f"Plan validation failed for tool '{step.tool}'."}

            step.duration_ms = int((time.time() - t_step) * 1000)
            step.timestamp = datetime.now().isoformat()

            # 8. Truncate large outputs
            self._truncate_output(step, tool_meta)

            # 9. Confirmation gate for irreversible tools
            risk = tool_meta.get("risk", "safe")
            if risk == "irreversible" and step.tool_output.get("status") == "success":
                # For send_email: stash the smtp payload into the plan so the
                # confirmation handler can call send_smtp() without re-running run().
                if step.tool == "send_email" and step.tool_output.get("_smtp_payload"):
                    plan["_smtp_payload"] = step.tool_output["_smtp_payload"]
                self._pending_action = plan
                draft = (
                    step.tool_output.get("content")
                    or step.tool_output.get("draft")
                    or step.tool_output.get("message", "")
                )
                reply = f"Here's what I have:\n{draft}\n\nShould I proceed? (yes/no)"
                self._log_conversation(user_input, reply)
                self._last_turn_type = "clarifying_question"

                # ── Log Signal ──────────────────────────────────────
                found_sig = next((s.signal for s in scratchpad if s.signal), None)
                if not found_sig and step.signal:
                    found_sig = step.signal

                self._log_signal(
                    source="chat",
                    topic_hint=found_sig.get("topic") if found_sig else None,
                    entity_text=found_sig.get("entity") if found_sig else None,
                    entity_type=found_sig.get("entity_type") if found_sig else None,
                    content_preview=user_input,
                    ref_id=trace_id,
                    ref_source="traces",
                )
                # Close the trace row as awaiting_confirmation (not completed — pending user yes/no)
                if trace_id != "local":
                    _total_ms = int((time.time() - t_start) * 1000)
                    _req_meta = {
                        "route": "react",
                        "model": getattr(self.router, "default_provider", "ollama"),
                        "raw_prompt": scratchpad[0].raw_prompt if scratchpad else step.raw_prompt,
                        "started_at": datetime.fromtimestamp(t_start).isoformat(),
                        "total_ms": _total_ms,
                        "step_count": step_num,
                        "total_prompt_tokens": sum(s.get("prompt_tokens", 0) for s in step_telemetry),
                        "total_response_tokens": sum(s.get("response_tokens", 0) for s in step_telemetry),
                    }
                    self.update_trace(
                        trace_id,
                        {"steps": step_num, "awaiting_confirmation": True},
                        "awaiting_confirmation",
                        steps_detail=step_telemetry,
                        request_meta=_req_meta,
                    )
                return reply

            # 10. Error tracking + retry guard
            if step.tool_output.get("status") == "error" or step.tool_output.get("error"):
                consecutive_errors += 1
                retry_counts[step.tool] += 1
                if retry_counts[step.tool] > 1:
                    step.tool_output["instruction"] = (
                        "Do not retry this tool. Try a completely different approach or use finish."
                    )
            else:
                consecutive_errors = 0

            # 11. Repetition detection
            if is_repeat(step, scratchpad):
                step.tool_output["warning"] = "You already tried a very similar query."
                step.tool_output["instruction"] = "Do not repeat yourself. Try a different approach or use finish."

            # 12. Error-based escalation only (Tier 4)
            # Escalate to Gemini only after 3+ consecutive hard tool errors.
            # No keyword detection — gemma2's natural hedging language is not confusion.
            if consecutive_errors >= 3 and "gemini" in self.router.providers:
                if self.router.default_provider != "gemini":
                    self.router.default_provider = "gemini"
                    step.tool_output["system_note"] = (
                        "SYSTEM: Multiple tool failures detected. Escalating reasoning to Tier 4 (Gemini)."
                    )
                    self.log_trace(
                        f"{trace_id}_reasoning",
                        "reasoning_escalation",
                        {"reason": "consecutive_errors", "step": step_num},
                    )
            elif consecutive_errors >= 3:
                step.tool_output["system_note"] = (
                    f"SYSTEM: You've had {consecutive_errors} consecutive failures. "
                    f"If you cannot complete this request, use finish and tell the user "
                    f"what capability is missing."
                )

            scratchpad.append(step)

            # Record lightweight telemetry for the trace DB
            output_summary = (
                str(step.tool_output)[:80] + "..." if len(str(step.tool_output)) > 80 else str(step.tool_output)
            )
            if isinstance(step.tool_output, dict) and "status" in step.tool_output:
                output_summary = f"[{step.tool_output['status']}] {output_summary}"

            # Capture error message if the tool failed
            _step_error = None
            if isinstance(step.tool_output, dict):
                if step.tool_output.get("status") == "error":
                    _step_error = step.tool_output.get("message") or step.tool_output.get("error")
                elif step.tool_output.get("error"):
                    _step_error = step.tool_output.get("error")

            # Extract diagnostic fields from tool output before truncating
            _step_probes = None
            if isinstance(step.tool_output, dict):
                raw_probes = step.tool_output.get("probes")
                if raw_probes and isinstance(raw_probes, list):
                    _step_probes = [{"q": p.get("query", ""), "hits": p.get("hits", 0)} for p in raw_probes]

            step_telemetry.append(
                {
                    "step": step.step_num,
                    "thought": step.thought[:300] if step.thought else None,
                    "tool": step.tool,
                    "params": step.tool_input,
                    "input": step.tool_input,
                    "output_hint": output_summary,
                    "probes": _step_probes,
                    "error": _step_error,
                    "prompt_tokens": _step_tokens.get("prompt_tokens", 0),
                    "response_tokens": _step_tokens.get("response_tokens", 0),
                    "tok_per_sec": _step_tokens.get("tok_per_sec", 0.0),
                    "ms": step.duration_ms,
                }
            )

        # Force-finish: limits hit
        last_useful = {}
        for s in reversed(scratchpad):
            if s.tool_output.get("status") == "success":
                last_useful = s.tool_output
                break

        content = last_useful.get("content") or last_useful.get("message") or str(last_useful)

        # Gemini Escalation (Tier 4) cache trigger
        if "gemini" in self.router.providers:
            orig_provider = self.router.default_provider
            self.router.default_provider = "gemini"
            try:
                # Use Gemini to synthesize the gathered data into a final answer
                # Create a pseudo-plan for the report generator
                synthesis_plan = {"skill": "search", "tool": "search_searxng", "parameters": {}}
                force_reply = self.router.generate_report(user_input, synthesis_plan, last_useful, context=context)
                # Log and close the gemini sub-trace
                self.log_trace(f"{trace_id}_gemini", "gemini_escalation", {"steps": step_num, "status": "success"})
                self.update_trace(f"{trace_id}_gemini", {"steps": step_num, "status": "success"}, "completed")
                self._log_conversation(user_input, force_reply)

                # ── Log Signal (Gemini Path) ────────────────────────
                found_sig = next((s.signal for s in scratchpad if s.signal), None)
                self._log_signal(
                    source="chat",
                    topic_hint=found_sig.get("topic") if found_sig else None,
                    entity_text=found_sig.get("entity") if found_sig else None,
                    entity_type=found_sig.get("entity_type") if found_sig else None,
                    content_preview=user_input,
                    ref_id=trace_id,
                    ref_source="traces",
                )

                # Passive Memory extraction (L2 — Aware)
                if len(scratchpad) > 0:
                    tools_used = [s.tool for s in scratchpad]
                    read_only = {"account_info", "recall", "list_unread"}
                    if not set(tools_used).issubset(read_only):
                        threading.Thread(
                            target=self._extract_passive_memory, args=(user_input, force_reply, scratchpad), daemon=True
                        ).start()

                # Close the original react trace row
                if trace_id != "local":
                    _total_ms = int((time.time() - t_start) * 1000)
                    _p_tok = sum(s.get("prompt_tokens", 0) for s in step_telemetry)
                    _r_tok = sum(s.get("response_tokens", 0) for s in step_telemetry)
                    _req_meta = {
                        "route": "react_gemini",
                        "model": "gemini",
                        "raw_prompt": scratchpad[0].raw_prompt if scratchpad else getattr(step, "raw_prompt", None),
                        "started_at": datetime.fromtimestamp(t_start).isoformat(),
                        "total_ms": _total_ms,
                        "step_count": step_num,
                        "total_prompt_tokens": _p_tok,
                        "total_response_tokens": _r_tok,
                        "overall_tok_per_sec": round(_r_tok / (_total_ms / 1000), 1) if _total_ms > 0 else 0.0,
                        "final_answer_length": len(force_reply) if force_reply else 0,
                    }
                    self.update_trace(
                        trace_id,
                        {"steps": step_num, "escalated": True},
                        "completed",
                        steps_detail=step_telemetry,
                        request_meta=_req_meta,
                    )

                return force_reply
            except Exception as e:
                print(f"⚠️ Gemini escalation failed: {e}", flush=True)
                self.log_trace(
                    f"{trace_id}_gemini", "gemini_escalation", {"steps": step_num, "status": "failed", "error": str(e)}
                )
            finally:
                self.router.default_provider = orig_provider

        # Reset provider in case reasoning escalation was active
        self.router.default_provider = "ollama"

        # Standard fallback if Gemini is missing or fails
        force_reply = f"I wasn't able to fully complete your request in time. Here's what I found:\n{content}"
        self.log_trace(f"{trace_id}_force", "react_force_finish", {"steps": step_num})
        if trace_id != "local":
            _total_ms = int((time.time() - t_start) * 1000)
            _p_tok = sum(s.get("prompt_tokens", 0) for s in step_telemetry)
            _r_tok = sum(s.get("response_tokens", 0) for s in step_telemetry)
            _overall_tps = round(_r_tok / (_total_ms / 1000), 1) if _total_ms > 0 else 0.0
            _req_meta = {
                "route": "react_force",
                "model": getattr(self.router, "default_provider", "ollama"),
                "raw_prompt": scratchpad[0].raw_prompt if scratchpad else getattr(step, "raw_prompt", None),
                "started_at": datetime.fromtimestamp(t_start).isoformat(),
                "total_ms": _total_ms,
                "step_count": step_num,
                "total_prompt_tokens": _p_tok,
                "total_response_tokens": _r_tok,
                "overall_tok_per_sec": _overall_tps,
                "final_answer_length": len(force_reply) if force_reply else 0,
            }
            self.update_trace(
                trace_id,
                {"steps": step_num, "force_finished": True},
                "completed",
                steps_detail=step_telemetry,
                request_meta=_req_meta,
            )
        self._log_conversation(user_input, force_reply)

        # ── Log Signal (Force-Finish Path) ──────────────────
        found_sig = next((s.signal for s in scratchpad if s.signal), None)
        self._log_signal(
            source="chat",
            topic_hint=found_sig.get("topic") if found_sig else None,
            entity_text=found_sig.get("entity") if found_sig else None,
            entity_type=found_sig.get("entity_type") if found_sig else None,
            content_preview=user_input,
            ref_id=trace_id,
            ref_source="traces",
        )

        # Passive Memory extraction (L2 — Aware)
        if len(scratchpad) > 0:
            tools_used = [s.tool for s in scratchpad]
            read_only = {"account_info", "recall", "list_unread"}
            if not set(tools_used).issubset(read_only):
                threading.Thread(
                    target=self._extract_passive_memory, args=(user_input, force_reply, scratchpad), daemon=True
                ).start()

        return force_reply

    # ── ReAct Helper Methods ──────────────────────────────────────────

    def _get_tool_meta(self, tool_name: str) -> Optional[Dict]:
        """Find a tool's manifest entry across all skills, attaching the skill name."""
        for skill_name, skill_info in self.registry.skills.items():
            for t in skill_info["manifest"].get("tools", []):
                if t["name"] == tool_name:
                    return {**t, "skill": skill_name}
        return None

    def _available_tools_summary(self) -> List[str]:
        """Return a compact list of available tool names + descriptions."""
        tools = []
        for skill_info in self.registry.skills.values():
            for t in skill_info["manifest"].get("tools", []):
                if t.get("operational", True) is False or t.get("access") == "operator":
                    continue
                tools.append(f"{t['name']}: {t.get('description', '')[:60]}")
        return tools

    def _truncate_output(self, step: Step, tool_meta: Dict) -> None:
        """Truncate tool output to max_output_size bytes (in-place)."""
        max_size = tool_meta.get("max_output_size", 2048)
        content = step.tool_output.get("content")
        if content and isinstance(content, str) and len(content) > max_size:
            step.tool_output["content"] = content[:max_size] + "\n... [truncated]"

    def _validate_tool_input(self, tool_name: str, params: Dict, tool_meta: Dict) -> Optional[Dict]:
        """Run lightweight pre-execution input validation. Returns error dict or None.

        Phase 1: Generic JSON-Schema type enforcement (string vs list mismatch).
        Phase 2: Tool-specific semantic checks (required fields, format guards).
        """
        # ──────────────────────────────────────────────────────────────
        # Phase 1: Schema-driven type coercion / enforcement
        # Enforces that string-typed manifest params actually receive a string.
        # Handles cases where the model returns a list or comma-string instead.
        # ──────────────────────────────────────────────────────────────
        params_schema = tool_meta.get("parameters", {})
        schema_props = params_schema.get("properties", {}) if isinstance(params_schema, dict) else {}
        for param_name, prop_schema in schema_props.items():
            if param_name not in params:
                continue
            if not isinstance(prop_schema, dict):
                continue
            declared_type = prop_schema.get("type")
            value = params[param_name]

            if declared_type == "string" and isinstance(value, list):
                if len(value) == 1:
                    # Safe coercion — single-element list, just unwrap it
                    params[param_name] = str(value[0]).strip()
                    print(
                        f"⚠️ [schema_validation] Coerced list→string for '{param_name}' in {tool_name}: {value}",
                        flush=True,
                    )
                else:
                    # Multi-element list — model is trying to batch. Block and self-correct.
                    return {
                        "status": "error",
                        "message": (
                            f"'{param_name}' must be a single string, but a list was provided: {value}. "
                            f"{tool_name} processes one item at a time. "
                            f"Call it again with '{value[0]}' first, then repeat for each remaining item."
                        ),
                    }

            elif declared_type == "integer" and isinstance(value, str):
                try:
                    params[param_name] = int(value)
                except ValueError:
                    return {
                        "status": "error",
                        "message": f"'{param_name}' must be an integer, got string: '{value}'.",
                    }

        # ──────────────────────────────────────────────────────────────
        # Phase 2: Tool-specific semantic validation
        # ──────────────────────────────────────────────────────────────
        if tool_name == "send_email":
            to = params.get("to", "")
            if not to or "@" not in str(to):
                return {
                    "status": "error",
                    "message": f"send_email requires a valid 'to' email address. Got: '{to}'.",
                    "suggestion": "Use recall() to look up the recipient's email address first.",
                }
            if not params.get("subject"):
                return {"status": "error", "message": "send_email requires a 'subject' field."}
            if not params.get("body"):
                return {"status": "error", "message": "send_email requires a 'body' field."}

        if tool_name == "read_file":
            filepath = params.get("filepath") or params.get("path") or params.get("name", "")
            if not filepath:
                return {
                    "status": "error",
                    "message": "read_file requires a 'filepath' parameter.",
                    "suggestion": "Use list_files() first to see available files.",
                }

        return None

    def _format_status(self) -> str:
        """Quick inline status response — no LLM needed."""
        skills = list(self.registry.skills.keys())
        llm_conf = self.config.get("llm", {})
        # Support both legacy 'model' key and new dual-model config
        chat_model = llm_conf.get("chat_model") or llm_conf.get("model", "unknown")
        triage_model = llm_conf.get("triage_model") or chat_model
        model_str = chat_model if chat_model == triage_model else f"{chat_model} (chat) / {triage_model} (triage)"
        history_count = 0
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute("SELECT COUNT(*) FROM conversation_history").fetchone()
                history_count = row[0] if row else 0
        except Exception as e:
            print(f"⚠️ [status_check] Failed to query conversation_history count: {e}", flush=True)
        return (
            f"✅ Bregger is up.\n"
            f"Model: {model_str}\n"
            f"Memory: {history_count} turns logged\n"
            f"Skills: {', '.join(skills) if skills else 'none loaded'}\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )

    def _format_capabilities(self) -> str:
        """Return loaded skills + tools — no LLM needed."""
        if not self.registry.skills:
            return "⚠️ No skills loaded."
        lines = ["🛠 Loaded Skills:\n"]
        for skill_name, skill_info in self.registry.skills.items():
            tools = [t["name"] for t in skill_info["manifest"].get("tools", [])]
            lines.append(f"• **{skill_name}**: {', '.join(tools) if tools else 'no tools'}")
        return "\n".join(lines)

    def _log_control_plane_metric(self, trace_id: str, raw: str, normalized: str, intent_obj: Dict, latency_ms: int):
        """Log metrics for every control plane match."""
        metric = {
            "raw_input": raw[:200],
            "normalized_input": normalized[:200],
            "matched_intent": intent_obj.get("intent"),
            "matched_pattern": intent_obj.get("matched_pattern"),
            "confidence": intent_obj.get("confidence"),
            "routed_by": "control_plane",
            "latency_ms": latency_ms,
        }
        print(f"⚡ Control Plane: {intent_obj['intent']} ({latency_ms}ms)", flush=True)
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO traces (id, intent, plan, status) VALUES (?, ?, ?, ?)",
                    (f"metric_{trace_id}", "control_plane_metric", json.dumps(metric), "logged"),
                )
        except Exception as e:
            print(f"⚠️ [metrics] Failed to log control plane metric: {e}", flush=True)

    def log_trace(self, trace_id: str, intent: str, plan: Dict, status: str = "pending"):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO traces (id, intent, plan, status)
                VALUES (?, ?, ?, ?)
            """,
                (trace_id, intent, json.dumps(plan), status),
            )

    def update_trace(
        self, trace_id: str, act_results: Any, status: str, steps_detail: List[Dict] = None, request_meta: Dict = None
    ):
        with sqlite3.connect(self.db_path) as conn:
            if steps_detail is not None:
                meta = request_meta or {}
                conn.execute(
                    """
                    UPDATE traces 
                    SET act_results = ?, status = ?, steps_detail = ?,
                        route = ?, model = ?, raw_prompt = ?, started_at = ?, total_ms = ?,
                        step_count = ?, total_prompt_tokens = ?,
                        total_response_tokens = ?, overall_tok_per_sec = ?,
                        final_answer_length = ?,
                        ram_start_pct = ?, ram_end_pct = ?, proc_rss_mb = ?
                    WHERE id = ?
                """,
                    (
                        json.dumps(act_results),
                        status,
                        json.dumps(steps_detail),
                        meta.get("route"),
                        meta.get("model"),
                        meta.get("raw_prompt"),
                        meta.get("started_at"),
                        meta.get("total_ms"),
                        meta.get("step_count"),
                        meta.get("total_prompt_tokens"),
                        meta.get("total_response_tokens"),
                        meta.get("overall_tok_per_sec"),
                        meta.get("final_answer_length"),
                        meta.get("ram_start_pct"),
                        meta.get("ram_end_pct"),
                        meta.get("proc_rss_mb"),
                        trace_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE traces 
                    SET act_results = ?, status = ?
                    WHERE id = ?
                """,
                    (json.dumps(act_results), status, trace_id),
                )

            # Update Working Memory (RAM) immediately on success
            if status == "completed":
                # Need to find the intent for this trace_id
                cursor = conn.execute("SELECT intent, created_at FROM traces WHERE id = ?", (trace_id,))
                row = cursor.fetchone()
                if row:
                    intent = row[0]
                    timestamp = row[1]
                    if intent != "fallback" and intent != "none":
                        self._tool_cache.append({"intent": intent, "status": status, "timestamp": timestamp})

    def _update_trace_shadow(self, trace_id: str, data: Dict):
        """Log shadow tier 2 prediction and grade the matched phrase."""
        if trace_id == "local":
            return
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("UPDATE traces SET tier2_shadow = ? WHERE id = ?", (json.dumps(data), trace_id))

                phrase = data.get("phrase_matched")
                tool = data.get("predicted_tool")
                match = data.get("match", False)

                if phrase and tool:
                    conn.execute(
                        """
                        INSERT INTO shadow_phrases (phrase, tool, hits, correct, last_seen)
                        VALUES (?, ?, 1, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT(phrase, tool) DO UPDATE SET
                            hits = hits + 1,
                            correct = correct + ?,
                            last_seen = CURRENT_TIMESTAMP
                    """,
                        (phrase, tool, 1 if match else 0, 1 if match else 0),
                    )
        except Exception as e:
            print(f"⚠️ [shadow_grade] Failed to update shadow grade: {e}", flush=True)


if __name__ == "__main__":
    import sys

    config_p = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.bregger/config.json")
    if os.path.exists(config_p):
        core = BreggerCore(config_p)
        if len(sys.argv) > 2:
            query = sys.argv[2]
            print(f"Bregger > {query}")
            print(f"Bregger Core: {core.process_query(query)}")
        else:
            print("Usage: python3 bregger_core.py <config_path> <query>")
    else:
        print(f"❌ Config not found at {config_p}")
