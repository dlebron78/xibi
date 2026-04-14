from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from xibi.router import AnthropicClient, Config, load_config


@dataclass
class RoutedResponse:
    content: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    model_id: str


class ModelRouter:
    """Route LLM calls to the correct provider/model based on manifest."""

    def __init__(self, config: Config | None = None):
        self.config = config or load_config()
        self.models = self.config.get("subagent_models", {
            "haiku": {"provider": "anthropic", "model_id": "claude-3-haiku-20240307"},
            "sonnet": {"provider": "anthropic", "model_id": "claude-3-5-sonnet-20240620"},
            "opus": {"provider": "anthropic", "model_id": "claude-3-opus-20240229"}
        })
        self.pricing = self.config.get("subagent_pricing", {
            "claude-3-haiku-20240307": {"input_per_mtok": 0.25, "output_per_mtok": 1.25},
            "claude-3-5-sonnet-20240620": {"input_per_mtok": 3.00, "output_per_mtok": 15.00},
            "claude-3-opus-20240229": {"input_per_mtok": 15.00, "output_per_mtok": 75.00}
        })

    def call(self, model: str, prompt: str, system: str | None = None, **kwargs: Any) -> RoutedResponse:
        """
        model: "haiku" | "sonnet" | "opus"
        """
        model_cfg = self.models.get(model, self.models.get("haiku"))
        provider = model_cfg["provider"]
        model_id = model_cfg["model_id"]

        if provider == "anthropic":
            import os
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            client = AnthropicClient(provider=provider, model=model_id, options={}, api_key=api_key)

            # Using generate instead of generate_structured as we'll parse JSON ourselves
            content = client.generate(prompt=prompt, system=system, **kwargs)

            # AnthropicClient stores tokens in _last_tokens
            input_tokens, output_tokens, _ = getattr(client, "_last_tokens", (0, 0, 0))

            pricing = self.pricing.get(model_id, {"input_per_mtok": 0, "output_per_mtok": 0})
            cost_usd = (input_tokens * pricing["input_per_mtok"] + output_tokens * pricing["output_per_mtok"]) / 1_000_000

            return RoutedResponse(
                content=content,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                model_id=model_id
            )
        else:
            raise ValueError(f"Unsupported provider: {provider}")
