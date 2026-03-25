# Step 1: `get_model()` Router

## Architecture Reference
- Design doc: `public/xibi_architecture.md` — "Role Architecture: Roles, Not Models" and "Config Structure" sections
- Roadmap: `public/xibi_roadmap.md` — Step 1
- Pipeline: `PIPELINE.md` — task spec format and PR requirements

## Objective

Extract model routing into a standalone module (`xibi/router.py`) that resolves any `get_model(specialty, effort)` call to a provider client. This is the foundation — every subsequent step depends on it. After this step, no code in the system should reference a model name directly. It requests a role, and the router resolves the rest from config.

## Files to Create

### `xibi/__init__.py`
- Package init. Exports `get_model` from `router.py`.

### `xibi/router.py`
- The main module. Contains `get_model()`, provider abstraction, fallback chain resolution, and config loading.

### `tests/test_router.py`
- Full unit test suite covering all contracts below.

### `tests/conftest.py`
- Shared fixtures: mock config dicts, mock provider clients.

### `tests/fixtures/configs/`
- `valid.json` — copy of `config.example.json` from repo root (use as baseline)
- `circular_fallback.json` — fast → think → fast (should fail validation)
- `missing_provider.json` — references a provider not in `providers` section (should fail validation)
- `dangling_fallback.json` — fast.fallback = "ultra" but "ultra" doesn't exist (should fail validation)

## Contract

### `get_model(specialty: str, effort: str) -> ModelClient`

The single entry point for the rest of the system.

```python
def get_model(specialty: str = "text", effort: str = "think") -> ModelClient:
    """
    Resolve a role (specialty × effort) to a callable model client.

    Args:
        specialty: What kind of work. "text" (default), "image", "code", "audio", "video".
                   Unknown specialty falls back to "text".
        effort: How hard to work. "fast", "think" (default), "review".
                Unknown effort falls back to "think".

    Returns:
        ModelClient instance configured for the resolved provider + model + options.

    Raises:
        NoModelAvailableError: If the entire fallback chain is exhausted
            (all providers unreachable). This should be rare — review role
            is the ceiling and should always be a cloud provider.
    """
```

### `ModelClient` (Protocol or ABC)

```python
class ModelClient(Protocol):
    """Unified interface for all LLM providers."""

    provider: str       # "ollama", "gemini", "openai", "anthropic", "groq"
    model: str          # "qwen3.5:9b", "gemini-2.5-flash", etc.
    options: dict       # Provider-specific options (e.g., {"think": false})

    def generate(self, prompt: str, system: str = None, **kwargs) -> str:
        """Generate a text completion. Returns the response text."""
        ...

    def generate_structured(self, prompt: str, schema: dict, system: str = None, **kwargs) -> dict:
        """Generate structured output conforming to a JSON schema. Returns parsed dict."""
        ...
```

### Provider Implementations

Create concrete `ModelClient` implementations for at least:
- `OllamaClient` — HTTP calls to local Ollama instance. Must handle: model not loaded (warmup/pull), connection refused (Ollama not running), timeout.
- `GeminiClient` — Google Generative AI SDK. API key from config.
- Stub classes for `OpenAIClient`, `AnthropicClient`, `GroqClient` — just enough structure to show the pattern. Raise `NotImplementedError` with a message like "Provider not yet implemented. Add implementation and tests."

### Fallback Chain Resolution

```python
def _resolve_model(specialty: str, effort: str) -> RoleConfig:
    """
    Resolve specialty + effort to a config entry.

    Fallback rules:
    1. Look up config["models"][specialty][effort]
    2. If missing, follow the role's "fallback" field: fast → think → review
    3. If specialty missing entirely, fall back to "text" specialty
    4. If nothing resolves, raise NoModelAvailableError

    Returns the resolved RoleConfig (provider, model, options, fallback).
    """
```

### Provider Health Check

```python
def _check_provider_health(config: RoleConfig) -> bool:
    """
    Quick health check before inference.
    - Ollama: GET /api/tags — is the model loaded? If not, trigger warmup.
    - Cloud providers: skip check (assume available, handle errors on call).
    Returns True if ready, False if unreachable.
    """
```

When `_check_provider_health` returns False, the router should automatically try the fallback chain before raising an error.

### Config Loading

```python
def load_config(path: str = "config.json") -> dict:
    """
    Load and validate the model config.

    Validates:
    - Required fields present: provider, model
    - Fallback references exist (fast.fallback = "think" → "think" must exist)
    - No circular fallback chains
    - At least one role defined for "text" specialty

    Returns parsed config dict.
    Raises ConfigValidationError with specific message on any failure.
    """
```

### Config Schema (what `config.json` looks like)

```json
{
  "models": {
    "text": {
      "fast": {
        "provider": "ollama",
        "model": "qwen3.5:4b",
        "options": { "think": false },
        "fallback": "think"
      },
      "think": {
        "provider": "ollama",
        "model": "qwen3.5:9b",
        "options": { "think": false },
        "fallback": "review"
      },
      "review": {
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "options": {}
      }
    }
  },
  "providers": {
    "ollama": {
      "base_url": "http://localhost:11434"
    },
    "gemini": {
      "api_key_env": "GEMINI_API_KEY"
    },
    "openai": {
      "api_key_env": "OPENAI_API_KEY"
    },
    "anthropic": {
      "api_key_env": "ANTHROPIC_API_KEY"
    },
    "groq": {
      "api_key_env": "GROQ_API_KEY"
    }
  }
}
```

Note: API keys are read from environment variables (referenced by `api_key_env`), never stored in config directly.

## Constraints

- **No hardcoded model names anywhere.** Not in router.py, not in tests (use fixtures).
- **No external dependencies beyond `requests`** for Ollama HTTP calls and the Google GenAI SDK for Gemini. Keep it minimal.
- **Python 3.10+.** Use type hints everywhere. `dataclass` or `TypedDict` for config structures.
- **No global state.** Config is loaded explicitly, not cached in module-level variables. The caller (core.py in Step 4) will handle caching.
- **This module must work standalone.** `python -c "from xibi.router import get_model; print(get_model('text', 'fast'))"` should work with a valid config.json present.
- **Security (read `SECURITY.md`):**
  - API keys read from env vars at call time, never cached in module-level variables.
  - Config references env var names (`api_key_env: "GEMINI_API_KEY"`), never values.
  - Cloud provider clients must enforce HTTPS-only connections.
  - No PII in test fixtures — use synthetic data, `@example.com` addresses only.
  - Cloud API calls must be structured so the audit log (Step 6) can wrap them later. Design the `generate()` and `generate_structured()` methods as clean interception points. **Specifically:** the actual HTTP/SDK call should happen in a single internal method (e.g., `_call_provider()`) that a decorator or subclass can wrap in Step 6 — don't scatter API calls across multiple code paths. You do NOT implement the audit log now.

## References

- **Architecture:** `public/xibi_architecture.md`
- **Security:** `SECURITY.md` — read before writing any code touching credentials or cloud APIs
- **Pipeline:** `PIPELINE.md` — environment protocol (dev/test/production tiers), testing strategy, PR format
- **Config template:** `config.example.json` — use as test fixture baseline
- **Profile template:** `profile.example.json` — dev overrides, dry_run_sends, mock_channels
- **Dependencies:** `pyproject.toml` — install with `pip install -e ".[dev]"`

## Tests Required

### `tests/test_router.py`

**Config loading:**
- `test_load_valid_config` — loads the example config, returns parsed dict
- `test_load_missing_file` — raises `FileNotFoundError` with clear message
- `test_load_invalid_json` — raises `ConfigValidationError`
- `test_load_missing_provider_field` — raises `ConfigValidationError` naming the missing field
- `test_load_circular_fallback` — e.g., fast → think → fast. Raises `ConfigValidationError`
- `test_load_dangling_fallback` — fast.fallback = "ultra" but "ultra" doesn't exist. Raises `ConfigValidationError`

**Fallback resolution:**
- `test_resolve_exact_match` — `get_model("text", "fast")` returns the fast config
- `test_resolve_effort_fallback` — effort not in config, follows fallback chain
- `test_resolve_specialty_fallback` — unknown specialty falls back to "text"
- `test_resolve_double_fallback` — unknown specialty + unknown effort → text/think
- `test_resolve_exhausted_chain` — all providers down → `NoModelAvailableError`

**Provider health:**
- `test_ollama_healthy` — mock HTTP 200 from Ollama → returns True
- `test_ollama_unreachable` — mock connection error → returns False, triggers fallback
- `test_ollama_model_not_loaded` — mock response missing model → triggers warmup
- `test_cloud_provider_skips_check` — Gemini/OpenAI skip health check

**ModelClient contracts:**
- `test_ollama_client_generate` — mock Ollama API, verify correct HTTP call format
- `test_ollama_client_generate_structured` — mock response, verify JSON parsing
- `test_gemini_client_generate` — mock SDK, verify API call
- `test_client_timeout_handling` — verify timeout raises, doesn't hang

**Integration (with mock providers):**
- `test_full_path_fast_role` — load config → get_model("text", "fast") → generate("test") → returns string
- `test_full_path_with_fallback` — primary provider fails → fallback provider succeeds → returns string
- `test_config_reload` — change config, reload, verify new model is used

## Definition of Done

- [ ] `xibi/__init__.py` exists, exports `get_model`
- [ ] `xibi/router.py` implements all contracts above
- [ ] `tests/test_router.py` covers all test cases listed
- [ ] `tests/conftest.py` has shared fixtures
- [ ] All tests pass: `pytest tests/test_router.py -v`
- [ ] No hardcoded model names in any file
- [ ] Type hints on all public functions
- [ ] Module works standalone: `python -c "from xibi.router import get_model"`
- [ ] PR opened with: summary of what was built, full test output, any deviations from this spec noted
