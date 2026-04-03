import contextvars
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, TypedDict

import requests

try:
    from google import genai as _google_genai
    from google.genai import types as _google_genai_types
except ImportError:
    _google_genai = None  # type: ignore[assignment]
    _google_genai_types = None  # type: ignore[assignment]

from xibi.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, FailureType
from xibi.errors import ErrorCategory, XibiError

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
    ) -> None:
        """Write span + inference_event. Never raises."""
        prompt_tokens, response_tokens, _ = getattr(self, "_last_tokens", (0, 0, 0))
        ctx = _active_trace.get()

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
                            0,
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

                    tracer.emit(
                        Span(
                            trace_id=ctx["trace_id"],
                            span_id=str(uuid.uuid4()),
                            parent_span_id=ctx.get("parent_span_id"),
                            operation="llm.generate",
                            component="router",
                            start_ms=int(time.time() * 1000) - duration_ms,
                            duration_ms=duration_ms,
                            status="ok" if parse_status != "failed" else "error",
                            attributes={
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
                            },
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
        text = self._call_provider(prompt, system, **kwargs)
        duration_ms = int((time.monotonic() - t_start) * 1000)
        self._emit_telemetry(
            prompt=prompt,
            system=system,
            response_text=text,
            duration_ms=duration_ms,
            parse_status="ok",
            recovery_attempt=recovery_attempt,
        )
        return text

    def generate_structured(self, prompt: str, schema: dict, system: str | None = None, **kwargs: Any) -> dict:
        prompt_with_schema = (
            f"{prompt}\n\nReturn output in JSON format conforming to this schema:\n{json.dumps(schema)}"
        )
        # Ollama can use format="json" for JSON mode
        kwargs.setdefault("format", "json")

        t_start = time.monotonic()
        response_text = self._call_provider(prompt_with_schema, system, **kwargs)
        duration_ms = int((time.monotonic() - t_start) * 1000)

        recovery_attempt = kwargs.get("recovery_attempt", False)
        try:
            result: dict = json.loads(response_text)
            self._emit_telemetry(
                prompt=prompt_with_schema,
                system=system,
                response_text=response_text,
                duration_ms=duration_ms,
                parse_status="ok",
                recovery_attempt=recovery_attempt,
            )
            return result
        except json.JSONDecodeError as e:
            self._emit_telemetry(
                prompt=prompt_with_schema,
                system=system,
                response_text=response_text,
                duration_ms=duration_ms,
                parse_status="failed",
                recovery_attempt=recovery_attempt,
            )
            raise RuntimeError(f"Ollama returned invalid JSON: {e}\nResponse: {response_text}") from e



    def generate_with_tools(
        self,
        messages,
        tools,
        system=None,
        **kwargs,
    ):
        # Native function calling via Ollama /api/chat with tools parameter.
        # Returns dict: tool_calls (list of {name, arguments}), content (str), thinking (str|None)
        url = f"{self.base_url}/api/chat"

        chat_messages = []
        if system:
            chat_messages.append({"role": "system", "content": system})
        chat_messages.extend(messages)

        ollama_tools = []
        for tool in tools:
            ollama_tools.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("inputSchema", tool.get("parameters", {})),
                },
            })

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
                result["tool_calls"].append({
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", {}),
                })

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
    ) -> None:
        """Write span + inference_event. Never raises."""
        prompt_tokens, response_tokens, _ = getattr(self, "_last_tokens", (0, 0, 0))
        ctx = _active_trace.get()

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
                            0,
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

                    tracer.emit(
                        Span(
                            trace_id=ctx["trace_id"],
                            span_id=str(uuid.uuid4()),
                            parent_span_id=ctx.get("parent_span_id"),
                            operation="llm.generate",
                            component="router",
                            start_ms=int(time.time() * 1000) - duration_ms,
                            duration_ms=duration_ms,
                            status="ok" if parse_status != "failed" else "error",
                            attributes={
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
                            },
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
        text = self._call_provider(prompt, system, **kwargs)
        duration_ms = int((time.monotonic() - t_start) * 1000)
        self._emit_telemetry(
            prompt=prompt,
            system=system,
            response_text=text,
            duration_ms=duration_ms,
            parse_status="ok",
            recovery_attempt=recovery_attempt,
        )
        return text

    def generate_structured(self, prompt: str, schema: dict, system: str | None = None, **kwargs: Any) -> dict:
        prompt_with_schema = (
            f"{prompt}\n\nReturn output in JSON format conforming to this schema:\n{json.dumps(schema)}"
        )
        # For Gemini, we could use response_mime_type="application/json"
        kwargs.setdefault("response_mime_type", "application/json")

        t_start = time.monotonic()
        response_text = self._call_provider(prompt_with_schema, system, **kwargs)
        duration_ms = int((time.monotonic() - t_start) * 1000)

        recovery_attempt = kwargs.get("recovery_attempt", False)
        try:
            result: dict = json.loads(response_text)
            self._emit_telemetry(
                prompt=prompt_with_schema,
                system=system,
                response_text=response_text,
                duration_ms=duration_ms,
                parse_status="ok",
                recovery_attempt=recovery_attempt,
            )
            return result
        except json.JSONDecodeError as e:
            self._emit_telemetry(
                prompt=prompt_with_schema,
                system=system,
                response_text=response_text,
                duration_ms=duration_ms,
                parse_status="failed",
                recovery_attempt=recovery_attempt,
            )
            raise RuntimeError(f"Gemini returned invalid JSON: {e}\nResponse: {response_text}") from e


class OpenAIClient:
    """OpenAI implementation of ModelClient using the openai SDK."""

    def __init__(self, provider: str, model: str, options: dict, api_key: str | None):
        try:
            import openai as _openai_sdk
        except ImportError:
            raise RuntimeError("openai package not installed. Run: pip install openai")
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
        return self._call_provider(prompt, system, **kwargs)

    def generate_structured(self, prompt: str, schema: dict, system: str | None = None, **kwargs: Any) -> dict:
        prompt_with_schema = (
            f"{prompt}\n\nReturn output in JSON format conforming to this schema:\n{json.dumps(schema)}"
        )
        response_text = self._call_provider(prompt_with_schema, system, **kwargs)
        try:
            return json.loads(response_text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"OpenAI returned invalid JSON: {e}\nResponse: {response_text}") from e


class AnthropicClient:
    """Anthropic implementation of ModelClient using the anthropic SDK."""

    def __init__(self, provider: str, model: str, options: dict, api_key: str | None):
        try:
            import anthropic as _anthropic_sdk
        except ImportError:
            raise RuntimeError("anthropic package not installed. Run: pip install anthropic")
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
        return self._call_provider(prompt, system, **kwargs)

    def generate_structured(self, prompt: str, schema: dict, system: str | None = None, **kwargs: Any) -> dict:
        prompt_with_schema = (
            f"{prompt}\n\nReturn output in JSON format conforming to this schema:\n{json.dumps(schema)}"
        )
        response_text = self._call_provider(prompt_with_schema, system, **kwargs)
        try:
            return json.loads(response_text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Anthropic returned invalid JSON: {e}\nResponse: {response_text}") from e


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


def get_model(
    specialty: str = "text", effort: str = "think", config_path: str = "config.json", config: Config | None = None
) -> ModelClient:
    """Resolve a role (specialty × effort) to a callable model client."""
    if config is None:
        config = load_config(config_path)

    db_path = config.get("db_path") or Path.home() / ".xibi" / "data" / "xibi.db"

    try:
        role_cfg = _resolve_model(config, specialty, effort)
    except NoModelAvailableError:
        raise

    # Check health and follow fallback chain if unhealthy
    role_to_check: RoleConfig | None = role_cfg
    tried_roles = []

    while role_to_check:
        provider_name = role_to_check["provider"]
        cb_config = CircuitBreakerConfig(recovery_timeout_secs=get_timeout(config, "circuit_recovery_secs"))
        cache_key = f"{provider_name}:{db_path}"
        if cache_key not in _circuit_breaker_cache:
            _circuit_breaker_cache[cache_key] = CircuitBreaker(provider_name, db_path=db_path, config=cb_config)
        breaker = _circuit_breaker_cache[cache_key]

        if breaker.is_open():
            tried_roles.append(f"{provider_name}/{role_to_check['model']} (circuit open)")
        elif _check_provider_health(config, role_to_check):
            # Create client
            provider_cfg = config["providers"][provider_name]
            model_name = role_to_check["model"]
            options = role_to_check.get("options", {})

            # Wrap client with breaker record_success/record_failure
            client: ModelClient | None = None

            if provider_name == "ollama":
                base_url = provider_cfg.get("base_url") or "http://localhost:11434"
                client = OllamaClient(provider=provider_name, model=model_name, options=options, base_url=base_url)
            elif provider_name == "gemini":
                api_key_env = provider_cfg.get("api_key_env") or "GEMINI_API_KEY"
                api_key = os.environ.get(api_key_env)
                if api_key:
                    client = GeminiClient(provider=provider_name, model=model_name, options=options, api_key=api_key)

            if client:

                class BreakerWrappedClient:
                    def __init__(self, inner: ModelClient, breaker: CircuitBreaker, effort: str):
                        self.inner = inner
                        self.breaker = breaker
                        self.provider = inner.provider
                        self.model = inner.model
                        self.options = inner.options
                        # Label effort/role
                        self.inner._role = effort
                        self._role = effort

                    def generate(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
                        try:
                            res = self.inner.generate(prompt, system, **kwargs)
                            self.breaker.record_success()
                            return res
                        except XibiError as e:
                            if e.category in (ErrorCategory.PROVIDER_DOWN, ErrorCategory.TIMEOUT):
                                self.breaker.record_failure(FailureType.PERSISTENT)
                            else:
                                self.breaker.record_failure(FailureType.TRANSIENT)
                            raise
                        except Exception:
                            self.breaker.record_failure(FailureType.PERSISTENT)
                            raise

                    def generate_structured(
                        self, prompt: str, schema: dict, system: str | None = None, **kwargs: Any
                    ) -> dict:
                        try:
                            res = self.inner.generate_structured(prompt, schema, system, **kwargs)
                            self.breaker.record_success()
                            return res
                        except XibiError as e:
                            if e.category in (ErrorCategory.PROVIDER_DOWN, ErrorCategory.TIMEOUT):
                                self.breaker.record_failure(FailureType.PERSISTENT)
                            else:
                                self.breaker.record_failure(FailureType.TRANSIENT)
                            raise
                        except Exception:
                            self.breaker.record_failure(FailureType.PERSISTENT)
                            raise

                return BreakerWrappedClient(client, breaker, effort)  # type: ignore
            elif provider_name == "openai":
                api_key_env = provider_cfg.get("api_key_env") or ""
                client = OpenAIClient(provider_name, model_name, options, os.environ.get(api_key_env))
                client._role = effort
                return client
            elif provider_name == "anthropic":
                api_key_env = provider_cfg.get("api_key_env") or ""
                client = AnthropicClient(provider_name, model_name, options, os.environ.get(api_key_env))
                client._role = effort
                return client
            elif provider_name == "groq":
                api_key_env = provider_cfg.get("api_key_env") or ""
                client = GroqClient(provider_name, model_name, options, os.environ.get(api_key_env))
                client._role = effort
                return client

        tried_roles.append(f"{role_to_check['provider']}/{role_to_check['model']}")
        fallback_effort = role_to_check.get("fallback")
        if fallback_effort:
            # Look up specialty efforts again
            resolved_specialty = specialty if specialty in config["models"] else "text"
            role_to_check = config["models"][resolved_specialty].get(fallback_effort)
        else:
            role_to_check = None

    raise NoModelAvailableError(f"Exhausted fallback chain: {tried_roles}")
