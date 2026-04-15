"""Azure DevOps kod arama araci."""

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient


class AzureDevOpsSearchCodeInput(BaseModel):
    search_text: str = Field(
        ...,
        description="Aranacak metin (fonksiyon adi, class adi, endpoint, degisken adi vb.)",
    )
    repo_name: str = Field(
        default="",
        description="Aramayı daraltmak icin repo adi. Bos birakilirsa tum repolarda arar.",
    )


class AzureDevOpsSearchCodeTool(BaseTool):
    name: str = "Azure DevOps Kod Arama"
    description: str = (
        "Azure DevOps repolarinda kod icinde metin arar. "
        "Fonksiyon, class, endpoint, degisken adlarini bulur."
    )
    args_schema: type[BaseModel] = AzureDevOpsSearchCodeInput

    def _run(self, search_text: str, repo_name: str = "") -> str:
        try:
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

        except Exception as e:
            return f"Kod aranirken hata: {e}"
