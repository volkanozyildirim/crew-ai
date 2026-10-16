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


def parse_build_dir(jenkinsfile: str, dockerfile: str) -> dict | None:
    m = _ORIGIN.search(jenkinsfile or "")
    if not m:
        return None
    jf, df = jenkinsfile or "", dockerfile or ""
    websites = _WEBSITE.findall(jf) + _ARG_WEBSITE.findall(df)
    names: list[str] = []
    for raw in _APPNAME.findall(df) + _APPNAME.findall(jf) + _TEMPLATE.findall(jf):
        raw = raw.strip()
        if "${website}" in raw:
            names += [raw.replace("${website}", w) for w in websites]
        elif "$" not in raw:
            names.append(raw)
    apps = list(dict.fromkeys(names))
    return {"project": m.group(1), "repo": m.group(2), "apps": apps}


def _read(client, devrepo: str, path: str) -> str:
    try:
        return client.get_file_content(devrepo, path, branch=DEVOPS_BRANCH, project=DEVOPS_PROJECT) or ""
    except Exception:  # noqa: BLE001 — servis dizininde dosya olmayabilir (404)
        return ""


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
                out[app] = {"repo": entry["repo"], "project": entry["project"], "src": f"{devrepo}{path}"}
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
            apps = build_map(client)
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
