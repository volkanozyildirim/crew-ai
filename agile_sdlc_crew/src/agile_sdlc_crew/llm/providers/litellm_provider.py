"""LiteLLM proxy provider (default).

LITELLM_BASE_URL + LITELLM_API_KEY uzerinden OpenAI-compatible bir proxy'ye
istek gonderir. Vertex AI, Azure OpenAI, Bedrock vb. backend'ler proxy
arkasinda olabilir."""

import os

from crewai import LLM

from agile_sdlc_crew import credentials

NAME = "litellm"

CREDS_SCHEMA = [
    {
        "name": "base_url",
        "label": "Base URL",
        "secret": False,
        "env_fallback": "LITELLM_BASE_URL",
        "placeholder": "https://litellm.example.com",
    },
    {
        "name": "api_key",
        "label": "API Key",
        "secret": True,
        "env_fallback": "LITELLM_API_KEY",
    },
]

_PASSTHROUGH_PREFIXES = (
    "openai/",
    "azure/",
    "vertex_ai/",
    "anthropic/",
    "bedrock/",
    "gemini/",
    "ollama/",
)


def build(model: str, max_tokens: int = 4096, **kwargs) -> LLM:
    base_url = (
        credentials.get("llm", NAME, "base_url")
        or os.environ.get("LITELLM_BASE_URL")
    )
    api_key = (
        credentials.get("llm", NAME, "api_key")
        or os.environ.get("LITELLM_API_KEY")
    )
    if base_url and not model.startswith(_PASSTHROUGH_PREFIXES):
        model = f"openai/{model}"
    return LLM(
        model=model,
        base_url=base_url,
        api_key=api_key,
        max_tokens=max_tokens,
    )
