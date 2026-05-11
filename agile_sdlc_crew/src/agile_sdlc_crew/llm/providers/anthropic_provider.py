"""Anthropic API provider — direkt Anthropic API uzerinden, ANTHROPIC_API_KEY ile."""

import os

from crewai import LLM

from agile_sdlc_crew import credentials

NAME = "anthropic"

CREDS_SCHEMA = [
    {
        "name": "api_key",
        "label": "API Key",
        "secret": True,
        "env_fallback": "ANTHROPIC_API_KEY",
    },
]


def build(model: str, max_tokens: int = 4096, **kwargs) -> LLM:
    if not model.startswith("anthropic/"):
        model = f"anthropic/{model.split('/')[-1]}"
    api_key = (
        credentials.get("llm", NAME, "api_key")
        or os.environ.get("ANTHROPIC_API_KEY")
    )
    return LLM(
        model=model,
        api_key=api_key,
        max_tokens=max_tokens,
    )
