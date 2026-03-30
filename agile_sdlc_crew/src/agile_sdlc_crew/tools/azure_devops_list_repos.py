"""Azure DevOps repo listeleme ve analiz araci."""

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient


class AzureDevOpsListReposInput(BaseModel):
    """Repo listeleme araci icin giris schemasi (parametre gerektirmez)."""

    dummy: str = Field(
        default="list",
        description="Sadece repolarini listelemek icin 'list' yazin.",
    )


class AzureDevOpsListReposTool(BaseTool):
    name: str = "Azure DevOps Repo Listeleme"
    description: str = (
        "Azure DevOps projesindeki tum Git repolarini listeler. "
        "Her repo icin ad, varsayilan dal (default branch), boyut ve "
        "URL bilgilerini dondurur. Projedeki repo haritasini cikarmak icin kullanin."
    )
    args_schema: type[BaseModel] = AzureDevOpsListReposInput

    def _run(self, dummy: str = "list") -> str:
        try:
            client = AzureDevOpsClient()
            repos = client.list_repositories()

            if not repos:
                return "Projede hic Git reposu bulunamadi."

            lines = [f"## Azure DevOps Repo Haritasi ({len(repos)} repo)\n"]
            for repo in repos:
                name = repo.get("name", "?")
                default_branch = repo.get("defaultBranch", "belirtilmemis")
                if default_branch.startswith("refs/heads/"):
                    default_branch = default_branch[len("refs/heads/"):]
                size_mb = round(repo.get("size", 0) / (1024 * 1024), 2)
                web_url = repo.get("webUrl", "")
                is_disabled = repo.get("isDisabled", False)
                status = " [DEVRE DISI]" if is_disabled else ""

                lines.append(f"### {name}{status}")
                lines.append(f"- **Varsayilan dal:** {default_branch}")
                lines.append(f"- **Boyut:** {size_mb} MB")
                lines.append(f"- **URL:** {web_url}")
                lines.append(f"- **ID:** {repo.get('id', '?')}")
                lines.append("")

            return "\n".join(lines)

        except Exception as e:
            return f"Repolar listelenirken hata: {e}"
