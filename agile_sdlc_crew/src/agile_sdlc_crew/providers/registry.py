"""Work Item ve SCM provider registry — LLM/embed registry'leri ile paralel mimari.

Iki bagimsiz registry: work_item ve scm. Bir provider modulu ikisinde de
kayitli olabilir (orn. azure_devops). Provider modulleri NAME, CREDS_SCHEMA,
build_work_item / build_scm sabitlerini sunar.

Server'in _registry_for_ns() yapisina uyum icin work_item_registry ve
scm_registry shim instance'lari LLM/embed registry modulleriyle ayni
metodlari sunar (list_providers, get_credential_schemas).
"""

from __future__ import annotations

import logging
from typing import Callable

from agile_sdlc_crew.providers.base import SCMProvider, WorkItemProvider

log = logging.getLogger("pipeline")

WorkItemFactory = Callable[..., WorkItemProvider]
SCMFactory = Callable[..., SCMProvider]

_WORK_ITEM_PROVIDERS: dict[str, WorkItemFactory] = {}
_SCM_PROVIDERS: dict[str, SCMFactory] = {}

# Registry-level credential schemas: provider name -> schema list
_WORK_ITEM_SCHEMAS: dict[str, list[dict]] = {}
_SCM_SCHEMAS: dict[str, list[dict]] = {}


def register_work_item(name: str, factory: WorkItemFactory, creds_schema: list[dict] | None = None) -> None:
    _WORK_ITEM_PROVIDERS[name] = factory
    _WORK_ITEM_SCHEMAS[name] = list(creds_schema or [])


def register_scm(name: str, factory: SCMFactory, creds_schema: list[dict] | None = None) -> None:
    _SCM_PROVIDERS[name] = factory
    _SCM_SCHEMAS[name] = list(creds_schema or [])


def list_work_item_providers() -> list[str]:
    return sorted(_WORK_ITEM_PROVIDERS.keys())


def list_scm_providers() -> list[str]:
    return sorted(_SCM_PROVIDERS.keys())


def build_work_item(name: str, **kwargs) -> WorkItemProvider:
    if name not in _WORK_ITEM_PROVIDERS:
        raise ValueError(
            f"Bilinmeyen work_item provider: {name!r}. "
            f"Kayitli: {sorted(_WORK_ITEM_PROVIDERS.keys())}"
        )
    return _WORK_ITEM_PROVIDERS[name](**kwargs)


def build_scm(name: str, **kwargs) -> SCMProvider:
    if name not in _SCM_PROVIDERS:
        raise ValueError(
            f"Bilinmeyen scm provider: {name!r}. "
            f"Kayitli: {sorted(_SCM_PROVIDERS.keys())}"
        )
    return _SCM_PROVIDERS[name](**kwargs)


def get_credential_schemas(kind: str) -> dict[str, list[dict]]:
    """kind: 'work_item' | 'scm' -> {provider_name: schema}."""
    if kind == "work_item":
        return dict(_WORK_ITEM_SCHEMAS)
    if kind == "scm":
        return dict(_SCM_SCHEMAS)
    raise ValueError(f"Bilinmeyen kind: {kind} (work_item|scm)")


# ── Shim instances for server._registry_for_ns() uniformity ──

class _RegistryShim:
    def __init__(self, kind: str):
        self._kind = kind

    def list_providers(self) -> list[str]:
        return list_work_item_providers() if self._kind == "work_item" else list_scm_providers()

    def get_credential_schemas(self) -> dict[str, list[dict]]:
        return get_credential_schemas(self._kind)


work_item_registry = _RegistryShim("work_item")
scm_registry = _RegistryShim("scm")


def _bootstrap_builtin_providers() -> None:
    """Tek somut provider: azure_devops. Diger provider'lar (jira/trello/github vb.)
    eklenmedi — kayitli degiller, build_*() ValueError verir."""
    from agile_sdlc_crew.providers import azure_devops as ado_mod

    register_work_item(
        ado_mod.NAME,
        ado_mod.build_work_item,
        creds_schema=getattr(ado_mod, "CREDS_SCHEMA", []),
    )
    register_scm(
        ado_mod.NAME,
        ado_mod.build_scm,
        creds_schema=getattr(ado_mod, "SCM_CREDS_SCHEMA", []),
    )


_bootstrap_builtin_providers()
