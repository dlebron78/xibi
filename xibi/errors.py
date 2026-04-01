from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class ErrorCategory(str, Enum):
    TIMEOUT = "timeout"  # Tool or provider took too long
    TOOL_NOT_FOUND = "tool_not_found"  # Tool name not in registry
    PARSE_FAILURE = "parse_failure"  # LLM response wasn't valid JSON
    PROVIDER_DOWN = "provider_down"  # LLM provider unreachable
    VALIDATION = "validation"  # Tool params failed schema check
    CIRCUIT_OPEN = "circuit_open"  # Circuit breaker active — not retrying
    PERMISSION = "permission"  # Access denied (channel auth)
    UNKNOWN = "unknown"  # Catch-all


@dataclass
class XibiError(RuntimeError):
    category: ErrorCategory
    message: str  # Human-readable, safe to show user
    component: str  # e.g. "executor", "router", "telegram"
    detail: str = ""  # Technical detail for logs, not user-facing
    retryable: bool = True
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def user_message(self) -> str:
        """Safe string to show end users."""
        match self.category:
            case ErrorCategory.TIMEOUT:
                return f"That took too long — {self.message}. Please try again."
            case ErrorCategory.TOOL_NOT_FOUND:
                return f"I don't have a tool for that: {self.message}"
            case ErrorCategory.PROVIDER_DOWN:
                return "I'm having trouble reaching my AI provider. Trying a fallback."
            case ErrorCategory.CIRCUIT_OPEN:
                return f"I'm temporarily pausing calls to {self.component} — too many recent failures."
            case ErrorCategory.PARSE_FAILURE:
                return "I had trouble understanding the response. Retrying."
            case _:
                return "Something went wrong. Please try again."

    def to_dict(self) -> dict:
        """JSON-serializable representation for logging and storage."""
        return {
            "category": self.category.value,
            "message": self.message,
            "component": self.component,
            "detail": self.detail,
            "retryable": self.retryable,
            "timestamp": self.timestamp,
        }
