"""Provider factory — env degiskenlerine gore dogru provider'i secer.

Env degiskenleri:
    CREW_WORK_ITEM_PROVIDER: "azure_devops" (default) | "jira" | "trello" | "linear"
    CREW_SCM_PROVIDER: "azure_devops" (default) | "github" | "gitlab" | "bitbucket"

Ornek:
    # Azure Boards + GitHub (farkli platformlar birlikte kullanilabilir)
    CREW_WORK_ITEM_PROVIDER=azure_devops
    CREW_SCM_PROVIDER=github

    # Jira + GitLab
    CREW_WORK_ITEM_PROVIDER=jira
    CREW_SCM_PROVIDER=gitlab
"""

import os
from agile_sdlc_crew.providers.base import WorkItemProvider, SCMProvider

# Singleton cache — ayni provider'i tekrar tekrar olusturma
_wi_provider: WorkItemProvider | None = None
_scm_provider: SCMProvider | None = None


def get_work_item_provider() -> WorkItemProvider:
    """Is yonetimi provider'ini dondurur (singleton)."""
    global _wi_provider
    if _wi_provider is not None:
        return _wi_provider

    provider_name = os.environ.get("CREW_WORK_ITEM_PROVIDER", "azure_devops").lower()

    if provider_name == "azure_devops":
        from agile_sdlc_crew.providers.azure_devops import AzureDevOpsWorkItemProvider
        _wi_provider = AzureDevOpsWorkItemProvider()
    elif provider_name == "jira":
        raise NotImplementedError(
            "Jira provider henuz implement edilmedi. "
            "providers/jira.py olusturup JiraWorkItemProvider sinifini yazin."
        )
    elif provider_name == "trello":
        raise NotImplementedError(
            "Trello provider henuz implement edilmedi. "
            "providers/trello.py olusturup TrelloWorkItemProvider sinifini yazin."
        )
    elif provider_name == "linear":
        raise NotImplementedError(
            "Linear provider henuz implement edilmedi. "
            "providers/linear.py olusturup LinearWorkItemProvider sinifini yazin."
        )
    else:
        raise ValueError(
            f"Bilinmeyen work item provider: {provider_name}. "
            f"Desteklenen: azure_devops, jira, trello, linear"
        )

    return _wi_provider


def get_scm_provider() -> SCMProvider:
    """Kaynak kod yonetimi provider'ini dondurur (singleton)."""
    global _scm_provider
    if _scm_provider is not None:
        return _scm_provider

    provider_name = os.environ.get("CREW_SCM_PROVIDER", "azure_devops").lower()

    if provider_name == "azure_devops":
        from agile_sdlc_crew.providers.azure_devops import AzureDevOpsSCMProvider
        _scm_provider = AzureDevOpsSCMProvider()
    elif provider_name == "github":
        raise NotImplementedError(
            "GitHub SCM provider henuz implement edilmedi. "
            "providers/github.py olusturup GitHubSCMProvider sinifini yazin."
        )
    elif provider_name == "gitlab":
        raise NotImplementedError(
            "GitLab SCM provider henuz implement edilmedi. "
            "providers/gitlab.py olusturup GitLabSCMProvider sinifini yazin."
        )
    elif provider_name == "bitbucket":
        raise NotImplementedError(
            "Bitbucket SCM provider henuz implement edilmedi. "
            "providers/bitbucket.py olusturup BitbucketSCMProvider sinifini yazin."
        )
    else:
        raise ValueError(
            f"Bilinmeyen SCM provider: {provider_name}. "
            f"Desteklenen: azure_devops, github, gitlab, bitbucket"
        )

    return _scm_provider


def reset_providers():
    """Provider cache'i sifirla (test icin)."""
    global _wi_provider, _scm_provider
    _wi_provider = None
    _scm_provider = None
