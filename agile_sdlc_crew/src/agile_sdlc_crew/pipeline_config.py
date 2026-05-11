"""Pipeline davranis ayarlari — dashboard tarafindan yonetilen toggle'lar.

LLM secimi (llm/), embedding (embed/), provider (providers/) gibi paketlerden
sonra geriye kalan pipeline davranisi knob'lari (kickoff toggle, cost limit,
context budget, vs.) bu modulun schema + yaml store + helper'i ile yonetilir.

Cozumleme onceligi:
    1. config/pipeline_config.yaml (dashboard tarafindan yazilir)
    2. env (CREW_*) — geriye uyumluluk
    3. SCHEMA default

Yeni knob ekleme: SCHEMA listesine bir entry ekle ve call site'tan
`pipeline_config.get("CREW_X")` ile oku — env okumayi asagidaki helper yapar.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("pipeline")

_CONFIG_FILE = Path(__file__).resolve().parent / "config" / "pipeline_config.yaml"


# Knob schemas. UI bu listeden form uretir.
# type: "bool" | "int" | "float"
# bool: env'de "1"/"true"/"yes" -> True
SCHEMA: list[dict] = [
    # ── Pipeline davranis toggle'lari ──
    {
        "key": "CREW_KICKOFF_MEETING",
        "label": "Kickoff Toplantısı",
        "type": "bool",
        "default": True,
        "desc": "Her is basinda 4 ajanin katildigi kickoff adimi. Kapatirsan ilgili adim atlanir.",
    },
    {
        "key": "CREW_SM_REVIEW",
        "label": "Scrum Master İncelemesi",
        "type": "bool",
        "default": False,
        "desc": "Her adim sonrasi SM kalite kontrolu. Ek LLM cagrisi yapar — maliyeti artirir.",
    },
    {
        "key": "CREW_ANALYZE_WI_MEDIA",
        "label": "WI Görsel/Link Analizi",
        "type": "bool",
        "default": True,
        "desc": "Work item description'daki gorseller ve linkler analiz edilsin mi?",
    },

    # ── Maliyet kontrolu ──
    {
        "key": "CREW_MAX_JOB_COST",
        "label": "Maks. Iş Maliyeti (USD)",
        "type": "float",
        "default": 5.0,
        "min": 0.5,
        "desc": "Kumulatif LLM maliyeti bu degeri asarsa pipeline durur, WI'ya yorum atilir.",
    },
    {
        "key": "CREW_PRICE_INPUT_USD_PER_M",
        "label": "Input Token Fiyatı (USD / 1M)",
        "type": "float",
        "default": 3.0,
        "desc": "Maliyet hesabi icin. Default: Sonnet.",
    },
    {
        "key": "CREW_PRICE_OUTPUT_USD_PER_M",
        "label": "Output Token Fiyatı (USD / 1M)",
        "type": "float",
        "default": 15.0,
        "desc": "Maliyet hesabi icin. Default: Sonnet.",
    },

    # ── Iterasyon ve retry limitleri ──
    {
        "key": "CREW_ARCHITECT_MAX_ITER",
        "label": "Architect Max Iter",
        "type": "int",
        "default": 10,
        "min": 1,
        "desc": "Mimar ajanin maksimum iterasyon sayisi.",
    },
    {
        "key": "CREW_REVIEW_MAX_RETRIES",
        "label": "Review Max Retry",
        "type": "int",
        "default": 2,
        "min": 0,
        "desc": "Code review reddedince kac kez yeniden gelistirme dongusu calisir.",
    },

    # ── Context bütçeleri ──
    {
        "key": "CREW_DEV_CONTEXT_BUDGET",
        "label": "Developer Context Bütçesi",
        "type": "int",
        "default": 12000,
        "min": 1000,
        "desc": "Developer ajana verilen toplam mevcut kod context limiti (karakter).",
    },
    {
        "key": "CREW_DEV_CONTEXT_PER_FILE",
        "label": "Developer Per-File Context",
        "type": "int",
        "default": 2000,
        "min": 200,
        "desc": "Developer ajana verilen tek dosya basina context limiti (karakter).",
    },
    {
        "key": "CREW_MIN_WI_CONTENT_CHARS",
        "label": "Min. WI İçerik Eşiği",
        "type": "int",
        "default": 100,
        "min": 0,
        "desc": "Plain text WI icerigi bu kadarin altindaysa pipeline baslamaz.",
    },

    # ── Claude CLI subprocess ──
    {
        "key": "CREW_CLAUDE_CLI_TIMEOUT",
        "label": "Claude CLI Timeout (sn)",
        "type": "int",
        "default": 300,
        "min": 30,
        "desc": "Claude CLI provider subprocess timeout. Karmasik kod uretiminde Opus 60-180s alabilir; 120s'lik eski sabit yetmiyordu.",
    },

    # ── Resume kontrolu ──
    {
        "key": "CREW_ENABLE_RESUME",
        "label": "Cache'ten Resume",
        "type": "bool",
        "default": True,
        "desc": "Onceki job'dan tamamlanan adimlar cache'ten okunarak atlanir. Vendor/yeni context ile taze calistirmak icin kapat.",
    },

    # ── Repo deps (vendor/) ──
    {
        "key": "CREW_INSTALL_DEPS",
        "label": "Repo Deps Install",
        "type": "bool",
        "default": False,
        "desc": "Hedef repo'da composer install / go mod vendor / npm install calistir. vendor/node_modules klasoru olusur, agent'lar 3rd-party kodu da inceleyebilir. UYARI: ilk install yavas (5-15dk).",
    },
    {
        "key": "CREW_VENDOR_INDEX",
        "label": "Vendor Vector Index",
        "type": "bool",
        "default": False,
        "desc": "vendor/node_modules altindaki 3rd-party paketleri (composer.json/package.json'daki require listesinden) vector DB'ye index'le. Semantic search Butterfly/Laravel framework kodunda da arar. Per-paket max 300 chunk; CREW_VENDOR_INCLUDE env ile ek path eklenir.",
    },
]

_SCHEMA_BY_KEY: dict[str, dict] = {f["key"]: f for f in SCHEMA}


@lru_cache(maxsize=1)
def load_config() -> dict:
    if not _CONFIG_FILE.exists():
        return {}
    try:
        return yaml.safe_load(_CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception as e:
        log.warning(f"  pipeline_config.yaml okuma hatasi: {e}")
        return {}


def reset_cache() -> None:
    load_config.cache_clear()


def _coerce(value: Any, kind: str) -> Any:
    if value is None or value == "":
        return None
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if kind == "int":
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if kind == "float":
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return value


def get(key: str) -> Any:
    """Knob degerini cozumlenmis (typed) olarak dondur. yaml > env > default."""
    field = _SCHEMA_BY_KEY.get(key)
    if not field:
        raise KeyError(f"Bilinmeyen pipeline knob: {key}")

    cfg = load_config()
    if key in cfg:
        coerced = _coerce(cfg[key], field["type"])
        if coerced is not None:
            return coerced

    env_val = os.environ.get(key)
    if env_val is not None and env_val != "":
        coerced = _coerce(env_val, field["type"])
        if coerced is not None:
            return coerced

    return field["default"]


def get_source(key: str) -> str:
    """Bir knob'un degeri nereden geliyor? 'dashboard' | 'env' | 'default'."""
    field = _SCHEMA_BY_KEY.get(key)
    if not field:
        return "unknown"
    cfg = load_config()
    if key in cfg and _coerce(cfg[key], field["type"]) is not None:
        return "dashboard"
    if os.environ.get(key) not in (None, ""):
        return "env"
    return "default"


def all_values() -> list[dict]:
    """Schema + degerleri + kaynak — UI icin."""
    out = []
    for f in SCHEMA:
        out.append({
            **f,
            "value": get(f["key"]),
            "source": get_source(f["key"]),
            "env_present": os.environ.get(f["key"]) not in (None, ""),
        })
    return out


def save(values: dict) -> dict:
    """Dashboard'dan gelen degerleri yaml'a yaz.

    Bilinmeyen key'ler reddedilir; bos/None degerler yaml'dan silinir
    (resolver bir sonraki katmana duser).
    """
    if not isinstance(values, dict):
        raise ValueError("values bir dict olmali")
    unknown = set(values.keys()) - set(_SCHEMA_BY_KEY.keys())
    if unknown:
        raise ValueError(f"Bilinmeyen knob'lar: {sorted(unknown)}")

    doc = dict(load_config() or {})
    for k, v in values.items():
        field = _SCHEMA_BY_KEY[k]
        if v is None or v == "":
            doc.pop(k, None)
            continue
        coerced = _coerce(v, field["type"])
        if coerced is None:
            raise ValueError(f"Gecersiz deger: {k}={v!r} (tip: {field['type']})")
        # Min check
        if "min" in field and isinstance(coerced, (int, float)) and coerced < field["min"]:
            raise ValueError(f"{k} en az {field['min']} olmali (alindi: {coerced})")
        doc[k] = coerced

    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_CONFIG_FILE, "w", encoding="utf-8") as fh:
        fh.write(
            "# Dashboard tarafindan yonetilir — pipeline davranis toggle'lari.\n"
            "# Cozumleme onceligi: BU DOSYA > env (CREW_*) > schema default\n\n"
        )
        yaml.safe_dump(doc, fh, allow_unicode=True, sort_keys=True, default_flow_style=False)
    reset_cache()
    return doc
