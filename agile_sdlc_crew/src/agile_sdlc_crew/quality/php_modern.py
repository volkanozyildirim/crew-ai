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
import math
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


# ── Gruplama / skor / dedup ──────────────────────────────────────────────

def fingerprint(repo: str, rule: str, where: str) -> str:
    from agile_sdlc_crew.quality.rank import cap_fingerprint
    return cap_fingerprint(f"{SOURCE}:{repo}:{rule}:{where}")


def _units(hits: list[dict], rules_by_key: dict[str, Rule], repo: str) -> tuple[list, dict]:
    """Kural birimine göre Group'lar: file birimi dosya başına, module birimi modül başına.
    report kuralları grup üretmez, yalnız özete girer."""
    from agile_sdlc_crew.quality.model import Finding, Group
    by: dict[str, Group] = {}
    summary: dict[str, dict] = {}
    for h in hits:
        r = rules_by_key.get(h.get("rule", ""))
        if r is None:
            continue
        s = summary.setdefault(r.key, {"count": 0, "_files": set(), "_modules": set(), "kind": r.kind, "auto": r.auto,
                                        "title": r.title, "since": r.since_str, "unit": r.unit, "sp_unit": r.sp_unit})
        s["count"] += 1
        s["_files"].add(h["file"])
        s["_modules"].add(h.get("module") or module_of(h["file"]))
        if r.kind == "report":
            continue
        where = h["file"] if r.unit == "file" else (h.get("module") or module_of(h["file"]))
        fp = fingerprint(repo, r.key, where)
        g = by.get(fp)
        if g is None:
            label = where.rsplit("/", 1)[-1] if r.unit == "file" else where
            g = by[fp] = Group(source=SOURCE, repo=repo, title=f"{r.title} — {label}", fingerprints=[fp], findings=[],
                               needs_human=r.kind == "structural",
                               human_reason=("Tasarım kararı: " + (r.risks[0] if r.risks else "")) if r.kind == "structural" else "")
        f = next((x for x in g.findings if x.file == h["file"]), None)
        if f is None:
            f = Finding(source=SOURCE, repo=repo, file=h["file"], rule=r.key, rule_name=r.title, count=0, lines=[],
                        message="")
            g.findings.append(f)
        f.count += 1
        f.lines.append(int(h.get("line") or 0))
        # satır metinleri: render "bulunan yerler" için (dosya:satır → metin)
        f.uris.append(f"{h.get('line') or 0}|{h.get('text') or ''}")
    for k, s in summary.items():
        s["files"] = len(s.pop("_files"))
        s["modules"] = len(s.pop("_modules"))
    for g in by.values():
        r = rules_by_key[g.findings[0].rule]
        g.score = r.weight * (1 + math.log10(max(1, g.total)))
        g.sp = r.sp_unit if r.unit == "module" else max(1, r.sp_unit)
    return list(by.values()), summary


def build_groups(scan: dict, rules: list[Rule], version: str | None, *, repo: str, board_index: dict, now: float,
                 cfg: dict) -> tuple[list, list[dict], dict]:
    """(gruplar, atlananlar, kural özeti). Yalnız hedef sürümün sunduğu kurallar; dedup board'dan;
    dosya birimleri Sonar gibi paketlenir, modül birimleri paketlenmez."""
    from agile_sdlc_crew.quality import dedup, rank
    rules_by_key = {r.key: r for r in applicable(rules, version)}
    units, summary = _units(scan.get("hits") or [], rules_by_key, repo)
    skipped: list[dict] = []
    live = []
    for u in units:
        fp = u.fingerprints[0]
        v, prev = dedup.verdict(board_index.get(fp), last_seen=now, now=now, grace_days=int(cfg.get("grace_days", 3)))
        if v == "blocked":
            skipped.append({"fingerprint": fp, "reason": f"açık WI #{prev}"})
            continue
        if v == "suppressed":
            skipped.append({"fingerprint": fp, "reason": f"iptal edilmiş WI #{prev}"})
            continue
        if v == "recurring":
            u.recurring_of = prev
        live.append(u)
    file_units = [u for u in live if rules_by_key[u.findings[0].rule].unit == "file"]
    module_units = [u for u in live if rules_by_key[u.findings[0].rule].unit == "module"]
    packed = rank.pack_units(file_units, pack_sp=int(cfg.get("pack_sp", 3)), pack_files=int(cfg.get("pack_files", 5)),
                             source=SOURCE)
    groups = packed + module_units
    groups.sort(key=lambda g: (-g.score, g.fingerprints[0]))
    return groups, skipped, summary


# ── Tarama önbelleği ─────────────────────────────────────────────────────

CACHE_DIR = _CONFIG_DIR / "quality_php_modern_cache"
_REPO_NAME = re.compile(r"^[A-Za-z0-9][\w.-]*$")


def scan_cached(repo: str, repo_root: Path, *, cache_dir: Path = CACHE_DIR, scan_fn=None, head_sha_fn=None,
                exclude: list[str] | None = None, php_bin: str = "php", ttl_h: int = 24, force: bool = False) -> dict:
    """Aynı HEAD sha + aynı katalog sürümü + TTL içinde → önbellek; aksi halde yeniden tarama.
    Dönen sözlükte `cached` alanı var."""
    if not _REPO_NAME.match(repo or "") or ".." in repo:
        raise ValueError(f"Geçersiz repo adı: {repo!r}")
    scan_fn = scan_fn or scan_repo
    head_sha_fn = head_sha_fn or _head_sha
    rv = rules_version()
    path = Path(cache_dir) / f"{repo}.json"
    sha = head_sha_fn(repo_root)
    if not force:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
        if (cached and cached.get("sha") == sha and cached.get("rules_version") == rv
                and ttl_h > 0 and time.time() - float(cached.get("built_at") or 0) < ttl_h * 3600):
            cached["cached"] = True
            return cached
    res = scan_fn(repo_root, exclude=exclude, php_bin=php_bin)
    res = {**res, "sha": sha or res.get("sha", ""), "rules_version": rv, "built_at": time.time(), "cached": False}
    try:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning(f"⚠️ php_modern: tarama önbelleği yazılamadı: {e}")
    return res
