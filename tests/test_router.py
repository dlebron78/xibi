import json
import os
from unittest.mock import MagicMock, patch

import pytest
import requests
import responses

from xibi.router import (
    AnthropicClient,
    ConfigValidationError,
    GeminiClient,
    GroqClient,
    OllamaClient,
    OpenAIClient,
    _check_provider_health,
    _resolve_model,
    get_model,
    load_config,
)


# Config loading tests
def test_load_valid_config():
    config = load_config("tests/fixtures/configs/valid.json")
    assert "models" in config
    assert "providers" in config
    assert config["models"]["text"]["fast"]["provider"] == "ollama"


def test_load_missing_file():
    with pytest.raises(FileNotFoundError):
        load_config("non_existent.json")


def test_load_invalid_json(tmp_path):
    d = tmp_path / "invalid.json"
    d.write_text("{invalid: json}")
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(str(d))
    assert "Invalid JSON" in str(excinfo.value)


def test_load_missing_provider_field(tmp_path):
    config_dict = {"models": {"text": {"fast": {"model": "m"}}}, "providers": {}}
    d = tmp_path / "missing.json"
    d.write_text(json.dumps(config_dict))
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(str(d))
    assert "Missing 'provider'" in str(excinfo.value)


def test_load_circular_fallback():
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config("tests/fixtures/configs/circular_fallback.json")
    assert "Circular fallback" in str(excinfo.value)


def test_load_dangling_fallback():
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config("tests/fixtures/configs/dangling_fallback.json")
    assert "Fallback 'ultra' for models.text.fast does not exist" in str(excinfo.value)


def test_load_missing_provider_in_providers():
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config("tests/fixtures/configs/missing_provider.json")
    assert "Provider 'magical_inference_cloud' referenced in models.text.fast not found" in str(excinfo.value)


# Fallback resolution tests
def test_resolve_exact_match(mock_config):
    res = _resolve_model(mock_config, "text", "fast")
    assert res["model"] == "qwen3.5:4b"


def test_resolve_effort_fallback(mock_config):
    # If effort missing, it should default to 'think' according to my implementation of _resolve_model
    res = _resolve_model(mock_config, "text", "ultra")
    assert res["model"] == "qwen3.5:9b"  # think model


def test_resolve_specialty_fallback(mock_config):
    res = _resolve_model(mock_config, "code", "fast")
    assert res["model"] == "qwen3.5:4b"  # from text.fast


def test_resolve_double_fallback(mock_config):
    res = _resolve_model(mock_config, "audio", "ultra")
    assert res["model"] == "qwen3.5:9b"  # from text.think


# Provider health tests
@responses.activate
def test_ollama_healthy():
    responses.add(
        responses.GET, "http://localhost:11434/api/tags", json={"models": [{"name": "qwen3.5:4b"}]}, status=200
    )
    config = {"providers": {"ollama": {"base_url": "http://localhost:11434"}}}
    role_cfg = {"provider": "ollama", "model": "qwen3.5:4b"}
    assert _check_provider_health(config, role_cfg) is True


@responses.activate
def test_ollama_unreachable():
    responses.add(
        responses.GET, "http://localhost:11434/api/tags", body=requests.exceptions.ConnectionError("Unreachable")
    )
    config = {"providers": {"ollama": {"base_url": "http://localhost:11434"}}}
    role_cfg = {"provider": "ollama", "model": "qwen3.5:4b"}
    assert _check_provider_health(config, role_cfg) is False


@responses.activate
def test_ollama_model_not_loaded_triggers_warmup():
    responses.add(
        responses.GET, "http://localhost:11434/api/tags", json={"models": [{"name": "other-model"}]}, status=200
    )
    responses.add(responses.POST, "http://localhost:11434/api/generate", status=200)

    config = {"providers": {"ollama": {"base_url": "http://localhost:11434"}}}
    role_cfg = {"provider": "ollama", "model": "qwen3.5:4b"}
    assert _check_provider_health(config, role_cfg) is True
    assert len(responses.calls) == 2
    assert responses.calls[1].request.url == "http://localhost:11434/api/generate"


def test_cloud_provider_skips_check():
    config = {"providers": {"gemini": {}}}
    role_cfg = {"provider": "gemini", "model": "gemini-2.5-flash"}
    assert _check_provider_health(config, role_cfg) is True


# ModelClient contracts
@responses.activate
def test_ollama_client_generate():
    responses.add(responses.POST, "http://localhost:11434/api/generate", json={"response": "Hello world"}, status=200)
    client = OllamaClient("ollama", "m", {}, "http://localhost:11434")
    res = client.generate("Hi")
    assert res == "Hello world"
    assert json.loads(responses.calls[0].request.body)["prompt"] == "Hi"


@responses.activate
def test_ollama_client_generate_structured():
    responses.add(
        responses.POST, "http://localhost:11434/api/generate", json={"response": '{"key": "value"}'}, status=200
    )
    client = OllamaClient("ollama", "m", {}, "http://localhost:11434")
    res = client.generate_structured("Hi", {"type": "object"})
    assert res == {"key": "value"}


@patch("google.generativeai.GenerativeModel")
@patch("google.generativeai.configure")
def test_gemini_client_generate(mock_configure, mock_gen_model):
    mock_model_instance = MagicMock()
    mock_gen_model.return_value = mock_model_instance
    mock_model_instance.generate_content.return_value = MagicMock(text="Gemini response")

    client = GeminiClient("gemini", "gemini-2.5-flash", {}, "fake-key")
    res = client.generate("Hi")
    assert res == "Gemini response"
    mock_configure.assert_called_with(api_key="fake-key")


@responses.activate
def test_client_timeout_handling():
    responses.add(responses.POST, "http://localhost:11434/api/generate", body=requests.exceptions.Timeout("Timeout"))
    client = OllamaClient("ollama", "m", {}, "http://localhost:11434")
    with pytest.raises(RuntimeError) as excinfo:
        client.generate("Hi", timeout=1)
    assert "failed" in str(excinfo.value)


# Integration tests
@responses.activate
@patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"})
@patch("google.generativeai.GenerativeModel")
def test_full_path_fast_role(mock_gen_model):
    # Mock Ollama health check
    responses.add(
        responses.GET, "http://localhost:11434/api/tags", json={"models": [{"name": "qwen3.5:4b"}]}, status=200
    )
    # Mock Ollama generate
    responses.add(
        responses.POST, "http://localhost:11434/api/generate", json={"response": "Ollama fast response"}, status=200
    )

    client = get_model("text", "fast", config_path="tests/fixtures/configs/valid.json")
    res = client.generate("test")
    assert res == "Ollama fast response"


@responses.activate
@patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"})
@patch("google.generativeai.GenerativeModel")
def test_full_path_with_fallback(mock_gen_model):
    # Mock Ollama health check - UNHEALTHY
    responses.add(responses.GET, "http://localhost:11434/api/tags", status=500)
    # Fallback from fast -> think (also ollama)
    # Mock Ollama think health check - UNHEALTHY
    # responses.add(responses.GET, "http://localhost:11434/api/tags", status=500) # already added for any GET to tags

    # Fallback from think -> review (gemini)
    mock_model_instance = MagicMock()
    mock_gen_model.return_value = mock_model_instance
    mock_model_instance.generate_content.return_value = MagicMock(text="Gemini fallback response")

    client = get_model("text", "fast", config_path="tests/fixtures/configs/valid.json")
    assert client.provider == "gemini"
    res = client.generate("test")
    assert res == "Gemini fallback response"


def test_config_reload(tmp_path):
    config1 = {
        "models": {"text": {"fast": {"provider": "ollama", "model": "m1"}}},
        "providers": {"ollama": {"base_url": "h1"}},
    }
    config2 = {
        "models": {"text": {"fast": {"provider": "ollama", "model": "m2"}}},
        "providers": {"ollama": {"base_url": "h2"}},
    }
    cp = tmp_path / "config.json"
    cp.write_text(json.dumps(config1))

    with patch("xibi.router._check_provider_health", return_value=True):
        client1 = get_model("text", "fast", config_path=str(cp))
        assert client1.model == "m1"

        cp.write_text(json.dumps(config2))
        client2 = get_model("text", "fast", config_path=str(cp))
        assert client2.model == "m2"


# Additional coverage tests


@responses.activate
def test_ollama_client_generate_with_system():
    """Covers the system prompt branch in OllamaClient._call_provider (line 72)."""
    responses.add(responses.POST, "http://localhost:11434/api/generate", json={"response": "Hi there"}, status=200)
    client = OllamaClient("ollama", "m", {}, "http://localhost:11434")
    res = client.generate("Hi", system="You are a helpful assistant.")
    assert res == "Hi there"
    body = json.loads(responses.calls[0].request.body)
    assert body["system"] == "You are a helpful assistant."


@responses.activate
def test_ollama_generate_structured_invalid_json():
    """Covers JSONDecodeError branch in OllamaClient.generate_structured (lines 95-96)."""
    responses.add(
        responses.POST, "http://localhost:11434/api/generate", json={"response": "not valid json {"}, status=200
    )
    client = OllamaClient("ollama", "m", {}, "http://localhost:11434")
    with pytest.raises(RuntimeError) as excinfo:
        client.generate_structured("Hi", {"type": "object"})
    assert "invalid JSON" in str(excinfo.value)


@patch("google.generativeai.GenerativeModel")
@patch("google.generativeai.configure")
def test_gemini_client_generate_structured(mock_configure, mock_gen_model):
    """Covers GeminiClient.generate_structured (lines 137-147)."""
    mock_model_instance = MagicMock()
    mock_gen_model.return_value = mock_model_instance
    mock_model_instance.generate_content.return_value = MagicMock(text='{"result": "ok"}')

    client = GeminiClient("gemini", "gemini-2.5-flash", {}, "fake-key")
    res = client.generate_structured("Summarize", {"type": "object"})
    assert res == {"result": "ok"}


@patch("google.generativeai.GenerativeModel")
@patch("google.generativeai.configure")
def test_gemini_client_generate_exception(mock_configure, mock_gen_model):
    """Covers the Exception branch in GeminiClient._call_provider (lines 130-131)."""
    mock_model_instance = MagicMock()
    mock_gen_model.return_value = mock_model_instance
    mock_model_instance.generate_content.side_effect = RuntimeError("API error")

    client = GeminiClient("gemini", "gemini-2.5-flash", {}, "fake-key")
    with pytest.raises(RuntimeError) as excinfo:
        client.generate("Hi")
    assert "Gemini call failed" in str(excinfo.value)


@patch("google.generativeai.GenerativeModel")
@patch("google.generativeai.configure")
def test_gemini_client_generate_structured_invalid_json(mock_configure, mock_gen_model):
    """Covers JSONDecodeError branch in GeminiClient.generate_structured."""
    mock_model_instance = MagicMock()
    mock_gen_model.return_value = mock_model_instance
    mock_model_instance.generate_content.return_value = MagicMock(text="not-json")

    client = GeminiClient("gemini", "gemini-2.5-flash", {}, "fake-key")
    with pytest.raises(RuntimeError) as excinfo:
        client.generate_structured("Hi", {"type": "object"})
    assert "invalid JSON" in str(excinfo.value)


@patch("google.generativeai.GenerativeModel")
@patch("google.generativeai.configure")
def test_gemini_client_generate_with_timeout(mock_configure, mock_gen_model):
    """Covers the timeout kwarg branch in GeminiClient._call_provider (line 120)."""
    mock_model_instance = MagicMock()
    mock_gen_model.return_value = mock_model_instance
    mock_model_instance.generate_content.return_value = MagicMock(text="response")

    client = GeminiClient("gemini", "gemini-2.5-flash", {}, "fake-key")
    res = client.generate("Hi", timeout=30)
    assert res == "response"


def test_openai_client_not_implemented():
    """Covers OpenAIClient stub __init__ (line 156)."""
    with pytest.raises(NotImplementedError):
        OpenAIClient("openai", "gpt-4", {}, "fake-key")


def test_anthropic_client_not_implemented():
    """Covers AnthropicClient stub __init__ (line 171)."""
    with pytest.raises(NotImplementedError):
        AnthropicClient("anthropic", "claude-3", {}, "fake-key")


def test_groq_client_not_implemented():
    """Covers GroqClient stub __init__ (line 186)."""
    with pytest.raises(NotImplementedError):
        GroqClient("groq", "llama3", {}, "fake-key")


@responses.activate
def test_ollama_model_matched_with_latest_tag():
    """Covers the ':latest' tag matching paths in _check_provider_health (lines 306-312)."""
    # Model list has "qwen3.5:latest" but we request "qwen3.5"
    responses.add(
        responses.GET,
        "http://localhost:11434/api/tags",
        json={"models": [{"name": "qwen3.5:latest"}]},
        status=200,
    )
    config = {"providers": {"ollama": {"base_url": "http://localhost:11434"}}}
    role_cfg = {"provider": "ollama", "model": "qwen3.5"}
    assert _check_provider_health(config, role_cfg) is True


@responses.activate
def test_ollama_model_matched_base_name_without_latest():
    """Covers model matching when requesting 'model:latest' but list has bare name (line ~312)."""
    responses.add(
        responses.GET,
        "http://localhost:11434/api/tags",
        json={"models": [{"name": "qwen3.5"}]},
        status=200,
    )
    config = {"providers": {"ollama": {"base_url": "http://localhost:11434"}}}
    role_cfg = {"provider": "ollama", "model": "qwen3.5:latest"}
    assert _check_provider_health(config, role_cfg) is True


def test_resolve_model_no_think_effort_returns_first(mock_config):
    """Covers the branch where 'think' is missing and we return the first available effort (line 279)."""
    config = {
        "models": {"text": {"fast": {"provider": "ollama", "model": "fast-model"}}},
        "providers": {"ollama": {}},
    }
    # "ultra" is not a defined effort; "think" is also missing; should return first available
    res = _resolve_model(config, "text", "ultra")
    assert res["model"] == "fast-model"
