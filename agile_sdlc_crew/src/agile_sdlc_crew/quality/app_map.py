"""New Relic uygulama adı → kaynak repo eşlemesi.

Kaynak: FLODevOps projesindeki `anatolia` (TR), `global`, `russia` repoları,
`production` dalı. Her `build/<servis>/` için:
  JenkinsfileBuild → gitOriginApp (…/v3/<org>/<proje>/<repo>)
  Dockerfile / JenkinsfileBuild → newrelic.appname="…" (literal) ya da
  websites şablonu newrelic_app_name = "[Prod] ${website}" + website: '…' listesi.
Sonuç config/quality_app_map.yaml'a önbelleklenir (git'te değil); elle
düzeltmeler config/quality_app_map_overrides.yaml'da ({app adı: repo}) ve
her zaman önceliklidir.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import yaml

from agile_sdlc_crew.quality.model import QualitySourceError

log = logging.getLogger("pipeline")

DEVOPS_PROJECT = "FLODevOps"
DEVOPS_REPOS = ("anatolia", "global", "russia")
DEVOPS_BRANCH = "production"

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
CACHE_PATH = _CONFIG_DIR / "quality_app_map.yaml"
OVERRIDES_PATH = _CONFIG_DIR / "quality_app_map_overrides.yaml"

_ORIGIN = re.compile(r'gitOriginApp\s*=\s*"[^"]*?v3/[^/"]+/([^/"]+)/([^/"]+)"')
_APPNAME = re.compile(r'newrelic\.appname\s*=\s*\\?"([^"\\]+)')
_TEMPLATE = re.compile(r'newrelic_app_name\s*=\s*"([^"]+)"')
_WEBSITE = re.compile(r"website:\s*'([^']+)'")
_ARG_WEBSITE = re.compile(r"^\s*ARG\s+website=(\S+)", re.M)
# russia/build/websites: `website = brandDomains[params.brand]` + `Map brandDomains = [sneakerbox: 'sneakerbox.ru', …]`
_WEBSITE_FROM_MAP = re.compile(r"\bwebsite\s*=\s*(\w+)\s*\[")
_MAP_VALUE = re.compile(r"[\w-]+\s*:\s*'([^'$]+)'")
# anatolia/build/segment, similar: `String website = "seg.flo.com.tr"` (NR adı build dosyasında değil)
_STR_WEBSITE = re.compile(r'\bString\s+website\s*=\s*"([^"$]+)"')


def _map_websites(jf: str) -> list[str]:
    m = _WEBSITE_FROM_MAP.search(jf)
    if not m:
        return []
    block = re.search(rf"\bMap\s+{re.escape(m.group(1))}\s*=\s*\[(.*?)\]", jf, re.S)
    return _MAP_VALUE.findall(block.group(1)) if block else []


def parse_build_dir(jenkinsfile: str, dockerfile: str) -> dict | None:
    """`fallback`: build dosyalarında NR adı yok, ad sitenin adından tahmin edildi ("[Prod] <site>"
    ve "[Prod]<site>" — NR'de seg/similar boşluksuz görünüyor). Tahmin, başka bir dizinin AÇIK
    adını asla ezmez (build_map)."""
    m = _ORIGIN.search(jenkinsfile or "")
    if not m:
        return None
    jf, df = jenkinsfile or "", dockerfile or ""
    websites = (_WEBSITE.findall(jf) + [w.strip('"\'') for w in _ARG_WEBSITE.findall(df)]
                + _map_websites(jf) + _STR_WEBSITE.findall(jf))
    websites = list(dict.fromkeys(w for w in websites if w))
    names: list[str] = []
    found_config = False
    for raw in _APPNAME.findall(df) + _APPNAME.findall(jf) + _TEMPLATE.findall(jf):
        raw = raw.strip()
        found_config = True
        if "${website}" in raw:
            names += [raw.replace("${website}", w) for w in websites]
        elif "$" not in raw:
            names.append(raw)
    fallback = not found_config and bool(websites)
    if fallback:
        names = [n for w in websites for n in (f"[Prod] {w}", f"[Prod]{w}")]
    apps = list(dict.fromkeys(names))
    return {"project": m.group(1), "repo": m.group(2), "apps": apps, "fallback": fallback}


def _read(client, devrepo: str, path: str) -> str:
    """Yalnızca 404 (servis dizininde dosya yok) boş döner; 401/429/5xx/zaman aşımı
    yükselir — yoksa kesinti "eşleme yok" gibi görünür ve önbelleği ezerdi."""
    try:
        return client.get_file_content(devrepo, path, branch=DEVOPS_BRANCH, project=DEVOPS_PROJECT) or ""
    except Exception as e:  # noqa: BLE001
        if getattr(getattr(e, "response", None), "status_code", None) == 404:
            return ""
        raise


def build_map(client) -> dict[str, dict]:
    """Ağdan kur. Dizin listelenemezse hata yükselir (load_map önbelleğe düşer)."""
    out: dict[str, dict] = {}
    for devrepo in DEVOPS_REPOS:
        items = client.get_items_in_path(devrepo, "/build", branch=DEVOPS_BRANCH,
                                         recursion_level="oneLevel", project=DEVOPS_PROJECT)
        for it in items:
            path = (it.get("path") or "").rstrip("/")
            if not it.get("isFolder") or path in ("", "/build"):
                continue
            entry = parse_build_dir(_read(client, devrepo, f"{path}/JenkinsfileBuild"),
                                    _read(client, devrepo, f"{path}/Dockerfile"))
            if not entry:
                continue
            for app in entry["apps"]:
                if entry.get("fallback") and app in out:
                    continue  # tahmin edilen ad, başka dizinin açık adını ezmez
                out[app] = {"repo": entry["repo"], "project": entry["project"], "src": f"{devrepo}{path}",
                            **({"guessed": True} if entry.get("fallback") else {})}
    return out


def _read_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def load_map(client=None, *, max_age_h: int = 24, force: bool = False,
             cache_path: Path = CACHE_PATH, overrides_path: Path = OVERRIDES_PATH) -> dict[str, dict]:
    cached = _read_yaml(cache_path)
    apps: dict = cached.get("apps") or {}
    fresh = bool(apps) and (time.time() - float(cached.get("built_at") or 0)) < max_age_h * 3600
    if force or not fresh:
        try:
            if client is None:
                from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient
                client = AzureDevOpsClient()
            built = build_map(client)
            if not built or (apps and len(built) < len(apps) // 2):
                raise QualitySourceError(
                    f"FLODevOps eşlemesi şüpheli küçük ({len(built)} uygulama, önbellekte {len(apps)})")
            apps = built
            cache_path.write_text(yaml.safe_dump({"built_at": time.time(), "apps": apps},
                                                 allow_unicode=True, sort_keys=True), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            if not apps:
                raise QualitySourceError(f"FLODevOps okunamadı ve önbellek yok: {e}") from e
            log.warning(f"⚠️ Kalite: FLODevOps okunamadı, eski eşleme kullanılıyor: {e}")
    merged = {str(k).lower(): v for k, v in apps.items()}
    for name, repo in (_read_yaml(overrides_path) or {}).items():
        merged[str(name).lower()] = {"repo": str(repo), "project": "", "src": "override"}
    return merged


def resolve(app: str, mapping: dict[str, dict]) -> str:
    return (mapping.get((app or "").lower()) or {}).get("repo", "")
