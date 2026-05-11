"""Work Item ve SCM provider resolver — config dosyasi + env override.

Config dosyalari:
    config/work_item_config.yaml  -> {provider: azure_devops}
    config/scm_config.yaml        -> {provider: azure_devops}

Cozumleme onceligi (her iki kind icin):
    1. yaml cfg.provider
    2. env (CREW_WORK_ITEM_PROVIDER veya CREW_SCM_PROVIDER)
    3. "azure_devops" (default)
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

import yaml

from agile_sdlc_crew.providers.base import SCMProvider, WorkItemProvider
from agile_sdlc_crew.providers.registry import (
    build_scm as _build_scm,
    build_work_item as _build_work_item,
    list_scm_providers,
    list_work_item_providers,
)

log = logging.getLogger("pipeline")

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
_WI_FILE = _CONFIG_DIR / "work_item_config.yaml"
_SCM_FILE = _CONFIG_DIR / "scm_config.yaml"

DEFAULT_PROVIDER = "azure_devops"


# ── Config load (lru_cache'li) ──

@lru_cache(maxsize=1)
def load_work_item_config() -> dict:
    if not _WI_FILE.exists():
        return {}
    try:
        return yaml.safe_load(_WI_FILE.read_text(encoding="utf-8")) or {}
    except Exception as e:
        log.warning(f"  work_item_config.yaml okuma hatasi: {e}")
        return {}


@lru_cache(maxsize=1)
def load_scm_config() -> dict:
    if not _SCM_FILE.exists():
        return {}
    try:
        return yaml.safe_load(_SCM_FILE.read_text(encoding="utf-8")) or {}
    except Exception as e:
        log.warning(f"  scm_config.yaml okuma hatasi: {e}")
        return {}


def reset_cache() -> None:
    load_work_item_config.cache_clear()
    load_scm_config.cache_clear()


# ── Active provider resolution ──

def get_work_item_provider_name() -> str:
    cfg = load_work_item_config()
    return (
        cfg.get("provider")
        or os.environ.get("CREW_WORK_ITEM_PROVIDER")
        or DEFAULT_PROVIDER
    )


def get_scm_provider_name() -> str:
    cfg = load_scm_config()
    return (
        cfg.get("provider")
        or os.environ.get("CREW_SCM_PROVIDER")
        or DEFAULT_PROVIDER
    )


# ── Active provider build ──

def build_active_work_item() -> WorkItemProvider:
    name = get_work_item_provider_name()
    log.info(f"  Active work_item provider: {name}")
    return _build_work_item(name)


def build_active_scm() -> SCMProvider:
    name = get_scm_provider_name()
    log.info(f"  Active scm provider: {name}")
    return _build_scm(name)


# ── Save (dashboard yazma kancasi) ──

def _save_yaml(path: Path, doc: dict, header: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header)
        yaml.safe_dump(doc, fh, allow_unicode=True, sort_keys=True, default_flow_style=False)


def save_work_item_config(provider: str) -> dict:
    if provider not in list_work_item_providers():
        raise ValueError(
            f"Bilinmeyen work_item provider: {provider}. "
            f"Mevcut: {list_work_item_providers()}"
        )
    cfg = {"provider": str(provider)}
    _save_yaml(
        _WI_FILE, cfg,
        "# Dashboard tarafindan yonetilir — aktif work_item provider.\n"
        "# Resolver onceligi: BU DOSYA > CREW_WORK_ITEM_PROVIDER env > 'azure_devops'\n\n",
    )
    reset_cache()
    return cfg


def save_scm_config(provider: str) -> dict:
    if provider not in list_scm_providers():
        raise ValueError(
            f"Bilinmeyen scm provider: {provider}. "
            f"Mevcut: {list_scm_providers()}"
        )
    cfg = {"provider": str(provider)}
    _save_yaml(
        _SCM_FILE, cfg,
        "# Dashboard tarafindan yonetilir — aktif scm provider.\n"
        "# Resolver onceligi: BU DOSYA > CREW_SCM_PROVIDER env > 'azure_devops'\n\n",
    )
    reset_cache()
    return cfg
