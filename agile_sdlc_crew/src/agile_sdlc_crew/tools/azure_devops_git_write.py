"""Azure DevOps Git yazma araclari - branch, commit, PR islemleri."""

from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient


# ── Branch Olusturma ──

class CreateBranchInput(BaseModel):
    repo_name: str = Field(
        ...,
        description="Repo adi - repo listeleme aracindaki Ad kolonundaki AYNI ismi kullan",
    )
    branch_name: str = Field(..., description="Olusturulacak branch adi (orn: feature/61655)")
    source_branch: str = Field(default="main", description="Kaynak branch")


class AzureDevOpsCreateBranchTool(BaseTool):
    name: str = "create_branch"
    description: str = (
        "Bir repoda yeni branch olusturur. "
        "ONEMLI: repo_name repo listeleme aracindaki Ad kolonuyla BIREBIR ayni olmali."
    )
    args_schema: type[BaseModel] = CreateBranchInput

    def _run(self, repo_name: str, branch_name: str, source_branch: str = "main", **kw: Any) -> str:
        try:
            client = AzureDevOpsClient()
            result = client.create_branch(repo_name, branch_name, source_branch)
            return f"BASARILI: Branch '{branch_name}' olusturuldu (repo: {repo_name}, kaynak: {source_branch})."
        except Exception as e:
            return f"HATA: Branch olusturulamadi. repo_name dogru mu? Repo listesindeki Ad kolonunu kontrol et. Detay: {e}"


# ── Dosya Commit Etme ──

class PushChangesInput(BaseModel):
    repo_name: str = Field(
        ...,
        description="Repo adi - repo listeleme aracindaki Ad kolonundaki AYNI ismi kullan",
    )
    branch: str = Field(..., description="Hedef branch (orn: feature/61655)")
    file_path: str = Field(..., description="Dosya yolu (orn: /src/services/translate.js)")
    content: str = Field(..., description="Dosyanin TAM icerigi (sadece degisen kisim degil, tum dosya)")
    commit_message: str = Field(..., description="Commit mesaji")
    change_type: str = Field(default="edit", description="'add' (yeni dosya) veya 'edit' (mevcut dosya)")


class AzureDevOpsPushChangesTool(BaseTool):
    name: str = "push_code"
    description: str = (
        "Bir branch'e dosya push eder. content dosyanin TAM icerigi olmali. "
        "Yeni dosya: change_type='add', mevcut dosya: change_type='edit'."
    )
    args_schema: type[BaseModel] = PushChangesInput

    def _run(
        self,
        repo_name: str,
        branch: str,
        file_path: str,
        content: str,
        commit_message: str,
        change_type: str = "edit",
        **kw: Any,
    ) -> str:
        try:
            client = AzureDevOpsClient()
            changes = [{"changeType": change_type, "path": file_path, "content": content}]
            result = client.push_changes(repo_name, branch, changes, commit_message)
            push_id = result.get("pushId", "?")
            return f"BASARILI: push #{push_id} - {file_path} -> {branch} ({repo_name})"
        except Exception as e:
            return f"HATA: Push basarisiz. Detay: {e}"


# ── PR Olusturma ──

class CreatePRInput(BaseModel):
    repo_name: str = Field(
        ...,
        description="Repo adi - repo listeleme aracindaki Ad kolonundaki AYNI ismi kullan",
    )
    source_branch: str = Field(..., description="Kaynak branch (orn: feature/61655)")
    target_branch: str = Field(default="main", description="Hedef branch")
    title: str = Field(..., description="PR basligi")
    description: str = Field(default="", description="PR aciklamasi (markdown)")
    work_item_id: int = Field(default=0, description="Iliskili work item ID (0=yok)")


class AzureDevOpsCreatePRTool(BaseTool):
    name: str = "create_pr"
    description: str = (
        "Pull request olusturur. Work item ID verilirse PR'a baglar. "
        "Oncesinde branch olusturulmus ve kod push edilmis olmali."
    )
    args_schema: type[BaseModel] = CreatePRInput

    def _run(
        self,
        repo_name: str,
        source_branch: str,
        title: str,
        target_branch: str = "main",
        description: str = "",
        work_item_id: int = 0,
        **kw: Any,
    ) -> str:
        try:
            client = AzureDevOpsClient()
            wids = [work_item_id] if work_item_id else None
            result = client.create_pull_request(
                repo_name, source_branch, target_branch, title, description, wids
            )
            pr_id = result.get("pullRequestId", "?")
            repo_data = result.get("repository", {})
            project = repo_data.get("project", {}).get("name", "")
            repo = repo_data.get("name", repo_name)
            web_url = f"{client.org_url}/{project}/_git/{repo}/pullrequest/{pr_id}"
            return f"BASARILI: PR #{pr_id} olusturuldu\nURL: {web_url}"
        except Exception as e:
            return f"HATA: PR olusturulamadi. Branch ve push basarili mi? Detay: {e}"


# ── PR Review Comment ──

class PRCommentInput(BaseModel):
    repo_name: str = Field(..., description="Repo adi")
    pull_request_id: int = Field(..., description="GERCEK PR numarasi (onceki adimdan alin)")
    content: str = Field(..., description="Review yorumu (markdown)")
    file_path: str = Field(default="", description="Dosya yolu (inline yorum icin)")


class AzureDevOpsPRReviewTool(BaseTool):
    name: str = "add_pr_comment"
    description: str = (
        "PR'a review yorumu ekler. pull_request_id onceki adimda olusturulan GERCEK PR numarasi olmali."
    )
    args_schema: type[BaseModel] = PRCommentInput

    def _run(
        self,
        repo_name: str,
        pull_request_id: int,
        content: str,
        file_path: str = "",
        **kw: Any,
    ) -> str:
        try:
            client = AzureDevOpsClient()
            fp = file_path if file_path else None
            client.add_pr_comment(repo_name, pull_request_id, content, fp)
            target = f" ({file_path})" if file_path else ""
            return f"BASARILI: PR #{pull_request_id} yorumu eklendi{target}."
        except Exception as e:
            return f"HATA: PR yorum eklenemedi. PR numarasi dogru mu? Detay: {e}"


# ── PR Degisikliklerini Goruntuleme ──

class PRChangesInput(BaseModel):
    repo_name: str = Field(..., description="Repo adi")
    pull_request_id: int = Field(..., description="GERCEK PR numarasi (onceki adimdan alin)")


class AzureDevOpsPRChangesTool(BaseTool):
    name: str = "get_pr_changes"
    description: str = (
        "PR'daki dosya degisikliklerini listeler. pull_request_id GERCEK olmali."
    )
    args_schema: type[BaseModel] = PRChangesInput

    def _run(self, repo_name: str, pull_request_id: int, **kw: Any) -> str:
        try:
            client = AzureDevOpsClient()
            changes = client.get_pull_request_changes(repo_name, pull_request_id)
            if not changes:
                return "PR'da degisiklik bulunamadi."
            lines = [f"PR #{pull_request_id} degisiklikleri ({len(changes)} dosya):"]
            for ch in changes:
                change_type = ch.get("changeType", "?")
                item = ch.get("item", {})
                path = item.get("path", "?")
                lines.append(f"  {change_type}: {path}")
            return "\n".join(lines)
        except Exception as e:
            return f"HATA: PR degisiklikleri alinamadi. PR numarasi dogru mu? Detay: {e}"
