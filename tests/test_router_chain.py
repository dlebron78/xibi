"""Step-60 §3: Runtime fallback chain — ChainedModelClient walks the
configured RoleConfig.fallback list on PROVIDER_DOWN/TIMEOUT/PARSE_FAILURE."""

from __future__ import annotations

import copy
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from xibi.errors import ErrorCategory, XibiError
from xibi.router import (
    ChainedModelClient,
    OllamaClient,
    _resolve_role_chain,
    get_model,
)


class FakeBreakerWrapped:
    def __init__(self, role: str, behavior: Any) -> None:
        self.provider = "fake"
        self.model = f"fake-{role}"
        self.options: dict = {}
        self._role = role
        self._behavior = behavior  # callable(prompt) -> str | raises
        self.calls = 0

    def generate(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        self.calls += 1
        return self._behavior(prompt)

    def generate_structured(self, prompt: str, schema: dict, system: str | None = None, **kwargs: Any) -> dict:
        self.calls += 1
        return {"raw": self._behavior(prompt)}


def _make_chained(*pairs: tuple[str, Any]) -> ChainedModelClient:
    chain = list(pairs)
    return ChainedModelClient(
        primary_role=chain[0][0],
        specialty="text",
        config={"models": {}, "providers": {}},  # type: ignore[typeddict-item]
        chain=chain,
    )


def _err(category: ErrorCategory, msg: str = "boom") -> XibiError:
    return XibiError(category=category, message=msg, component="fake", retryable=True)


def test_chain_walks_on_provider_down() -> None:
    fast = FakeBreakerWrapped("fast", lambda p: (_ for _ in ()).throw(_err(ErrorCategory.PROVIDER_DOWN)))
    think = FakeBreakerWrapped("think", lambda p: "think-result")
    chained = _make_chained(("fast", fast), ("think", think))

    with patch("xibi.router.time.sleep"):
        out = chained.generate("hello", system=None)

    assert out == "think-result"
    assert fast.calls == 1
    assert think.calls == 1


def test_chain_walks_on_timeout() -> None:
    fast = FakeBreakerWrapped("fast", lambda p: (_ for _ in ()).throw(_err(ErrorCategory.TIMEOUT)))
    think = FakeBreakerWrapped("think", lambda p: "ok")
    chained = _make_chained(("fast", fast), ("think", think))

    with patch("xibi.router.time.sleep"):
        assert chained.generate("hi") == "ok"
    assert fast.calls == 1 and think.calls == 1


def test_chain_does_not_walk_on_validation_error() -> None:
    fast = FakeBreakerWrapped("fast", lambda p: (_ for _ in ()).throw(_err(ErrorCategory.VALIDATION)))
    think = FakeBreakerWrapped("think", lambda p: "should not run")
    chained = _make_chained(("fast", fast), ("think", think))

    with pytest.raises(XibiError) as excinfo:
        chained.generate("hi")
    assert excinfo.value.category == ErrorCategory.VALIDATION
    assert fast.calls == 1
    assert think.calls == 0


def test_chain_exhausted_raises_enriched_error() -> None:
    fast = FakeBreakerWrapped("fast", lambda p: (_ for _ in ()).throw(_err(ErrorCategory.PROVIDER_DOWN, "fast-down")))
    think = FakeBreakerWrapped("think", lambda p: (_ for _ in ()).throw(_err(ErrorCategory.TIMEOUT, "think-slow")))
    review = FakeBreakerWrapped(
        "review", lambda p: (_ for _ in ()).throw(_err(ErrorCategory.PROVIDER_DOWN, "review-out"))
    )
    chained = _make_chained(("fast", fast), ("think", think), ("review", review))

    with patch("xibi.router.time.sleep"):
        with pytest.raises(XibiError) as excinfo:
            chained.generate("hi")

    err = excinfo.value
    assert "All 3 roles" in err.message
    detail = json.loads(err.detail)
    assert [a["role"] for a in detail["attempts"]] == ["fast", "think", "review"]
    assert detail["attempts"][0]["category"] == "provider_down"
    assert detail["attempts"][1]["category"] == "timeout"
    assert err.retryable is False


def test_chain_applies_backoff_between_walks() -> None:
    fast = FakeBreakerWrapped("fast", lambda p: (_ for _ in ()).throw(_err(ErrorCategory.PROVIDER_DOWN)))
    think = FakeBreakerWrapped("think", lambda p: (_ for _ in ()).throw(_err(ErrorCategory.PROVIDER_DOWN)))
    review = FakeBreakerWrapped("review", lambda p: "ok")
    chained = _make_chained(("fast", fast), ("think", think), ("review", review))

    sleeps: list[float] = []
    with patch("xibi.router.time.sleep", side_effect=lambda s: sleeps.append(s)):
        chained.generate("hi")

    # First attempt: no sleep. Walk to think: 0.1*2^1=0.2. Walk to review: 0.1*2^2=0.4
    assert sleeps == [pytest.approx(0.2), pytest.approx(0.4)]
    # All sleeps must be capped at 1.0
    assert all(s <= 1.0 for s in sleeps)


def test_chain_does_not_mutate_config_after_walk() -> None:
    cfg: dict[str, Any] = {
        "models": {
            "text": {
                "fast": {"provider": "ollama", "model": "a", "options": {}, "fallback": "think"},
                "think": {"provider": "ollama", "model": "b", "options": {}, "fallback": None},
            }
        },
        "providers": {"ollama": {"base_url": "x"}},
    }
    snapshot = copy.deepcopy(cfg)

    fast = FakeBreakerWrapped("fast", lambda p: (_ for _ in ()).throw(_err(ErrorCategory.PROVIDER_DOWN)))
    think = FakeBreakerWrapped("think", lambda p: "ok")
    chained = ChainedModelClient(
        primary_role="fast",
        specialty="text",
        config=cfg,  # type: ignore[arg-type]
        chain=[("fast", fast), ("think", think)],
    )

    with patch("xibi.router.time.sleep"):
        chained.generate("hi")

    assert cfg == snapshot
    assert chained._chain[0][0] == "fast"  # primary unchanged for next call


def test_structured_parse_failure_raises_xibi_error_not_runtime() -> None:
    """Latent bug regression: bad JSON from generate_structured must surface as XibiError(PARSE_FAILURE)."""
    client = OllamaClient(provider="ollama", model="m", options={}, base_url="x")
    client._role = "fast"

    fake_response = MagicMock()
    fake_response.json.return_value = {"response": "not json {{", "prompt_eval_count": 0, "eval_count": 0}
    fake_response.raise_for_status = MagicMock()

    with patch("xibi.router.requests.post", return_value=fake_response):
        with pytest.raises(XibiError) as excinfo:
            client.generate_structured("hello", {"type": "object"}, system=None)

    assert excinfo.value.category == ErrorCategory.PARSE_FAILURE
    assert excinfo.value.component == "ollama"


def test_structured_parse_failure_walks_chain() -> None:
    bad = FakeBreakerWrapped(
        "fast",
        lambda p: (_ for _ in ()).throw(
            XibiError(category=ErrorCategory.PARSE_FAILURE, message="bad json", component="fake", retryable=True)
        ),
    )
    good = FakeBreakerWrapped("think", lambda p: '{"ok": true}')
    chained = _make_chained(("fast", bad), ("think", good))

    with patch("xibi.router.time.sleep"):
        out = chained.generate_structured("hi", {})
    assert out == {"raw": '{"ok": true}'}
    assert bad.calls == 1 and good.calls == 1


def test_resolve_role_chain_walks_fallback_field(mock_config) -> None:
    chain = _resolve_role_chain(mock_config, "text", "fast")
    assert [name for name, _ in chain] == ["fast", "think", "review"]
