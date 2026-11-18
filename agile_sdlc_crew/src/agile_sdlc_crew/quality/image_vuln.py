"""Taban imaj güvenliği: üretim servislerinin dayandığı imajlarda bilinen açık, geride kalmış yama ya da biten destek.

Spec: language-upgrade-design.md, "Ek 2026-11-18: taban imaj güvenlik uyarısı". Yayınlanmış imajın kendisi taranmaz
(registry erişimi yok): kendi taban imajımız `base-images@production`'daki Dockerfile'ın `FROM` satırıyla dayanak
resmî imaja indirilir, o imaj taranır. Sonuç alt sınırdır (bizim eklediğimiz katmanlar görülmez).

Üç işaret: Trivy (düzeltmesi yayınlanmış CRITICAL/HIGH), yama geride (Docker Hub), destek bitmiş (endoflife.date).
Bilinmeyen sessizce "temiz" sayılmaz: `scan.state` nedeni taşır. İmaj adı repo içeriğidir (güvenilmez): alt sürece
gitmeden `valid_image` ile doğrulanır; kabuk yok, `verify._env()`, zaman aşımı, cwd repo dışı."""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger("pipeline")

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
CACHE_DIR = _CONFIG_DIR / "quality_image_vuln_cache"
TRIVY_CACHE = Path.home() / ".cache" / "tempo-trivy"    # ~1.7 GB veritabanı: repo içinde DEĞİL
IGNORE_PATH = _CONFIG_DIR / "image_vuln.yaml"           # yok sayılan paketler (linux-libc-dev: çekirdek başlıkları)
PROD_BRANCH = "production"                             # üretim imajları bu daldan üretildi (yaml'daki staging değil)
OWN_REGISTRY = "prod-ar"                                # kendi imajımız: `…/prod-ar/<dizin>:<etiket>`

_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*(:[\w][\w.-]*)?$", re.ASCII)
_OWN_RE = re.compile(rf"^(?:[^/\s]+/)+{OWN_REGISTRY}/([a-z0-9][a-z0-9._-]*):([\w][\w.-]*)$", re.ASCII)
_VER_RE = re.compile(r"^(\d+(?:\.\d+){0,2})", re.ASCII)
_ARG_RE = re.compile(r"""^\s*ARG\s+(\w+)\s*=\s*(?:"([^"]*)"|'([^']*)'|(\S*))\s*$""", re.I)
_FROM_RE = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*(\S+)", re.I)
_LANG = {"php": "php", "python": "python", "golang": "go", "node": "node"}
_HUB = {"php": "php", "python": "python", "go": "golang", "node": "node"}
_EOL_PRODUCT = {"php": "php", "python": "python", "go": "go", "node": "nodejs"}
_BARE_OFFICIAL = frozenset({"debian", "ubuntu", "alpine", "busybox"})   # etiketsiz ama gerçek imaj (scratch değil)
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


def external_registry(image: str) -> bool:
    """İmaj bir kayıt deposu sunucusu adlandırıyor mu (ilk yol bileşeni `.` / `:` içerir ya da `localhost`)?
    Kapsam yalnız herkese açık Docker Hub imajıdır; böyle bir ad taranmaz (sunucudan keyfi adrese çıkış olmasın)."""
    if "/" not in image:
        return False
    first = image.split("/", 1)[0]
    return "." in first or ":" in first or first == "localhost"


def _why_invalid(ref: str) -> str:
    if "@" in ref:
        return "özetle sabitlenmiş imaj taranmıyor"
    if "$" in ref or "{" in ref:
        return "ARG varsayılanı yok / ${X:-…} desteklenmiyor"
    return "geçersiz imaj adı"


def _lang_version(image: str) -> tuple[str, str]:
    base, _, tag = image.rpartition(":") if ":" in image.rsplit("/", 1)[-1] else (image, "", "")
    if external_registry(image) or ("/" in base and not base.startswith("library/") or base.count("/") > 1):
        return "", ""                       # yalnız resmî kitaplık adları (`php`, `library/php`); `evil/php` php değildir
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
                out = {"image": image, "error": _why_invalid(ref), "dir": d}
        resolved[image] = out
        return out

    for items in (index or {}).values():
        for item in items or []:
            region, _, path = str(item.get("src") or "").partition("/")
            for raw in item.get("images") or []:
                if not isinstance(raw, str) or not raw:
                    continue
                # `build_index` `FROM --platform=… img` satırında bayrağı imaj sanır; gerçek imaj orada kaybolur
                # (`build_index` değişmez: yükseltme özelliği çıktısına dayanır). Bayrak bir imaj değildir: atlanır.
                if raw.startswith("-"):
                    continue
                m = _OWN_RE.match(raw)
                if m:
                    r = own(raw, m.group(1))
                    image, error, via = r["image"], r["error"], r["dir"]
                else:
                    # Çok aşamalı derlemenin takma adı / etiketsiz ad (`base`, `builder`) bir imaj değildir;
                    # yalnız bilinen resmî tabanlar (`debian`) etiketsiz de imajdır.
                    if ":" not in raw and "/" not in raw and "@" not in raw and raw not in _BARE_OFFICIAL:
                        continue
                    image, error, via = raw, "" if valid_image(raw) else _why_invalid(raw), ""
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
        # Dizin burada OLUŞTURULMAZ (yalnız "tarayıcı var mı" sorusu yan etki bırakmasın); tarama çalışırken açılır.
        return [docker, "run", "--rm", "--name", f"tempo-trivy-{uuid.uuid4().hex[:12]}",
                "-v", f"{TRIVY_CACHE}:/root/.cache/", "aquasec/trivy:latest"]
    return None


def _run(cmd: list[str], timeout: int) -> tuple[int, str]:
    from agile_sdlc_crew import verify
    with tempfile.TemporaryDirectory(prefix="tempo-trivy-") as cwd:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=verify._env(),
                           shell=False)
    return r.returncode, r.stdout or ""


def _docker_rm(base: list[str]) -> None:
    """Zaman aşımında docker kabı arkada kalmasın: `docker rm -f <ad>` (en iyi çaba, liste argv)."""
    if "--name" not in base:
        return
    name = base[base.index("--name") + 1]
    if not re.fullmatch(r"tempo-trivy-[0-9a-f]{12}", name):
        return
    try:
        from agile_sdlc_crew import verify
        subprocess.run([base[0], "rm", "-f", name], capture_output=True, timeout=30, env=verify._env(),
                       shell=False)
    except Exception as e:  # noqa: BLE001
        log.warning(f"⚠️ image_vuln: kap silinemedi ({type(e).__name__})")


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
                                  "severity": sev, "cves": [], "critical": 0, "high": 0}
            elif _SEV_RANK[sev] > _SEV_RANK[p["severity"]]:
                p.update(severity=sev, installed=str(v.get("InstalledVersion") or ""), fixed=fixed)
            p["critical" if sev == "CRITICAL" else "high"] += 1     # yok sayma (ignore_packages) için paket başına sayım
            if cve and len(p["cves"]) < MAX_CVES and cve not in p["cves"]:
                p["cves"].append(cve)
    out["packages"] = sorted(pkgs.values(), key=lambda p: (-_SEV_RANK[p["severity"]], p["name"]))
    return out


def scan_image(image: str, *, run=None, timeout: int = 600) -> dict:
    """Trivy ile tara. `run(cmd, timeout) -> (dönüş_kodu, stdout)` enjekte edilebilir. Yalnız `FixedVersion` dolu
    CRITICAL/HIGH sayılır; `critical` / `high` açık (CVE, paket) sayısıdır, `packages` paket başına en yüksek önemdir."""
    if not valid_image(image):
        return _empty_scan("geçersiz imaj")
    if external_registry(image):
        return _empty_scan("harici kayıt deposu")
    base = trivy_cmd()
    if not base:
        return _empty_scan("tarayıcı yok")
    if any(a.startswith(f"{TRIVY_CACHE}:") for a in base):
        try:
            TRIVY_CACHE.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            return _empty_scan("tarama başarısız")
    cmd = [*base, "image", "--quiet", "--timeout", f"{max(timeout - 15, 30)}s", "--platform", "linux/amd64",
           "--scanners", "vuln", "--severity", "HIGH,CRITICAL", "--format", "json", image]
    try:
        rc, stdout = (run or _run)(cmd, timeout)
    except subprocess.TimeoutExpired:
        _docker_rm(base)
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


_EOL_DOWN: dict[str, float] = {}      # ürün → bu ana kadar (monotonic) yeniden denenmez
EOL_DOWN_S = 300


def eol(lang: str, version: str, *, fetch=None, today: date | None = None, clock=time.monotonic) -> dict | None:
    """endoflife.date'e göre sürümün destek tarihi geçmişse `{"eol": "2025-12-31"}` (tarih bilinmiyorsa `""`).
    Yanıt güvenilmezdir; okunamadı / tanınmadı → None (bilinmiyor, uyarı değil)."""
    product = _EOL_PRODUCT.get(lang)
    parts = _ints(version)
    if not product or not parts or (lang != "node" and len(parts) < 2):
        return None
    cycle = str(parts[0]) if lang == "node" else f"{parts[0]}.{parts[1]}"
    hit = _EOL_CACHE.get(product)
    if hit and clock() - hit[0] < EOL_TTL_S:
        rows = hit[1]
    else:
        if clock() < _EOL_DOWN.get(product, 0.0):
            return None
        try:
            rows = (fetch or _eol_fetch)(f"https://endoflife.date/api/{product}.json")
        except Exception as e:  # noqa: BLE001
            log.warning(f"⚠️ image_vuln: endoflife.date okunamadı ({type(e).__name__})")
            _EOL_DOWN[product] = clock() + EOL_DOWN_S
            return None
        if not isinstance(rows, list):
            _EOL_DOWN[product] = clock() + EOL_DOWN_S
            return None
        _EOL_CACHE[product] = (clock(), rows)
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
    # Önbellek dosyası güvenilmezdir (bozuk / elle düzenlenmiş olabilir): biçim tutmazsa yeniden taranır.
    if not isinstance(doc, dict) or doc.get("image") != image:
        return None
    built, scan = doc.get("built_at"), doc.get("scan")
    if isinstance(built, bool) or not isinstance(built, (int, float)) or not isinstance(scan, dict):
        return None
    if (scan.get("state") != "tamam" or not isinstance(scan.get("packages"), list)
            or not all(isinstance(scan.get(k), int) and not isinstance(scan.get(k), bool) for k in ("critical", "high"))
            or not all(isinstance(p, dict) for p in scan["packages"])):
        return None
    if not (ttl_h > 0 and 0 <= now - built < ttl_h * 3600):
        return None
    return {**scan, "os": str(scan.get("os") or ""), "created": str(scan.get("created") or "")}


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
    if scan["state"] != "tamam":
        return "bilinmiyor", actions
    parts = _ints(rec["version"])
    if rec["lang"] and parts and len(parts) < 3:
        # İki parçalı etiket (`python:3.11-slim`): yama sürümü bilinemez; "temiz" denmez.
        actions.append("yama sürümü belirsiz (etiket iki parçalı; imaj yeniden çekildiğinde güncellenir)")
        return "bilgi", actions
    return "temiz", actions


def load_ignore(path: Path | None = None) -> set[str]:
    """`config/image_vuln.yaml` → yok sayılan paket adları. Bozuk / beklenmedik biçim / dosya yok → boş küme
    (çökmez; bozuksa uyarı loglanır): yanlış yaml hiçbir bulguyu sessizce gizlemez."""
    import yaml
    p = Path(path) if path else IGNORE_PATH
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return set()
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        log.warning(f"⚠️ image_vuln: {p.name} okunamadı, paket yok sayma kapalı ({type(e).__name__})")
        return set()
    names = doc.get("ignore_packages") if isinstance(doc, dict) else None
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        log.warning(f"⚠️ image_vuln: {p.name} biçimi geçersiz (ignore_packages: [paket, …]); paket yok sayma kapalı")
        return set()
    return {n.strip() for n in names if n.strip()}


def _pkg_count(p: dict, kind: str) -> int:
    v = p.get(kind)
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    return 1 if str(p.get("severity") or "").upper() == kind.upper() else 0


def _counts_missing(scan: dict, ignore: set[str]) -> bool:
    """Eski biçimli önbellek: yok sayılan pakette paket başına sayım yok → doğru düşüm için yeniden taranmalı."""
    return any(isinstance(p, dict) and p.get("name") in ignore
               and not all(isinstance(p.get(k), int) and not isinstance(p.get(k), bool) for k in ("critical", "high"))
               for p in scan.get("packages") or [])


def apply_ignore(scan: dict, ignore: set[str]) -> dict:
    """Ham taramadan yok sayılan paketleri süzer: `critical` / `high` ve `packages` onlarsız; bulgular `ignored`
    alanında sayılır, paket adlarıyla (gizlenmez). Ham sözlük değişmez (önbellek ham kalır)."""
    pkgs = [p for p in scan.get("packages") or [] if isinstance(p, dict)]
    hit = [p for p in pkgs if p.get("name") in ignore]
    ic = sum(_pkg_count(p, "critical") for p in hit)
    ih = sum(_pkg_count(p, "high") for p in hit)
    return {**scan, "critical": max(0, int(scan.get("critical") or 0) - ic), "high": max(0, int(scan.get("high") or 0) - ih),
            "packages": [p for p in pkgs if p.get("name") not in ignore],
            "ignored": {"critical": ic, "high": ih, "packages": sorted({str(p["name"]) for p in hit})}}


def report(client, *, index: dict | None = None, force: bool = False, scan_fn=None, resolver=None, eol_fn=None,
           read=None, cache_dir: Path | None = None, ttl_h: float | None = None, now: float | None = None,
           today: date | None = None, ignore_packages: set[str] | None = None) -> dict:
    """Envanter + üç işaret; imaj başına tarama sonucu `CACHE_DIR`'de (TTL `CREW_TECHDEBT_IMAGE_VULN_SCAN_H`).
    Önbellek HAM taramayı tutar; yok sayılan paketler (`config/image_vuln.yaml`) rapor kurulurken süzülür."""
    from agile_sdlc_crew.quality import modern
    if index is None:
        index = modern.build_index(client)
    ignore = load_ignore() if ignore_packages is None else set(ignore_packages)
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
        elif external_registry(rec["image"]):
            scan = _empty_scan("harici kayıt deposu")
            scan["reason"] = "yalnız herkese açık Docker Hub imajı taranır; kayıt deposu adlı imaj taranmaz"
        else:
            scan = None if force else _cache_read(cache_dir, rec["image"], ttl, clock)
            if scan is not None and _counts_missing(scan, ignore):
                scan = None
            if scan is None:
                scan = scan_fn(rec["image"])
                if scan.get("state") == "tamam":
                    _cache_write(cache_dir, rec["image"], scan, clock)
        rec["scan"] = apply_ignore(scan, ignore)
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


# ── Uç: önbellekten dön, taramayı arka planda yap ────────────────────────

SOURCE = "image_vuln"
TASK_REPO = "base-images"           # giderme (yeniden üretim) bu repoda yapılır; Task açıklamasının "Repo:" satırı
TASK_TAG = "tempo-image-vuln"
LIMIT = ("Yayınlanmış imaj değil, dayandığı resmî imaj tarandı; kendi eklediğimiz katmanlar ve derleme anında "
         "kurulan yamalar görülmez — sonuç alt sınırdır.")
RETRY_S = 600                       # taranamayan imaj için son sonuç bu süre taze sayılır (GET döngüsü kurulmasın)

_STATE_LOCK = threading.Lock()
_RUN: dict = {"running": False, "done_at": 0.0, "report": None}     # aynı anda tek tarama


def _begin() -> bool:
    with _STATE_LOCK:
        if _RUN["running"]:
            return False
        _RUN["running"] = True
        return True


def _end(rep: dict | None) -> None:
    with _STATE_LOCK:
        _RUN["running"] = False
        if rep is not None:
            _RUN.update(done_at=time.time(), report=rep)


def _spawn_thread(fn) -> None:
    threading.Thread(target=fn, daemon=True, name="image-vuln-scan").start()


def serve(client, *, force: bool = False, spawn=None, **kw) -> dict:
    """Karta dönen rapor. Asla taramada beklemez: önbellekteki kısmı hemen döner; önbellek eksikse (ya da `force`)
    taramayı arka plan iş parçacığında başlatır ve `scanning: true` ile eldekini verir. Aynı anda tek tarama.
    `kw` `report`'a gider (enjeksiyon: `scan_fn`, `cache_dir`, `index`, …)."""
    snap_kw = {k: v for k, v in kw.items() if k != "scan_fn"}
    snap = report(client, force=False, scan_fn=lambda image: _empty_scan("bekliyor"), **snap_kw)
    pending = sum(1 for r in snap["images"] if r["scan"]["state"] == "bekliyor")
    with _STATE_LOCK:
        running, last = _RUN["running"], _RUN["report"]
        fresh = last is not None and time.time() - _RUN["done_at"] < RETRY_S
    if running:
        return {**snap, "scanning": True}
    if not force and pending == 0:
        return {**snap, "scanning": False}
    if not force and fresh:
        return {**last, "scanning": False}        # son tarama bitti ama bu imajlar taranamadı: sonuç, döngü değil
    if not _begin():
        return {**snap, "scanning": True}

    def job():
        rep = None
        try:
            rep = report(client, force=force, **kw)
        except Exception as e:  # noqa: BLE001
            log.warning(f"⚠️ image_vuln: arka plan taraması başarısız: {str(e)[:300]}")
        finally:
            _end(rep)
    try:
        (spawn or _spawn_thread)(job)
    except Exception:
        _end(None)
        raise
    return {**snap, "scanning": True}


# ── Yoklayıcı ────────────────────────────────────────────────────────────

_POLLER: threading.Thread | None = None
_POLLER_LOCK = threading.Lock()


def _tick(get_client) -> None:
    """Günlük tur: `report(force=True)` + `alert`. Ayarlar her turda okunur; başka tarama sürüyorsa atlanır."""
    from agile_sdlc_crew import pipeline_config as pc
    if not (pc.get("CREW_TECHDEBT") and pc.get("CREW_TECHDEBT_IMAGE_VULN")):
        return
    if not _begin():
        log.info("image_vuln: tarama sürüyor, günlük tur atlandı")
        return
    rep = None
    try:
        from agile_sdlc_crew.quality import intake
        client = get_client()
        rep = report(client, force=True)
        team = str(getattr(client, "team", "") or "")
        results = alert(client, rep, team=team, deps=intake.default_deps())
        if pc.get("CREW_TECHDEBT_IMAGE_VULN_FIX"):
            try:                         # giderme hatası turu düşürmez; uyarı zaten yapıldı
                _fix_round(client, rep, results, team=team)
            except Exception as e:  # noqa: BLE001
                log.warning(f"⚠️ image_vuln: giderme turu hata verdi: {str(e)[:300]}")
    finally:
        _end(rep)


def _fix_round(client, rep: dict, results: list[dict] | None, *, team: str, index: dict | None = None, resolver=None,
               apply_fn=None) -> list[dict]:
    """Uyarının üretildiği turda gidermeyi de başlatır: `kritik` / `yüksek` her imaj için `image_fix.apply`. Yürüyen
    eylem varsa sonuç (PR / izleme bağlantıları) imajın uyarı işine yorum olarak eklenir. Bir imajın hatası
    ötekilerini durdurmaz."""
    from agile_sdlc_crew.quality import image_fix, modern, upgrade_registry, upgrade_track
    recs = [r for r in rep.get("images") or [] if r.get("level") in ("kritik", "yüksek")]
    if not recs:
        return []
    if index is None:
        index = modern.build_index(client, max_age_h=0)     # yazmadan önce yollar taze index'ten türetilir
    resolver = resolver or upgrade_registry.default()
    apply_fn = apply_fn or image_fix.apply
    wi = {r["image"]: r["wi"] for r in results or [] if r.get("wi")}
    out = []
    for rec in recs:
        try:
            acts = apply_fn(client, rec, team=team, index=index, resolver=resolver)
        except Exception as e:  # noqa: BLE001
            log.warning(f"⚠️ image_vuln: {rec['image']} gidermesi başarısız: {str(e)[:300]}")
            continue
        out.append({"image": rec["image"], "actions": image_fix.public(acts)})
        if wi.get(rec["image"]) and any(a.get("ran") for a in acts):
            try:
                client.add_comment(int(wi[rec["image"]]), image_fix.comment_html(acts) + upgrade_track.SIGNATURE)
            except Exception as e:  # noqa: BLE001 — PR'lar zaten açıldı
                log.warning(f"⚠️ image_vuln: giderme yorumu yazılamadı: {str(e)[:200]}")
    return out


def attach_fix(client, rep: dict, *, index: dict | None = None, resolver=None) -> dict:
    """Rapor kayıtlarına `fix` (yazmasız `image_fix.plan`) ekler. Okunamayan durum belirsiz eylem olur; hiçbir hata
    raporu düşürmez."""
    from agile_sdlc_crew.quality import image_fix, modern, upgrade_registry
    try:
        if index is None:
            index = modern.build_index(client)
        resolver = resolver or upgrade_registry.default()
    except Exception as e:  # noqa: BLE001
        log.warning(f"⚠️ image_vuln: giderme planı kurulamadı: {str(e)[:200]}")
        return rep
    for rec in rep.get("images") or []:
        try:
            rec["fix"] = image_fix.public(image_fix.plan(client, rec, index=index, resolver=resolver))
        except Exception as e:  # noqa: BLE001
            log.warning(f"⚠️ image_vuln: {rec.get('image')} giderme planı başarısız: {str(e)[:200]}")
    return rep


def start_poller(get_client, *, interval_h, sleep=None, tick=None,
                 stop_event: threading.Event | None = None) -> threading.Thread:
    """Süreç başına TEK daemon thread (ikinci çağrı aynısını döner). İlk tur bir aralık sonra; `interval_h` sayı ya
    da her turda okunan çağrılabilir (saat, en az 1). Bir turun hatası thread'i durdurmaz; pipeline işlerinden
    ayrı thread'de koşar."""
    global _POLLER
    with _POLLER_LOCK:
        if _POLLER is not None and _POLLER.is_alive():
            return _POLLER
        sleep = sleep or time.sleep
        tick = tick or _tick
        stop_event = stop_event or threading.Event()

        def hours() -> float:
            try:
                v = interval_h() if callable(interval_h) else interval_h
                return max(1.0, float(v or 24))
            except Exception:  # noqa: BLE001
                return 24.0

        def loop():
            while not stop_event.is_set():
                sleep(hours() * 3600)
                if stop_event.is_set():
                    break
                try:
                    tick(get_client)
                except Exception as e:  # noqa: BLE001
                    log.warning(f"⚠️ image_vuln: günlük tur hata verdi: {str(e)[:300]}")

        _POLLER = threading.Thread(target=loop, daemon=True, name="image-vuln-poller")
        _POLLER.start()
        return _POLLER


# ── Uyarı: Task + Telegram özeti ─────────────────────────────────────────

def _fp(image: str) -> str:
    from agile_sdlc_crew.quality import rank
    return rank.cap_fingerprint(f"{SOURCE}:{image}")


def _signals(rec: dict) -> list[str]:
    sc = rec.get("scan") or {}
    out = []
    if sc.get("critical"):
        out.append("critical")
    if sc.get("high"):
        out.append("high")
    if rec.get("patch"):
        out.append("patch")
    if rec.get("eol"):
        out.append("eol")
    return out


def _alert_rank(rec: dict) -> tuple:
    """Önce destek bitmiş, sonra düzeltmeli kritik sayısı (çok olan önce), sonra kullanan servis sayısı."""
    users = sum(len(p) for p in (rec.get("users") or {}).values())
    return (0 if rec.get("eol") else 1, -int((rec.get("scan") or {}).get("critical") or 0), -users, rec["image"])


def _e(s) -> str:
    from agile_sdlc_crew.quality import rank
    return html.escape(rank.defang(str(s if s is not None else "")))


def task_body(rec: dict) -> str:
    """Task gövdesi: işaretler, ilk 15 paket, kullanan servisler (bölge başına), önerilen eylem, sınır cümlesi.
    İmaj / paket adları güvenilmez metindir: hepsi kaçışlanır."""
    sc = rec.get("scan") or {}
    sig = []
    if sc.get("critical") or sc.get("high"):
        sig.append(f"Düzeltmesi yayınlanmış açık: {int(sc.get('critical') or 0)} kritik, {int(sc.get('high') or 0)} yüksek")
    if rec.get("patch"):
        sig.append(f"Yama geride: aynı minörün son yaması {_e(rec['patch'].get('latest'))} (kullanılan {_e(rec.get('version'))})")
    if rec.get("eol"):
        d = rec["eol"].get("eol")
        sig.append(f"Destek bitti ({_e(d)})" if d else "Destek bitti")
    parts = [f"<p><b>Taban imaj:</b> <code>{_e(rec['image'])}</code> · seviye: {_e(rec.get('level'))}"
             + (f" · işletim sistemi: {_e(sc.get('os'))}" if sc.get("os") else "") + "</p>",
             "<p><b>İşaretler:</b></p><ul>" + "".join(f"<li>{x}</li>" for x in sig) + "</ul>"]
    ig = sc.get("ignored") or {}
    if ig.get("packages"):
        parts.append(f"<p><small>Yok sayılan paketler (sayıma girmedi): {_e(', '.join(ig['packages']))} — "
                     f"{int(ig.get('critical') or 0)} kritik, {int(ig.get('high') or 0)} yüksek</small></p>")
    pk = sc.get("packages") or []
    if pk:
        rows = "".join(
            f"<li><code>{_e(p.get('name'))}</code> {_e(p.get('installed'))} → {_e(p.get('fixed'))} "
            f"({_e(p.get('severity'))}{', ' + _e(', '.join(p.get('cves') or [])) if p.get('cves') else ''})</li>"
            for p in pk[:15])
        more = f"<p><small>… ve {len(pk) - 15} paket daha</small></p>" if len(pk) > 15 else ""
        parts.append(f"<p><b>Paketler (ilk 15):</b></p><ul>{rows}</ul>{more}")
    users = "".join(f"<li><b>{_e(r)}</b>: {_e(', '.join(p))}</li>" for r, p in (rec.get("users") or {}).items())
    if users:
        parts.append(f"<p><b>Kullanan servisler:</b></p><ul>{users}</ul>")
    parts.append("<p><b>Önerilen eylem:</b> " + _e("; ".join(rec.get("actions") or [])) + "</p>")
    parts.append(f"<p><i>{LIMIT}</i></p>")
    return "".join(parts)


def task_group(rec: dict):
    """Teknik borç hattının Task'ı: needs-human, parmak izi `image_vuln:<imaj>`, ek etiket `tempo-image-vuln`."""
    from agile_sdlc_crew.quality.model import Finding, Group
    crit = int((rec.get("scan") or {}).get("critical") or 0)
    return Group(source=SOURCE, repo=TASK_REPO, title=f"Taban imaj güvenliği: {rec['image']}",
                 fingerprints=[_fp(rec["image"])],
                 findings=[Finding(source=SOURCE, repo=TASK_REPO, count=max(1, crit))],
                 score=float(crit), sp=1, needs_human=True,
                 human_reason="İmajın yeniden üretilmesi / sürüm yükseltmesi DevOps ve insan kararıdır; Tempo imaj üretmez.",
                 extra_tags=[TASK_TAG], body_html=task_body(rec))


def _open_task(client, rec: dict, *, team: str, deps) -> dict:
    """Mevcut teknik borç yardımcılarıyla (story + Task + SP + kayıt) bir needs-human Task açar."""
    from agile_sdlc_crew.quality import intake, php_modern
    with intake._LOCK:
        out = php_modern._open_and_report(client, [task_group(rec)], [], repo=TASK_REPO, source=SOURCE, team=team,
                                          sprint="", task_cfg=deps.cfg(), deps=deps, head={},
                                          log_line="🛡️ Taban imaj güvenliği")
    row = next(iter((out.get("human_tasks") or []) + (out.get("tasks") or [])), {})
    if not row.get("id"):
        raise RuntimeError(row.get("error") or "Task açılamadı")
    return {"id": row["id"], "url": intake._wi_url(client, row["id"])}


def _notified_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / "notified.json"      # imaj önbellek dosyaları 40 hex + .json: çakışmaz


def _notify_key(crit: list[dict]) -> str:
    items = sorted([r["image"], _signals(r)] for r in crit)
    return hashlib.sha1(json.dumps(items, ensure_ascii=False).encode("utf-8")).hexdigest() if items else ""


def _summary_text(images: list[dict], crit: list[dict]) -> str:
    n: dict = {}
    for r in images:
        n[r["level"]] = n.get(r["level"], 0) + 1
    top = sorted(crit, key=_alert_rank)[:5]
    lines = ["Tempo — taban imaj güvenliği",
             f"Kritik: {n.get('kritik', 0)} · Yüksek: {n.get('yüksek', 0)} · Bilinmiyor: {n.get('bilinmiyor', 0)} · "
             f"Bilgi/temiz: {n.get('bilgi', 0) + n.get('temiz', 0)}", "", "En önemli imajlar:"]
    for r in top:
        sc = r["scan"]
        lines.append(f"• {r['image']} — {'; '.join(r.get('actions') or ['inceleme'])} "
                     f"[{sc.get('critical', 0)} kritik, {sc.get('high', 0)} yüksek]")
    if len(crit) > len(top):
        lines.append(f"… ve {len(crit) - len(top)} imaj daha (kartta)")
    lines += ["", "Yayınlanmış imaj değil, dayandığı imaj tarandı; sonuç alt sınırdır."]
    return "\n".join(lines)


def alert(client, rep: dict, *, team: str, deps, open_fn=None, notify_fn=None, cache_dir: Path | None = None,
          max_tasks: int | None = None) -> list[dict]:
    """Kritik imajlar için (a) `_TASK` açıksa en çok `_MAX_TASKS` YENİ needs-human Task — sıra: destek bitmiş, düzeltmeli
    kritik sayısı, servis sayısı; açık işi olan imaj sayıya girmez, kalanlar `sırada` — ve (b) `_NOTIFY` açıksa
    TEK özet Telegram mesajı (imaj, işaret kümesi değişmedikçe yeniden gitmez). → imaj başına durum listesi."""
    images = rep.get("images") or []
    crit = [r for r in images if r.get("level") == "kritik"]
    results: list[dict] = []
    if _knob("CREW_TECHDEBT_IMAGE_VULN_TASK", False) and crit:
        if not (team or "").strip():
            log.warning("⚠️ image_vuln: takım bilinmiyor (AZURE_DEVOPS_TEAM) — Task açılmadı")
        else:
            cap = max(1, int(max_tasks if max_tasks is not None else _knob("CREW_TECHDEBT_IMAGE_VULN_MAX_TASKS", 3)))
            results = _open_tasks(client, crit, team=team, deps=deps, open_fn=open_fn or _open_task, cap=cap)
    if _knob("CREW_TECHDEBT_IMAGE_VULN_NOTIFY", False):
        _notify(images, crit, notify_fn, Path(cache_dir) if cache_dir else CACHE_DIR)
    return results


def _open_tasks(client, crit: list[dict], *, team: str, deps, open_fn, cap: int) -> list[dict]:
    from agile_sdlc_crew.quality import dedup
    try:
        board = deps.board(client)
    except Exception as e:  # noqa: BLE001 — pano okunamazsa tekilleştirilemez: yazma yok
        log.warning(f"⚠️ image_vuln: pano okunamadı, Task açılmadı: {str(e)[:200]}")
        return []
    grace = int(_knob("CREW_QUALITY_REOPEN_GRACE_DAYS", 3))
    out: list[dict] = []
    opened = 0
    for rec in sorted(crit, key=_alert_rank):
        v, wi = dedup.verdict(board.get(_fp(rec["image"])), last_seen=0.0, now=deps.now(), grace_days=grace)
        if v in ("blocked", "suppressed"):
            out.append({"image": rec["image"], "state": "açık iş", "wi": wi})
            continue
        if opened >= cap:
            out.append({"image": rec["image"], "state": "sırada", "wi": None})
            continue
        opened += 1
        try:
            res = open_fn(client, rec, team=team, deps=deps)
            out.append({"image": rec["image"], "state": "açıldı", "wi": res.get("id"), "url": res.get("url")})
        except Exception as e:  # noqa: BLE001
            log.warning(f"⚠️ image_vuln: {rec['image']} için Task açılamadı: {str(e)[:200]}")
            out.append({"image": rec["image"], "state": "hata", "wi": None, "error": str(e)[:200]})
    return out


def _notify(images: list[dict], crit: list[dict], notify_fn, cache_dir: Path) -> None:
    from agile_sdlc_crew import daily
    if notify_fn is None:
        if not daily.telegram_configured():
            return
        notify_fn = daily.send_telegram
    path = _notified_path(cache_dir)
    try:
        last = json.loads(path.read_text(encoding="utf-8")).get("key", "")
    except (OSError, ValueError, AttributeError):
        last = ""
    key = _notify_key(crit)
    if key == last:
        return
    if crit and not notify_fn(_summary_text(images, crit)):
        return                       # gitmedi: kaydedilmez, sonraki turda yeniden denenir
    try:                             # kritik kalmadıysa anahtar sıfırlanır (geri dönerse yeniden haber verilir)
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"key": key}), encoding="utf-8")
    except OSError as e:
        log.warning(f"⚠️ image_vuln: bildirim durumu yazılamadı: {e}")
