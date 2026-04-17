"""Hangi repoda arayacagini bul — semantic search over REPO_SUMMARY.md'ler."""

from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from agile_sdlc_crew.tools.tool_cache import CachedToolMixin


class FindRelevantReposInput(BaseModel):
    query: str = Field(
        ...,
        description=(
            "Is kalemi aciklamasi, aranacak ozellik veya fonksiyonelite. "
            "Orn: 'siparis iptali', 'kargo firmasi entegrasyonu', 'odeme'"
        ),
    )


class FindRelevantReposTool(CachedToolMixin, BaseTool):
    name: str = "find_relevant_repos"
    description: str = (
        "Bir is kalemi veya fonksiyonelite icin hangi repolarda arama yapilmasi "
        "gerektigini onerir. Her repo'nun REPO_SUMMARY.md'si uzerinde semantic "
        "arama yapar. Architect/Developer kod aramadan ONCE bu tool'u kullanarak "
        "hangi repoyu hedefleyecegini belirler."
    )
    args_schema: type[BaseModel] = FindRelevantReposInput
    vector_store: Any = None

    def _run(self, query: str) -> str:
        return self._cached_wrap(self._run_inner, query)

    def _run_inner(self, query: str) -> str:
        if not self.vector_store:
            return "Vector store mevcut degil — manuel olarak repo seciniz."

        results = self.vector_store.find_relevant_repos(query, limit=5)
        if not results:
            return (
                f"'{query}' icin repo bulunamadi. "
                f"Manuel olarak 'Azure DevOps Repo Listeleme' kullanin."
            )

        lines = [f"'{query}' icin en uygun {len(results)} repo:\n"]
        for i, r in enumerate(results, 1):
            lines.append(
                f"{i}. {r['repo']} (score: {r['score']})\n"
                f"   {r['summary_excerpt'][:200].replace(chr(10), ' ')}\n"
            )
        return "\n".join(lines)
