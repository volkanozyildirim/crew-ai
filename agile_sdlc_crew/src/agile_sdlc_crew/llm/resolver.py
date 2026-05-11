"""Agent -> Profile -> LLM cozumleyici.

Cozumleme onceligi:
    1. CREW_LLM_PROFILE_<AGENT_UPPER> env override
    2. agents.yaml icindeki `llm_profile:` alani
    3. CREW_USE_LOCAL_LLM / CREW_LOCAL_DEVELOPER (geriye uyumluluk)
    4. llm_profiles.yaml icindeki `agent_defaults` mapping
"""

import logging
import os
from functools import lru_cache
from pathlib import Path

import yaml

from crewai import LLM

from agile_sdlc_crew.llm.registry import build_llm

log = logging.getLogger("pipeline")

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
_PROFILES_FILE = _CONFIG_DIR / "llm_profiles.yaml"
_AGENTS_FILE = _CONFIG_DIR / "agents.yaml"
# Dashboard yazar; agents.yaml'i bozmamak icin ayri dosya
_OVERRIDES_FILE = _CONFIG_DIR / "agent_llm_overrides.yaml"


@lru_cache(maxsize=1)
def _load_profiles_doc() -> dict:
    if not _PROFILES_FILE.exists():
        return {}
    with open(_PROFILES_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def _load_agents_doc() -> dict:
    if not _AGENTS_FILE.exists():
        return {}
    with open(_AGENTS_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def _load_overrides_doc() -> dict:
    if not _OVERRIDES_FILE.exists():
        return {}
    with open(_OVERRIDES_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _bool_env(key: str, default: str = "") -> bool:
    return os.environ.get(key, default).lower() in ("1", "true", "yes")


def _backwards_compat_profile(agent_key: str) -> str | None:
    """Eski env-flag mantigini profile ismine cevirir.

    Architect ve developer icin ozel kurallar var (kullanici isteyle
    bunlari ayarlanabilir kildi); diger agentlar icin CREW_USE_LOCAL_LLM
    flag'i tek anahtar."""
    use_local = _bool_env("CREW_USE_LOCAL_LLM")
    use_local_dev = os.environ.get("CREW_LOCAL_DEVELOPER", "1").lower() not in (
        "0", "false", "no",
    )

    if agent_key == "software_architect":
        # Architect tarihsel olarak hep premium — degistirmek isteyen
        # llm_profile ile veya CREW_LLM_PROFILE_SOFTWARE_ARCHITECT ile yapsin.
        return None
    if agent_key == "senior_developer":
        if use_local and use_local_dev:
            return "developer_local_coder"
        return None
    if agent_key == "scrum_master":
        return "scrum_local" if use_local else None
    # BA / QA / UAT / Reviewer
    if use_local:
        return "reasoning_local"
    return None


def _profile_to_spec(profile_name: str) -> dict:
    """Profile adindan spec dict (provider/model/max_tokens + extras) cikarir."""
    p = get_profile(profile_name)
    spec = dict(p)
    spec["_profile"] = profile_name
    return spec


def _normalize_inline_spec(raw) -> dict | None:
    """Override degeri inline spec mi (dict provider+model) yoksa profile ref mi?

    Donen dict: {provider, model, max_tokens, ...} ya da None (profile ref ise)."""
    if isinstance(raw, dict) and raw.get("provider") and raw.get("model"):
        spec = dict(raw)
        spec.setdefault("max_tokens", 4096)
        return spec
    return None


def resolve_spec_with_source(agent_key: str) -> tuple[dict, str]:
    """Agent icin LLM spec'i (provider/model/max_tokens) + kaynagini dondurur.

    Source: 'env' | 'dashboard' | 'agents.yaml' | 'backwards_compat' | 'default'
    Spec'in icinde '_profile' alani olabilir (profile referansiyla geldiyse).
    """
    # 1. Per-agent env override (profile name)
    env_key = f"CREW_LLM_PROFILE_{agent_key.upper()}"
    env_val = os.environ.get(env_key)
    if env_val:
        return _profile_to_spec(env_val), "env"

    # 2. Dashboard override (inline spec ya da profile ref)
    overrides_doc = _load_overrides_doc()
    overrides = (overrides_doc.get("agents") or {}) if isinstance(overrides_doc, dict) else {}
    raw = overrides.get(agent_key)
    if raw:
        inline = _normalize_inline_spec(raw)
        if inline is not None:
            return inline, "dashboard"
        # profile ref: dict { profile: name } veya direkt string
        if isinstance(raw, dict) and raw.get("profile"):
            return _profile_to_spec(raw["profile"]), "dashboard"
        if isinstance(raw, str):
            return _profile_to_spec(raw), "dashboard"

    # 3. agents.yaml icinde llm_profile alani
    agents_doc = _load_agents_doc()
    agent_block = agents_doc.get(agent_key) or {}
    if isinstance(agent_block, dict):
        configured = agent_block.get("llm_profile")
        if configured:
            return _profile_to_spec(configured), "agents.yaml"

    # 4. Geriye uyumluluk (CREW_USE_LOCAL_LLM ailesi)
    bc = _backwards_compat_profile(agent_key)
    if bc:
        return _profile_to_spec(bc), "backwards_compat"

    # 5. Default mapping
    profiles_doc = _load_profiles_doc()
    defaults = profiles_doc.get("agent_defaults") or {}
    if agent_key in defaults:
        return _profile_to_spec(defaults[agent_key]), "default"

    raise ValueError(
        f"Agent {agent_key!r} icin LLM spec'i cozumlenemedi. "
        f"agents.yaml veya llm_profiles.yaml icinde tanimlayin."
    )


def resolve_with_source(agent_key: str) -> tuple[str, str]:
    """Geriye uyumluluk: profile adini ve kaynagini dondurur.

    Inline override durumunda profile adi yoktur, sentetik bir id donulur.
    """
    spec, source = resolve_spec_with_source(agent_key)
    return spec.get("_profile") or f"<inline:{spec['provider']}/{spec['model']}>", source


def resolve_profile_name(agent_key: str) -> str:
    return resolve_with_source(agent_key)[0]


def set_agent_override(
    agent_key: str,
    profile_name: str | None = None,
    *,
    inline_spec: dict | None = None,
) -> None:
    """Dashboard override'i yazar.

    - profile_name verilirse profil ref (mevcut profil olmali).
    - inline_spec verilirse provider+model+max_tokens dict olarak kaydedilir.
    - Ikisi de None ise override silinir.
    """
    if profile_name and inline_spec:
        raise ValueError("profile_name ve inline_spec ayni anda verilemez")

    value: object | None = None
    if profile_name is not None:
        valid = (_load_profiles_doc().get("profiles") or {})
        if profile_name not in valid:
            raise ValueError(
                f"Bilinmeyen profil: {profile_name}. "
                f"Mevcut: {sorted(valid.keys())}"
            )
        value = profile_name

    if inline_spec is not None:
        provider = inline_spec.get("provider")
        model = inline_spec.get("model")
        if not provider or not model:
            raise ValueError("inline_spec 'provider' ve 'model' alanlarini icermeli")
        # Registry kontrolu: provider kayitli mi?
        from agile_sdlc_crew.llm.registry import list_providers
        if provider not in list_providers():
            raise ValueError(
                f"Bilinmeyen provider: {provider}. Mevcut: {list_providers()}"
            )
        clean: dict = {"provider": provider, "model": str(model)}
        mt = inline_spec.get("max_tokens")
        if mt is not None:
            try:
                clean["max_tokens"] = max(64, int(mt))
            except (TypeError, ValueError):
                raise ValueError(f"max_tokens int olmali, alindi: {mt!r}")
        # Diger profile-uyumlu alanlar (base_url_env vb.) gecirilir
        for k, v in inline_spec.items():
            if k not in ("provider", "model", "max_tokens") and not k.startswith("_"):
                clean[k] = v
        value = clean

    doc = dict(_load_overrides_doc() or {})
    agents = dict(doc.get("agents") or {})
    if value is None:
        agents.pop(agent_key, None)
    else:
        agents[agent_key] = value
    doc["agents"] = agents

    _OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_OVERRIDES_FILE, "w", encoding="utf-8") as f:
        f.write(
            "# Dashboard tarafindan yonetilir — agent -> LLM override.\n"
            "# Deger formatlari:\n"
            "#   <agent_key>: <profile_name>                   (profil referansi)\n"
            "#   <agent_key>: { profile: <profile_name> }      (profil referansi)\n"
            "#   <agent_key>: { provider: ..., model: ..., max_tokens: ... }  (inline)\n"
            "# Resolver onceligi: env > BU DOSYA > agents.yaml > "
            "CREW_USE_LOCAL_LLM > agent_defaults\n\n"
        )
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=True, default_flow_style=False)
    _load_overrides_doc.cache_clear()


def get_profile(profile_name: str) -> dict:
    profiles = (_load_profiles_doc().get("profiles") or {})
    if profile_name not in profiles:
        raise ValueError(
            f"Bilinmeyen LLM profili: {profile_name!r}. "
            f"Mevcut: {sorted(profiles.keys())}"
        )
    return profiles[profile_name]


def _build_from_spec(spec: dict) -> LLM:
    provider = spec["provider"]
    model = spec["model"]
    max_tokens = int(spec.get("max_tokens", 4096))
    extras = {
        k: v for k, v in spec.items()
        if k not in ("provider", "model", "max_tokens") and not k.startswith("_")
    }
    return build_llm(provider, model, max_tokens=max_tokens, **extras)


def build_for_profile(profile_name: str) -> LLM:
    """Bir profile adindan LLM nesnesi olusturur."""
    return _build_from_spec(_profile_to_spec(profile_name))


def build_for_agent(agent_key: str) -> LLM:
    """Agent_key -> spec -> LLM. Inline override veya profile referansi olabilir."""
    spec, source = resolve_spec_with_source(agent_key)
    label = spec.get("_profile") or f"inline {spec['provider']}/{spec['model']}"
    log.info(
        f"  LLM resolve: agent={agent_key} {label} "
        f"(provider={spec['provider']} model={spec['model']} source={source})"
    )
    return _build_from_spec(spec)


def reset_cache() -> None:
    """Test/devel: yaml cache'lerini sifirla."""
    _load_profiles_doc.cache_clear()
    _load_agents_doc.cache_clear()
    _load_overrides_doc.cache_clear()
