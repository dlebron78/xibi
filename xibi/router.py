import json
import os
from typing import Any, Protocol, TypedDict

import google.generativeai as genai
import requests


class ModelClient(Protocol):
    """Unified interface for all LLM providers."""

    provider: str  # "ollama", "gemini", "openai", "anthropic", "groq"
    model: str  # "qwen3.5:9b", "gemini-2.5-flash", etc.
    options: dict  # Provider-specific options (e.g., {"think": false})

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


class Config(TypedDict):
    models: dict[str, dict[str, RoleConfig]]
    providers: dict[str, ProviderConfig]


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

    def _call_provider(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        url = f"{self.base_url}/api/generate"
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {**self.options, **kwargs},
        }
        if system:
            payload["system"] = system

        try:
            response = requests.post(url, json=payload, timeout=kwargs.get("timeout", 60))
            response.raise_for_status()
            result: str = response.json().get("response", "")
            return result
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Ollama call failed: {e}") from e

    def generate(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        return self._call_provider(prompt, system, **kwargs)

    def generate_structured(self, prompt: str, schema: dict, system: str | None = None, **kwargs: Any) -> dict:
        prompt_with_schema = (
            f"{prompt}\n\nReturn output in JSON format conforming to this schema:\n{json.dumps(schema)}"
        )
        # Ollama can use format="json" for JSON mode
        kwargs.setdefault("format", "json")
        response_text = self._call_provider(prompt_with_schema, system, **kwargs)
        try:
            result: dict = json.loads(response_text)
            return result
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Ollama returned invalid JSON: {e}\nResponse: {response_text}") from e


class GeminiClient:
    """Gemini implementation of ModelClient."""

    def __init__(self, provider: str, model: str, options: dict, api_key: str):
        self.provider = provider
        self.model = model
        self.options = options
        genai.configure(api_key=api_key)
        self.client = genai.GenerativeModel(model_name=model)

    def _call_provider(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        # Note: Gemini SDK handles system instructions differently, but for simplicity here:
        # we can pass it if we re-initialize the model or just prepend.
        # But properly: genai.GenerativeModel(model_name=model, system_instruction=system)
        client = genai.GenerativeModel(model_name=self.model, system_instruction=system) if system else self.client

        # options are passed via generation_config
        generation_config: Any = {**self.options}
        # handle timeout via request_options
        request_options: Any = {}
        if "timeout" in kwargs:
            request_options["timeout"] = kwargs.pop("timeout")

        generation_config.update(kwargs)

        try:
            response = client.generate_content(
                prompt, generation_config=generation_config, request_options=request_options
            )
            result: str = response.text
            return result
        except Exception as e:
            raise RuntimeError(f"Gemini call failed: {e}") from e

    def generate(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        return self._call_provider(prompt, system, **kwargs)

    def generate_structured(self, prompt: str, schema: dict, system: str | None = None, **kwargs: Any) -> dict:
        prompt_with_schema = (
            f"{prompt}\n\nReturn output in JSON format conforming to this schema:\n{json.dumps(schema)}"
        )
        # For Gemini, we could use response_mime_type="application/json"
        kwargs.setdefault("response_mime_type", "application/json")
        response_text = self._call_provider(prompt_with_schema, system, **kwargs)
        try:
            result: dict = json.loads(response_text)
            return result
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Gemini returned invalid JSON: {e}\nResponse: {response_text}") from e


class OpenAIClient:
    provider: str
    model: str
    options: dict

    def __init__(self, provider: str, model: str, options: dict, api_key: str | None):
        raise NotImplementedError("Provider OpenAI not yet implemented. Add implementation and tests.")

    def generate(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        return ""

    def generate_structured(self, prompt: str, schema: dict, system: str | None = None, **kwargs: Any) -> dict:
        return {}


class AnthropicClient:
    provider: str
    model: str
    options: dict

    def __init__(self, provider: str, model: str, options: dict, api_key: str | None):
        raise NotImplementedError("Provider Anthropic not yet implemented. Add implementation and tests.")

    def generate(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        return ""

    def generate_structured(self, prompt: str, schema: dict, system: str | None = None, **kwargs: Any) -> dict:
        return {}


class GroqClient:
    provider: str
    model: str
    options: dict

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

    try:
        role_cfg = _resolve_model(config, specialty, effort)
    except NoModelAvailableError:
        raise

    # Check health and follow fallback chain if unhealthy
    role_to_check: RoleConfig | None = role_cfg
    tried_roles = []

    while role_to_check:
        if _check_provider_health(config, role_to_check):
            # Create client
            provider_name = role_to_check["provider"]
            provider_cfg = config["providers"][provider_name]
            model = role_to_check["model"]
            options = role_to_check.get("options", {})

            if provider_name == "ollama":
                base_url = provider_cfg.get("base_url") or "http://localhost:11434"
                return OllamaClient(provider=provider_name, model=model, options=options, base_url=base_url)
            elif provider_name == "gemini":
                api_key_env = provider_cfg.get("api_key_env") or "GEMINI_API_KEY"
                api_key = os.environ.get(api_key_env)
                if not api_key:
                    # If API key missing, maybe try fallback?
                    pass
                else:
                    return GeminiClient(provider=provider_name, model=model, options=options, api_key=api_key)
            elif provider_name == "openai":
                api_key_env = provider_cfg.get("api_key_env") or ""
                return OpenAIClient(provider_name, model, options, os.environ.get(api_key_env))
            elif provider_name == "anthropic":
                api_key_env = provider_cfg.get("api_key_env") or ""
                return AnthropicClient(provider_name, model, options, os.environ.get(api_key_env))
            elif provider_name == "groq":
                api_key_env = provider_cfg.get("api_key_env") or ""
                return GroqClient(provider_name, model, options, os.environ.get(api_key_env))

        tried_roles.append(f"{role_to_check['provider']}/{role_to_check['model']}")
        fallback_effort = role_to_check.get("fallback")
        if fallback_effort:
            # Look up specialty efforts again
            resolved_specialty = specialty if specialty in config["models"] else "text"
            role_to_check = config["models"][resolved_specialty].get(fallback_effort)
        else:
            role_to_check = None

    raise NoModelAvailableError(f"Exhausted fallback chain: {tried_roles}")
