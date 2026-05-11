"""OpenAI-compatible embedding provider.

LiteLLM proxy, OpenAI'in kendisi, LM Studio (/v1 endpoint), Azure OpenAI
gibi tum OpenAI-compatible API'leri kapsiyor.

POST {base_url}/embeddings  with body {model, input}.
"""

import os

import requests

from agile_sdlc_crew import credentials

NAME = "openai"

CREDS_SCHEMA = [
    {
        "name": "base_url",
        "label": "Base URL",
        "secret": False,
        "env_fallback": "OPENAI_BASE_URL",
        "placeholder": "https://api.openai.com/v1",
    },
    {
        "name": "api_key",
        "label": "API Key",
        "secret": True,
        "env_fallback": "OPENAI_API_KEY",
    },
]


def embed(
    text: str,
    model: str,
    base_url: str = "",
    api_key: str = "",
    api_key_env: str = "",
    timeout: int = 60,
    **kwargs,
) -> list[float]:
    # 1. embed_config.yaml > 2. credentials.yaml > 3. env > 4. default
    base_url = (
        base_url
        or credentials.get("embedding", NAME, "base_url")
        or os.environ.get("CREW_EMBED_BASE_URL")
        or os.environ.get("LITELLM_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or "https://api.openai.com/v1"
    ).rstrip("/")
    if not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"

    if not api_key:
        if api_key_env:
            api_key = os.environ.get(api_key_env, "")
        if not api_key:
            api_key = (
                credentials.get("embedding", NAME, "api_key")
                or os.environ.get("LITELLM_API_KEY")
                or os.environ.get("OPENAI_API_KEY")
                or ""
            )

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    resp = requests.post(
        f"{base_url}/embeddings",
        json={"model": model, "input": text},
        headers=headers,
        timeout=timeout,
        verify=False,
    )
    resp.raise_for_status()
    data = resp.json()
    items = data.get("data") or []
    if not items or "embedding" not in items[0]:
        raise RuntimeError(f"OpenAI embedding bos yanit: {data}")
    return items[0]["embedding"]
