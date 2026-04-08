import contextvars
import json
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, TypedDict, cast

import requests

try:
    from google import genai as _google_genai
    from google.genai import types as _google_genai_types
except ImportError:
    _google_genai = None  # type: ignore[assignment]
    _google_genai_types = None  # type: ignore[assignment]

from xibi.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, FailureType
from xibi.errors import ErrorCategory, XibiError

logger = logging.getLogger(__name__)

# Process-scoped cache — one CircuitBreaker per provider, reset on restart
_circuit_breaker_cache: dict[str, CircuitBreaker] = {}

# Any code that wants LLM calls attributed to a trace sets this before calling generate().
# router.py reads it automatically. Falls back gracefully if not set.
_active_trace: contextvars.ContextVar[dict | None] = contextvars.ContextVar("_active_trace", default=None)

_active_db_path: contextvars.ContextVar[Path | None] = contextvars.ContextVar("_active_db_path", default=None)
_active_tracer: contextvars.ContextVar[Any | None] = contextvars.ContextVar("_active_tracer", default=None)


def set_trace_context(trace_id: str | None, span_id: str | None, operation: str) -> None:
    """Called by react.py, heartbeat, etc. to label subsequent LLM calls."""
    _active_trace.set(
        {
            "trace_id": trace_id,
            "parent_span_id": span_id,
            "operation": operation,
        }
    )


def clear_trace_context() -> None:
    _active_trace.set(None)


def init_telemetry(db_path: Path, tracer: Any | None = None) -> None:
    """Call once at startup (cmd_telegram, cmd_heartbeat) to wire telemetry globally."""
    _active_db_path.set(db_path)
    _active_tracer.set(tracer)


def set_last_parse_status(status: str) -> None:
    """Called by react.py after parsing the LLM response. Updates the span in-place."""
    try:
        from xibi.db import open_db

        db_path = _active_db_path.get()
        ctx = _active_trace.get()
        if not db_path or not ctx or not ctx.get("trace_id"):
            return

        with open_db(db_path) as conn:
            # Update last span where operation="llm.generate" for current trace
            conn.execute(
                """
                UPDATE spans
                SET attributes = json_set(attributes, '$.parse_status', ?)
                WHERE trace_id = ? AND operation = 'llm.generate'
                AND id = (SELECT MAX(id) FROM spans WHERE trace_id = ? AND operation = 'llm.generate')
                """,
                (status, ctx["trace_id"], ctx["trace_id"]),
            )
            conn.commit()
    except Exception:
        pass


class TimeoutsConfig(TypedDict, total=False):
    tool_default_secs: int  # default: 15
    llm_fast_secs: int  # default: 10
    llm_think_secs: int  # default: 45
    llm_review_secs: int  # default: 120
    health_check_secs: int  # default: 2
    circuit_recovery_secs: int  # default: 60


class ModelClient(Protocol):
    """Unified interface for all LLM providers."""

    provider: str  # "ollama", "gemini", "openai", "anthropic", "groq"
    model: str  # "qwen3.5:9b", "gemini-2.5-flash", etc.
    options: dict  # Provider-specific options (e.g., {"think": false})
    _role: str | None  # Internal label for effort level

    def generate(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        """Generate a text completion. Returns the response text."""
        ...

    def generate_structured(self, prompt: str, schema: dict, system: str | None = None, **kwargs: Any) -> dict:
        """Generate structured output conforming to a JSON schema. Returns parsed dict."""
        ...


class RoleConfig(TypedDict):
    provider: str
    model: str
    options: dict[str, Any]
    fallback: str | None


class ProviderConfig(TypedDict):
    base_url: str | None
    api_key_env: str | None


class Config(TypedDict, total=False):
    models: dict[str, dict[str, RoleConfig]]
    providers: dict[str, ProviderConfig]
    db_path: Path
    timeouts: TimeoutsConfig
    profile: dict[str, Any]


class ConfigValidationError(Exception):
    """Raised when the configuration is invalid."""

    pass


class NoModelAvailableError(Exception):
    """Raised when the entire fallback chain is exhausted."""

    pass


class OllamaClient:
    """Ollama implementation of ModelClient."""

    def __init__(self, provider: str, model: str, options: dict, base_url: str):
        self.provider = provider
        self.model = model
        self.options = options
        self.base_url = base_url
        self._role: str | None = None

    @staticmethod
    def _extract_tokens(rjson: dict) -> tuple[int, int]:
        """Returns (prompt_tokens, response_tokens). Safe — returns (0,0) if fields missing."""
        return (
            int(rjson.get("prompt_eval_count", 0) or 0),
            int(rjson.get("eval_count", 0) or 0),
        )

    def _emit_telemetry(
        self,
        prompt: str,
        system: str | None,
        response_text: str,
        duration_ms: int,
        parse_status: str = "ok",
        recovery_attempt: bool = False,
        error: XibiError | None = None,
    ) -> None:
        """Write span + inference_event. Never raises."""
        prompt_tokens, response_tokens, _ = getattr(self, "_last_tokens", (0, 0, 0))
        ctx = _active_trace.get()
        is_error = error is not None or parse_status == "failed"

        # 1. Inference event — always written regardless of trace context
        try:
            from xibi.db import open_db

            db_path = _active_db_path.get()
            if db_path:
                with open_db(db_path) as conn:
                    conn.execute(
                        """
                        INSERT INTO inference_events
                            (recorded_at, role, provider, model, operation,
                             prompt_tokens, response_tokens, duration_ms, cost_usd, degraded, trace_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            datetime.utcnow().isoformat(),
                            getattr(self, "_role", "unknown") or "unknown",
                            self.provider,
                            self.model,
                            ctx["operation"] if ctx else "unknown",
                            prompt_tokens,
                            response_tokens,
                            duration_ms,
                            0.0,
                            1 if is_error else 0,
                            ctx["trace_id"] if ctx else None,
                        ),
                    )
                    conn.commit()
        except Exception:
            pass

        # 2. Span — only if a trace context is active
        if ctx and ctx.get("trace_id"):
            try:
                tracer = _active_tracer.get()
                if tracer:
                    from xibi.tracing import Span

                    attributes: dict[str, Any] = {
                        "provider": self.provider,
                        "model": self.model,
                        "role": getattr(self, "_role", "unknown") or "unknown",
                        "operation": ctx.get("operation", "unknown"),
                        "prompt_tokens": prompt_tokens,
                        "response_tokens": response_tokens,
                        "system_prompt_len": len(system) if system else 0,
                        "system_prompt_preview": (system or "")[:800],
                        "prompt_len": len(prompt),
                        "prompt_preview": prompt[:800],
                        "raw_response_preview": response_text[:800],
                        "parse_status": parse_status,
                        "recovery_attempt": recovery_attempt,
                    }
                    if error is not None:
                        attributes["error.category"] = error.category.value
                        attributes["error.message"] = error.message[:200]
                        attributes["error.component"] = error.component
                    tracer.emit(
                        Span(
                            trace_id=ctx["trace_id"],
                            span_id=str(uuid.uuid4()),
                            parent_span_id=ctx.get("parent_span_id"),
                            operation="llm.generate",
                            component="router",
                            start_ms=int(time.time() * 1000) - duration_ms,
                            duration_ms=duration_ms,
                            status="error" if is_error else "ok",
                            attributes=attributes,
                        )
                    )
            except Exception:
                pass

    def _call_provider(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        url = f"{self.base_url}/api/generate"
        # Separate top-level Ollama API flags (e.g. "think") from model options
        top_level_keys = {"think", "keep_alive", "format"}
        merged = {**self.options, **kwargs}
        top_level = {k: merged.pop(k) for k in top_level_keys if k in merged}
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": merged,
            **top_level,
        }
        if system:
            payload["system"] = system

        t_start = time.monotonic()
        try:
            response = requests.post(url, json=payload, timeout=kwargs.get("timeout", 60))
            response.raise_for_status()
            rjson = response.json()
            result: str = rjson.get("response", "")
            prompt_tokens, response_tokens = self._extract_tokens(rjson)
            duration_ms = int((time.monotonic() - t_start) * 1000)
            self._last_tokens = (prompt_tokens, response_tokens, duration_ms)
            return result
        except requests.exceptions.Timeout as e:
            raise XibiError(
                category=ErrorCategory.TIMEOUT,
                message=f"Ollama call failed: {e}",
                component="ollama",
                retryable=True,
            ) from e
        except requests.exceptions.RequestException as e:
            raise XibiError(
                category=ErrorCategory.PROVIDER_DOWN,
                message=f"Ollama call failed: {e}",
                component="ollama",
                retryable=True,
            ) from e

    def generate(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        recovery_attempt = kwargs.get("recovery_attempt", False)
        t_start = time.monotonic()
        text = ""
        error: XibiError | None = None
        try:
            text = self._call_provider(prompt, system, **kwargs)
            return text
        except XibiError as e:
            error = e
            raise
        finally:
            duration_ms = int((time.monotonic() - t_start) * 1000)
            self._emit_telemetry(
                prompt=prompt,
                system=system,
                response_text=text,
                duration_ms=duration_ms,
                parse_status="failed" if error else "ok",
                recovery_attempt=recovery_attempt,
                error=error,
            )

    def generate_structured(self, prompt: str, schema: dict, system: str | None = None, **kwargs: Any) -> dict:
        prompt_with_schema = (
            f"{prompt}\n\nReturn output in JSON format conforming to this schema:\n{json.dumps(schema)}"
        )
        # Ollama can use format="json" for JSON mode
        kwargs.setdefault("format", "json")

        recovery_attempt = kwargs.get("recovery_attempt", False)
        t_start = time.monotonic()
        response_text = ""
        parse_status = "ok"
        error: XibiError | None = None
        try:
            response_text = self._call_provider(prompt_with_schema, system, **kwargs)
            try:
                result: dict = json.loads(response_text)
                return result
            except json.JSONDecodeError as e:
                parse_status = "failed"
                error = XibiError(
                    category=ErrorCategory.PARSE_FAILURE,
                    message=f"Provider returned invalid JSON: {e}",
                    component="ollama",
                    detail=f"Response: {response_text[:500]}",
                    retryable=True,
                )
                raise error from e
        except XibiError as e:
            if error is None:
                error = e
            raise
        finally:
            duration_ms = int((time.monotonic() - t_start) * 1000)
            self._emit_telemetry(
                prompt=prompt_with_schema,
                system=system,
                response_text=response_text,
                duration_ms=duration_ms,
                parse_status=parse_status if error is None else "failed",
                recovery_attempt=recovery_attempt,
                error=error,
            )

    def generate_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        # Native function calling via Ollama /api/chat with tools parameter.
        # Returns dict: tool_calls (list of {name, arguments}), content (str), thinking (str|None)
        url = f"{self.base_url}/api/chat"

        chat_messages = []
        if system:
            chat_messages.append({"role": "system", "content": system})
        chat_messages.extend(messages)

        ollama_tools = []
        for tool in tools:
            ollama_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("inputSchema", tool.get("parameters", {})),
                    },
                }
            )

        payload = {
            "model": self.model,
            "messages": chat_messages,
            "tools": ollama_tools,
            "stream": False,
            "options": {k: v for k, v in self.options.items() if k not in ("think", "keep_alive", "format")},
        }
        for key in ("think", "keep_alive"):
            if key in self.options:
                payload[key] = self.options[key]

        t_start = time.monotonic()
        try:
            response = requests.post(url, json=payload, timeout=kwargs.get("timeout", 120))
            response.raise_for_status()
            rjson = response.json()
            msg = rjson.get("message", {})

            prompt_tokens, response_tokens = self._extract_tokens(rjson)
            duration_ms = int((time.monotonic() - t_start) * 1000)
            self._last_tokens = (prompt_tokens, response_tokens, duration_ms)

            result = {
                "content": msg.get("content", ""),
                "thinking": msg.get("thinking"),
                "tool_calls": [],
            }

            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                result["tool_calls"].append(
                    {
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", {}),
                    }
                )

            self._emit_telemetry(
                prompt=str(chat_messages),
                system=system,
                response_text=msg.get("content", "") or str(result["tool_calls"]),
                duration_ms=duration_ms,
            )

            return result

        except requests.exceptions.Timeout as e:
            raise XibiError(
                category=ErrorCategory.TIMEOUT,
                message=f"Ollama chat call failed: {e}",
                component="ollama",
                retryable=True,
            ) from e
        except requests.exceptions.RequestException as e:
            raise XibiError(
                category=ErrorCategory.PROVIDER_DOWN,
                message=f"Ollama chat call failed: {e}",
                component="ollama",
                retryable=True,
            ) from e


class GeminiClient:
    """Gemini implementation of ModelClient using google-genai SDK."""

    def __init__(self, provider: str, model: str, options: dict, api_key: str):
        if _google_genai is None:
            raise RuntimeError("google-genai package not installed. Run: pip install google-genai")
        self.provider = provider
        self.model = model
        self.options = options
        self._role: str | None = None
        self.client = _google_genai.Client(api_key=api_key)

    @staticmethod
    def _extract_tokens(response: Any) -> tuple[int, int]:
        """Returns (prompt_tokens, response_tokens). Safe — returns (0,0) if fields missing."""
        try:
            usage = getattr(response, "usage_metadata", None)
            if usage:
                return (
                    int(getattr(usage, "prompt_token_count", 0)),
                    int(getattr(usage, "candidates_token_count", 0)),
                )
        except Exception:
            pass
        return (0, 0)

    def _emit_telemetry(
        self,
        prompt: str,
        system: str | None,
        response_text: str,
        duration_ms: int,
        parse_status: str = "ok",
        recovery_attempt: bool = False,
        error: XibiError | None = None,
    ) -> None:
        """Write span + inference_event. Never raises."""
        prompt_tokens, response_tokens, _ = getattr(self, "_last_tokens", (0, 0, 0))
        ctx = _active_trace.get()
        is_error = error is not None or parse_status == "failed"

        # 1. Inference event — always written regardless of trace context
        try:
            from xibi.db import open_db

            db_path = _active_db_path.get()
            if db_path:
                with open_db(db_path) as conn:
                    conn.execute(
                        """
                        INSERT INTO inference_events
                            (recorded_at, role, provider, model, operation,
                             prompt_tokens, response_tokens, duration_ms, cost_usd, degraded, trace_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            datetime.utcnow().isoformat(),
                            getattr(self, "_role", "unknown") or "unknown",
                            self.provider,
                            self.model,
                            ctx["operation"] if ctx else "unknown",
                            prompt_tokens,
                            response_tokens,
                            duration_ms,
                            0.0,
                            1 if is_error else 0,
                            ctx["trace_id"] if ctx else None,
                        ),
                    )
                    conn.commit()
        except Exception:
            pass

        # 2. Span — only if a trace context is active
        if ctx and ctx.get("trace_id"):
            try:
                tracer = _active_tracer.get()
                if tracer:
                    from xibi.tracing import Span

                    attributes: dict[str, Any] = {
                        "provider": self.provider,
                        "model": self.model,
                        "role": getattr(self, "_role", "unknown") or "unknown",
                        "operation": ctx.get("operation", "unknown"),
                        "prompt_tokens": prompt_tokens,
                        "response_tokens": response_tokens,
                        "system_prompt_len": len(system) if system else 0,
                        "system_prompt_preview": (system or "")[:800],
                        "prompt_len": len(prompt),
                        "prompt_preview": prompt[:800],
                        "raw_response_preview": response_text[:800],
                        "parse_status": parse_status,
                        "recovery_attempt": recovery_attempt,
                    }
                    if error is not None:
                        attributes["error.category"] = error.category.value
                        attributes["error.message"] = error.message[:200]
                        attributes["error.component"] = error.component
                    tracer.emit(
                        Span(
                            trace_id=ctx["trace_id"],
                            span_id=str(uuid.uuid4()),
                            parent_span_id=ctx.get("parent_span_id"),
                            operation="llm.generate",
                            component="router",
                            start_ms=int(time.time() * 1000) - duration_ms,
                            duration_ms=duration_ms,
                            status="error" if is_error else "ok",
                            attributes=attributes,
                        )
                    )
            except Exception:
                pass

    def _call_provider(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        config_kwargs: dict[str, Any] = {**self.options}
        if system:
            config_kwargs["system_instruction"] = system
        if "timeout" in kwargs:
            kwargs.pop("timeout")  # handled via http_options if needed; ignore for now

        config = _google_genai_types.GenerateContentConfig(**config_kwargs) if config_kwargs else None

        t_start = time.monotonic()
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=config,
            )
            result: str = response.text
            prompt_tokens, response_tokens = self._extract_tokens(response)
            duration_ms = int((time.monotonic() - t_start) * 1000)
            self._last_tokens = (prompt_tokens, response_tokens, duration_ms)
            return result
        except Exception as e:
            if "deadline exceeded" in str(e).lower():
                raise XibiError(
                    category=ErrorCategory.TIMEOUT,
                    message=f"Gemini request timed out: {e}",
                    component="gemini",
                    retryable=True,
                ) from e
            raise XibiError(
                category=ErrorCategory.PROVIDER_DOWN,
                message=f"Gemini call failed: {e}",
                component="gemini",
                retryable=True,
            ) from e

    def generate(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        recovery_attempt = kwargs.get("recovery_attempt", False)
        t_start = time.monotonic()
        text = ""
        error: XibiError | None = None
        try:
            text = self._call_provider(prompt, system, **kwargs)
            return text
        except XibiError as e:
            error = e
            raise
        finally:
            duration_ms = int((time.monotonic() - t_start) * 1000)
            self._emit_telemetry(
                prompt=prompt,
                system=system,
                response_text=text,
                duration_ms=duration_ms,
                parse_status="failed" if error else "ok",
                recovery_attempt=recovery_attempt,
                error=error,
            )

    def generate_structured(self, prompt: str, schema: dict, system: str | None = None, **kwargs: Any) -> dict:
        prompt_with_schema = (
            f"{prompt}\n\nReturn output in JSON format conforming to this schema:\n{json.dumps(schema)}"
        )
        # For Gemini, we could use response_mime_type="application/json"
        kwargs.setdefault("response_mime_type", "application/json")

        recovery_attempt = kwargs.get("recovery_attempt", False)
        t_start = time.monotonic()
        response_text = ""
        parse_status = "ok"
        error: XibiError | None = None
        try:
            response_text = self._call_provider(prompt_with_schema, system, **kwargs)
            try:
                result: dict = json.loads(response_text)
                return result
            except json.JSONDecodeError as e:
                parse_status = "failed"
                error = XibiError(
                    category=ErrorCategory.PARSE_FAILURE,
                    message=f"Provider returned invalid JSON: {e}",
                    component="gemini",
                    detail=f"Response: {response_text[:500]}",
                    retryable=True,
                )
                raise error from e
        except XibiError as e:
            if error is None:
                error = e
            raise
        finally:
            duration_ms = int((time.monotonic() - t_start) * 1000)
            self._emit_telemetry(
                prompt=prompt_with_schema,
                system=system,
                response_text=response_text,
                duration_ms=duration_ms,
                parse_status=parse_status if error is None else "failed",
                recovery_attempt=recovery_attempt,
                error=error,
            )


def _emit_provider_telemetry(
    client: Any,
    prompt: str,
    system: str | None,
    response_text: str,
    duration_ms: int,
    parse_status: str = "ok",
    recovery_attempt: bool = False,
    error: XibiError | None = None,
) -> None:
    """Free-standing telemetry helper for providers that lack their own.
    Writes inference_event row + span. Never raises."""
    prompt_tokens, response_tokens, _ = getattr(client, "_last_tokens", (0, 0, 0))
    ctx = _active_trace.get()
    is_error = error is not None or parse_status == "failed"

    try:
        from xibi.db import open_db

        db_path = _active_db_path.get()
        if db_path:
            with open_db(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO inference_events
                        (recorded_at, role, provider, model, operation,
                         prompt_tokens, response_tokens, duration_ms, cost_usd, degraded, trace_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        datetime.utcnow().isoformat(),
                        getattr(client, "_role", "unknown") or "unknown",
                        client.provider,
                        client.model,
                        ctx["operation"] if ctx else "unknown",
                        prompt_tokens,
                        response_tokens,
                        duration_ms,
                        0.0,
                        1 if is_error else 0,
                        ctx["trace_id"] if ctx else None,
                    ),
                )
                conn.commit()
    except Exception:
        pass

    if ctx and ctx.get("trace_id"):
        try:
            tracer = _active_tracer.get()
            if tracer:
                from xibi.tracing import Span

                attributes: dict[str, Any] = {
                    "provider": client.provider,
                    "model": client.model,
                    "role": getattr(client, "_role", "unknown") or "unknown",
                    "operation": ctx.get("operation", "unknown"),
                    "prompt_tokens": prompt_tokens,
                    "response_tokens": response_tokens,
                    "system_prompt_len": len(system) if system else 0,
                    "system_prompt_preview": (system or "")[:800],
                    "prompt_len": len(prompt),
                    "prompt_preview": prompt[:800],
                    "raw_response_preview": response_text[:800],
                    "parse_status": parse_status,
                    "recovery_attempt": recovery_attempt,
                }
                if error is not None:
                    attributes["error.category"] = error.category.value
                    attributes["error.message"] = error.message[:200]
                    attributes["error.component"] = error.component
                tracer.emit(
                    Span(
                        trace_id=ctx["trace_id"],
                        span_id=str(uuid.uuid4()),
                        parent_span_id=ctx.get("parent_span_id"),
                        operation="llm.generate",
                        component="router",
                        start_ms=int(time.time() * 1000) - duration_ms,
                        duration_ms=duration_ms,
                        status="error" if is_error else "ok",
                        attributes=attributes,
                    )
                )
        except Exception:
            pass


class OpenAIClient:
    """OpenAI implementation of ModelClient using the openai SDK."""

    def __init__(self, provider: str, model: str, options: dict, api_key: str | None):
        try:
            import openai as _openai_sdk
        except ImportError as err:
            raise RuntimeError("openai package not installed. Run: pip install openai") from err
        if not api_key:
            raise RuntimeError("OpenAI api_key is required. Set OPENAI_API_KEY env var.")
        self.provider = provider
        self.model = model
        self.options = options
        self._role: str | None = None
        self._client = _openai_sdk.OpenAI(api_key=api_key)
        self._last_tokens: tuple[int, int, int] = (0, 0, 0)

    def _call_provider(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        call_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if "temperature" in self.options:
            call_kwargs["temperature"] = self.options["temperature"]
        timeout = kwargs.get("timeout", 120)

        t_start = time.monotonic()
        try:
            response = self._client.chat.completions.create(timeout=timeout, **call_kwargs)
            result: str = response.choices[0].message.content or ""
            usage = response.usage
            prompt_tokens = usage.prompt_tokens if usage else 0
            response_tokens = usage.completion_tokens if usage else 0
            duration_ms = int((time.monotonic() - t_start) * 1000)
            self._last_tokens = (prompt_tokens, response_tokens, duration_ms)
            return result
        except Exception as e:
            if "timeout" in str(e).lower():
                raise XibiError(
                    category=ErrorCategory.TIMEOUT,
                    message=f"OpenAI request timed out: {e}",
                    component="openai",
                    retryable=True,
                ) from e
            raise XibiError(
                category=ErrorCategory.PROVIDER_DOWN,
                message=f"OpenAI call failed: {e}",
                component="openai",
                retryable=True,
            ) from e

    def generate(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        recovery_attempt = kwargs.get("recovery_attempt", False)
        t_start = time.monotonic()
        text = ""
        error: XibiError | None = None
        try:
            text = self._call_provider(prompt, system, **kwargs)
            return text
        except XibiError as e:
            error = e
            raise
        finally:
            duration_ms = int((time.monotonic() - t_start) * 1000)
            _emit_provider_telemetry(
                self,
                prompt=prompt,
                system=system,
                response_text=text,
                duration_ms=duration_ms,
                parse_status="failed" if error else "ok",
                recovery_attempt=recovery_attempt,
                error=error,
            )

    def generate_structured(self, prompt: str, schema: dict, system: str | None = None, **kwargs: Any) -> dict:
        prompt_with_schema = (
            f"{prompt}\n\nReturn output in JSON format conforming to this schema:\n{json.dumps(schema)}"
        )
        recovery_attempt = kwargs.get("recovery_attempt", False)
        t_start = time.monotonic()
        response_text = ""
        parse_status = "ok"
        error: XibiError | None = None
        try:
            response_text = self._call_provider(prompt_with_schema, system, **kwargs)
            try:
                return dict(json.loads(response_text))
            except json.JSONDecodeError as e:
                parse_status = "failed"
                error = XibiError(
                    category=ErrorCategory.PARSE_FAILURE,
                    message=f"Provider returned invalid JSON: {e}",
                    component="openai",
                    detail=f"Response: {response_text[:500]}",
                    retryable=True,
                )
                raise error from e
        except XibiError as e:
            if error is None:
                error = e
            raise
        finally:
            duration_ms = int((time.monotonic() - t_start) * 1000)
            _emit_provider_telemetry(
                self,
                prompt=prompt_with_schema,
                system=system,
                response_text=response_text,
                duration_ms=duration_ms,
                parse_status=parse_status if error is None else "failed",
                recovery_attempt=recovery_attempt,
                error=error,
            )


class AnthropicClient:
    """Anthropic implementation of ModelClient using the anthropic SDK."""

    def __init__(self, provider: str, model: str, options: dict, api_key: str | None):
        try:
            import anthropic as _anthropic_sdk
        except ImportError as err:
            raise RuntimeError("anthropic package not installed. Run: pip install anthropic") from err
        if not api_key:
            raise RuntimeError("Anthropic api_key is required. Set ANTHROPIC_API_KEY env var.")
        self.provider = provider
        self.model = model
        self.options = options
        self._role: str | None = None
        self._client = _anthropic_sdk.Anthropic(api_key=api_key)
        self._last_tokens: tuple[int, int, int] = (0, 0, 0)

    def _call_provider(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        call_kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": kwargs.get("max_tokens", 4096),
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            call_kwargs["system"] = system
        if "temperature" in self.options:
            call_kwargs["temperature"] = self.options["temperature"]
        timeout = kwargs.get("timeout", 120)

        t_start = time.monotonic()
        try:
            response = self._client.messages.create(timeout=timeout, **call_kwargs)
            result: str = response.content[0].text if response.content else ""
            usage = response.usage
            prompt_tokens = usage.input_tokens if usage else 0
            response_tokens = usage.output_tokens if usage else 0
            duration_ms = int((time.monotonic() - t_start) * 1000)
            self._last_tokens = (prompt_tokens, response_tokens, duration_ms)
            return result
        except Exception as e:
            if "timeout" in str(e).lower():
                raise XibiError(
                    category=ErrorCategory.TIMEOUT,
                    message=f"Anthropic request timed out: {e}",
                    component="anthropic",
                    retryable=True,
                ) from e
            raise XibiError(
                category=ErrorCategory.PROVIDER_DOWN,
                message=f"Anthropic call failed: {e}",
                component="anthropic",
                retryable=True,
            ) from e

    def generate(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        recovery_attempt = kwargs.get("recovery_attempt", False)
        t_start = time.monotonic()
        text = ""
        error: XibiError | None = None
        try:
            text = self._call_provider(prompt, system, **kwargs)
            return text
        except XibiError as e:
            error = e
            raise
        finally:
            duration_ms = int((time.monotonic() - t_start) * 1000)
            _emit_provider_telemetry(
                self,
                prompt=prompt,
                system=system,
                response_text=text,
                duration_ms=duration_ms,
                parse_status="failed" if error else "ok",
                recovery_attempt=recovery_attempt,
                error=error,
            )

    def generate_structured(self, prompt: str, schema: dict, system: str | None = None, **kwargs: Any) -> dict:
        prompt_with_schema = (
            f"{prompt}\n\nReturn output in JSON format conforming to this schema:\n{json.dumps(schema)}"
        )
        recovery_attempt = kwargs.get("recovery_attempt", False)
        t_start = time.monotonic()
        response_text = ""
        parse_status = "ok"
        error: XibiError | None = None
        try:
            response_text = self._call_provider(prompt_with_schema, system, **kwargs)
            try:
                return dict(json.loads(response_text))
            except json.JSONDecodeError as e:
                parse_status = "failed"
                error = XibiError(
                    category=ErrorCategory.PARSE_FAILURE,
                    message=f"Provider returned invalid JSON: {e}",
                    component="anthropic",
                    detail=f"Response: {response_text[:500]}",
                    retryable=True,
                )
                raise error from e
        except XibiError as e:
            if error is None:
                error = e
            raise
        finally:
            duration_ms = int((time.monotonic() - t_start) * 1000)
            _emit_provider_telemetry(
                self,
                prompt=prompt_with_schema,
                system=system,
                response_text=response_text,
                duration_ms=duration_ms,
                parse_status=parse_status if error is None else "failed",
                recovery_attempt=recovery_attempt,
                error=error,
            )


class GroqClient:
    provider: str
    model: str
    options: dict
    _role: str | None

    def __init__(self, provider: str, model: str, options: dict, api_key: str | None):
        raise NotImplementedError("Provider Groq not yet implemented. Add implementation and tests.")

    def generate(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        return ""

    def generate_structured(self, prompt: str, schema: dict, system: str | None = None, **kwargs: Any) -> dict:
        return {}


def load_config(path: str = "config.json") -> Config:
    """Load and validate the model config."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")

    try:
        with open(path) as f:
            config: Config = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigValidationError(f"Invalid JSON in config file: {e}") from e

    # Validation
    if "models" not in config:
        raise ConfigValidationError("Missing 'models' section in config")
    if "providers" not in config:
        raise ConfigValidationError("Missing 'providers' section in config")

    if "text" not in config["models"]:
        raise ConfigValidationError("At least one role must be defined for 'text' specialty")

    for specialty, efforts in config["models"].items():
        for effort, role_cfg in efforts.items():
            if "provider" not in role_cfg:
                raise ConfigValidationError(f"Missing 'provider' in models.{specialty}.{effort}")
            if "model" not in role_cfg:
                raise ConfigValidationError(f"Missing 'model' in models.{specialty}.{effort}")

            provider = role_cfg["provider"]
            if provider not in config["providers"]:
                raise ConfigValidationError(
                    f"Provider '{provider}' referenced in models.{specialty}.{effort} not found in providers section"
                )

            # Validate fallbacks
            fallback = role_cfg.get("fallback")
            if fallback:
                if fallback not in efforts:
                    raise ConfigValidationError(
                        f"Fallback '{fallback}' for models.{specialty}.{effort} does not exist in that specialty"
                    )

                # Check for circular fallback
                chain = [effort, fallback]
                current = fallback
                while True:
                    next_fallback = efforts[current].get("fallback")
                    if not next_fallback:
                        break
                    if next_fallback in chain:
                        raise ConfigValidationError(
                            f"Circular fallback chain detected: {' -> '.join(chain)} -> {next_fallback}"
                        )
                    chain.append(next_fallback)
                    current = next_fallback

    return config


def _resolve_model(config: Config, specialty: str, effort: str) -> RoleConfig:
    """Resolve specialty + effort to a config entry with fallbacks."""
    # 1. Unknown specialty falls back to "text"
    resolved_specialty = specialty if specialty in config["models"] else "text"
    efforts = config["models"][resolved_specialty]

    # 2. If effort missing, follow default fallback if defined (though spec says fast -> think -> review)
    # Actually spec says: 1. Look up config["models"][specialty][effort]
    # 2. If missing, follow the role's "fallback" field: fast → think → review
    # 3. If specialty missing entirely, fall back to "text" specialty

    if effort in efforts:
        return efforts[effort]

    # If effort not in efforts, we need a starting point for fallback.
    # The spec is a bit ambiguous if the *requested* effort is missing but not defined as a role.
    # "Unknown effort falls back to 'think'."
    start_effort = "think"
    if start_effort not in efforts:
        # If think is also missing, pick the first available effort?
        # Spec: "At least one role defined for 'text' specialty"
        # Let's try to find ANY effort in the resolved specialty.
        if not efforts:
            if resolved_specialty != "text":
                return _resolve_model(config, "text", effort)
            raise NoModelAvailableError(f"No models defined for specialty '{resolved_specialty}'")
        return next(iter(efforts.values()))

    return efforts[start_effort]


_TIMEOUT_DEFAULTS: TimeoutsConfig = {
    "tool_default_secs": 15,
    "llm_fast_secs": 10,
    "llm_think_secs": 45,
    "llm_review_secs": 120,
    "health_check_secs": 2,
    "circuit_recovery_secs": 60,
}


def get_timeout(
    config: Config,
    key: Literal[
        "tool_default_secs",
        "llm_fast_secs",
        "llm_think_secs",
        "llm_review_secs",
        "health_check_secs",
        "circuit_recovery_secs",
    ],
) -> int:
    timeouts = config.get("timeouts", {})
    val = timeouts.get(key, _TIMEOUT_DEFAULTS[key])
    return int(val) if val is not None else _TIMEOUT_DEFAULTS[key]


def _check_provider_health(config: Config, role_cfg: RoleConfig) -> bool:
    """Quick health check before inference."""
    provider_name = role_cfg["provider"]
    provider_cfg = config["providers"][provider_name]

    if provider_name == "ollama":
        base_url = provider_cfg.get("base_url") or "http://localhost:11434"
        try:
            # GET /api/tags — is the model loaded?
            response = requests.get(f"{base_url}/api/tags", timeout=2)
            if response.status_code != 200:
                return False
            models = response.json().get("models", [])
            # Check if our specific model is in the list
            # Note: Ollama model names can be slightly different (e.g. including tag)
            model_names = [m["name"] for m in models]
            requested_model = role_cfg["model"]
            if requested_model in model_names:
                return True

            # If it's missing a tag, try matching with :latest
            if ":" not in requested_model and f"{requested_model}:latest" in model_names:
                return True

            # If the list has it without a tag but we requested with :latest
            if requested_model.endswith(":latest"):
                base_name = requested_model.rsplit(":", 1)[0]
                if base_name in model_names:
                    return True

            # If not loaded, trigger warmup (POST /api/show or just /api/generate with empty prompt)
            # Spec says "trigger warmup". /api/generate with just the model name will pull/load it.
            requests.post(f"{base_url}/api/generate", json={"model": role_cfg["model"]}, timeout=1)
            return True  # Assume it's warming up
        except requests.exceptions.RequestException:
            return False

    if provider_name == "gemini":
        try:
            # Quick connectivity check to Gemini API
            requests.get("https://generativelanguage.googleapis.com", timeout=2)
            return True
        except requests.exceptions.RequestException:
            return False

    # Other providers: skip check (assume available)
    return True


class BreakerWrappedClient:
    """Per-role wrapper that records circuit-breaker state on every call."""

    def __init__(self, inner: ModelClient, breaker: CircuitBreaker, effort: str):
        self.inner = inner
        self.breaker = breaker
        self.provider = inner.provider
        self.model = inner.model
        self.options = inner.options
        self.inner._role = effort
        self._role = effort

    def _record_failure(self, exc: BaseException) -> None:
        if isinstance(exc, XibiError) and exc.category in (
            ErrorCategory.PROVIDER_DOWN,
            ErrorCategory.TIMEOUT,
        ):
            self.breaker.record_failure(FailureType.PERSISTENT)
        elif isinstance(exc, XibiError):
            self.breaker.record_failure(FailureType.TRANSIENT)
        else:
            self.breaker.record_failure(FailureType.PERSISTENT)

    def generate(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        try:
            res = self.inner.generate(prompt, system, **kwargs)
            self.breaker.record_success()
            return res
        except Exception as e:
            self._record_failure(e)
            raise

    def generate_structured(self, prompt: str, schema: dict, system: str | None = None, **kwargs: Any) -> dict:
        try:
            res = self.inner.generate_structured(prompt, schema, system, **kwargs)
            self.breaker.record_success()
            return res
        except Exception as e:
            self._record_failure(e)
            raise

    def supports_tool_calling(self) -> bool:
        """True if the wrapped client has native tool-calling support."""
        return hasattr(self.inner, "generate_with_tools")

    def generate_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Proxy to inner client's generate_with_tools. Records circuit-breaker state."""
        try:
            res = self.inner.generate_with_tools(messages, tools, system, **kwargs)  # type: ignore
            self.breaker.record_success()
            return cast(dict[str, Any], res)
        except Exception as e:
            self._record_failure(e)
            raise


_WALKABLE_CATEGORIES = (
    ErrorCategory.PROVIDER_DOWN,
    ErrorCategory.TIMEOUT,
    ErrorCategory.PARSE_FAILURE,
)


class ChainedModelClient:
    """Walks the existing RoleConfig.fallback chain on runtime provider failure.

    Invariant: walks are ephemeral. This class does not mutate config or any
    shared state. The next call starts from the configured primary.

    Invariant: walks go through BreakerWrappedClient instances, so walking
    past a failed role increments that role's breaker. Eventually the breaker
    opens and get_model() pre-flight-skips that role on future calls.
    """

    def __init__(
        self,
        primary_role: str,
        specialty: str,
        config: Config,
        chain: list[tuple[str, Any]],
    ):
        self.primary_role = primary_role
        self.specialty = specialty
        self.config = config
        self._chain = chain  # list of (role_name, client) — client may be BreakerWrappedClient or raw
        # Surface primary client identity for callers that need provider/model labels.
        first_client = chain[0][1] if chain else None
        self.provider = getattr(first_client, "provider", "unknown")
        self.model = getattr(first_client, "model", "unknown")
        self.options = getattr(first_client, "options", {})
        self._role = primary_role

    def _should_walk(self, e: XibiError) -> bool:
        # Walk on network-shaped failures. Do NOT walk on validation,
        # tool-not-found, permission, circuit-open, or unknown.
        return e.category in _WALKABLE_CATEGORIES

    def _walk(self, attempt_fn: Any) -> Any:
        last_err: XibiError | None = None
        attempts: list[dict] = []

        for i, (role_name, client) in enumerate(self._chain):
            if i > 0:
                time.sleep(min(0.1 * (2**i), 1.0))  # exponential backoff, capped at 1s
            try:
                return attempt_fn(client)
            except XibiError as e:
                last_err = e
                attempts.append(
                    {
                        "role": role_name,
                        "category": e.category.value,
                        "message": e.message[:200],
                    }
                )
                if not self._should_walk(e):
                    raise
                logger.warning(
                    "role %s failed (%s); walking fallback chain to next role",
                    role_name,
                    e.category.value,
                )
                continue

        raise XibiError(
            category=(last_err.category if last_err else ErrorCategory.PROVIDER_DOWN),
            message=f"All {len(self._chain)} roles in fallback chain failed",
            component="router",
            detail=json.dumps({"attempts": attempts}),
            retryable=False,
        )

    def generate(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        return cast(str, self._walk(lambda c: c.generate(prompt, system, **kwargs)))

    def generate_structured(self, prompt: str, schema: dict, system: str | None = None, **kwargs: Any) -> dict:
        return cast(dict, self._walk(lambda c: c.generate_structured(prompt, schema, system, **kwargs)))

    def supports_tool_calling(self) -> bool:
        first = self._chain[0][1] if self._chain else None
        if first is None:
            return False
        if hasattr(first, "supports_tool_calling"):
            return bool(first.supports_tool_calling())
        return hasattr(first, "generate_with_tools")

    def generate_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        def _call(c: Any) -> dict[str, Any]:
            if not hasattr(c, "generate_with_tools"):
                raise XibiError(
                    category=ErrorCategory.TOOL_NOT_FOUND,
                    message=f"role {getattr(c, '_role', 'unknown')} does not support tool calling",
                    component="router",
                    retryable=False,
                )
            return cast(dict[str, Any], c.generate_with_tools(messages, tools, system, **kwargs))

        return cast(dict[str, Any], self._walk(_call))


def _build_role_client(
    config: Config,
    role_cfg: RoleConfig,
    effort: str,
    db_path: Path,
) -> Any | None:
    """Construct a single role's client (BreakerWrappedClient when wrapping is supported,
    or a raw client for providers we don't wrap). Returns None if the role is unhealthy
    or its circuit is open."""
    provider_name = role_cfg["provider"]
    cb_config = CircuitBreakerConfig(recovery_timeout_secs=get_timeout(config, "circuit_recovery_secs"))
    cache_key = f"{provider_name}:{db_path}"
    if cache_key not in _circuit_breaker_cache:
        _circuit_breaker_cache[cache_key] = CircuitBreaker(provider_name, db_path=db_path, config=cb_config)
    breaker = _circuit_breaker_cache[cache_key]

    if breaker.is_open():
        return None
    if not _check_provider_health(config, role_cfg):
        return None

    provider_cfg = config["providers"][provider_name]
    model_name = role_cfg["model"]
    options = role_cfg.get("options", {})

    raw: ModelClient | None = None
    if provider_name == "ollama":
        base_url = provider_cfg.get("base_url") or "http://localhost:11434"
        raw = OllamaClient(provider=provider_name, model=model_name, options=options, base_url=base_url)
    elif provider_name == "gemini":
        api_key_env = provider_cfg.get("api_key_env") or "GEMINI_API_KEY"
        api_key = os.environ.get(api_key_env)
        if api_key:
            raw = GeminiClient(provider=provider_name, model=model_name, options=options, api_key=api_key)
    elif provider_name == "openai":
        api_key_env = provider_cfg.get("api_key_env") or ""
        raw = OpenAIClient(provider_name, model_name, options, os.environ.get(api_key_env))
    elif provider_name == "anthropic":
        api_key_env = provider_cfg.get("api_key_env") or ""
        raw = AnthropicClient(provider_name, model_name, options, os.environ.get(api_key_env))
    elif provider_name == "groq":
        api_key_env = provider_cfg.get("api_key_env") or ""
        raw = GroqClient(provider_name, model_name, options, os.environ.get(api_key_env))

    if raw is None:
        return None

    return BreakerWrappedClient(raw, breaker, effort)


def _resolve_role_chain(config: Config, specialty: str, effort: str) -> list[tuple[str, RoleConfig]]:
    """Walk RoleConfig.fallback links and return [(role_name, role_cfg), ...].
    Pure: does not check health or breakers."""
    resolved_specialty = specialty if specialty in config["models"] else "text"
    efforts = config["models"][resolved_specialty]

    chain: list[tuple[str, RoleConfig]] = []
    seen: set[str] = set()
    current_effort: str | None = effort if effort in efforts else "think"
    if current_effort not in efforts:
        # Pick any
        for k, v in efforts.items():
            chain.append((k, v))
            return chain

    while current_effort and current_effort not in seen:
        seen.add(current_effort)
        role_cfg = efforts.get(current_effort)
        if role_cfg is None:
            break
        chain.append((current_effort, role_cfg))
        current_effort = role_cfg.get("fallback")

    return chain


def get_model(
    specialty: str = "text", effort: str = "think", config_path: str = "config.json", config: Config | None = None
) -> ModelClient:
    """Resolve a role (specialty × effort) to a callable ChainedModelClient.

    Builds the full fallback chain at construction time. Pre-flight health checks
    and circuit-breaker state still skip dead roles, but the returned object also
    walks the chain *at runtime* on XibiError(PROVIDER_DOWN/TIMEOUT/PARSE_FAILURE).
    """
    if config is None:
        config = load_config(config_path)

    db_path = Path(config.get("db_path") or Path.home() / ".xibi" / "data" / "xibi.db")

    role_chain_cfg = _resolve_role_chain(config, specialty, effort)
    if not role_chain_cfg:
        raise NoModelAvailableError(f"No roles resolvable for specialty={specialty} effort={effort}")

    built_chain: list[tuple[str, Any]] = []
    tried_roles: list[str] = []

    for role_name, role_cfg in role_chain_cfg:
        provider_name = role_cfg["provider"]
        client = _build_role_client(config, role_cfg, role_name, db_path)
        if client is None:
            tried_roles.append(f"{provider_name}/{role_cfg['model']} (unavailable)")
            continue
        built_chain.append((role_name, client))

    if not built_chain:
        raise NoModelAvailableError(f"Exhausted fallback chain: {tried_roles}")

    primary_role = built_chain[0][0]
    return cast(
        ModelClient,
        ChainedModelClient(
            primary_role=primary_role,
            specialty=specialty,
            config=config,
            chain=built_chain,
        ),
    )
