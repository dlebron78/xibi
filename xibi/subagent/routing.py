from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, cast

from xibi.router import AnthropicClient, Config, GeminiClient, load_config


@dataclass
class RoutedResponse:
    content: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    model_id: str


class ModelRouter:
    """Route LLM calls to the correct provider/model based on manifest."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or load_config()
        config_dict = dict(self.config)
        self.models = config_dict.get(
            "subagent_models",
            {
                "haiku": {"provider": "gemini", "model_id": "gemini-3.1-flash-lite-preview"},
                "sonnet": {"provider": "gemini", "model_id": "gemini-3.1-flash-lite-preview"},
                "opus": {"provider": "gemini", "model_id": "gemini-3.1-flash-lite-preview"},
            },
        )
        self.pricing = config_dict.get(
            "subagent_pricing",
            {
                "gemini-3.1-flash-lite-preview": {"input_per_mtok": 0.075, "output_per_mtok": 0.30},
            },
        )

    def call(self, model: str, prompt: str, system: str | None = None, **kwargs: Any) -> RoutedResponse:
        """
        model: "haiku" | "sonnet" | "opus"
        """
        models_dict = cast(dict[str, Any], self.models)
        model_cfg = models_dict.get(model, models_dict.get("haiku", {}))
        provider = model_cfg.get("provider", "anthropic")
        model_id = model_cfg.get("model_id", "claude-3-haiku-20240307")
        pricing_dict = cast(dict[str, Any], self.pricing)

        if provider == "anthropic":
            anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
            if not anthropic_key:
                raise RuntimeError("ANTHROPIC_API_KEY is required for provider='anthropic'")
            anthropic_client = AnthropicClient(provider=provider, model=model_id, options={}, api_key=anthropic_key)
            content = anthropic_client.generate(prompt=prompt, system=system, **kwargs)
            input_tokens, output_tokens, _ = getattr(anthropic_client, "_last_tokens", (0, 0, 0))
            pricing = pricing_dict.get(model_id, {"input_per_mtok": 0.0, "output_per_mtok": 0.0})
            cost_usd = (
                input_tokens * pricing["input_per_mtok"] + output_tokens * pricing["output_per_mtok"]
            ) / 1_000_000
            return RoutedResponse(
                content=content,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                model_id=model_id,
            )
        elif provider == "gemini":
            gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            if not gemini_key:
                raise RuntimeError("GEMINI_API_KEY or GOOGLE_API_KEY is required for provider='gemini'")
            gemini_client = GeminiClient(provider=provider, model=model_id, options={}, api_key=gemini_key)
            content = gemini_client.generate(prompt=prompt, system=system, **kwargs)
            input_tokens, output_tokens, _ = getattr(gemini_client, "_last_tokens", (0, 0, 0))
            pricing = pricing_dict.get(model_id, {"input_per_mtok": 0.075, "output_per_mtok": 0.30})
            cost_usd = (
                input_tokens * pricing["input_per_mtok"] + output_tokens * pricing["output_per_mtok"]
            ) / 1_000_000
            return RoutedResponse(
                content=content,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                model_id=model_id,
            )
        else:
            raise ValueError(f"Unsupported provider: {provider}")
