"""FastEmbed embedding provider — Qdrant'in yerel ONNX kutuphanesi.

Dis servis gerektirmez (Ollama/OpenAI API yok). Model ilk kullanımda
~/.cache/fastembed altına indirilir.

Recommended modeller:
    BAAI/bge-small-en-v1.5   384 dim  ~130 MB  hizli, iyi kalite
    BAAI/bge-base-en-v1.5    768 dim  ~440 MB  daha iyi kalite
    nomic-ai/nomic-embed-text-v1  768 dim  ~270 MB
"""

from __future__ import annotations

from typing import Any

NAME = "fastembed"

CREDS_SCHEMA: list[dict] = []  # Kimlik bilgisi gerekmiyor

_model_cache: dict[str, Any] = {}


def embed(
    text: str,
    model: str,
    **kwargs,
) -> list[float]:
    try:
        from fastembed import TextEmbedding
    except ImportError as exc:
        raise ImportError(
            "fastembed kurulu degil: pip install fastembed"
        ) from exc

    if model not in _model_cache:
        _model_cache[model] = TextEmbedding(model_name=model)

    emb_model: Any = _model_cache[model]
    results = list(emb_model.embed([text]))
    if not results:
        raise RuntimeError("fastembed bos yanit dondu")
    vec = results[0]
    return vec.tolist() if hasattr(vec, "tolist") else list(vec)
