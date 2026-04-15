from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, cast

from xibi.router import AnthropicClient, GeminiClient, Config, load_config


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
                "haiku": {"provider": "anthropic", "model_id": "claude-3-haiku-20240307"},
                "sonnet": {"provider": "anthropic", "model_id": "claude-3-5-sonnet-20240620"},
                "opus": {"provider": "anthropic", "model_id": "claude-3-opus-20240229"},
            },
        )
        self.pricing = config_dict.get(
            "subagent_pricing",
            {
                "claude-3-haiku-20240307": {"input_per_mtok": 0.25, "output_per_mtok": 1.25},
                "claude-3-5-sonnet-20240620": {"input_per_mtok": 3.00, "output_per_mtok": 15.00},
                "claude-3-opus-20240229": {"input_per_mtok": 15.00, "output_per_mtok": 75.00},
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

        if provider == "anthropic":
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            client = AnthropicClient(provider=provider, model=model_id, options={}, api_key=api_key)

            # Using generate instead of generate_structured as we'll parse JSON ourselves
            content = client.generate(prompt=prompt, system=system, **kwargs)

            # AnthropicClient stores tokens in _last_tokens
            input_tokens, output_tokens, _ = getattr(client, "_last_tokens", (0, 0, 0))

            pricing_dict = cast(dict[str, Any], self.pricing)
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
            api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            client = GeminiClient(provider=provider, model=model_id, options={}, api_key=api_key)
            content = client.generate(prompt=prompt, system=system, **kwargs)
            input_tokens, output_tokens, _ = getattr(client, "_last_tokens", (0, 0, 0))
            pricing_dict = cast(dict[str, Any], self.pricing)
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
