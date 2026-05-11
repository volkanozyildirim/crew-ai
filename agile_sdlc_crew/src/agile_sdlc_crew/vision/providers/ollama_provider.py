"""Ollama vision provider — POST {base_url}/api/generate.

qwen2.5vl, llava, llama3.2-vision gibi modeller. Image base64 olarak `images`
listesinde gonderilir, response.response field'inda metin doner."""

import logging
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

log = logging.getLogger("pipeline")


def analyze(
    image_b64: str,
    mime: str,
    prompt: str,
    model: str,
    base_url: str = "",
    timeout: int = 120,
    max_tokens: int = 500,
    **kwargs,
) -> str:
    base_url = (
        base_url
        or credentials.get("vision", NAME, "base_url")
        or credentials.get("llm", NAME, "base_url")
        or os.environ.get("CREW_VISION_BASE_URL")
        or os.environ.get("OLLAMA_BASE_URL")
        or "http://localhost:11434"
    ).rstrip("/")
    resp = requests.post(
        f"{base_url}/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "images": [image_b64],
            "stream": False,
            "options": {"num_predict": max_tokens},
        },
        timeout=timeout,
        verify=False,
    )
    resp.raise_for_status()
    text = resp.json().get("response", "").strip()
    return text or "(Local vision bos yanit dondu)"
