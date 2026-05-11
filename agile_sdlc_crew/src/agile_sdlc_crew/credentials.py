"""Provider credentials store — dashboard'dan yonetilen API keyler ve base URL'ler.

Iki namespace: 'llm' ve 'embedding'. Her namespace altinda provider adina gore
credential dict'i saklanir.

Cozumleme akisi (provider build kodlarinda):
    1. credentials.get(namespace, provider, field) -> bos degilse done
    2. env_fallback (provider'in CREDS_SCHEMA'sinda tanimli) -> env okunur
    3. Provider built-in default

Dosya: config/provider_credentials.yaml (gitignore edilmeli — secret iceriyor).
"""

import logging
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import yaml

log = logging.getLogger("pipeline")

_CONFIG_FILE = Path(__file__).resolve().parent / "config" / "provider_credentials.yaml"

VALID_NAMESPACES = ("llm", "embedding", "vision", "work_item", "scm")


@lru_cache(maxsize=1)
def load() -> dict:
    if not _CONFIG_FILE.exists():
        return {}
    try:
        return yaml.safe_load(_CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception as e:
        log.warning(f"  provider_credentials.yaml okuma hatasi: {e}")
        return {}


def reset_cache() -> None:
    load.cache_clear()


def get(namespace: str, provider: str, field: str, default: str = "") -> str:
    """Bir credential degerini al. Yoksa default.

    namespace: 'llm' | 'embedding'
    """
    if namespace not in VALID_NAMESPACES:
        return default
    doc = load() or {}
    ns = doc.get(namespace) or {}
    p = ns.get(provider) or {}
    val = p.get(field)
    if val is None:
        return default
    return str(val)


def get_all(namespace: str, provider: str) -> dict:
    """Bir provider icin tum credential degerlerini dondurur."""
    if namespace not in VALID_NAMESPACES:
        return {}
    doc = load() or {}
    ns = doc.get(namespace) or {}
    return dict(ns.get(provider) or {})


def save(namespace: str, provider: str, fields: dict, allowed: Iterable[str] | None = None) -> dict:
    """Credentials yaz. allowed verilirse sadece o alanlar kabul edilir.

    Bos string degerler dictten silinir (env fallback'a duser).
    """
    if namespace not in VALID_NAMESPACES:
        raise ValueError(f"Bilinmeyen namespace: {namespace}")
    if not provider or not isinstance(provider, str):
        raise ValueError("provider gerekli")

    allowed_set = set(allowed) if allowed else None
    clean: dict = {}
    for k, v in (fields or {}).items():
        if allowed_set is not None and k not in allowed_set:
            continue
        if v is None:
            continue
        if isinstance(v, str):
            v = v.strip()
            if not v:
                continue
        clean[k] = v

    doc = dict(load() or {})
    ns = dict(doc.get(namespace) or {})
    if not clean:
        ns.pop(provider, None)
    else:
        ns[provider] = clean
    doc[namespace] = ns

    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_CONFIG_FILE, "w", encoding="utf-8") as fh:
        fh.write(
            "# Dashboard tarafindan yonetilir — provider API keyler ve base URL'ler.\n"
            "# DIKKAT: secret iceriyor; .gitignore icine ekleyin.\n"
            "# Cozumleme: BU DOSYA > env_fallback > provider built-in default\n\n"
        )
        yaml.safe_dump(doc, fh, allow_unicode=True, sort_keys=True, default_flow_style=False)
    reset_cache()
    return clean


def mask(value: str, keep: int = 4) -> str:
    """Secret degerini UI icin maskele."""
    if not value:
        return ""
    if len(value) <= keep + 2:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep - 2) + value[-2:]
