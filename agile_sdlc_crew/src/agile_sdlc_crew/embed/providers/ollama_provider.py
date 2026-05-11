"""Ollama embedding provider — POST {base_url}/api/embeddings.

Ollama yerel kurulumu icin default port 11434. mxbai-embed-large,
nomic-embed-text, bge-m3 gibi modeller desteklenir."""

import os

import requests

from agile_sdlc_crew import credentials

NAME = "ollama"

CREDS_SCHEMA = [
    {
        "name": "base_url",
        "label": "Base URL",
        "secret": False,
        "env_fallback": "OLLAMA_BASE_URL",
        "placeholder": "http://localhost:11434",
    },
]


def embed(
    text: str,
    model: str,
    base_url: str = "",
    timeout: int = 90,
    **kwargs,
) -> list[float]:
    base_url = (
        base_url
        or credentials.get("embedding", NAME, "base_url")
        or os.environ.get("CREW_EMBED_BASE_URL")
        or os.environ.get("OLLAMA_BASE_URL")
        or "http://localhost:11434"
    ).rstrip("/")
    resp = requests.post(
        f"{base_url}/api/embeddings",
        json={"model": model, "prompt": text},
        timeout=timeout,
        verify=False,
    )
    resp.raise_for_status()
    data = resp.json()
    emb = data.get("embedding")
    if not emb:
        raise RuntimeError(f"Ollama embedding bos yanit: {data}")
    return emb
