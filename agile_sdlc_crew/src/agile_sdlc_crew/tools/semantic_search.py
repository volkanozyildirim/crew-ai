"""Semantic kod arama araci — LanceDB vector search ile."""

from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from agile_sdlc_crew.tools.tool_cache import CachedToolMixin


class SemanticCodeSearchInput(BaseModel):
    query: str = Field(
        ...,
        description="Aranacak icerik (fonksiyon aciklamasi, is mantigi, endpoint amaci vb.)",
    )
    repo_name: str = Field(
        ...,
        description="Arama yapilacak repo adi",
    )


class SemanticCodeSearchTool(CachedToolMixin, BaseTool):
    name: str = "semantic_search"
    description: str = (
        "Repoda anlamsal kod arama yapar. Fonksiyon adi bilmeden bile "
        "'siparis iptali yapan fonksiyon' gibi dogal dilde arama yapabilir. "
        "Literal string aramasi icin 'Azure DevOps Kod Arama' aracini kullanin."
    )
    args_schema: type[BaseModel] = SemanticCodeSearchInput
    vector_store: Any = None

    def _run(self, query: str, repo_name: str) -> str:
        return self._cached_wrap(self._run_inner, query, repo_name)

    def _run_inner(self, query: str, repo_name: str) -> str:
        if not self.vector_store:
            return "Semantic arama yapilandirmasi eksik."

        results = self.vector_store.search_code(repo_name, query, limit=10)
        if not results:
            return f"'{query}' icin '{repo_name}' reposunda sonuc bulunamadi."

        lines = [f"'{query}' icin {len(results)} sonuc ({repo_name}):\n"]
        for r in results:
            lines.append(
                f"  {r['file_path']}:{r['lines']} (score:{r['score']})\n"
                f"    {r['content'][:200]}\n"
            )
        return "\n".join(lines)
