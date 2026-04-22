from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Finding:
    check_name: str
    severity: Severity
    dedup_key: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)
