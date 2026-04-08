from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

from xibi.errors import ErrorCategory, XibiError

logger = logging.getLogger(__name__)


@dataclass
class ToolHandle:
    """A reference to a tool output stored out-of-band from the LLM prompt.

    Handles are session-scoped: created during a run, valid for the lifetime
    of that run, dropped at run end. They are NOT persisted across runs.
    """

    handle_id: str  # short, e.g. "h_a8f3" — must be safe to inline
    tool: str  # producing tool name
    schema: str  # one-line shape hint, e.g. "list[dict] (25 items)"
    summary: str  # ≤500 char human-readable preview
    item_count: int | None  # nullable; populated for list-shaped data
    size_bytes: int  # serialized size of underlying payload
    created_at: float  # monotonic timestamp


class HandleStore:
    """Session-scoped, in-memory only. One instance per ReAct run."""

    def __init__(self, max_handles: int = 64, max_total_bytes: int = 32 * 1024 * 1024):
        self._handles: dict[str, ToolHandle] = {}
        self._payloads: dict[str, Any] = {}
        self._evicted_ids: set[str] = set()
        self._max_handles = max_handles
        self._max_total_bytes = max_total_bytes
        self._total_bytes = 0

    def create(self, tool: str, payload: Any) -> ToolHandle:
        # 1. Serialize to calculate size
        serialized = json.dumps(payload, default=str, separators=(",", ":"))
        size_bytes = len(serialized)

        # 2. Heuristics for schema and item_count
        item_count = self._get_item_count(payload)
        schema_hint = self._get_schema_hint(payload, item_count)

        # 3. Generate summary
        summary = self._generate_summary(payload)

        # 4. Generate random, unique ID
        handle_id = self._generate_unique_id()

        # 5. Evict if necessary
        self._evict_if_needed(size_bytes)

        # 6. Create handle
        handle = ToolHandle(
            handle_id=handle_id,
            tool=tool,
            schema=schema_hint,
            summary=summary,
            item_count=item_count,
            size_bytes=size_bytes,
            created_at=time.monotonic(),
        )

        self._handles[handle_id] = handle
        self._payloads[handle_id] = payload
        self._total_bytes += size_bytes

        return handle

    def get(self, handle_id: str) -> Any:
        if handle_id not in self._payloads:
            if handle_id in self._evicted_ids:
                # Handle existed but was evicted
                raise XibiError(
                    category=ErrorCategory.VALIDATION,
                    message=f"Handle {handle_id} has been evicted from this run's store",
                    component="handle_store",
                )
            raise XibiError(
                category=ErrorCategory.VALIDATION,
                message=f"Handle {handle_id} not in store",
                component="handle_store",
            )
        return self._payloads[handle_id]

    def get_handle(self, handle_id: str) -> ToolHandle | None:
        return self._handles.get(handle_id)

    def drop(self, handle_id: str) -> None:
        if handle_id not in self._handles:
            return
        handle = self._handles.pop(handle_id)
        # Always decrement byte accounting when a handle is removed, even if
        # the payload dict has somehow diverged from the handle dict. Keeping
        # these two in lockstep is the whole point of drop() being the single
        # mutation path; letting them drift silently breaks eviction.
        self._total_bytes -= handle.size_bytes
        if self._total_bytes < 0:
            self._total_bytes = 0
        self._payloads.pop(handle_id, None)

    def _generate_unique_id(self) -> str:
        for _attempt in range(8):
            suffix = secrets.token_hex(2)
            hid = f"h_{suffix}"
            if hid not in self._handles:
                return hid
        # Fallback to longer ID on collision
        return f"h_{secrets.token_hex(3)}"

    def _evict_if_needed(self, incoming_bytes: int) -> None:
        while len(self._payloads) >= self._max_handles or (self._total_bytes + incoming_bytes > self._max_total_bytes):
            if not self._payloads:
                break
            # Evict oldest by created_at
            oldest_id = min(self._handles.keys(), key=lambda k: self._handles[k].created_at)
            oldest_handle = self._handles[oldest_id]
            logger.warning("Evicting handle %s (from tool %s) to free up space", oldest_id, oldest_handle.tool)
            self._evicted_ids.add(oldest_id)
            self.drop(oldest_id)

    def _get_item_count(self, payload: Any) -> int | None:
        if isinstance(payload, list):
            return len(payload)
        if isinstance(payload, dict):
            for key in ("data", "items", "results", "jobs"):
                if isinstance(payload.get(key), list):
                    return len(payload[key])
        return None

    def _get_schema_hint(self, payload: Any, item_count: int | None) -> str:
        if isinstance(payload, list):
            inner_type = "dict" if payload and isinstance(payload[0], dict) else "item"
            return f"list[{inner_type}] ({item_count} items)"
        if isinstance(payload, dict):
            if item_count is not None:
                return f"dict containing {item_count} items"
            return "dict"
        return type(payload).__name__

    def _generate_summary(self, payload: Any) -> str:
        # Simple summary: first N chars of stringified payload
        s = str(payload)
        if len(s) > 500:
            return s[:497] + "..."
        return s

    def __len__(self) -> int:
        return len(self._handles)


def _is_large_collection(payload: Any) -> bool:
    """Heuristic for handle wrapping: >= 20 items."""
    if isinstance(payload, list):
        return len(payload) >= 20
    if isinstance(payload, dict):
        for key in ("data", "items", "results", "jobs"):
            if isinstance(payload.get(key), list) and len(payload[key]) >= 20:
                return True
    return False
