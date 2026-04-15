"""Azure DevOps repo listeleme araci - optimize edilmis cikti."""

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient


class AzureDevOpsListReposInput(BaseModel):
    dummy: str = Field(
        default="list",
        description="Repolarini listelemek icin 'list' yazin.",
    )


class AzureDevOpsListReposTool(BaseTool):
    name: str = "Azure DevOps Repo Listeleme"
    description: str = (
        "Azure DevOps projelerindeki Git repolarini listeler. "
        "Kompakt format: ad | proje | dal | boyut"
    )
    args_schema: type[BaseModel] = AzureDevOpsListReposInput

    def _run(self, dummy: str = "list") -> str:
        try:
            client = AzureDevOpsClient()
            repos = client.list_repositories()

            if not repos:
                return "Hic repo bulunamadi."

            # Devre disi repolari filtrele
            active_repos = [r for r in repos if not r.get("isDisabled", False)]

            lines = [f"Toplam {len(active_repos)} aktif repo:\n"]
            lines.append("Ad | Proje | Dal | Boyut(MB)")
            lines.append("---|-------|-----|----------")
            for repo in active_repos:
                name = repo.get("name", "?")
                project = repo.get("_project", "?")
                branch = repo.get("defaultBranch", "?")
                if branch.startswith("refs/heads/"):
                    branch = branch[len("refs/heads/"):]
                size = round(repo.get("size", 0) / (1024 * 1024), 1)
                lines.append(f"{name} | {project} | {branch} | {size}")

            return "\n".join(lines)

        except Exception as e:
            return f"Hata: {e}"
