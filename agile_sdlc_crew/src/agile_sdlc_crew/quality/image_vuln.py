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
_TAGV_RE = re.compile(r"\d+(?:\.\d+){0,3}", re.ASCII)       # kendi imaj etiketi (tag.txt): 1.0, 1.1
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
MAX_CRIT_CVES = 20
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


def tag_newer(have: str, latest: str) -> bool:
    """Kendi imaj etiketi sayısal olarak geride mi (`1.0` < `1.1`, `1.9` < `1.10`). Sayısal olmayan etiket karşılaştırılmaz."""
    if not (_TAGV_RE.fullmatch(str(have or "")) and _TAGV_RE.fullmatch(str(latest or ""))):
        return False
    a, b = have.split("."), latest.split(".")
    # Parça sayısı farklıysa (`1.0` ile `1.0.0`) karşılaştırılmaz: yalnız rakam değişimiyle yazılamaz, kalıcı uyarı olurdu.
    return len(a) == len(b) and tuple(int(x) for x in a) < tuple(int(x) for x in b)


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


_OWN_DIR_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,60}", re.ASCII)


def own_dirs(client) -> dict[str, str]:
    """`base-images` kökündeki taban dizinleri → okunacağı dal. Üretim dalında olan oradan, yalnız daha erken bir
    dalda olan (ör. `php-8-5` staging / preprod'da) üretime en yakın daldan okunur. Hiçbir servisin kullanmadığı
    imajlar da buradan envantere girer (kullanılmaya başlamadan güncel tutulsun). Okunamayan dal atlanır."""
    from agile_sdlc_crew.quality import upgrade_image
    cfg = upgrade_image.load_cfg()
    repo, project = cfg["repo"], cfg.get("project") or None
    flow = [b for b in (cfg.get("flow") or []) if isinstance(b, str)]
    branches = [PROD_BRANCH] + [b for b in reversed(flow) if b != PROD_BRANCH]
    out: dict[str, str] = {}
    for b in branches:
        try:
            items = client.get_items_in_path(repo, "/", branch=b, recursion_level="oneLevel", project=project)
        except Exception as e:  # noqa: BLE001
            log.warning(f"⚠️ image_vuln: base-images@{b} dizinleri okunamadı: {str(e)[:200]}")
            continue
        for it in items or []:
            name = str(it.get("path") or "").strip("/")
            if it.get("isFolder") and _OWN_DIR_RE.fullmatch(name) and ".." not in name and name not in out:
                out[name] = b
    return out


def _read_at(client, repo: str, project: str | None):
    def read(branch: str, path: str) -> str | None:
        try:
            return client.get_file_content(repo, path, branch=branch, project=project)
        except Exception:  # noqa: BLE001  — dosya yok / okunamadı
            return None
    return read


def inventory(client, index: dict, *, read=None, dirs: dict | None = None, read_at=None) -> list[dict]:
    """Dayanak imaj başına bir kayıt (bölgeler / dizinler birleşir). `dirs` (dizin → dal; varsayılan `own_dirs`):
    hiçbir servisin kullanmadığı taban dizinleri de `unused` kaydı olarak eklenir. `read` enjekte edilmişse (test)
    ve `dirs` verilmemişse kullanılmayan dizinler aranmaz."""
    repo, project = _base_cfg()
    if dirs is None and read is None:
        try:
            dirs = own_dirs(client)
        except Exception as e:  # noqa: BLE001 — kullanılmayanlar listelenemedi: kullanılanlar yine raporlanır
            log.warning(f"⚠️ image_vuln: kullanılmayan taban dizinleri okunamadı: {str(e)[:200]}")
            dirs = {}
    read_at = read_at or _read_at(client, repo, project)
    read = read or _default_read(client, repo, project)
    resolved: dict[str, dict] = {}       # kendi imaj adı → {"image", "error", "dir"}
    recs: dict[str, dict] = {}
    prod_tags: dict[str, str] = {}       # dizin → üretim dalındaki tag.txt

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
        if d not in prod_tags:
            # Dizinin üretim dalındaki etiketi: bölge repoları daha eski etiketi çekiyorsa `tag_lag` (etiket geride).
            prod_tags[d] = str(read(f"/{d}/tag.txt") or "").strip()
        resolved[image] = out
        return out

    for code_repo, items in (index or {}).items():
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
                                              "repos": {}, "tag_lag": {}, "touched": "", "error": error})
                if not rec["error"] and error:
                    rec["error"] = error
                if via and via not in rec["via"]:
                    rec["via"].append(via)
                if m and via and tag_newer(m.group(2), prod_tags.get(via, "")):
                    lag = rec["tag_lag"].setdefault(via, {"have": [], "latest": prod_tags[via]})
                    if m.group(2) not in lag["have"]:
                        lag["have"].append(m.group(2))
                paths = rec["users"].setdefault(region, [])
                if path not in paths:
                    paths.append(path)
                # etkilenen kod reposu (index anahtarı = Jenkinsfile gitOriginApp) → bölge → derleme dizinleri
                rp = rec["repos"].setdefault(str(code_repo), {}).setdefault(region, [])
                if path not in rp:
                    rp.append(path)
    # Hiçbir servisin kullanmadığı taban dizinleri: dayanağı çözülebilen ve dili bilinenler kayda girer.
    used = {d for r in recs.values() for d in r["via"]}
    for d, b in sorted((dirs or {}).items()):
        if d in used or not isinstance(b, str):
            continue
        txt = read_at(b, f"/{d}/Dockerfile")
        if txt is None:
            txt = read_at(b, f"/{d}/dockerfile")
        ref, err = _resolve_from(txt) if txt else ("", "okunamadı")
        if err or not ref or not valid_image(ref) or external_registry(ref) or not _lang_version(ref)[0]:
            continue                        # taban imaj dizini değil / çözülemedi: gürültü eklenmez
        rec = recs.get(ref)
        if rec is None:
            rec = recs[ref] = {"image": ref, "lang": "", "version": "", "via": [], "users": {}, "repos": {},
                               "tag_lag": {}, "touched": "", "error": "", "unused": True}
        rec["via"].append(d)
        rec.setdefault("unused_dirs", {})[d] = b
    out = []
    for rec in recs.values():
        if not rec["error"]:
            rec["lang"], rec["version"] = _lang_version(rec["image"])
        rec["via"].sort()
        rec["users"] = {r: sorted(p) for r, p in sorted(rec["users"].items())}
        rec["repos"] = {c: {r: sorted(p) for r, p in sorted(regs.items())} for c, regs in sorted(rec["repos"].items())}
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

# Sunucuda çalışan üçüncü taraf kod: `:latest` DEĞİL, ölçülen (2026-11-18) özet. Yükseltmek için `config/image_vuln.yaml`
# `trivy_image` değerini yeni bir `aquasec/trivy:<sürüm>` ya da `aquasec/trivy@sha256:<64 hex>` yapın.
DEFAULT_TRIVY_IMAGE = "aquasec/trivy@sha256:8f6bb947dbc528902e11f2f5be52f7b519f6a39d5d148f41166f2040499768fb"
_TRIVY_IMAGE_RE = re.compile(r"aquasec/trivy(?::[\w.-]+|@sha256:[0-9a-f]{64})", re.ASCII)


def trivy_image(path: Path | None = None) -> str:
    """`config/image_vuln.yaml` `trivy_image` (katı desen: `aquasec/trivy(:[\w.-]+|@sha256:<64 hex>)`); yok / geçersiz →
    sabitlenmiş varsayılan (+ geçersizse uyarı). Docker'a giden bir ad olduğu için yalnız bu desen geçer."""
    import yaml
    p = Path(path) if path else IGNORE_PATH
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return DEFAULT_TRIVY_IMAGE
    v = doc.get("trivy_image") if isinstance(doc, dict) else None
    if v is None:
        return DEFAULT_TRIVY_IMAGE
    if isinstance(v, str) and len(v) <= 200 and _TRIVY_IMAGE_RE.fullmatch(v):
        return v
    log.warning(f"⚠️ image_vuln: {p.name} trivy_image geçersiz (aquasec/trivy:<sürüm> ya da @sha256:<64 hex> olmalı); "
                "sabitlenmiş varsayılan kullanılıyor")
    return DEFAULT_TRIVY_IMAGE


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
                "-v", f"{TRIVY_CACHE}:/root/.cache/", trivy_image()]
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


_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_CVE_RE = re.compile(r"CVE-\d{4}-\d{4,}", re.ASCII)
_GHSA_RE = re.compile(r"GHSA(-[0-9a-z]{4}){3}", re.ASCII)


def cve_url(vid: str) -> str:
    """Açık kimliğinin kayıt sayfası; tanınmayan kimlik için boş (bağlantı yazılmaz). Kimlik güvenilmez metindir:
    yalnız tam kalıba uyan kimlik bir adrese girer."""
    vid = str(vid or "")
    if _CVE_RE.fullmatch(vid):
        return f"https://nvd.nist.gov/vuln/detail/{vid}"
    if _GHSA_RE.fullmatch(vid):
        return f"https://github.com/advisories/{vid}"
    return ""


def cve_age(pub, today: date | None = None) -> int | None:
    """Duyurudan bugüne gün sayısı; tarih okunamıyorsa / gelecekteyse None."""
    try:
        d = date.fromisoformat(str(pub or "")[:10])
    except ValueError:
        return None
    n = ((today or date.today()) - d).days
    return n if n >= 0 else None


def _cve_html(p: dict, today: date | None = None) -> str:
    pubs = p.get("published") if isinstance(p.get("published"), dict) else {}
    out = []
    for c in p.get("cves") or []:
        url, pub = cve_url(c), pubs.get(c)
        item = f'<a href="{url}">{_e(c)}</a>' if url else _e(c)
        age = cve_age(pub, today)
        if age is not None:
            item += f" — duyuru {_e(str(pub)[:10])}, {age} gün önce"
        out.append(item)
    return "; ".join(out)


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
                                  "severity": sev, "cves": [], "sev": {}, "published": {}, "critical": 0, "high": 0}
            elif _SEV_RANK[sev] > _SEV_RANK[p["severity"]]:
                p.update(severity=sev, installed=str(v.get("InstalledVersion") or ""), fixed=fixed)
            p["critical" if sev == "CRITICAL" else "high"] += 1     # yok sayma (ignore_packages) için paket başına sayım
            if cve and cve not in p["sev"]:
                p["sev"][cve] = sev
                pub = str(v.get("PublishedDate") or "")[:10]      # açığın kamuya duyurulduğu gün (Trivy / NVD)
                if _DAY_RE.fullmatch(pub):
                    p["published"][cve] = pub
    for p in pkgs.values():
        # Saklanan kimlikler: önce kritikler (en çok MAX_CRIT_CVES — kritik açık listeden düşmesin), kalan yer
        # yükseklerle MAX_CVES'e tamamlanır. `sev` kimlik başına önemdir (kart kritikleri ayrı listeler).
        crit = [c for c, v in p["sev"].items() if v == "CRITICAL"][:MAX_CRIT_CVES]
        high = [c for c, v in p["sev"].items() if v != "CRITICAL"][:max(0, MAX_CVES - len(crit))]
        p["cves"] = crit + high
        p["sev"] = {c: p["sev"][c] for c in p["cves"]}
        p["published"] = {c: d for c, d in p["published"].items() if c in p["sev"]}
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
    """Dosyadaki bilinmeyen diğer alanlar korunur; yalnız `image` / `scan` / `built_at` yenilenir. (Telegram durumu
    burada değil, `notified.json`'dadır.)"""
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
        # Kendi taban imajımız (via dolu) DevOps'un işidir; genel etiket (`python:3.11`) servis imajı yeniden
        # derlenip yayınlanınca taze çekilir — DevOps'a yönlendirmek yanlış olur.
        # Üç parçalı sabit etiket (`golang:1.24.4`) yeniden derlemede aynı eski imajı çeker: "taze taban" denmez;
        # yama geride / destek bitti eylemleri zaten aşağıda yazılır, ikisi de yoksa çare sürüm yükseltmektir.
        pinned = not rec.get("via") and len(_ints(rec.get("version") or "") or ()) >= 3
        if rec.get("via"):
            actions.append("taban imaj yeniden üretilmeli (DevOps, bölge bayrakları)")
        elif not pinned:
            actions.append("servis imajı yeniden derlendiğinde taze taban çekilir — servisi yeniden yayınla")
        elif not patch and not end:
            actions.append("sabit etiket: yeniden derleme gidermez — daha yeni sürüme geç")
    if patch:
        actions.append(f"yama sürümünü yükselt ({patch['latest']})")
    lag = rec.get("tag_lag") or {}
    for d in sorted(lag):
        # Üretim dalında daha yeni etiket var ama servisler eskisini çekiyor: tarama YENİ etiketin dayanağını gösterir,
        # çalışan servisler ondan eski olabilir.
        actions.append(f"bölge repolarında imaj etiketini yükselt ({d}:{lag[d]['latest']})")
    if end:
        actions.append(f"sürüm yükselt (destek {end['eol']}'de bitti)" if end["eol"] else "sürüm yükselt (destek bitti)")
    if rec.get("unused"):
        # Hiçbir servis kullanmıyor: güncel tutulur (yama / taşıma eylemleri planlanır) ama kritik uyarı üretmez.
        return ("yüksek" if (crit or high or patch or end) else "temiz" if scan["state"] == "tamam" else "bilinmiyor"), actions
    if crit or end:
        return "kritik", actions
    if high or patch or lag:
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
GUARD_S = 300                       # bir tarama (başarılı ya da çöken) bitince 5 dk yenisi başlamaz: GET döngüsü / POST seli yok
SNAP_S = 60                         # bellekteki anlık görüntü en çok bu kadar eskir; GET Azure'a gitmez

_STATE_LOCK = threading.Lock()
_BUILD_LOCK = threading.Lock()      # eşzamanlı GET'ler anlık görüntüyü bir kez kurar
# running: aynı anda tek tarama · done_at: son taramanın BİTİŞİ (çökmüş olsa da) · report: son tam rapor ·
# snap / snap_at: bellekteki anlık görüntü (envanter + önbellekteki taramalar)
_RUN: dict = {"running": False, "done_at": 0.0, "report": None, "snap": None, "snap_at": 0.0}


def _reset_state() -> None:
    with _STATE_LOCK:
        _RUN.update(running=False, done_at=0.0, report=None, snap=None, snap_at=0.0)


def _begin() -> bool:
    with _STATE_LOCK:
        if _RUN["running"]:
            return False
        _RUN["running"] = True
        return True


def _end(rep: dict | None, at: float | None = None) -> None:
    """Tarama bitti (rapor None = çöktü): bayrak iner, bitiş zamanı HER durumda yazılır (çöken tarama hemen yeniden
    doğmasın), anlık görüntü geçersiz kılınır (yeni sonuçlar bir sonraki GET'te görünür)."""
    with _STATE_LOCK:
        _RUN.update(running=False, done_at=time.time() if at is None else at, snap=None)
        if rep is not None:
            _RUN["report"] = rep


def _spawn_thread(fn) -> None:
    threading.Thread(target=fn, daemon=True, name="image-vuln-scan").start()


def _snapshot(client, clock, decorate, snap_kw: dict) -> dict:
    """Bellekteki anlık görüntü (envanter + önbellekteki taramalar + `decorate`): en çok `SNAP_S`'de bir kurulur ve bir
    arka plan taraması bitince; taze görüntü varken Azure'a hiç gidilmez."""
    now = clock()
    with _STATE_LOCK:
        snap, at = _RUN["snap"], _RUN["snap_at"]
    if snap is not None and 0 <= now - at < SNAP_S:
        return snap
    with _BUILD_LOCK:
        with _STATE_LOCK:
            snap, at = _RUN["snap"], _RUN["snap_at"]
        if snap is not None and 0 <= now - at < SNAP_S:
            return snap
        rep = report(client, force=False, scan_fn=lambda image: _empty_scan("bekliyor"), **snap_kw)
        rep = _decorated(rep, decorate)
        with _STATE_LOCK:
            _RUN.update(snap=rep, snap_at=now)
        return rep


def _decorated(rep: dict, decorate) -> dict:
    if decorate is None:
        return rep
    try:
        return decorate(rep) or rep
    except Exception as e:  # noqa: BLE001 — giderme planı eksik kalsa da rapor düşmez
        log.warning(f"⚠️ image_vuln: rapor zenginleştirilemedi: {str(e)[:200]}")
        return rep


def _start_scan(client, base: dict, *, force: bool, spawn, clock, decorate, kw: dict) -> dict:
    if not _begin():
        return {**base, "scanning": True}

    def job():
        rep = None
        try:
            rep = _decorated(report(client, force=force, **kw), decorate)
        except Exception as e:  # noqa: BLE001
            log.warning(f"⚠️ image_vuln: arka plan taraması başarısız: {str(e)[:300]}")
        finally:
            _end(rep, clock())
    try:
        (spawn or _spawn_thread)(job)
    except Exception:
        _end(None, clock())
        raise
    return {**base, "scanning": True}


def serve(client, *, force: bool = False, spawn=None, clock=None, decorate=None, **kw) -> dict:
    """GET: karta dönen rapor. Asla taramada ve (taze anlık görüntü varken) Azure'da beklemez; `force` YOK SAYILIR
    (yan etkili GET çapraz-site tetiklenebilir). Önbellek eksikse tek bir otomatik ilk tarama arka planda başlar
    (`scanning: true`); bir tarama (çöken dahil) bitince `GUARD_S` boyunca yenisi başlamaz, son sonuç döner.
    `decorate(rapor)` anlık görüntü kurulurken uygulanır (giderme planı). `kw` `report`'a gider."""
    clock = clock or time.time
    base = _snapshot(client, clock, decorate, {k: v for k, v in kw.items() if k != "scan_fn"})
    pending = sum(1 for r in base["images"] if r["scan"]["state"] == "bekliyor")
    with _STATE_LOCK:
        running, last, done_at = _RUN["running"], _RUN["report"], _RUN["done_at"]
    if running:
        return {**base, "scanning": True}
    if pending == 0:
        return {**base, "scanning": False}
    if done_at and clock() - done_at < GUARD_S:
        return {**(last or base), "scanning": False}     # son tarama bitti ama bu imajlar taranamadı: sonuç, döngü değil
    return _start_scan(client, base, force=False, spawn=spawn, clock=clock, decorate=decorate, kw=kw)


def rescan(client, *, spawn=None, clock=None, decorate=None, **kw) -> dict:
    """POST (kartın "Yeniden tara"sı): yalnız tarama sürmüyorsa VE son tarama `GUARD_S`'den önce bittiyse tam yeniden
    tarama başlatır; aksi halde anlık görüntüyü Türkçe nedenle (ve `cooldown`: kalan sn) döner."""
    clock = clock or time.time
    base = _snapshot(client, clock, decorate, {k: v for k, v in kw.items() if k != "scan_fn"})
    with _STATE_LOCK:
        running, done_at = _RUN["running"], _RUN["done_at"]
    if running:
        return {**base, "scanning": True, "message": "Tarama zaten sürüyor."}
    left = GUARD_S - (clock() - done_at) if done_at else 0
    if left > 0:
        n = int(left) + (0 if left == int(left) else 1)
        return {**base, "scanning": False, "cooldown": n,
                "message": f"Son tarama yeni bitti; yeniden taramak için {n} sn sonra deneyin."}
    return _start_scan(client, base, force=True, spawn=spawn, clock=clock, decorate=decorate, kw=kw)


# ── Yoklayıcı ────────────────────────────────────────────────────────────

_POLLER: threading.Thread | None = None
_POLLER_LOCK = threading.Lock()


def _tick(get_client, *, sleep=time.sleep, max_wait_s: int = 1800, step_s: int = 30) -> None:
    """Günlük tur: `report(force=True)` + `alert`. Ayarlar her turda okunur. Kartın başlattığı bir tarama sürüyorsa
    onu (en çok `max_wait_s`, `step_s`'lik adımlarla) bekler ve bitince koşar — bitmiş taramanın önbelleği yeniden
    kullanılır (zorlama yok, dakikalarca yeniden tarama yok); hâlâ sürüyorsa günü atlar ve UYARI yazar."""
    from agile_sdlc_crew import pipeline_config as pc
    if not (pc.get("CREW_TECHDEBT") and pc.get("CREW_TECHDEBT_IMAGE_VULN")):
        return
    waited = False
    if not _begin():
        waited = True
        for _ in range(max(1, int(max_wait_s // step_s))):
            sleep(step_s)
            if _begin():
                break
        else:
            log.warning(f"⚠️ image_vuln: tarama {int(max_wait_s // 60)} dk sonra hâlâ sürüyor, günlük tur atlandı")
            return
    rep = None
    try:
        from agile_sdlc_crew.quality import modern
        client = get_client()
        # Tek TAZE index (24 sa'lik disk önbelleği değil): rapor da giderme de aynı nesneyi kullanır; giderme yollarını
        # (`via` → dizin, `users` → bölge) bayat veriden türetmesin.
        index = modern.build_index(client, max_age_h=0)
        rep = report(client, force=not waited, index=index)
    finally:
        _end(rep)       # tarama bitti: alert / giderme `intake._LOCK`'ta dakikalarca bekleyebilir, kart "Taranıyor" demesin
    from agile_sdlc_crew.quality import intake
    team = str(getattr(client, "team", "") or "")
    results = alert(client, rep, team=team, deps=intake.default_deps())
    if pc.get("CREW_TECHDEBT_IMAGE_VULN_FIX"):
        try:                         # giderme hatası turu düşürmez; uyarı zaten yapıldı
            _fix_round(client, rep, results, team=team, index=index)
        except Exception as e:  # noqa: BLE001
            log.warning(f"⚠️ image_vuln: giderme turu hata verdi: {str(e)[:300]}")


def _default_deps():
    from agile_sdlc_crew.quality import intake
    return intake.default_deps()


def _fix_err_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / "fix_errors.json"      # imaj önbellek dosyaları 40 hex + .json, notified.json: çakışmaz


def _fix_err_load(cache_dir: Path) -> dict:
    """İmaj → son yorumlanan hata anahtarı. Dosya güvenilmezdir (bozuk / elle düzenlenmiş olabilir): biçim tutmazsa boş."""
    try:
        doc = json.loads(_fix_err_path(cache_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict):
        return {}
    return {k: v for k, v in doc.items() if isinstance(k, str) and isinstance(v, str) and len(v) <= 64}


def _fix_err_save(cache_dir: Path, memo: dict) -> None:
    try:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        _fix_err_path(cache_dir).write_text(json.dumps(memo, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning(f"⚠️ image_vuln: giderme hata notu yazılamadı: {e}")


def _fix_errors(acts: list[dict]) -> list[tuple[str, str]]:
    out = [(a["kind"], str(a.get("reason") or "")) for a in acts if a.get("state") == "hata"]
    out += [(f"{a['kind']}/{u['repo']}", str(u.get("reason") or "")) for a in acts for u in a.get("repos") or []
            if u.get("state") == "hata"]
    return sorted(set(out))


def _fix_round(client, rep: dict, results: list[dict] | None, *, team: str, index: dict | None = None, resolver=None,
               apply_fn=None, deps=None, cache_dir: Path | None = None) -> list[dict]:
    """Uyarının üretildiği turda gidermeyi de başlatır. Tur başına en çok `CREW_TECHDEBT_IMAGE_VULN_MAX_TASKS` imaj
    (`alert` ile AYNI sıra: destek bitmiş, düzeltmeli kritik sayısı, servis sayısı; yalnız `kritik` / `yüksek`);
    sınıra YALNIZ gerçekten bir şey YAZILAN imaj (`ran`) sayılır — PR açık / izleniyor / durduruldu / güncel / yazmadan
    reddedilenler slot yemez, yoksa her gece aynı imajlar sınırı doldurur ve sıradakiler hiç işlenmezdi. Bastırma
    kararı `alert` sonuçlarından değil PANODAN okunur (`_TASK` kapalı / pano okunamadı da kapsanır): insanın iptal
    ettiği uyarıya dokunulmaz, pano okunamazsa tur HİÇBİR şey yapmaz. Tüm tur için TEK izleme-başlatma bütçesi
    (`CREW_TECHDEBT_UPGRADE_MAX_OPEN`, yalnız başarılı yeni başlatma tüketir). Yoklayıcı (`auto=True`) durdurulan
    izlemeyi yeniden açmaz. Yazılan sonuç — ya da DEĞİŞEN bir hata nedeni — imajın uyarı işine yorum olur; aynı hata
    her gece yorumlanmaz. Bir imajın hatası ötekilerini durdurmaz."""
    from agile_sdlc_crew.quality import image_fix, modern, upgrade_registry, upgrade_track
    recs = sorted((r for r in rep.get("images") or [] if r.get("level") in ("kritik", "yüksek")), key=_alert_rank)
    if not recs:
        return []
    deps = deps or _default_deps()
    try:
        verdicts = _board_verdicts(deps, client, [r["image"] for r in recs])
    except Exception as e:  # noqa: BLE001 — bastırma bilinmiyor: yazma yok
        log.warning(f"⚠️ image_vuln: pano okunamadı, giderme turu yapılmadı: {str(e)[:200]}")
        return []
    if index is None:
        index = modern.build_index(client, max_age_h=0)     # yazmadan önce yollar taze index'ten türetilir
    resolver = resolver or upgrade_registry.default()
    apply_fn = apply_fn or image_fix.apply
    cap = max(1, int(_knob("CREW_TECHDEBT_IMAGE_VULN_MAX_TASKS", 3)))
    budget = [image_fix._max_open_default()]
    wi = {r["image"]: r["wi"] for r in results or [] if r.get("wi")}
    for image, (_v, w) in verdicts.items():
        if w and image not in wi:
            wi[image] = w
    cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
    memo = _fix_err_load(cache_dir)
    memo0 = dict(memo)
    out = []
    ran_images = 0
    for rec in recs:
        image = rec["image"]
        if verdicts[image][0] == "suppressed":       # insan uyarıyı iptal etti / reddetti: yoklayıcı dokunmaz
            out.append({"image": image, "state": "bastırıldı", "actions": []})
            continue
        if ran_images >= cap:
            out.append({"image": image, "state": "sırada", "actions": []})
            log.info(f"image_vuln: {image} gidermesi sırada (tur başına en çok {cap} imaj)")
            continue
        try:
            acts = apply_fn(client, rec, team=team, index=index, resolver=resolver, budget=budget, auto=True)
        except Exception as e:  # noqa: BLE001
            log.warning(f"⚠️ image_vuln: {image} gidermesi başarısız: {str(e)[:300]}")
            continue
        out.append({"image": image, "actions": image_fix.public(acts)})
        unsure = [f"{a['kind']}: {a.get('reason') or 'belirsiz'}" for a in acts if a.get("state") == "belirsiz"]
        unsure += [f"{a['kind']}/{u['repo']}: {u.get('reason') or 'belirsiz'}" for a in acts for u in a.get("repos") or []
                   if u.get("state") == "belirsiz"]
        if unsure:                                   # nöbetçi için: imaj başına TEK satır, iş yorumu yok
            log.warning(f"⚠️ image_vuln: {image} gidermesi belirsiz (yazılmadı): " + "; ".join(dict.fromkeys(unsure))[:600])
        wrote = any(a.get("ran") for a in acts)
        errs = _fix_errors(acts)
        key = hashlib.sha1(json.dumps(errs, ensure_ascii=False).encode("utf-8")).hexdigest()[:16] if errs else ""
        new_error = bool(key) and key != memo.get(image)
        if key:
            memo[image] = key
        else:
            memo.pop(image, None)
        if not (wrote or new_error):
            continue                                 # yazılacak bir şey yoktu / aynı red zaten yorumlandı: slot yok, yorum yok
        if wrote:
            ran_images += 1
        if wi.get(image):
            try:
                client.add_comment(int(wi[image]), image_fix.comment_html(acts) + upgrade_track.SIGNATURE)
            except Exception as e:  # noqa: BLE001 — PR'lar zaten açıldı
                log.warning(f"⚠️ image_vuln: giderme yorumu yazılamadı: {str(e)[:200]}")
        else:
            log.info(f"image_vuln: {image} gidermesi sonuçlandı (uyarı işi yok): "
                     + "; ".join(f"{a['kind']}={a['state']}" for a in acts if a.get("ran") or a.get("state") == "hata"))
    if memo != memo0:
        _fix_err_save(cache_dir, memo)
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


def task_body(rec: dict, *, today: date | None = None) -> str:
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
            f"({_e(p.get('severity'))}{', ' + _cve_html(p, today) if p.get('cves') else ''})</li>"
            for p in pk[:15])
        more = f"<p><small>… ve {len(pk) - 15} paket daha</small></p>" if len(pk) > 15 else ""
        parts.append(f"<p><b>Paketler (ilk 15):</b></p><ul>{rows}</ul>{more}")
    repos = "".join(f"<li><b>{_e(c)}</b>: {_e(', '.join(regs))}</li>" for c, regs in (rec.get("repos") or {}).items()
                    if isinstance(regs, dict))
    if repos:
        parts.append(f"<p><b>Etkilenen repolar:</b></p><ul>{repos}</ul>")
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
                 human_reason="Yeniden üretim / servisin yeniden yayını / sürüm yükseltmesi insan (DevOps) kararıdır; Tempo imaj üretmez.",
                 extra_tags=[TASK_TAG], body_html=task_body(rec), priority=1)


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


def _board_verdicts(deps, client, images: list[str]) -> dict:
    """Panodaki uyarı işi kararı (`dedup.verdict`) imaj başına: {imaj: (karar, iş no)}. `alert` ve giderme turu AYNI
    aramayı kullanır; pano okunamazsa istisna yükselir (çağıran yazmadan vazgeçer)."""
    from agile_sdlc_crew.quality import dedup
    board = deps.board(client)
    grace = int(_knob("CREW_QUALITY_REOPEN_GRACE_DAYS", 3))
    return {i: dedup.verdict(board.get(_fp(i)), last_seen=0.0, now=deps.now(), grace_days=grace) for i in images}


def _open_tasks(client, crit: list[dict], *, team: str, deps, open_fn, cap: int) -> list[dict]:
    try:
        verdicts = _board_verdicts(deps, client, [r["image"] for r in crit])
    except Exception as e:  # noqa: BLE001 — pano okunamazsa tekilleştirilemez: yazma yok
        log.warning(f"⚠️ image_vuln: pano okunamadı, Task açılmadı: {str(e)[:200]}")
        return []
    out: list[dict] = []
    opened = 0
    for rec in sorted(crit, key=_alert_rank):
        v, wi = verdicts[rec["image"]]
        if v in ("blocked", "suppressed"):
            out.append({"image": rec["image"], "state": "açık iş" if v == "blocked" else "bastırıldı", "wi": wi})
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
