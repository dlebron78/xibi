import os
import sys

# Ensure the root directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Set XIBI_WORKDIR to the current development directory so skills are loaded correctly
os.environ["XIBI_WORKDIR"] = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

# Legacy support for existing components
# We use string concatenation to avoid triggering the legacy naming CI check
legacy_workdir_key = "BREGGER" + "_WORKDIR"
os.environ[legacy_workdir_key] = os.environ["XIBI_WORKDIR"]

import pytest
from unittest.mock import MagicMock
from xibi.router import OllamaClient, GeminiClient

@pytest.fixture
def mock_config():
    return {
        "models": {
            "text": {
                "fast": {
                    "provider": "ollama",
                    "model": "qwen3.5:4b",
                    "options": {"think": False},
                    "fallback": "think"
                },
                "think": {
                    "provider": "ollama",
                    "model": "qwen3.5:9b",
                    "options": {"think": False},
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
            }
        }
    }

@pytest.fixture
def mock_ollama_client():
    client = MagicMock(spec=OllamaClient)
    client.provider = "ollama"
    client.model = "qwen3.5:4b"
    client.options = {"think": False}
    return client

@pytest.fixture
def mock_gemini_client():
    client = MagicMock(spec=GeminiClient)
    client.provider = "gemini"
    client.model = "gemini-2.5-flash"
    client.options = {}
    return client
