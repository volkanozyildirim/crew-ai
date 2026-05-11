"""OpenAI-compatible vision provider — chat/completions endpoint with image content.

Multimodal mesaj formati: messages[0].content = [
    {type: "text", text: prompt},
    {type: "image_url", image_url: {url: "data:image/png;base64,..."}}
]

Bu format LiteLLM proxy + Vertex Claude, OpenAI GPT-4o, Azure OpenAI'da calisir.
"""

import logging
import os

import requests

from agile_sdlc_crew import credentials

NAME = "openai"

CREDS_SCHEMA = [
    {
        "name": "base_url",
        "label": "Base URL",
        "secret": False,
        "env_fallback": "LITELLM_BASE_URL",
        "placeholder": "https://api.openai.com/v1",
    },
    {
        "name": "api_key",
        "label": "API Key",
        "secret": True,
        "env_fallback": "LITELLM_API_KEY",
    },
]

log = logging.getLogger("pipeline")


def analyze(
    image_b64: str,
    mime: str,
    prompt: str,
    model: str,
    base_url: str = "",
    api_key: str = "",
    timeout: int = 60,
    max_tokens: int = 500,
    **kwargs,
) -> str:
    # 1. arg > 2. vision creds > 3. llm/litellm creds > 4. env > 5. default
    base_url = (
        base_url
        or credentials.get("vision", NAME, "base_url")
        or credentials.get("llm", "litellm", "base_url")
        or os.environ.get("CREW_VISION_BASE_URL")
        or os.environ.get("LITELLM_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or "https://api.openai.com/v1"
    ).rstrip("/")
    if not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"

    if not api_key:
        api_key = (
            credentials.get("vision", NAME, "api_key")
            or credentials.get("llm", "litellm", "api_key")
            or os.environ.get("LITELLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        )

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{image_b64}"},
                },
            ],
        }],
        "max_tokens": max_tokens,
    }
    resp = requests.post(
        f"{base_url}/chat/completions",
        json=body,
        headers=headers,
        timeout=timeout,
        verify=False,
    )
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenAI vision bos yanit: {data}")
    msg = choices[0].get("message") or {}
    content = msg.get("content") or ""
    return content.strip() or "(Vision bos yanit dondu)"
