"""Provider factory — registry+resolver'a delegate eden ince shim.

Geriye uyumluluk: `get_work_item_provider()` ve `get_scm_provider()`
public sembolleri korunur. Aktif provider secimi resolver tarafindan
yapilir (yaml > env > default), instantiate registry tarafindan.
"""

from agile_sdlc_crew.providers.base import SCMProvider, WorkItemProvider

# Singleton cache — ayni provider'i tekrar tekrar olusturma
_wi_provider: WorkItemProvider | None = None
_scm_provider: SCMProvider | None = None


def get_work_item_provider() -> WorkItemProvider:
    """Aktif work_item provider'ini dondurur (singleton)."""
    global _wi_provider
    if _wi_provider is None:
        from agile_sdlc_crew.providers.resolver import build_active_work_item
        _wi_provider = build_active_work_item()
    return _wi_provider


def get_scm_provider() -> SCMProvider:
    """Aktif scm provider'ini dondurur (singleton)."""
    global _scm_provider
    if _scm_provider is None:
        from agile_sdlc_crew.providers.resolver import build_active_scm
        _scm_provider = build_active_scm()
    return _scm_provider


def reset_providers() -> None:
    """Provider cache'i sifirla (test ve dashboard config degisimleri icin)."""
    global _wi_provider, _scm_provider
    _wi_provider = None
    _scm_provider = None
    from agile_sdlc_crew.providers.resolver import reset_cache
    reset_cache()
