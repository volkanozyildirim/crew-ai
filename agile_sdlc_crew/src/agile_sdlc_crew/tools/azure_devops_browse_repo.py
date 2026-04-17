"""Azure DevOps repo icerigi gozden gecirme araci - local repo oncelikli."""

from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient
from agile_sdlc_crew.tools.tool_cache import CachedToolMixin


class AzureDevOpsBrowseRepoInput(BaseModel):
    repo_name: str = Field(..., description="Repo adi")
    path: str = Field(default="/", description="Dizin yolu (orn: /, /src, /app/Controller)")
    branch: str = Field(default="", description="Branch adi (bos = varsayilan dal)")
    include_file_content: bool = Field(default=False, description="True = dosya iceriklerini de getir")


class AzureDevOpsBrowseRepoTool(CachedToolMixin, BaseTool):
    name: str = "browse_repo"
    description: str = (
        "Bir repodaki dizin yapisini ve dosyalari listeler. "
        "include_file_content=true ile dosya icerigini okur."
    )
    args_schema: type[BaseModel] = AzureDevOpsBrowseRepoInput
    local_repo_mgr: Any = None

    def _run(self, repo_name: str, path: str = "/", branch: str = "", include_file_content: bool = False) -> str:
        return self._cached_wrap(self._run_inner, repo_name, path, branch, include_file_content)

    def _run_inner(self, repo_name: str, path: str = "/", branch: str = "", include_file_content: bool = False) -> str:
        try:
            branch_arg = branch if branch else None

            # Local repo varsa filesystem'den oku
            if self.local_repo_mgr:
                return self._run_local(repo_name, path, branch_arg, include_file_content)

            # Fallback: Azure API
            return self._run_api(repo_name, path, branch_arg, include_file_content)

        except Exception as e:
            return f"Hata: {e}"

    def _run_local(self, repo_name: str, path: str, branch: str | None, include_file_content: bool) -> str:
        """Local filesystem ile dizin listeleme."""
        mgr = self.local_repo_mgr
        items = mgr.get_items_in_path(repo_name, path=path, branch=branch)

        if not items:
            return f"'{repo_name}:{path}' bulunamadi."

        folders = []
        files = []
        for item in items:
            item_path = item.get("path", "")
            if item_path == path or item_path == f"/{path.lstrip('/')}":
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

        if include_file_content:
            for fpath in files[:5]:
                try:
                    content = mgr.get_file_content(repo_name, fpath, branch=branch)
                    if len(content) > 5000:
                        content = content[:5000] + "\n... (truncated)"
                    lines.append(f"\n--- {fpath} ---")
                    lines.append(content)
                except Exception:
                    pass

        return "\n".join(lines)

    def _run_api(self, repo_name: str, path: str, branch: str | None, include_file_content: bool) -> str:
        """Azure REST API ile dizin listeleme (fallback)."""
        client = AzureDevOpsClient()
        items = client.get_items_in_path(repo_name, path=path, branch=branch, recursion_level="oneLevel")

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

        if include_file_content:
            for fpath in files[:5]:
                try:
                    content = client.get_file_content(repo_name, fpath, branch=branch)
                    if len(content) > 5000:
                        content = content[:5000] + "\n... (truncated)"
                    lines.append(f"\n--- {fpath} ---")
                    lines.append(content)
                except Exception:
                    pass

        return "\n".join(lines)
