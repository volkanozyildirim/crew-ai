"""Taban imaj güvenliği: üretim servislerinin dayandığı imajlarda bilinen açık, geride kalmış yama ya da biten destek.

Spec: language-upgrade-design.md, "Ek 2026-11-18: taban imaj güvenlik uyarısı". Yayınlanmış imajın kendisi taranmaz
(registry erişimi yok): kendi taban imajımız `base-images@production`'daki Dockerfile'ın `FROM` satırıyla dayanak
resmî imaja indirilir, o imaj taranır. Sonuç alt sınırdır (bizim eklediğimiz katmanlar görülmez).

Üç işaret: Trivy (düzeltmesi yayınlanmış CRITICAL/HIGH), yama geride (Docker Hub), destek bitmiş (endoflife.date).
Bilinmeyen sessizce "temiz" sayılmaz: `scan.state` nedeni taşır. İmaj adı repo içeriğidir (güvenilmez): alt sürece
gitmeden `valid_image` ile doğrulanır; kabuk yok, `verify._env()`, zaman aşımı, cwd repo dışı."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger("pipeline")

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
CACHE_DIR = _CONFIG_DIR / "quality_image_vuln_cache"
TRIVY_CACHE = Path.home() / ".cache" / "tempo-trivy"    # ~1.7 GB veritabanı: repo içinde DEĞİL
PROD_BRANCH = "production"                              # üretim imajları bu daldan üretildi (yaml'daki staging değil)
OWN_REGISTRY = "prod-ar"                                # kendi imajımız: `…/prod-ar/<dizin>:<etiket>`

_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*(:[\w][\w.-]*)?$", re.ASCII)
_OWN_RE = re.compile(rf"^(?:[^/\s]+/)+{OWN_REGISTRY}/([a-z0-9][a-z0-9._-]*):([\w][\w.-]*)$", re.ASCII)
_VER_RE = re.compile(r"^(\d+(?:\.\d+){0,2})", re.ASCII)
_ARG_RE = re.compile(r"""^\s*ARG\s+(\w+)\s*=\s*(?:"([^"]*)"|'([^']*)'|(\S*))\s*$""", re.I)
_FROM_RE = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*(\S+)", re.I)
_LANG = {"php": "php", "python": "python", "golang": "go", "node": "node"}
_HUB = {"php": "php", "python": "python", "go": "golang", "node": "node"}
_EOL_PRODUCT = {"php": "php", "python": "python", "go": "go", "node": "nodejs"}
_SEV_RANK = {"CRITICAL": 2, "HIGH": 1}
_LEVEL_RANK = {"kritik": 0, "yüksek": 1, "bilgi": 2, "bilinmiyor": 3, "temiz": 4}
EOL_TTL_S = 24 * 3600
MAX_CVES = 5
NOTE = ("Yayınlanmış imaj değil, dayandığı resmî imaj taranır; bizim eklediğimiz katmanlar (nginx, filebeat, "
        "New Relic, PECL) ve derleme anında kurulan yamalar görülmez. Sonuç alt sınırdır; işletim sistemi "
        "bulguları imaj yeniden üretilirse kapanır.")


def valid_image(name: str) -> bool:
    """Alt sürece gidebilecek imaj adı: katı ASCII deseni, ≤ 200, `-` ile başlayamaz."""
    return (isinstance(name, str) and 0 < len(name) <= 200 and not name.startswith("-")
            and _IMAGE_RE.fullmatch(name) is not None)


def _knob(key: str, default):
    from agile_sdlc_crew import pipeline_config
    try:
        v = pipeline_config.get(key)
    except KeyError:
        return default
    return default if v is None or v == "" else v


# ── Envanter ─────────────────────────────────────────────────────────────

def _base_cfg() -> tuple[str, str | None]:
    from agile_sdlc_crew.quality import upgrade_image
    cfg = upgrade_image.load_cfg()
    return cfg["repo"], cfg.get("project") or None


def _resolve_from(dockerfile: str) -> tuple[str, str]:
    """İlk `FROM` imajı; ilk `FROM`'dan önceki `ARG AD=değer` varsayılanlarıyla `${AD}` / `$AD` yerine konur.
    → (imaj, hata)."""
    args: dict[str, str] = {}
    for line in dockerfile.splitlines():
        m = _FROM_RE.match(line)
        if m:
            ref = m.group(1)
            ref = re.sub(r"\$\{(\w+)\}|\$(\w+)", lambda x: args.get(x.group(1) or x.group(2), x.group(0)), ref)
            if "$" in ref or "{" in ref:
                return "", "çözülemeyen ARG: " + re.sub(r"[^\w${}.:/-]", "", ref)[:80]
            return ref, ""
        a = _ARG_RE.match(line)
        if a:
            args[a.group(1)] = next((g for g in a.groups()[1:] if g is not None), "")
    return "", "Dockerfile'da FROM satırı yok"


def _default_read(client, repo: str, project: str | None):
    def read(path: str) -> str | None:
        try:
            return client.get_file_content(repo, path, branch=PROD_BRANCH, project=project)
        except Exception:  # noqa: BLE001  — dosya yok / okunamadı
            return None
    return read


def _lang_version(image: str) -> tuple[str, str]:
    base, _, tag = image.rpartition(":") if ":" in image.rsplit("/", 1)[-1] else (image, "", "")
    lang = _LANG.get(base.rsplit("/", 1)[-1], "")
    m = _VER_RE.match(tag)
    return (lang, m.group(1)) if lang and m else (lang, "")


def inventory(client, index: dict, *, read=None) -> list[dict]:
    """Dayanak imaj başına bir kayıt (bölgeler / dizinler birleşir)."""
    repo, project = _base_cfg()
    read = read or _default_read(client, repo, project)
    resolved: dict[str, dict] = {}       # kendi imaj adı → {"image", "error", "dir"}
    recs: dict[str, dict] = {}

    def own(image: str, d: str) -> dict:
        if image in resolved:
            return resolved[image]
        txt = read(f"/{d}/Dockerfile")
        if txt is None:
            txt = read(f"/{d}/dockerfile")
        if txt is None:
            out = {"image": image, "error": f"base-images/{PROD_BRANCH}'da {d}/Dockerfile okunamadı", "dir": d}
        else:
            ref, err = _resolve_from(txt)
            out = {"image": ref or image, "error": err, "dir": d}
            if ref and not valid_image(ref):
                out = {"image": image, "error": "geçersiz imaj adı", "dir": d}
        resolved[image] = out
        return out

    for items in (index or {}).values():
        for item in items or []:
            region, _, path = str(item.get("src") or "").partition("/")
            for raw in item.get("images") or []:
                if not isinstance(raw, str) or not raw:
                    continue
                m = _OWN_RE.match(raw)
                if m:
                    r = own(raw, m.group(1))
                    image, error, via = r["image"], r["error"], r["dir"]
                else:
                    # Çok aşamalı derlemenin takma adı / etiketsiz ad (`base`, `builder`) bir imaj değildir.
                    if ":" not in raw and "/" not in raw and "@" not in raw:
                        continue
                    image, error, via = raw, "" if valid_image(raw) else "geçersiz imaj adı", ""
                    if not error and re.search(r"\$", raw):
                        error = "çözülemeyen ARG"
                if error and not m:
                    # Özgün ad kaydı: ARG'lı / geçersiz imajlar `taranamadı` olarak listelenir.
                    image = raw
                rec = recs.setdefault(image, {"image": image, "lang": "", "version": "", "via": [], "users": {},
                                              "touched": "", "error": error})
                if not rec["error"] and error:
                    rec["error"] = error
                if via and via not in rec["via"]:
                    rec["via"].append(via)
                paths = rec["users"].setdefault(region, [])
                if path not in paths:
                    paths.append(path)
    out = []
    for rec in recs.values():
        if not rec["error"]:
            rec["lang"], rec["version"] = _lang_version(rec["image"])
        rec["via"].sort()
        rec["users"] = {r: sorted(p) for r, p in sorted(rec["users"].items())}
        rec["touched"] = _touched(client, repo, project, rec["via"])
        out.append(rec)
    return out


def _touched(client, repo: str, project: str | None, dirs: list[str]) -> str:
    """Taban dizinlerine `production`'da son dokunulan tarih (YYYY-MM-DD, en yeni); bilinmiyorsa ""."""
    best = ""
    for d in dirs:
        try:
            day = str(client.get_last_commit_date(repo, f"/{d}", PROD_BRANCH, project=project) or "")[:10]
        except Exception:  # noqa: BLE001
            continue
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) and day > best:
            best = day
    return best


# ── Trivy ────────────────────────────────────────────────────────────────

def trivy_cmd(*, which=shutil.which) -> list[str] | None:
    """`CREW_TRIVY_BIN` (yalnız ortam değişkeni; mutlak yol, dosya adı `trivy…`) → PATH'te `trivy` →
    `docker run aquasec/trivy` → yoksa None."""
    env = (os.environ.get("CREW_TRIVY_BIN") or "").strip()
    if env:
        p = Path(env)
        if p.is_absolute() and p.name.startswith("trivy") and p.is_file() and os.access(p, os.X_OK):
            return [str(p)]
        log.warning("⚠️ image_vuln: CREW_TRIVY_BIN geçersiz (mutlak yol, ad 'trivy…', çalıştırılabilir olmalı)")
    found = which("trivy")
    if found:
        return [found]
    docker = which("docker")
    if docker:
        try:
            TRIVY_CACHE.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        return [docker, "run", "--rm", "-v", f"{TRIVY_CACHE}:/root/.cache/", "aquasec/trivy:latest"]
    return None


def _run(cmd: list[str], timeout: int) -> tuple[int, str]:
    from agile_sdlc_crew import verify
    with tempfile.TemporaryDirectory(prefix="tempo-trivy-") as cwd:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=verify._env(),
                           shell=False)
    return r.returncode, r.stdout or ""


def _empty_scan(state: str) -> dict:
    return {"state": state, "critical": 0, "high": 0, "packages": [], "os": "", "created": ""}


def _parse_scan(doc: dict) -> dict:
    out = _empty_scan("tamam")
    meta = doc.get("Metadata") if isinstance(doc.get("Metadata"), dict) else {}
    osd = meta.get("OS") if isinstance(meta.get("OS"), dict) else {}
    out["os"] = " ".join(str(osd[k]) for k in ("Family", "Name") if osd.get(k))
    cfg = meta.get("ImageConfig") if isinstance(meta.get("ImageConfig"), dict) else {}
    out["created"] = str(cfg.get("created") or "")[:10]
    seen: set[tuple[str, str]] = set()
    pkgs: dict[str, dict] = {}
    for res in doc.get("Results") or []:
        for v in (res.get("Vulnerabilities") or []) if isinstance(res, dict) else []:
            if not isinstance(v, dict):
                continue
            sev = str(v.get("Severity") or "").upper()
            fixed = str(v.get("FixedVersion") or "").strip()
            name = str(v.get("PkgName") or "")
            cve = str(v.get("VulnerabilityID") or "")
            if sev not in _SEV_RANK or not fixed or not name or (cve, name) in seen:
                continue
            seen.add((cve, name))
            out["critical" if sev == "CRITICAL" else "high"] += 1
            p = pkgs.get(name)
            if p is None:
                p = pkgs[name] = {"name": name, "installed": str(v.get("InstalledVersion") or ""), "fixed": fixed,
                                  "severity": sev, "cves": []}
            elif _SEV_RANK[sev] > _SEV_RANK[p["severity"]]:
                p.update(severity=sev, installed=str(v.get("InstalledVersion") or ""), fixed=fixed)
            if cve and len(p["cves"]) < MAX_CVES and cve not in p["cves"]:
                p["cves"].append(cve)
    out["packages"] = sorted(pkgs.values(), key=lambda p: (-_SEV_RANK[p["severity"]], p["name"]))
    return out


def scan_image(image: str, *, run=None, timeout: int = 600) -> dict:
    """Trivy ile tara. `run(cmd, timeout) -> (dönüş_kodu, stdout)` enjekte edilebilir. Yalnız `FixedVersion` dolu
    CRITICAL/HIGH sayılır; `critical` / `high` açık (CVE, paket) sayısıdır, `packages` paket başına en yüksek önemdir."""
    if not valid_image(image):
        return _empty_scan("geçersiz imaj")
    base = trivy_cmd()
    if not base:
        return _empty_scan("tarayıcı yok")
    cmd = [*base, "image", "--quiet", "--platform", "linux/amd64", "--scanners", "vuln",
           "--severity", "HIGH,CRITICAL", "--format", "json", image]
    try:
        rc, stdout = (run or _run)(cmd, timeout)
    except subprocess.TimeoutExpired:
        return _empty_scan("zaman aşımı")
    except Exception as e:  # noqa: BLE001
        log.warning(f"⚠️ image_vuln: tarama çalıştırılamadı ({type(e).__name__})")
        return _empty_scan("tarama başarısız")
    if rc != 0:
        return _empty_scan("tarama başarısız")
    try:
        doc = json.loads(stdout)
    except ValueError:
        return _empty_scan("tarama başarısız")
    if not isinstance(doc, dict):
        return _empty_scan("tarama başarısız")
    return _parse_scan(doc)


# ── Yama geride / destek bitmiş ──────────────────────────────────────────

def _ints(version: str) -> list[int] | None:
    if not isinstance(version, str) or not re.fullmatch(r"\d+(\.\d+)*", version, re.ASCII):
        return None
    return [int(x) for x in version.split(".")]


def patch_lag(lang: str, version: str, *, resolver=None) -> dict | None:
    """Aynı minörde Hub'daki en büyük yama kullanılandan büyükse `{"latest": "8.4.30"}`; bilinmiyorsa None.
    Yalnız üç parçalı sürümde (`8.4.27`) anlamlıdır."""
    name = _HUB.get(lang)
    parts = _ints(version)
    if not name or not parts or len(parts) != 3:
        return None
    if resolver is None:
        from agile_sdlc_crew.quality import upgrade_registry
        resolver = upgrade_registry.default()
    latest = resolver.latest_patch(name, f"{parts[0]}.{parts[1]}")
    got = _ints(latest or "")
    return {"latest": latest} if got and len(got) == 3 and got[2] > parts[2] else None


_EOL_CACHE: dict[str, tuple[float, list]] = {}


def _eol_fetch(url: str):
    import requests
    r = requests.get(url, timeout=10, headers={"User-Agent": "tempo-upgrade"})
    return r.json() if r.status_code == 200 else None


def eol(lang: str, version: str, *, fetch=None, today: date | None = None) -> dict | None:
    """endoflife.date'e göre sürümün destek tarihi geçmişse `{"eol": "2025-12-31"}` (tarih bilinmiyorsa `""`).
    Yanıt güvenilmezdir; okunamadı / tanınmadı → None (bilinmiyor, uyarı değil)."""
    product = _EOL_PRODUCT.get(lang)
    parts = _ints(version)
    if not product or not parts or (lang != "node" and len(parts) < 2):
        return None
    cycle = str(parts[0]) if lang == "node" else f"{parts[0]}.{parts[1]}"
    hit = _EOL_CACHE.get(product)
    if hit and time.monotonic() - hit[0] < EOL_TTL_S:
        rows = hit[1]
    else:
        try:
            rows = (fetch or _eol_fetch)(f"https://endoflife.date/api/{product}.json")
        except Exception as e:  # noqa: BLE001
            log.warning(f"⚠️ image_vuln: endoflife.date okunamadı ({type(e).__name__})")
            return None
        if not isinstance(rows, list):
            return None
        _EOL_CACHE[product] = (time.monotonic(), rows)
    row = next((r for r in rows if isinstance(r, dict) and str(r.get("cycle")) == cycle), None)
    if row is None:
        return None
    v = row.get("eol")
    if v is True:
        return {"eol": ""}
    if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v, re.ASCII):
        return None
    try:
        when = datetime.strptime(v, "%Y-%m-%d").date()
    except ValueError:
        return None
    return {"eol": v} if when < (today or date.today()) else None


# ── Önbellek ve rapor ────────────────────────────────────────────────────

def _cache_path(cache_dir: Path, image: str) -> Path:
    return Path(cache_dir) / f"{hashlib.sha256(image.encode('utf-8')).hexdigest()[:40]}.json"


def _cache_read(cache_dir: Path, image: str, ttl_h: float, now: float) -> dict | None:
    try:
        doc = json.loads(_cache_path(cache_dir, image).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (isinstance(doc, dict) and doc.get("image") == image and isinstance(doc.get("scan"), dict)
            and ttl_h > 0 and 0 <= now - float(doc.get("built_at") or 0) < ttl_h * 3600):
        return doc["scan"]
    return None


def _cache_write(cache_dir: Path, image: str, scan: dict, now: float) -> None:
    """Dosyadaki diğer alanlar (ör. `notified`) korunur; yalnız `scan` / `built_at` yenilenir."""
    path = _cache_path(cache_dir, image)
    try:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            doc = doc if isinstance(doc, dict) and doc.get("image") == image else {}
        except (OSError, ValueError):
            doc = {}
        doc.update(image=image, scan=scan, built_at=now)
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning(f"⚠️ image_vuln: önbellek yazılamadı: {e}")


def _judge(rec: dict) -> tuple[str, list[str]]:
    scan, patch, end = rec["scan"], rec["patch"], rec["eol"]
    crit = scan["state"] == "tamam" and scan["critical"] > 0
    high = scan["state"] == "tamam" and scan["high"] > 0
    actions: list[str] = []
    if crit or high:
        actions.append("yeniden üret")
    if patch:
        actions.append(f"yama sürümünü yükselt ({patch['latest']})")
    if end:
        actions.append(f"sürüm yükselt (destek {end['eol']}'de bitti)" if end["eol"] else "sürüm yükselt (destek bitti)")
    if crit or end:
        return "kritik", actions
    if high or patch:
        return "yüksek", actions
    return ("temiz" if scan["state"] == "tamam" else "bilinmiyor"), actions


def report(client, *, index: dict | None = None, force: bool = False, scan_fn=None, resolver=None, eol_fn=None,
           read=None, cache_dir: Path | None = None, ttl_h: float | None = None, now: float | None = None,
           today: date | None = None) -> dict:
    """Envanter + üç işaret; imaj başına tarama sonucu `CACHE_DIR`'de (TTL `CREW_TECHDEBT_IMAGE_VULN_SCAN_H`)."""
    from agile_sdlc_crew.quality import modern
    if index is None:
        index = modern.build_index(client)
    cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
    ttl = float(ttl_h if ttl_h is not None else _knob("CREW_TECHDEBT_IMAGE_VULN_SCAN_H", 24))
    clock = now if now is not None else time.time()
    scan_fn = scan_fn or scan_image
    eol_fn = eol_fn or eol
    images = []
    for rec in inventory(client, index, read=read):
        if rec["error"]:
            scan = _empty_scan("dayanak çözülemedi")
            scan["reason"] = rec["error"]
        else:
            scan = None if force else _cache_read(cache_dir, rec["image"], ttl, clock)
            if scan is None:
                scan = scan_fn(rec["image"])
                if scan.get("state") == "tamam":
                    _cache_write(cache_dir, rec["image"], scan, clock)
        rec["scan"] = scan
        rec["patch"] = patch_lag(rec["lang"], rec["version"], resolver=resolver) if rec["lang"] else None
        rec["eol"] = eol_fn(rec["lang"], rec["version"], today=today) if rec["lang"] else None
        rec["level"], rec["actions"] = _judge(rec)
        images.append(rec)
    images.sort(key=lambda r: (_LEVEL_RANK[r["level"]], -sum(len(p) for p in r["users"].values()), r["image"]))
    states = {r["scan"]["state"] for r in images}
    scanner = False if "tarayıcı yok" in states else (bool(states - {"dayanak çözülemedi", "geçersiz imaj"})
                                                      or trivy_cmd() is not None)
    return {"scanned_at": datetime.fromtimestamp(clock).isoformat(timespec="seconds"), "scanner": scanner,
            "images": images, "note": NOTE}
