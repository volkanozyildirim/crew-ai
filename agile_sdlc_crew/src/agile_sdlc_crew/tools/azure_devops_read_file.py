"""Tek bir dosyanin TAM icerigini doner — reviewer/architect ihtiyacina yonelik.

browse_repo include_file_content=true ile dosya listesi+icerik karisik geliyor
ve her dosya 5000 char ile truncate ediliyor. Bu tool dosya bazinda calisir,
buyuk truncate limitiyle (default 200K char) tam icerige erisim saglar.
"""

from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient
from agile_sdlc_crew.tools.tool_cache import CachedToolMixin

# 200K char ~= 4000 satir kod. Bir kontroller/view dosyasi icin fazlasiyla
# yeterli; ozellikle buyuk dosyalar icin agent uyarilir.
_MAX_FILE_CHARS = 200_000


class AzureDevOpsReadFileInput(BaseModel):
    repo_name: str = Field(..., description="Repo adi (orn: orkestra)")
    file_path: str = Field(..., description="Dosya yolu (orn: /app/Controller/Cms/Stock.php)")
    branch: str = Field(default="", description="Branch adi (bos = varsayilan dal/main)")


class AzureDevOpsReadFileTool(CachedToolMixin, BaseTool):
    name: str = "read_file"
    description: str = (
        "Bir repodaki TEK dosyanin TAM icerigini doner. "
        "Reviewer'in/architect'in icerik denetimi yapabilmesi icin browse_repo "
        "yerine bu tool kullanilmalidir (truncate sinirli degil). "
        "branch bos birakilirsa varsayilan branch'ten okur."
    )
    args_schema: type[BaseModel] = AzureDevOpsReadFileInput
    local_repo_mgr: Any = None

    def _run(self, repo_name: str, file_path: str, branch: str = "") -> str:
        return self._cached_wrap(self._run_inner, repo_name, file_path, branch)

    def _run_inner(self, repo_name: str, file_path: str, branch: str = "") -> str:
        branch_arg = branch if branch else None
        try:
            # Local oncelikli (hizli), API fallback
            if self.local_repo_mgr:
                try:
                    content = self.local_repo_mgr.get_file_content(
                        repo_name, file_path, branch=branch_arg,
                    )
                except Exception:
                    content = AzureDevOpsClient().get_file_content(
                        repo_name, file_path, branch=branch_arg,
                    )
            else:
                content = AzureDevOpsClient().get_file_content(
                    repo_name, file_path, branch=branch_arg,
                )
        except Exception as e:
            return f"HATA: {repo_name}:{file_path} okunamadi. Detay: {e}"

        if not content:
            return f"BOS: {repo_name}:{file_path} icerigi bos veya bulunamadi."

        total = len(content)
        if total > _MAX_FILE_CHARS:
            head = content[:_MAX_FILE_CHARS]
            return (
                f"--- {repo_name}:{file_path} (TRUNCATED: {total} char, ilk {_MAX_FILE_CHARS}) ---\n"
                f"{head}\n... (dosya {total - _MAX_FILE_CHARS} char devam ediyor)"
            )
        return f"--- {repo_name}:{file_path} ({total} char) ---\n{content}"
