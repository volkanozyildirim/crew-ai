"""Azure DevOps kod arama araci - local repo oncelikli."""

from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient
from agile_sdlc_crew.tools.tool_cache import CachedToolMixin


class AzureDevOpsSearchCodeInput(BaseModel):
    search_text: str = Field(
        ...,
        description="Aranacak metin (fonksiyon adi, class adi, endpoint, degisken adi vb.)",
    )
    repo_name: str = Field(
        default="",
        description="Aramayı daraltmak icin repo adi. Bos birakilirsa tum repolarda arar.",
    )


class AzureDevOpsSearchCodeTool(CachedToolMixin, BaseTool):
    name: str = "search_code"
    description: str = (
        "Azure DevOps repolarinda kod icinde metin arar. "
        "Fonksiyon, class, endpoint, degisken adlarini bulur."
    )
    args_schema: type[BaseModel] = AzureDevOpsSearchCodeInput
    local_repo_mgr: Any = None

    def _run(self, search_text: str, repo_name: str = "") -> str:
        return self._cached_wrap(self._run_inner, search_text, repo_name)

    def _run_inner(self, search_text: str, repo_name: str = "") -> str:
        try:
            # Local repo varsa grep ile ara
            if self.local_repo_mgr and repo_name:
                return self._run_local(search_text, repo_name)

            # Fallback: Azure Search API
            return self._run_api(search_text, repo_name)

        except Exception as e:
            return f"Kod aranirken hata: {e}"

    def _run_local(self, search_text: str, repo_name: str) -> str:
        """Local repo'da grep ile arama."""
        results = self.local_repo_mgr.search_code(repo_name, search_text)

        if not results:
            return f"'{search_text}' icin '{repo_name}' reposunda sonuc bulunamadi."

        lines = [f"'{search_text}' icin {len(results)} sonuc (local):\n"]
        for r in results[:10]:
            repo = r.get("repository", {}).get("name", "?")
            fpath = r.get("path", "?")
            lines.append(f"  {repo}:{fpath}")
        return "\n".join(lines)

    def _run_api(self, search_text: str, repo_name: str) -> str:
        """Azure Search API ile arama (fallback)."""
        client = AzureDevOpsClient()
        results = client.search_code(search_text, repo_name=repo_name or None)

        if not results:
            scope = f"'{repo_name}' reposunda" if repo_name else "tum repolarda"
            return f"'{search_text}' icin {scope} sonuc bulunamadi."

        lines = [f"'{search_text}' icin {len(results)} sonuc:\n"]
        for r in results[:10]:
            repo = r.get("repository", {}).get("name", "?")
            fpath = r.get("path", "?")
            lines.append(f"  {repo}:{fpath}")
        return "\n".join(lines)
