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
import threading
from collections import Counter
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
    requires: list[str] = field(default_factory=list)            # PHP eklentileri
    requires_if: dict[str, list[str]] = field(default_factory=dict)  # bulunan yerde geçen ad → eklentiler

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
            requires=[str(x) for x in r.get("requires") or []],
            requires_if={str(k): [str(x) for x in v or []] for k, v in (r.get("requires_if") or {}).items()},
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
                   cache_path: Path = TARGETS_CACHE_PATH, max_age_h: int = 24, write_cache: bool = True) -> dict:
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
    if write_cache:
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
    from agile_sdlc_crew.quality.render import mask_secrets
    hits = []
    for h in data.get("hits") or []:
        h = dict(h)
        h["module"] = module_of(h.get("file", ""))
        # Satır metni kimlik doğrulamasız /scan yanıtına ve önbelleğe gider: `private $apiKey = '…'`
        # gibi satırlar tipsiz property kuralına takılır → daha burada maskelenir.
        h["text"] = mask_secrets(str(h.get("text") or ""))[:200]
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


# Eklentinin üretimde var olduğuna dair repo kanıtı: composer'da ext-<ad> şartı / polyfill,
# ya da uygulama kodunda (vendor hariç) o eklentinin API'si zaten kullanılıyor.
# git grep -E POSIX ERE ister (\b / \s yok)
EXT_EVIDENCE = {
    "mbstring": r"(^|[^A-Za-z0-9_])mb_[a-z_]+[[:space:]]*\(",
    "intl": r"(NumberFormatter|IntlDateFormatter|Collator|Transliterator|Normalizer)",
    "iconv": r"(^|[^A-Za-z0-9_])iconv[[:space:]]*\(",
}


def ext_available(root: Path, ext: str) -> bool:
    import subprocess
    root = Path(root)
    for name in ("composer.json", "composer.lock"):
        try:
            txt = (root / name).read_text(encoding="utf-8")
        except OSError:
            continue
        if f'"ext-{ext}"' in txt or f"symfony/polyfill-{ext}" in txt:
            return True
    pat = EXT_EVIDENCE.get(ext)
    if not pat:
        return False
    try:
        r = subprocess.run(["git", "grep", "-q", "-E", pat, "HEAD", "--", "*.php", ":!vendor", ":!node_modules"],
                           cwd=str(root), capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _ext_checker(root: Path):
    seen: dict[str, bool] = {}

    def ok(ext: str) -> bool:
        if ext not in seen:
            seen[ext] = ext_available(root, ext)
        return seen[ext]
    return ok


def _devops_requests(client, repo: str, picked: list, rows: list[dict], cfg: dict) -> list[dict]:
    """Altyapı ön koşullu Task'lar için DevOps takımına ayrı iş (eklenti başına tek iş, her
    Task'a Predecessor bağı). CREW_DEVOPS_REQUESTS kapalıysa açılmaz."""
    from agile_sdlc_crew import devops_request as dr
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.quality import intake
    if not pc.get("CREW_DEVOPS_REQUESTS"):
        return []
    out: list[dict] = []
    deps_cache: list | None = None
    for g, row in zip(picked, rows):
        if not g.infra or not row.get("id"):
            continue
        exts = sorted(i.split(":", 1)[1] for i in g.infra if i.startswith("ext:"))
        if not exts:
            continue
        try:
            if deps_cache is None:
                deps_cache = dr.deployments(client, repo)
            body = dr.ext_request_body(repo=repo, exts=exts, deps=deps_cache, source_wi=row["id"],
                                       reason=f"Teknik borç işi ({g.title}) kodda bu eklentinin fonksiyonlarını kullanacak; "
                                              "repoda eklentinin üretimde yüklü olduğuna dair kanıt yok.")
            res = dr.open_request(client, team=pc.get("CREW_DEVOPS_TEAM") or "DevOps",
                                  title=f"[{repo}] PHP eklentisi: {', '.join(exts)}", body_html=body,
                                  source_wi=row["id"], key=f"ext:{repo}:{'+'.join(exts)}",
                                  task_fields=cfg.get("task_fields") or intake.load_task_fields())
            out.append({"task": row["id"], "devops": res["id"], "created": res["created"], "exts": exts})
        except Exception as e:  # noqa: BLE001 — Task açıldı; DevOps isteği elle de açılabilir
            log.warning(f"  🛠️ DevOps isteği açılamadı ({repo} {exts}): {e}")
            out.append({"task": row["id"], "error": str(e), "exts": exts})
    return out


def _missing_exts(u, r: Rule, ext_ok) -> list[str]:
    need = list(r.requires)
    texts = " ".join(uri for f in u.findings for uri in f.uris)
    for name, exts in r.requires_if.items():
        if name in texts:
            need += exts
    return [e for e in dict.fromkeys(need) if not (ext_ok and ext_ok(e))]


def build_groups(scan: dict, rules: list[Rule], version: str | None, *, repo: str, board_index: dict, now: float,
                 cfg: dict, ext_ok=None) -> tuple[list, list[dict], dict]:
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
        # "Otomatik" yalnız KOD düzenlemesiyle çözülen iş: eklenti gibi kod dışı ön koşul
        # repoda kanıtlanamıyorsa iş insana + DevOps'a gider (kullanıcı kararı 2026-10-30).
        missing = _missing_exts(u, rules_by_key[u.findings[0].rule], ext_ok)
        if missing:
            u.needs_human = True
            u.infra = [f"ext:{e}" for e in missing]
            u.human_reason = f"Altyapı: PHP eklentisi {', '.join(missing)} imajda doğrulanamadı — DevOps isteği açılır"
        live.append(u)
    infra_units = [u for u in live if u.infra]
    live = [u for u in live if not u.infra]
    file_units = [u for u in live if rules_by_key[u.findings[0].rule].unit == "file"]
    module_units = [u for u in live if rules_by_key[u.findings[0].rule].unit == "module"] + infra_units
    packed = rank.pack_units(file_units, pack_sp=int(cfg.get("pack_sp", 3)), pack_files=int(cfg.get("pack_files", 5)),
                             source=SOURCE)
    groups = packed + module_units
    groups.sort(key=lambda g: (-g.score, g.fingerprints[0]))
    return groups, skipped, summary


# ── Tarama önbelleği ─────────────────────────────────────────────────────

CACHE_DIR = _CONFIG_DIR / "quality_php_modern_cache"
_REPO_NAME = re.compile(r"^[A-Za-z0-9][\w.-]*$")


def _inputs_key(exclude) -> str:
    """Hariç listesi ya da tarayıcı dosyası değişince önbellek geçersiz (aksi halde 24 saat
    eski hariç listesiyle Task açılır)."""
    import hashlib
    try:
        st = SCAN_PHP.stat()
        scanner = f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        scanner = "?"
    ex = ",".join(sorted(x.strip() for x in (exclude if exclude is not None else DEFAULT_EXCLUDE.split(",")) if x.strip()))
    return hashlib.sha1(f"{ex}|{scanner}".encode("utf-8")).hexdigest()[:10]


def scan_cached(repo: str, repo_root: Path, *, cache_dir: Path = CACHE_DIR, scan_fn=None, head_sha_fn=None,
                exclude: list[str] | None = None, php_bin: str = "php", ttl_h: int = 24, force: bool = False) -> dict:
    """Aynı HEAD sha + aynı katalog sürümü + TTL içinde → önbellek; aksi halde yeniden tarama.
    Dönen sözlükte `cached` alanı var."""
    if not _REPO_NAME.match(repo or "") or ".." in repo:
        raise ValueError(f"Geçersiz repo adı: {repo!r}")
    scan_fn = scan_fn or scan_repo
    head_sha_fn = head_sha_fn or _head_sha
    rv = rules_version() + "|" + _inputs_key(exclude)
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


# ── Ayarlar / bağımlılıklar ──────────────────────────────────────────────

def _knob(key: str, default):
    from agile_sdlc_crew import pipeline_config
    try:
        v = pipeline_config.get(key)
    except KeyError:
        return default
    return default if v is None or v == "" else v


_BIN = re.compile(r"^[\w./~-]+$")
_BIN_NAME = re.compile(r"^php[\d.]*$")


def _safe_bin(v: str) -> str:
    """CREW_PHP_BIN yalnız .env'den okunur (dashboard knob'u DEĞİL — sunucuda kimlik
    doğrulama yok, dashboard'dan yazılabilen bir yol sunucu kullanıcısı adına keyfi
    binary çalıştırmak olurdu; CREW_SONAR_URL ile aynı karar). Ek koruma: boşluk/;
    reddedilir ve dosya adı php / php8.4 biçiminde olmalı."""
    v = (v or "").strip()
    if not v or not _BIN.match(v) or not _BIN_NAME.match(Path(v).name):
        if v:
            log.warning(f"⚠️ CREW_PHP_BIN geçersiz ({v!r}) — 'php' kullanılıyor")
        return "php"
    return v


def load_cfg() -> dict:
    """intake.load_cfg + php_modern'e özel ayarlar."""
    import os
    from agile_sdlc_crew.quality import intake
    cfg = intake.load_cfg()
    ex = str(_knob("CREW_TECHDEBT_PATH_EXCLUDE", DEFAULT_EXCLUDE))
    cfg.update({
        "php_exclude": [x.strip() for x in ex.split(",") if x.strip()],
        "php_bin": _safe_bin(os.environ.get("CREW_PHP_BIN", "php")),
        "scan_ttl_h": int(_knob("CREW_TECHDEBT_SCAN_TTL_H", 24)),
        "max_take": int(_knob("CREW_TECHDEBT_MODERN_MAX_TAKE", 8)),
        "repos_dir": Path(os.environ.get("CREW_REPOS_DIR", "~/.crew_repos")).expanduser(),
        "targets_cache": TARGETS_CACHE_PATH,
        "scan_cache": CACHE_DIR,
    })
    return cfg


def default_deps():
    from agile_sdlc_crew.quality import intake
    return intake.default_deps()._replace(cfg=load_cfg)


def _root(cfg: dict, repo: str, deps) -> Path:
    if not _REPO_NAME.match(repo or "") or ".." in repo or not deps.repo_ok(repo):
        raise ValueError(f"Repo yerelde yok ya da adı geçersiz: {repo!r}")
    base = Path(cfg["repos_dir"])
    # Dizin adı kanonik ad: macOS'ta (base / "WEBSERVICE").is_dir() da True döner, bu yüzden
    # gerçek dizin adı listeden alınır (parmak izi her zaman aynı adla üretilsin).
    low = repo.lower()
    match = None
    for p in base.iterdir() if base.is_dir() else []:
        if not p.is_dir():
            continue
        if p.name == repo:
            return p
        if match is None and p.name.lower() == low:
            match = p
    if match is not None:
        return match
    raise ValueError(f"Repo yerelde yok: {repo!r}")


def _target(client, repo: str, root: Path, cfg: dict, app_map_cache: dict | None, *, write_cache: bool = True) -> dict:
    return target_version(client, repo, app_map_cache=app_map_cache, repo_root=root,
                          cache_path=Path(cfg["targets_cache"]), max_age_h=int(cfg.get("scan_ttl_h", 24)),
                          write_cache=write_cache)


# ── Sekme akışı: repolar → tarama → iş açma ──────────────────────────────

def gid_of(g) -> str:
    import hashlib
    return hashlib.sha1("|".join(sorted(g.fingerprints)).encode("utf-8")).hexdigest()[:12]


def _row(g, rules_by_key: dict[str, Rule]) -> dict:
    from agile_sdlc_crew.quality import intake, render
    r = rules_by_key.get(g.findings[0].rule)
    row = intake._row(g)
    locs = render._modern_locations(g)
    where = g.fingerprints[0].split(":", 3)[-1] if r and r.unit == "module" else ""
    row.update({
        "gid": gid_of(g), "rule": g.findings[0].rule, "kind": r.kind if r else "", "since": r.since_str if r else "",
        "rule_title": r.title if r else "", "summary": r.summary if r else "", "advantages": list(r.advantages) if r else [],
        "risks": list(r.risks) if r else [], "before": r.before if r else "", "after": r.after if r else "",
        "ref": r.ref if r else "", "count": g.total, "files": g.files, "module": where,
        "locations": [{"file": f, "line": ln, "text": t} for f, ln, t in locs[:20]], "recurring_of": g.recurring_of,
        "infra": list(g.infra),
    })
    return row


def repos(client, *, deps=None, app_map_cache: dict | None = None, **_ignored) -> dict:
    """Yerel PHP repoları (composer.json) + hedef sürüm. Sürümü bilinmeyen repo taranamaz."""
    deps = deps or default_deps()
    cfg = deps.cfg()
    base = Path(cfg["repos_dir"])
    items = []
    if base.is_dir():
        amap = app_map_cache
        amap_failed = False
        for p in sorted(base.iterdir(), key=lambda x: x.name.lower()):
            if not p.is_dir() or not (p / "composer.json").is_file():
                continue
            if amap is None:
                try:
                    amap = app_map.load_map(client)
                except Exception as e:  # noqa: BLE001
                    log.warning(f"⚠️ php_modern: FLODevOps eşlemesi okunamadı, composer.json'a düşülüyor: {e}")
                    amap, amap_failed = {}, True  # geçici kesinti 24 saatlik yanlış hedef olarak kalmasın
            try:
                tv = _target(client, p.name, p, cfg, amap, write_cache=not amap_failed)
            except Exception as e:  # noqa: BLE001
                tv = {"version": None, "images": [], "source": None, "error": str(e)}
            items.append({"repo": p.name, "version": tv.get("version"), "source": tv.get("source"),
                          "images": tv.get("images") or [], "scannable": bool(tv.get("version"))})
    return {"items": items}


_SCAN_LOCKS: dict[str, threading.Lock] = {}
_SCAN_LOCKS_GUARD = threading.Lock()


def _scan_lock(repo: str) -> threading.Lock:
    with _SCAN_LOCKS_GUARD:
        return _SCAN_LOCKS.setdefault(repo, threading.Lock())


def scan(client, repo: str, *, team: str = "", force: bool = False, deps=None, scan_fn=None, head_sha_fn=None,
         app_map_cache: dict | None = None) -> dict:
    """Yazma yok: hedef sürüm + (önbellekli) tarama + kurallar + dedup → kural özeti ve aday gruplar."""
    deps = deps or default_deps()
    cfg = deps.cfg()
    root = _root(cfg, repo, deps)
    repo = root.name  # kanonik: WEBSERVICE ve webservice aynı parmak izini üretsin
    tv = _target(client, repo, root, cfg, app_map_cache)
    with _scan_lock(repo):  # aynı repo için paralel 300 sn'lik php süreçleri açılmasın
        res = scan_cached(repo, root, cache_dir=Path(cfg["scan_cache"]), scan_fn=scan_fn, head_sha_fn=head_sha_fn,
                          exclude=cfg.get("php_exclude"), php_bin=cfg.get("php_bin", "php"),
                          ttl_h=int(cfg.get("scan_ttl_h", 24)), force=force)
    rules = load_rules()
    rules_by_key = {r.key: r for r in rules}
    groups, skipped, summary = build_groups(res, rules, tv["version"], repo=repo, board_index=deps.board(client),
                                            now=deps.now(), cfg=cfg, ext_ok=_ext_checker(root))
    out = {
        "repo": repo, "version": tv["version"], "version_source": tv.get("source"), "images": tv.get("images") or [],
        "sha": res.get("sha", ""), "files": res.get("files", 0), "adoption": res.get("adoption") or {},
        "cached": bool(res.get("cached")), "scanned_at": res.get("built_at"), "errors": res.get("errors") or [],
        "rules": summary,
        # auto: yalnız kod düzenlemesiyle çözülen işler; infra: kod dışı ön koşul (DevOps)
        "auto": [_row(g, rules_by_key) for g in groups if not g.needs_human],
        "infra": [_row(g, rules_by_key) for g in groups if g.needs_human and g.infra],
        "human": [_row(g, rules_by_key) for g in groups if g.needs_human and not g.infra],
        "skipped": skipped[:50],
        "skipped_counts": dict(Counter(s["reason"].split(" #")[0] for s in skipped)),
    }
    if not tv["version"]:
        out["message"] = "Hedef PHP sürümü bilinmiyor (FLODevOps imajı ve composer.json'da yok) — öneri üretilmez."
    elif not groups:
        out["message"] = "Hedef sürümün sunduğu özelliklerle ilgili öneri yok."
    return out


def take(client, repo: str, group_ids: list[str], *, team: str = "", deps=None, scan_fn=None, head_sha_fn=None,
         app_map_cache: dict | None = None) -> dict:
    """Seçilen grupları 'Teknik borç — <sprint>' story'si altında Task olarak aç; mekanik olanları
    kuyruğa al, yapısal olanlar needs-human. Seçim kilit altında taze taramaya (aynı sha) ve board'a
    karşı yeniden doğrulanır."""
    from agile_sdlc_crew import estimation
    from agile_sdlc_crew.quality import intake
    deps = deps or default_deps()
    if not intake._LOCK.acquire(blocking=False):
        raise intake.Busy("Başka bir alım / kuyruk isteği sürüyor")
    try:
        cfg = deps.cfg()
        root = _root(cfg, repo, deps)
        repo = root.name
        tv = _target(client, repo, root, cfg, app_map_cache)
        if not tv["version"]:
            raise ValueError("Hedef PHP sürümü bilinmiyor — bu repo için öneri açılamaz")
        with _scan_lock(repo):
            res = scan_cached(repo, root, cache_dir=Path(cfg["scan_cache"]), scan_fn=scan_fn, head_sha_fn=head_sha_fn,
                              exclude=cfg.get("php_exclude"), php_bin=cfg.get("php_bin", "php"),
                              ttl_h=int(cfg.get("scan_ttl_h", 24)))
        rules = load_rules()
        rules_by_key = {r.key: r for r in rules}
        groups, _skipped, _summary = build_groups(res, rules, tv["version"], repo=repo, board_index=deps.board(client),
                                                  now=deps.now(), cfg=cfg, ext_ok=_ext_checker(root))
        by_gid = {gid_of(g): g for g in groups}
        picked, skipped = [], []
        max_take = max(1, int(cfg.get("max_take", 8)))
        for gid in dict.fromkeys(str(x) for x in (group_ids or [])):
            g = by_gid.get(gid)
            if g is None:
                skipped.append({"gid": gid, "reason": "tarama değişti ya da board'da açık — yeniden tarayın"})
            elif len(picked) >= max_take:
                skipped.append({"gid": gid, "reason": f"istek başı tavan ({max_take})"})
            else:
                picked.append(g)
        if not picked:
            if skipped and all("tarama" in s["reason"] for s in skipped):
                raise ValueError("Seçim güncel taramada yok — tarama değişti, yeniden tarayın")
            return {"repo": repo, "tasks": [], "human_tasks": [], "skipped": skipped, "story": None}
        story = intake.resolve_story(client, SOURCE, None, team, cfg.get("target_sprint") or "next", lane="techdebt")
        cfg2 = {**cfg, "php_target": tv["version"]}
        rows, added = intake._open_tasks(client, picked, story, cfg2, deps, source=SOURCE, lane="techdebt", queue=True)
        if added:
            estimation.write_story_points(client, str(story["id"]), int(story["sp"] + added), logger=log.info)
        tasks = [r for r in rows if not r["needs_human"]]
        human = [r for r in rows if r["needs_human"]]
        devops = _devops_requests(client, repo, picked, rows, cfg)
        log.info(f"🧹 PHP modernleştirme ({repo}): story #{story['id']} · {len(rows)} Task · {added} SP")
        return {"repo": repo, "version": tv["version"],
                "story": {"id": story["id"], "url": intake._wi_url(client, story["id"]), "created": story["created"]},
                "tasks": tasks, "human_tasks": human, "devops": devops, "skipped": skipped,
                "total_sp": sum(r["sp"] for r in tasks if r.get("id")), "human_sp": sum(r["sp"] for r in human if r.get("id"))}
    finally:
        intake._LOCK.release()
