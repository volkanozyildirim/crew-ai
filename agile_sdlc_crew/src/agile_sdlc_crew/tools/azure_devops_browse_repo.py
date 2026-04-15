"""Azure DevOps repo icerigi gozden gecirme araci - kompakt cikti."""

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient


class AzureDevOpsBrowseRepoInput(BaseModel):
    repo_name: str = Field(..., description="Repo adi")
    path: str = Field(default="/", description="Dizin yolu (orn: /, /src, /app/Controller)")
    branch: str = Field(default="", description="Branch adi (bos = varsayilan dal)")
    include_file_content: bool = Field(default=False, description="True = dosya iceriklerini de getir")


class AzureDevOpsBrowseRepoTool(BaseTool):
    name: str = "Azure DevOps Repo Icerik Gozden Gecirme"
    description: str = (
        "Bir repodaki dizin yapisini ve dosyalari listeler. "
        "include_file_content=true ile dosya icerigini okur."
    )
    args_schema: type[BaseModel] = AzureDevOpsBrowseRepoInput

    def _run(self, repo_name: str, path: str = "/", branch: str = "", include_file_content: bool = False) -> str:
        try:
            client = AzureDevOpsClient()
            branch_arg = branch if branch else None
            items = client.get_items_in_path(repo_name, path=path, branch=branch_arg, recursion_level="oneLevel")

            if not items:
                return f"'{repo_name}:{path}' bulunamadi."

            folders = []
            files = []
            for item in items:
                item_path = item.get("path", "")
                if item_path == path:
                    continue
                if item.get("isFolder", False):
                    folders.append(item_path)
                else:
                    files.append(item_path)

            lines = [f"{repo_name}:{path}"]
            for f in sorted(folders):
                lines.append(f"  {f}/")
            for f in sorted(files):
                lines.append(f"  {f}")

            # Dosya icerigi istenirse oku
            if include_file_content:
                for fpath in files[:5]:
                    try:
                        content = client.get_file_content(repo_name, fpath, branch=branch_arg)
                        if len(content) > 5000:
                            content = content[:5000] + "\n... (truncated)"
                        lines.append(f"\n--- {fpath} ---")
                        lines.append(content)
                    except Exception:
                        pass

            return "\n".join(lines)

        except Exception as e:
            return f"Hata: {e}"
