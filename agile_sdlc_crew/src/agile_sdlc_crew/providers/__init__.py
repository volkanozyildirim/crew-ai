"""Provider abstraction — is yonetimi ve kaynak kod platformlarini soyutlar.

Iki bagimsiz interface:
- WorkItemProvider: Is yonetimi (Azure Boards, Jira, Trello, Linear)
- SCMProvider: Kaynak kod yonetimi (Azure Repos, GitHub, GitLab, Bitbucket)

LLM/embed mimarisinin paraleli: registry + resolver + credentials.
Provider modulleri NAME, CREDS_SCHEMA, build_work_item / build_scm sunar.
Aktif secim resolver tarafindan yaml + env'den okunur.

Kullanim:
    from agile_sdlc_crew.providers import get_work_item_provider, get_scm_provider
    wi = get_work_item_provider()   # registry+resolver uzerinden
    scm = get_scm_provider()
"""

from agile_sdlc_crew.providers.base import (
    PRComment,
    PullRequest,
    Repository,
    SCMProvider,
    WorkItem,
    WorkItemProvider,
)
from agile_sdlc_crew.providers.factory import (
    get_scm_provider,
    get_work_item_provider,
    reset_providers,
)
from agile_sdlc_crew.providers.registry import (
    build_scm,
    build_work_item,
    get_credential_schemas,
    list_scm_providers,
    list_work_item_providers,
    register_scm,
    register_work_item,
    scm_registry,
    work_item_registry,
)
from agile_sdlc_crew.providers.resolver import (
    build_active_scm,
    build_active_work_item,
    get_scm_provider_name,
    get_work_item_provider_name,
    load_scm_config,
    load_work_item_config,
    save_scm_config,
    save_work_item_config,
)

__all__ = [
    # Interfaces / data models
    "WorkItemProvider",
    "SCMProvider",
    "WorkItem",
    "Repository",
    "PullRequest",
    "PRComment",
    # Factory (geriye uyumluluk)
    "get_work_item_provider",
    "get_scm_provider",
    "reset_providers",
    # Registry
    "register_work_item",
    "register_scm",
    "list_work_item_providers",
    "list_scm_providers",
    "build_work_item",
    "build_scm",
    "get_credential_schemas",
    "work_item_registry",
    "scm_registry",
    # Resolver
    "get_work_item_provider_name",
    "get_scm_provider_name",
    "load_work_item_config",
    "load_scm_config",
    "save_work_item_config",
    "save_scm_config",
    "build_active_work_item",
    "build_active_scm",
]
