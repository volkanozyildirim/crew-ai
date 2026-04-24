"""Provider abstraction — iş yönetimi ve kaynak kod platformlarını soyutlar.

İki bağımsız interface:
- WorkItemProvider: İş yönetimi (Azure Boards, Jira, Trello, Linear)
- SCMProvider: Kaynak kod yönetimi (Azure Repos, GitHub, GitLab, Bitbucket)

Kullanım:
    from agile_sdlc_crew.providers import get_work_item_provider, get_scm_provider

    wi = get_work_item_provider()   # env'den provider secer
    scm = get_scm_provider()        # env'den provider secer
"""

from agile_sdlc_crew.providers.base import WorkItemProvider, SCMProvider
from agile_sdlc_crew.providers.factory import get_work_item_provider, get_scm_provider

__all__ = [
    "WorkItemProvider",
    "SCMProvider",
    "get_work_item_provider",
    "get_scm_provider",
]
