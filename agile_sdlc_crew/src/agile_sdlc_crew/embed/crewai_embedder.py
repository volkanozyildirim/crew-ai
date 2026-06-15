"""CrewAI Knowledge icin custom embedder — projenin embed_text() registry'sini
CrewAI'a kopruler. Boylece Knowledge, vector store ile AYNI embedder'i kullanir
ve CrewAI'nin OpenAI embedder default'una (API key gerektirir) DUSMEZ.

CrewAI custom embedder formati:
    {"provider": "custom", "config": {"embedding_callable": <CustomEmbeddingFunction altsinifi>}}

Sinif iki base'den miras alir:
  1. chromadb.api.types.EmbeddingFunction — CustomProviderSpec pydantic validasyonu
     icin issubclass() kontrolunu gecmek gerekiyor (chromadb Protocol, runtime_checkable).
  2. crewai.rag.embeddings.providers.custom.embedding_callable.CustomEmbeddingFunction —
     CustomProvider.embedding_callable field tipi icin gerekli.
"""

import logging

from chromadb.api.types import Documents
from chromadb.api.types import EmbeddingFunction as ChromaEmbeddingFunction
from crewai.rag.embeddings.providers.custom.embedding_callable import (
    CustomEmbeddingFunction,
)

log = logging.getLogger("pipeline")


class ProjectEmbeddingFunction(ChromaEmbeddingFunction[Documents], CustomEmbeddingFunction):
    """chromadb + CrewAI EmbeddingFunction arayuzu: __call__(input: list[str]) -> list[vector].
    Her cagrida embed resolver'i okur (provider/model dashboard'dan degisebilir).

    chromadb base'i __call__ donusunu normalize_embeddings() ile sarar:
    list[list[float]] -> list[np.ndarray[float32]] — bu chromadb'nin beklentisiyle uyumlu.
    """

    def __init__(self) -> None:
        pass

    def __call__(self, input):  # type: ignore[override]
        from agile_sdlc_crew.embed import embed_text, get_model, get_provider

        if isinstance(input, str):
            input = [input]
        provider = get_provider()
        model = get_model()
        return [embed_text(provider, text, model) for text in input]


def crewai_embedder_config() -> dict:
    """CrewAI Agent/Crew/Knowledge'a verilecek embedder config dict'i."""
    return {
        "provider": "custom",
        "config": {"embedding_callable": ProjectEmbeddingFunction},
    }
