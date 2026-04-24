"""Abstract base interfaces for Work Item and SCM providers.

Her yeni provider (Jira, GitHub vb.) bu interface'leri implement eder.
Pipeline kodu sadece bu interface'e baglıdır — provider degistiginde
pipeline kodu degismez.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


# ── Data Models (provider-agnostic) ──────────────────────────


@dataclass
class WorkItem:
    """Platform-bagımsız is kalemi."""
    id: str
    title: str
    description: str = ""
    acceptance_criteria: str = ""
    status: str = ""
    assigned_to: str = ""
    comments: list[dict] = field(default_factory=list)
    # raw: orijinal platform verisini tasir (debug/ek bilgi icin)
    raw: dict = field(default_factory=dict)


@dataclass
class Repository:
    """Platform-bagımsız repository bilgisi."""
    name: str
    clone_url: str = ""
    default_branch: str = "main"
    raw: dict = field(default_factory=dict)


@dataclass
class PullRequest:
    """Platform-bagımsız PR bilgisi."""
    id: str
    title: str
    source_branch: str
    target_branch: str = "main"
    status: str = ""
    url: str = ""
    work_item_ids: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class PRComment:
    """Platform-bagımsız PR yorumu."""
    author: str
    content: str
    file_path: str | None = None
    date: str = ""


# ── Work Item Provider Interface ─────────────────────────────


class WorkItemProvider(ABC):
    """Is yonetimi platformu interface'i.

    Implementasyonlar:
    - AzureDevOpsWorkItemProvider (Azure Boards)
    - JiraWorkItemProvider (Jira)
    - TrelloWorkItemProvider (Trello)
    - LinearWorkItemProvider (Linear)
    """

    @abstractmethod
    def get_work_item(self, work_item_id: str) -> WorkItem:
        """Is kalemini getirir."""
        ...

    @abstractmethod
    def add_comment(self, work_item_id: str, text: str) -> None:
        """Is kalemine yorum ekler."""
        ...

    @abstractmethod
    def get_comments(self, work_item_id: str) -> list[dict]:
        """Is kaleminin yorumlarini getirir."""
        ...

    @abstractmethod
    def list_work_items(self, query: str = "") -> list[WorkItem]:
        """Is kalemlerini listeler. query: platform-spesifik filtre."""
        ...

    @abstractmethod
    def update_status(self, work_item_id: str, status: str) -> None:
        """Is kaleminin statusunu gunceller."""
        ...

    def download_attachment(self, url: str) -> bytes:
        """Ek dosya indirir (opsiyonel — tum platformlarda olmayabilir)."""
        raise NotImplementedError


# ── SCM Provider Interface ───────────────────────────────────


class SCMProvider(ABC):
    """Kaynak kod yonetimi platformu interface'i.

    Implementasyonlar:
    - AzureDevOpsSCMProvider (Azure Repos)
    - GitHubSCMProvider (GitHub)
    - GitLabSCMProvider (GitLab)
    - BitbucketSCMProvider (Bitbucket)
    """

    @abstractmethod
    def list_repositories(self) -> list[Repository]:
        """Tum repository'leri listeler."""
        ...

    @abstractmethod
    def get_file_content(self, repo: str, file_path: str, branch: str = "") -> str:
        """Dosya icerigini getirir."""
        ...

    @abstractmethod
    def create_branch(self, repo: str, branch_name: str, source: str = "main") -> dict:
        """Branch olusturur."""
        ...

    @abstractmethod
    def push_changes(
        self,
        repo: str,
        branch: str,
        changes: list[dict],
        commit_message: str,
    ) -> dict:
        """Degisiklikleri push eder.
        changes: [{"changeType": "edit"|"add", "path": str, "content": str}]
        """
        ...

    @abstractmethod
    def create_pull_request(
        self,
        repo: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        work_item_ids: list[str] | None = None,
    ) -> PullRequest:
        """PR olusturur."""
        ...

    @abstractmethod
    def get_pull_request(self, repo: str, pr_id: int) -> PullRequest:
        """PR detaylarini getirir."""
        ...

    @abstractmethod
    def get_pr_comments(self, repo: str, pr_id: int) -> list[PRComment]:
        """PR yorumlarini getirir."""
        ...

    @abstractmethod
    def add_pr_comment(
        self,
        repo: str,
        pr_id: int,
        content: str,
        file_path: str | None = None,
    ) -> None:
        """PR'a yorum ekler."""
        ...

    @abstractmethod
    def get_pr_changes(self, repo: str, pr_id: int) -> list[dict]:
        """PR'daki degisen dosyalari getirir."""
        ...

    def browse_directory(self, repo: str, path: str = "/", branch: str = "") -> list[dict]:
        """Dizin icerigini listeler (opsiyonel — local repo ile de yapilabilir)."""
        raise NotImplementedError

    def search_code(self, repo: str, query: str) -> list[dict]:
        """Kod arar (opsiyonel — local repo ile de yapilabilir)."""
        raise NotImplementedError
