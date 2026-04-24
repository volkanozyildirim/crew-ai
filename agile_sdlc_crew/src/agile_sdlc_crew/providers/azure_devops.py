"""Azure DevOps provider — mevcut AzureDevOpsClient'i provider interface'ine sarar.

Hem WorkItemProvider hem SCMProvider'i implement eder (Azure DevOps ikisini de saglar).
"""

import re
from agile_sdlc_crew.providers.base import (
    WorkItemProvider, SCMProvider,
    WorkItem, Repository, PullRequest, PRComment,
)
from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient


class AzureDevOpsWorkItemProvider(WorkItemProvider):
    """Azure Boards uzerinden is kalemi yonetimi."""

    def __init__(self, client: AzureDevOpsClient | None = None):
        self._client = client or AzureDevOpsClient()

    def get_work_item(self, work_item_id: str) -> WorkItem:
        raw = self._client.get_work_item(int(work_item_id))
        fields = raw.get("fields", {})
        desc_raw = fields.get("System.Description", "") or ""
        ac_raw = fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", "") or ""
        return WorkItem(
            id=str(work_item_id),
            title=fields.get("System.Title", ""),
            description=re.sub(r'<[^>]+>', ' ', desc_raw).strip(),
            acceptance_criteria=re.sub(r'<[^>]+>', ' ', ac_raw).strip(),
            status=fields.get("System.State", ""),
            assigned_to=fields.get("System.AssignedTo", {}).get("displayName", "") if isinstance(fields.get("System.AssignedTo"), dict) else "",
            raw=raw,
        )

    def add_comment(self, work_item_id: str, text: str) -> None:
        self._client.add_comment(int(work_item_id), text)

    def get_comments(self, work_item_id: str) -> list[dict]:
        return self._client.get_work_item_comments(int(work_item_id))

    def list_work_items(self, query: str = "") -> list[WorkItem]:
        raw_items = self._client.list_work_items(query)
        items = []
        for raw in raw_items:
            fields = raw.get("fields", {})
            items.append(WorkItem(
                id=str(raw.get("id", "")),
                title=fields.get("System.Title", ""),
                status=fields.get("System.State", ""),
                raw=raw,
            ))
        return items

    def update_status(self, work_item_id: str, status: str) -> None:
        self._client.update_work_item(int(work_item_id), [
            {"op": "replace", "path": "/fields/System.State", "value": status}
        ])

    def download_attachment(self, url: str) -> bytes:
        return self._client.download_attachment(url)


class AzureDevOpsSCMProvider(SCMProvider):
    """Azure Repos uzerinden kaynak kod yonetimi."""

    def __init__(self, client: AzureDevOpsClient | None = None):
        self._client = client or AzureDevOpsClient()

    def list_repositories(self) -> list[Repository]:
        raw_repos = self._client.list_repositories()
        return [
            Repository(
                name=r.get("name", ""),
                clone_url=r.get("remoteUrl", ""),
                default_branch=(r.get("defaultBranch", "") or "refs/heads/main").replace("refs/heads/", ""),
                raw=r,
            )
            for r in raw_repos
        ]

    def get_file_content(self, repo: str, file_path: str, branch: str = "") -> str:
        return self._client.get_file_content(repo, file_path, branch or "")

    def create_branch(self, repo: str, branch_name: str, source: str = "main") -> dict:
        self._client.create_branch(repo, branch_name)
        return {"success": True, "branch": branch_name}

    def push_changes(
        self, repo: str, branch: str, changes: list[dict], commit_message: str,
    ) -> dict:
        result = self._client.push_changes(repo, branch, changes, commit_message)
        return result

    def create_pull_request(
        self, repo: str, source_branch: str, target_branch: str,
        title: str, description: str, work_item_ids: list[str] | None = None,
    ) -> PullRequest:
        wi_ids = [int(w) for w in work_item_ids] if work_item_ids else None
        result = self._client.create_pull_request(
            repo, source_branch, target_branch, title, description, wi_ids,
        )
        pr_id = result.get("pullRequestId", "")
        repo_data = result.get("repository", {})
        project = repo_data.get("project", {}).get("name", "")
        repo_name = repo_data.get("name", repo)
        web_url = f"{self._client.org_url}/{project}/_git/{repo_name}/pullrequest/{pr_id}"
        return PullRequest(
            id=str(pr_id),
            title=title,
            source_branch=source_branch,
            target_branch=target_branch,
            url=web_url,
            work_item_ids=work_item_ids or [],
            raw=result,
        )

    def get_pull_request(self, repo: str, pr_id: int) -> PullRequest:
        raw = self._client.get_pull_request(repo, pr_id)
        return PullRequest(
            id=str(raw.get("pullRequestId", "")),
            title=raw.get("title", ""),
            source_branch=raw.get("sourceRefName", "").replace("refs/heads/", ""),
            target_branch=raw.get("targetRefName", "").replace("refs/heads/", ""),
            status=raw.get("status", ""),
            raw=raw,
        )

    def get_pr_comments(self, repo: str, pr_id: int) -> list[PRComment]:
        raw_comments = self._client.get_pr_comments_text(repo, pr_id)
        return [
            PRComment(
                author=c["author"],
                content=c["content"],
                file_path=c.get("file_path"),
                date=c.get("date", ""),
            )
            for c in raw_comments
        ]

    def add_pr_comment(self, repo: str, pr_id: int, content: str, file_path: str | None = None) -> None:
        self._client.add_pr_comment(repo, pr_id, content, file_path)

    def get_pr_changes(self, repo: str, pr_id: int) -> list[dict]:
        return self._client.get_pull_request_changes(repo, pr_id)

    def browse_directory(self, repo: str, path: str = "/", branch: str = "") -> list[dict]:
        return self._client.get_items_in_path(repo, path, branch)

    def search_code(self, repo: str, query: str) -> list[dict]:
        return self._client.search_code(repo, query)
