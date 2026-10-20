"""PHP modernleştirme — teknik borç aşama 2a.

Repo 8.4 imajında koşuyor ama kod 7.4 gibi yazılmış: tipsiz property, sabit
grubu sınıfları, dizi şekli dönen metotlar, `strpos(...) !== false`. Burada:

* hedef PHP sürümü FLODevOps imajından (`FROM …/php-8-4:1.0`; birden çok imaj
  → en düşük), yoksa composer.json alt sınırı, yoksa repo taranmaz;
* `config/php_modern_rules.yaml` kataloğu: kural başına sürüm, sınıf, ayrıntı,
  avantajlar, riskler, önce/sonra; yalnız hedefin sunduğu kurallar önerilir;
* tarama `php_modern_scan.php` (token_get_all) ile, LLM yok; gruplama/skor/
  dedup Sonar hattıyla aynı; Task'lar `techdebt` hattında açılır.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from agile_sdlc_crew.quality import app_map

log = logging.getLogger("pipeline")

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
RULES_PATH = _CONFIG_DIR / "php_modern_rules.yaml"
TARGETS_CACHE_PATH = _CONFIG_DIR / "quality_php_targets.yaml"

SOURCE = "php_modern"
KINDS = ("deprecation", "mechanical", "structural", "report")
WEIGHT = {"deprecation": 20, "structural": 10, "mechanical": 8, "report": 0}


# ── Katalog ──────────────────────────────────────────────────────────────

@dataclass
class Rule:
    key: str
    since: tuple[int, int]
    kind: str
    title: str
    summary: str
    advantages: list[str]
    risks: list[str]
    before: str
    after: str
    ref: str
    unit: str = "file"
    auto: bool = False
    sp_unit: int = 1
    rector: str = ""

    @property
    def since_str(self) -> str:
        return f"{self.since[0]}.{self.since[1]}"

    @property
    def weight(self) -> int:
        return WEIGHT.get(self.kind, 0)


def _ver(s) -> tuple[int, int]:
    m = re.match(r"^\s*(\d+)\.(\d+)", str(s))
    if not m:
        raise ValueError(f"PHP sürümü anlaşılamadı: {s!r}")
    return int(m.group(1)), int(m.group(2))


def load_rules(path: Path = RULES_PATH) -> list[Rule]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: list[Rule] = []
    for r in data.get("rules") or []:
        kind = str(r.get("kind") or "")
        if kind not in KINDS:
            raise ValueError(f"php_modern kuralı {r.get('key')}: kind geçersiz ({kind})")
        out.append(Rule(
            key=str(r["key"]), since=_ver(r["since"]), kind=kind, title=str(r.get("title") or ""),
            summary=str(r.get("summary") or ""), advantages=[str(x) for x in r.get("advantages") or []],
            risks=[str(x) for x in r.get("risks") or []], before=str(r.get("before") or "").rstrip("\n"),
            after=str(r.get("after") or "").rstrip("\n"), ref=str(r.get("ref") or ""),
            unit=str(r.get("unit") or "file"),
            # structural / report asla otonom değil — YAML ne derse desin
            auto=bool(r.get("auto")) and kind in ("deprecation", "mechanical"),
            sp_unit=int(r.get("sp_unit") or 0), rector=str(r.get("rector") or ""),
        ))
    return out


def rules_version(path: Path = RULES_PATH) -> str:
    try:
        return str((yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("version") or "0")
    except (OSError, yaml.YAMLError):
        return "0"


def applicable(rules: list[Rule], version: str | None) -> list[Rule]:
    """Yalnız hedef sürümün sunduğu kurallar (since ≤ hedef)."""
    if not version:
        return []
    v = _ver(version)
    return [r for r in rules if r.since <= v]


# ── Hedef sürüm ──────────────────────────────────────────────────────────

_IMAGE = re.compile(r"(?:^|/)php[-:]?(\d+)[-.](\d+)(?:[-:.]|$)")
_FROM = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", re.M | re.I)
_COMPOSER_PHP = re.compile(r"(\d+)\.(\d+)")


def parse_php_image(image: str) -> tuple[int, int] | None:
    """`…/php-8-4:1.0` → (8, 4); `php:8.3-fpm` → (8, 3); PHP imajı değilse None."""
    m = _IMAGE.search((image or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def _read_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _dockerfile_versions(client, repo: str, amap: dict) -> tuple[list[tuple[int, int]], list[str]]:
    srcs = sorted({e.get("src", "") for e in amap.values()
                   if isinstance(e, dict) and (e.get("repo") or "").lower() == repo.lower() and e.get("src")})
    versions: list[tuple[int, int]] = []
    images: list[str] = []
    for src in srcs:
        devrepo, _, path = src.partition("/")
        df = app_map._read(client, devrepo, "/" + path.strip("/") + "/Dockerfile")
        for img in _FROM.findall(df or ""):
            v = parse_php_image(img)
            if v:
                versions.append(v)
                if img not in images:
                    images.append(img)
    return versions, images


def _composer_version(repo_root: Path | None) -> tuple[int, int] | None:
    if not repo_root:
        return None
    try:
        data = json.loads((repo_root / "composer.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    req = str(((data or {}).get("require") or {}).get("php") or "")
    found = [(int(a), int(b)) for a, b in _COMPOSER_PHP.findall(req)]
    return min(found) if found else None


def target_version(client, repo: str, *, app_map_cache: dict | None = None, repo_root: Path | None = None,
                   cache_path: Path = TARGETS_CACHE_PATH, max_age_h: int = 24) -> dict:
    """{"version": "8.4"|None, "images": [...], "source": "dockerfile"|"composer"|None}.
    Birden çok imaj → EN DÜŞÜK sürüm (yüksek alınırsa 7.4 imajına match önerilir, kırılır)."""
    cached = _read_yaml(cache_path)
    hit = (cached.get("repos") or {}).get(repo)
    if hit and max_age_h > 0 and time.time() - float(hit.get("built_at") or 0) < max_age_h * 3600:
        return {"version": hit.get("version"), "images": list(hit.get("images") or []), "source": hit.get("source")}
    amap = app_map_cache if app_map_cache is not None else app_map.load_map(client)
    versions, images = _dockerfile_versions(client, repo, amap)
    if versions:
        v = min(versions)
        res = {"version": f"{v[0]}.{v[1]}", "images": images, "source": "dockerfile"}
    else:
        cv = _composer_version(repo_root)
        res = ({"version": f"{cv[0]}.{cv[1]}", "images": [], "source": "composer"} if cv
               else {"version": None, "images": [], "source": None})
    try:
        cached.setdefault("repos", {})[repo] = {**res, "built_at": time.time()}
        cache_path.write_text(yaml.safe_dump(cached, allow_unicode=True), encoding="utf-8")
    except OSError as e:
        log.warning(f"⚠️ php_modern: hedef sürüm önbelleği yazılamadı: {e}")
    return res


# ── Tarama ───────────────────────────────────────────────────────────────

SCAN_PHP = Path(__file__).resolve().parent / "php_modern_scan.php"
DEFAULT_EXCLUDE = "vendor,node_modules,app/Themes,tmp,webroot,public,storage,cache,Test,tests"
MODULE_DEPTH = 2


def module_of(rel_path: str, depth: int = MODULE_DEPTH) -> str:
    parts = rel_path.replace("\\", "/").split("/")
    dirs = parts[:-1]
    return "/".join(dirs[:depth]) if dirs else "."


def _head_sha(root: Path) -> str:
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=20)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def scan_repo(repo_root: Path, *, exclude: list[str] | None = None, php_bin: str = "php", timeout: int = 300) -> dict:
    """php_modern_scan.php'yi koştur. php yok / hata / bozuk JSON → QualitySourceError
    (kısmi veriyle liste gösterilmez)."""
    import subprocess
    from agile_sdlc_crew.quality.model import QualitySourceError
    root = Path(repo_root)
    if not root.is_dir():
        raise QualitySourceError(f"Repo dizini yok: {root}")
    ex = DEFAULT_EXCLUDE.split(",") if exclude is None else list(exclude)
    try:
        r = subprocess.run([php_bin, "-d", "display_errors=stderr", str(SCAN_PHP), str(root), ",".join(ex)],
                           capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise QualitySourceError(f"PHP bulunamadı ({php_bin}) — CREW_PHP_BIN ayarlayın") from e
    except subprocess.TimeoutExpired as e:
        raise QualitySourceError(f"Tarama {timeout} sn içinde bitmedi") from e
    if r.returncode != 0:
        raise QualitySourceError(f"Tarayıcı hata verdi (kod {r.returncode}): {(r.stderr or r.stdout)[-400:]}")
    try:
        data = json.loads(r.stdout)
    except ValueError as e:
        raise QualitySourceError(f"Tarayıcı çıktısı okunamadı: {r.stdout[:200]!r}") from e
    hits = []
    for h in data.get("hits") or []:
        h = dict(h)
        h["module"] = module_of(h.get("file", ""))
        hits.append(h)
    return {"files": int(data.get("files") or 0), "sha": _head_sha(root), "hits": hits,
            "adoption": data.get("adoption") or {}, "errors": data.get("errors") or []}
