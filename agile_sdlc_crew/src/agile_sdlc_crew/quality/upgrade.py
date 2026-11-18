"""Dil sürümü yükseltme: katalogdan yükseltmeyi etkileyen kuralları süzer."""
from __future__ import annotations

import ast
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

import yaml

from . import php_modern as pm

log = logging.getLogger("pipeline")

TARGETS_PATH = Path(__file__).resolve().parent.parent / "config" / "upgrade_targets.yaml"
_VER_RE = re.compile(r"^\d+\.\d+$")
CI_DIR = "Test Pipelines"
UNPUBLISHED = "imaj yayınlanmamış"
_IMG_RES = {
    "php": re.compile(r"php(?:-(\d+)-(\d+)|:(\d+)\.(\d+))"),
    "python": re.compile(r"python(?:-(\d+)-(\d+)|:(\d+)\.(\d+))"),
    "go": re.compile(r"golang:(\d+)\.(\d+)"),
}


def upgrade_rules(rules: list[pm.Rule], current: str, target: str) -> list[pm.Rule]:
    """`breaks` dolu ve current < since ≤ target olan kurallar."""
    cur, tgt = pm._ver(current), pm._ver(target)
    return [r for r in rules if r.breaks and cur < r.since <= tgt]


def blocking(rule: pm.Rule, *, auto: bool) -> bool:
    """Bulgu yükseltmeyi bekletir mi: error/behavior her zaman; deprecation yalnız otomatik düzeltilebiliyorsa."""
    if rule.breaks in ("error", "behavior"):
        return True
    if rule.breaks == "deprecation":
        return bool(auto)
    return False


_PROBE_KEYS = ("probe:error", "probe:deprecation")


def _fp_rule(rest: str, rules: dict) -> str | None:
    """Parmak izinin kural kısmından (`<kural>:<dosya|modül>`) kural anahtarı (en uzun önek); yoksa None."""
    keys = [k for k in list(rules) + list(_PROBE_KEYS) if rest.startswith(k + ":")]
    return max(keys, key=len) if keys else None


def _fp_blocking(rest: str, rules: dict, auto: bool) -> bool:
    """Parmak izinin kural kısmı (`<kural>:<dosya|modül>`) yükseltmeyi bekletir mi.
    Bilinmeyen kural muhafazakâr olarak bekletir. (upgrade_track.open_tasks ve scan'in atlananları aynı yüklem.)"""
    k = _fp_rule(rest, rules)
    if k is None:
        return True
    if k == "probe:error":
        return True
    if k == "probe:deprecation":
        return False
    return blocking(rules[k], auto=auto)


# ── Hedef sürüm ve sürüm yerleri ─────────────────────────────────────────

def _v(s) -> tuple[int, int]:
    return pm._ver(s)


def _fmt(t: tuple[int, int]) -> str:
    return f"{t[0]}.{t[1]}"


def known_versions(lang: str) -> list[str]:
    try:
        data = yaml.safe_load(TARGETS_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    vs = [str(x) for x in (data.get(lang) or []) if _VER_RE.match(str(x))]
    return sorted(set(vs), key=_v)


def _image_version(image: str, lang: str) -> tuple[int, int] | None:
    rx = _IMG_RES.get(lang)
    m = rx.search(image or "") if rx else None
    if not m:
        return None
    g = [x for x in m.groups() if x is not None]
    return int(g[0]), int(g[1])


def _all_images(index: dict) -> list[str]:
    return [i for es in (index or {}).values() for e in es for i in e.get("images", [])]


def published(lang: str, index: dict) -> set[str]:
    if lang not in ("php", "python"):
        return set()
    return {_fmt(v) for v in (_image_version(i, lang) for i in _all_images(index)) if v}


def default_target(lang: str, current: str, index: dict) -> str | None:
    if lang in ("php", "python"):
        # Seçicinin gösteremeyeceği (listede olmayan) bir yayınlanmış sürüm varsayılan olmasın.
        pub = sorted(published(lang, index) & set(known_versions(lang)), key=_v)
        return pub[-1] if pub and _v(pub[-1]) > _v(current) else None
    # Liste elle bakılır: repo listedeki son sürümü geçmişse (Next 16.3, liste 15.0) hedef yoktur —
    # aksi halde kart "16.3 → 15.0" gibi geriye dönük bir yükseltme gösteriyordu.
    kv = known_versions(lang)
    return kv[-1] if kv and _v(kv[-1]) > _v(current) else None


PY_UPGRADE_RULES = Path(__file__).resolve().parent.parent / "config" / "modern_python_upgrade_rules.yaml"
_PHP_FIX_MIN = (7, 0)


def catalog(lang: str) -> list[pm.Rule]:
    """Dilin kataloğu (modernleştirme + yükseltme kuralları). Python'un yükseltme kuralları ayrı dosyada:
    ruff kodlarıyla aynı katalogda olsalar araç düzeltmesine (ruff --select) karışırlardı."""
    from agile_sdlc_crew.quality import modern
    if lang == "php":
        return pm.load_rules()
    if lang not in modern.LANGS:
        return []
    rules = modern.rules_for(modern.LANGS[lang]["source"])
    if lang == "python":
        rules = rules + pm.load_rules(PY_UPGRADE_RULES)
    # fix_min'in varsayılanı ("7.0") PHP sürümüdür; Go 1.x / Python 3.x'te her bulguyu "önceden
    # düzeltilemez" gösterirdi → diğer dillerde varsayılan "kısıt yok" sayılır.
    return [replace(r, fix_min=(0, 0)) if r.fix_min == _PHP_FIX_MIN else r for r in rules]


def _has_catalog(lang: str, version: str) -> bool:
    """O sürümde `breaks` dolu bir katalog kuralı var mı (Go/Python/Next.js'te henüz olmayabilir)."""
    try:
        return any(r.breaks and r.since == _v(version) for r in catalog(lang))
    except Exception:  # noqa: BLE001
        return False


_BIN_RE = re.compile(r"^[\w./@+~-]+$")
_PROBE_ENV = {"php": ("CREW_UPGRADE_PHP_BINS", "php"), "python": ("CREW_UPGRADE_PYTHON_BINS", "python3")}


def tool_bins(env_name: str, prefix: str) -> dict[str, str]:
    """`8.4=/yol/php,8.5=/yol/php8.5` → {sürüm: yol}. Yalnız env'den okunur (panelden yazılamaz; sunucuda
    kimlik doğrulama yok — php_modern._safe_bin ile aynı karar). Sürüm `N.N`, yol mutlak ve var olan bir
    dosya, dosya adı `prefix` ile başlamalı; aksi atılır."""
    out: dict[str, str] = {}
    for part in (os.environ.get(env_name) or "").split(","):
        ver, sep, path = part.strip().partition("=")
        ver, path = ver.strip(), path.strip()
        if not sep or not _VER_RE.match(ver) or not path or not _BIN_RE.match(path):
            continue
        p = Path(path)
        if p.is_absolute() and p.is_file() and p.name.startswith(prefix):
            out[ver] = path
    return out


def _target_bin(lang: str, version: str) -> str | None:
    spec = _PROBE_ENV.get(lang)
    return tool_bins(*spec).get(version) if spec else None


def _tool_ok(lang: str, version: str) -> bool:
    """Yerelde o sürümü karşılayan araç var mı. Go: yerel `go version`; PHP/Python: env'de o sürümün binary'si."""
    if lang != "go":
        return _target_bin(lang, version) is not None
    try:
        out = subprocess.run(["go", "version"], capture_output=True, text=True, timeout=15, cwd="/").stdout
        m = re.search(r"go(\d+)\.(\d+)", out)
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= _v(version)


def _region_images(index: dict) -> dict[str, set[str]]:
    """Bölge reposu (anatolia / russia / …) → o repoda kullanılan bütün imajlar."""
    by: dict[str, set[str]] = {}
    for es in (index or {}).values():
        for e in es:
            by.setdefault(str(e.get("src") or "").strip("/").split("/")[0], set()).update(e.get("images", []))
    return by


def _three_part(image: str, lang: str) -> bool:
    """Dil imajının etiketi üç parçalı mı (`golang:1.24.4-alpine`, `php:8.2.12-fpm`, `…/python-3-13-7:1.0`;
    kayıt deposu önekli ya da değil). Rakam-dizisi kuralı böyle bir etiketin yalnız ilk iki parçasını değiştirir ve
    var olmayabilecek bir etiket uydururdu (`golang:1.27.4`) — site otomatik yazılmaz."""
    name = _LANG_IMG_NAME.get(lang)
    if not name:
        return False
    return re.search(rf"(?:^|/){name}(?::\d+\.\d+\.\d+|-\d+-\d+-\d+)(?![\d])", image or "") is not None


def is_library_image(image: str, lang: str) -> bool:
    """Kayıt deposu öneki olmayan dil imajı (`golang:…`, `python:…`, `php:…`; `docker.io/library/` da sayılır)."""
    name = _LANG_IMG_NAME.get(lang)
    return bool(name) and re.match(rf"(?:docker\.io/)?(?:library/)?{name}:", image or "") is not None


def image_for_full(image: str, lang: str, full: str) -> str:
    """Üç parçalı etiketi tam sürümle yazar: `golang:1.24.4-alpine` + `1.27.3` → `golang:1.27.3-alpine`;
    `…/python-3-13-7:1.0` + `3.14.2` → `…/python-3-14-2:1.0`. Eşleşme yoksa aynen."""
    name = _LANG_IMG_NAME.get(lang)
    if not name or not re.fullmatch(r"\d+\.\d+\.\d+", full or ""):
        return image
    new = re.sub(rf"((?:^|/){name}):\d+\.\d+\.\d+", rf"\g<1>:{full}", image, count=1)
    if new != image:
        return new
    return re.sub(rf"((?:^|/){name})-\d+-\d+-\d+", rf"\g<1>-{full.replace('.', '-')}", image, count=1)


def _tag(image: str) -> str:
    return image.rpartition(":")[2] if ":" in image.rpartition("/")[2] else ""


def _library_tag_ok(new: str, lang: str, resolver) -> bool:
    """Genel imajın yeni etiketi Docker Hub'da var mı (kanıt: `tag_exists is True`)."""
    name = _LANG_IMG_NAME.get(lang)
    return bool(resolver and name and is_library_image(new, lang)
                and resolver.tag_exists(name, _tag(new)) is True)


def _prod_image(region_images, img: str, lang: str, to: str, *, resolver=None) -> tuple[str, str]:
    """(yeni imaj, engel). Spec §2 imaj kanıtı: yeni imaj aynı bölge reposunda başka bir serviste kullanılıyor ya da
    genel imajsa Docker Hub'da var. Üç parçalı etiket yama sürümüyle (`resolver.latest_patch`) yazılır; çözülemezse
    ya da genel imajın etiketi yoksa elle (`CI_HAND`, yeni == eski). `_devops_sites` ve `selectable` aynı yüklemi
    kullanır."""
    name = _LANG_IMG_NAME.get(lang)
    three = _three_part(img, lang)
    if three:
        full = resolver.latest_patch(name, to) if resolver and name else None
        new = image_for_full(img, lang, full) if full else img
        if new == img:
            return img, CI_HAND
    else:
        new = image_for(img, lang, to)
    if new != img:
        if new in (region_images or set()) or _library_tag_ok(new, lang, resolver):
            return new, ""
        if three and is_library_image(new, lang):
            return img, CI_HAND
    return new, UNPUBLISHED


def _image_ok(region_images, img: str, lang: str, to: str, *, resolver=None) -> bool:
    return _prod_image(region_images, img, lang, to, resolver=resolver)[1] == ""


def selectable(lang: str, current: str, index: dict, services: dict | None = None, *, resolver=None) -> list[dict]:
    """Seçilebilir sürümler. `services` (`_devops_services`'in bu repo için verdiği src → imajlar) verilirse
    `published` site kuralıyla aynıdır: hedefin altındaki her dil imajının yeni hâli o bölgede kullanılıyor
    (`_image_ok`); reponun FLODevOps servisi yoksa True. Verilmezse eski davranış (imaj sürümü görülmüş mü)."""
    pub = published(lang, index)
    regions = _region_images(index) if services is not None else {}
    name = _LANG_IMG_NAME.get(lang, "")

    def ok(v: str) -> bool:
        if services is None:
            return True if lang not in ("php", "python") else v in pub
        for src, images in services.items():
            region = str(src).strip("/").split("/")[0]
            for img in images:
                iv = _image_version(img, lang)
                if iv is None:
                    if name and name in img:      # dil imajı ama sürümü okunamıyor: kanıtlanamaz
                        return False
                    continue
                if iv < _v(v) and not _image_ok(regions.get(region, set()), img, lang, v, resolver=resolver):
                    return False
        return True
    out = []
    for v in known_versions(lang):
        if _v(current) < _v(v):
            out.append({"version": v, "published": ok(v), "catalog": _has_catalog(lang, v), "tool": _tool_ok(lang, v)})
    return out


def image_for(image: str, lang: str, to: str) -> str:
    """`…/php-8-2:1.0` → `…/php-8-4:1.0`; `php:8.2-fpm` → `php:8.4-fpm` (önek ve etiket korunur)."""
    a, b = _v(to)
    name = {"php": "php", "python": "python", "go": "golang"}.get(lang)
    if not name:
        return image
    new = re.sub(rf"{name}-\d+-\d+", f"{name}-{a}-{b}", image, count=1)
    return new if new != image else re.sub(rf"{name}:\d+\.\d+", f"{name}:{a}.{b}", image, count=1)


def _site(where, repo, project, branch, path, line, old, new, blocked="") -> dict:
    return {"where": where, "repo": repo, "project": project, "branch": branch, "path": path,
            "line": line, "old": old, "new": new, "blocked": blocked}


_LANG_IMG_NAME = {"php": "php", "python": "python", "go": "golang"}
RANGE_BLOCK = "sürüm kısıtı elle düzenlenmeli"
CI_HAND = "sürüm satırı elle düzenlenmeli"
NO_LINE = "Dockerfile satırı bulunamadı"


def _devops_services(repo, lang, index, per_src) -> dict[str, list[str]]:
    """src -> imajlar: build_index + (varsa) hedef aramasının per_src listesi (app_map'li servisler dahil)."""
    svc: dict[str, list[str]] = {}
    for e in (index or {}).get(repo.lower(), []):
        src = str(e.get("src") or "").strip("/")
        for img in e.get("images", []):
            if img not in svc.setdefault(src, []):
                svc[src].append(img)
    for r in per_src or []:
        src, img = str(r.get("src") or "").strip("/"), r.get("image")
        if src and img and img not in svc.setdefault(src, []):
            svc[src].append(img)
    return svc


def _devops_sites(client, repo, lang, to, index, read, per_src=None, *, resolver=None) -> list[dict]:
    from agile_sdlc_crew.quality import app_map as am
    by_devrepo = _region_images(index)
    rd = read or (lambda d, p: am._read(client, d, p))
    name = _LANG_IMG_NAME.get(lang, "")
    out = []
    for src, images in sorted(_devops_services(repo, lang, index, per_src).items()):
        devrepo, _, rest = src.partition("/")
        path = "/" + rest.strip("/") + "/Dockerfile"
        text = None
        for img in images:
            v = _image_version(img, lang)
            if v is None:
                if name and name in img:  # dil imajı ama sürüm okunamıyor: hedefin altında olabilir
                    out.append(_site("devops", devrepo, am.DEVOPS_PROJECT, am.DEVOPS_BRANCH, path, 0, img, img,
                                     "imaj sürümü okunamadı"))
                continue
            if v >= _v(to):
                continue
            if text is None:
                text = rd(devrepo, path) or ""
            new, blocked = _prod_image(by_devrepo.get(devrepo, set()), img, lang, to, resolver=resolver)
            lines = [i for i, ln in enumerate(text.splitlines(), 1)
                     if (m := re.match(r"(?i)\s*FROM\s+(?:--\S+\s+)?(\S+)", ln)) and m.group(1) == img]
            if not lines:
                out.append(_site("devops", devrepo, am.DEVOPS_PROJECT, am.DEVOPS_BRANCH, path, 0, img, new, NO_LINE))
                continue
            for line in lines:
                out.append(_site("devops", devrepo, am.DEVOPS_PROJECT, am.DEVOPS_BRANCH, path, line, img, new, blocked))
    return out


def _ci_sites(client, repo, lang, to, yml_files, *, resolver=None) -> list[dict]:
    return _ci_scan(client, repo, lang, to, yml_files, resolver=resolver)[0]


# Yalnız enjekte edilen `yml_files` yolu (testler): dosyaların hangi repoda durduğu verilmez, bu adla etiketlenir.
_INJECTED_SRC = {"repo": "yml-files", "project": "EcomBackend", "branch": "main"}
_CI_KEYS = {"php": "phpVersion", "python": "pythonVersion", "go": "goVersion"}


def _ci_scan(client, repo, lang, to, yml_files, *, resolver=None) -> tuple[list[dict], list[dict] | None]:
    """(siteler, sabitlemeler). Sabitleme: repoyu checkout eden test hattı dosyasında dilin sürüm satırı, sürümü ne
    olursa olsun — `{repo, project, branch, path, line, version}`; site yalnız hedefin altındakiler. Test hattı
    okunamazsa sabitlemeler None (bilinmiyor): "test hattı sabitlemesi yok" ile karışmasın diye."""
    r = _ci_read(client, repo, lang, to, yml_files, resolver=resolver)
    return r["sites"], r["pins"]


def _ci_read(client, repo, lang, to, yml_files, *, resolver=None) -> dict:
    """{sites, pins, source, definition, live}. Canlı yol (`yml_files is None`): kaynak `<repo>-test` build
    TANIMIDIR (`client.get_test_pipeline`) — repository + process.yamlFilename; o tek dosya tanımın reposundan /
    dalından / projesinden okunur. Enjekte yol (testler): verilen `Test Pipelines/` dosyalarından repoyu ananlar."""
    if not _CI_KEYS.get(lang):
        return {"sites": [], "pins": [], "source": None, "definition": None, "live": yml_files is None}
    if yml_files is None:
        return _ci_live(client, repo, lang, to, resolver=resolver)
    src = dict(_INJECTED_SRC)
    mention = _mention_re(repo)
    out, pins = [], []
    for path in sorted(yml_files):
        if not path.startswith(CI_DIR + "/") or not mention.search(yml_files[path] or ""):
            continue
        s_, p_ = _parse_ci_file(yml_files[path] or "", path, lang, to, src, resolver=resolver)
        out += s_
        pins += p_
    source = {k: pins[0][k] for k in ("repo", "project", "branch", "path")} if pins else None
    return {"sites": out, "pins": pins, "source": source, "definition": None, "live": False}


_TEMPLATE_RE = re.compile(r"(?m)^\s*(?:-\s*)?(?:template|extends)\s*:")


def _http_404(e: Exception) -> bool:
    return getattr(getattr(e, "response", None), "status_code", None) == 404


def _legacy_ci_file(client, repo: str) -> tuple[str, str] | None:
    """Tanım yokken eski keşif: kod reposunun projesindeki `yml-files:Test Pipelines/` altında repoyu anan dosya
    (proje, yol). O projede yml-files yoksa (404) None; başka her hata yükselir."""
    proj = client._repo_project_strict(repo)
    try:
        items = client.get_items_in_path(_INJECTED_SRC["repo"], "/" + CI_DIR, recursion_level="oneLevel",
                                         project=proj)
    except Exception as e:  # noqa: BLE001
        if _http_404(e):
            return None
        raise
    mention = _mention_re(repo)
    for it in sorted(items or [], key=lambda x: str(x.get("path") or "")):
        p = str(it.get("path") or "").lstrip("/")
        if it.get("isFolder") or not p.lower().endswith((".yml", ".yaml")):
            continue
        if mention.search(client.get_file_content(_INJECTED_SRC["repo"], "/" + p, project=proj) or ""):
            return proj, p
    return None


def _ci_live(client, repo: str, lang: str, to: str, *, resolver=None) -> dict:
    """Canlı test hattı okuması — her belirsizlik kapıyı kapalı tutar: okunamayan → pins None; tanımlanamayan /
    elle ele alınması gereken → tek okunamayan sabitleme (`version: ""`)."""
    res = {"sites": [], "pins": [], "source": None, "definition": None, "live": True}
    try:
        d = client.get_test_pipeline(repo)
    except Exception as e:  # noqa: BLE001
        log.warning(f"⚠️ upgrade: '{repo}-test' tanımı okunamadı: {e}")
        return dict(res, pins=None)
    res["definition"] = d
    if d is None:
        try:
            legacy = _legacy_ci_file(client, repo)
        except Exception as e:  # noqa: BLE001
            log.warning(f"⚠️ upgrade: {repo} için eski test hattı dosyası aranamadı: {e}")
            return dict(res, pins=None)
        if legacy:
            src = {"repo": _INJECTED_SRC["repo"], "project": legacy[0], "branch": ""}
            res["pins"] = [_ci_pin(src, legacy[1], 0, version="",
                                   unparsed=f"test hattı dosyası var, '{repo}-test' tanımı yok — elle ele alınmalı")]
        return res
    src = {"repo": str(d.get("repo_name") or ""), "project": str(d.get("repo_project") or ""),
           "branch": str(d.get("branch") or "")}
    path = str(d.get("yaml") or "").lstrip("/")

    def unread(why: str) -> dict:
        res["pins"] = [_ci_pin(src, path, 0, version="", unparsed=why)]
        return res
    if not path:
        return unread(f"'{d.get('name')}' test hattı tanımı bir pipeline reposundaki YAML dosyası değil — "
                      "elle ele alınmalı")
    if src["repo"].lower() == repo.lower():
        return unread(f"'{d.get('name')}' test hattı dosyası kod reposunun kendisinde ({path}) — elle ele alınmalı")
    if not all(src.values()):
        return unread(f"'{d.get('name')}' test hattı dosyasının yeri (repo / proje / dal) okunamadı")
    res["source"] = dict(src, path=path)
    try:
        text = client.get_file_content(src["repo"], "/" + path, branch=src["branch"], project=src["project"]) or ""
    except Exception as e:  # noqa: BLE001
        log.warning(f"⚠️ upgrade: test hattı dosyası okunamadı ({src['project']}/{src['repo']}:{path}): {e}")
        return dict(res, pins=None)
    if not _mention_re(repo).search(text):
        return unread(f"paylaşılan / şablon dosya — {path} {repo} reposunu checkout etmiyor")
    sites, pins = _parse_ci_file(text, path, lang, to, src, resolver=resolver)
    if not pins and _TEMPLATE_RE.search(text):
        return unread(f"{path} template / extends kullanıyor, sürüm satırı bulunamadı — elle doğrulanmalı")
    res["sites"], res["pins"] = sites, pins
    return res


def _mention_re(repo: str):
    """Dosya repoyu checkout ediyor mu: `name: <proje>/<repo>` satırı."""
    return re.compile(rf"(?im)^\s*(?:-\s*)?name:\s*['\"]?(?:[\w.-]+/)?{re.escape(repo)}['\"]?\s*$")


def _indent(ln: str) -> int:
    return len(ln) - len(ln.lstrip(" "))


def _parse_ci_file(text: str, path: str, lang: str, to: str, src: dict, *,
                   resolver=None) -> tuple[list[dict], list[dict]]:
    """Tek test hattı dosyasının sabitlemeleri ve siteleri. Tanınan biçimler: `<anahtar>: '8.2'`, liste biçimi
    değişken / parametre (`- name: <anahtar>` + `value:` / `default:`), sürüm görevi bloğu (GoTool `version`,
    UsePythonVersion `versionSpec`) ve dil imajı (`image:` / `container:`). Bilinmeyen 'yok' sayılmaz: değeri
    bulunamayan sürüm görevi, değersiz liste değişkeni ve anahtar adı geçtiği hâlde ondan sabitleme çıkmayan dosya
    okunamayan sabitleme (`version: ""`, `unparsed`) üretir — kapı kapalı kalır."""
    key = _CI_KEYS.get(lang)
    out: list[dict] = []
    pins: list[dict] = []
    if not key:
        return out, pins
    keyline = re.compile(rf"^\s*{key}\s*:(.*)$")
    namevar = re.compile(rf"""^(\s*)-\s*name:\s*(['"]?){key}\2\s*(?:#.*)?$""")
    key_word = re.compile(rf"(?<![\w]){key}(?![\w])")
    task_re, task_key = _CI_TASKS.get(lang, (None, None))
    task = None       # açık sürüm görevi bloğu: {"line", "indent", "name"}
    var = None        # açık `- name: <anahtar>` öğesi: {"line", "indent"}
    from_key = False  # anahtar adını taşıyan bir satırdan sabitleme çıktı mı
    lines = text.splitlines()

    def unread(line: int, why: str) -> None:
        pins.append(_ci_pin(src, path, line, version="", unparsed=why))

    for i, ln in enumerate(lines, 1):
        if not ln.strip() or re.match(r"^\s*#", ln):
            continue
        ind = _indent(ln)
        # Blok, görevin / öğenin girintisinde ya da daha sığ İLK satırda biter (sonraki adım, `- publish:`,
        # `- ${{ … }}`, `variables:` …): sürümsüz görev başka bir adımın `version:`'ını almaz.
        if task is not None and ind <= task["indent"]:
            unread(task["line"], f"{task['name']} sürümü okunamadı")
            task = None
        if var is not None and ind <= var["indent"]:
            unread(var["line"], f"{key} değeri okunamadı")
            var = None
        if task_re is not None and (m := task_re.match(ln)):
            task = {"line": i, "indent": len(m.group(1)), "name": m.group(3).split("@")[0]}
            continue
        if (m := namevar.match(ln)):
            var = {"line": i, "indent": len(m.group(1))}
            from_key = True
            continue
        if task is not None and (m := re.match(rf"^\s*{task_key}\s*:(.*)$", ln)):
            task = None
            _ci_value_pin(out, pins, m.group(1), path, i, src, to, lang=lang, resolver=resolver)
            from_key = from_key or bool(key_word.search(ln))
            continue
        if var is not None and (m := re.match(r"^\s*(?:value|default)\s*:(.*)$", ln)):
            var = None
            _ci_value_pin(out, pins, m.group(1), path, i, src, to, lang=lang, resolver=resolver)
            continue
        if (m := keyline.match(ln)):
            _ci_value_pin(out, pins, m.group(1), path, i, src, to, lang=lang, resolver=resolver)
            from_key = True
            continue
        if (m := _CI_IMAGE_KEY.match(ln)):
            _ci_image_pin(out, pins, m.group(1), m.group(2), lang, path, i, src, to, resolver=resolver)
    if task is not None:
        unread(task["line"], f"{task['name']} sürümü okunamadı")
    if var is not None:
        unread(var["line"], f"{key} değeri okunamadı")
    if not from_key:
        first = next((i for i, ln in enumerate(lines, 1)
                      if not re.match(r"^\s*#", ln) and key_word.search(ln)), None)
        if first is not None:
            unread(first, f"{key} geçiyor ama sürüm satırı tanınmadı")
    return out, pins


# Sürüm görevi → bloğundaki sürüm anahtarı (gerçek test hatları: gocrons GoTool@0 version, Python
# UsePythonVersion@0 versionSpec). Görev adı tırnaklı olabilir, satır sonunda yorum olabilir.
_CI_TASKS = {"go": (re.compile(r"""^(\s*)-\s*task:\s*(['"]?)(GoTool@\d+)\2\s*(?:#.*)?$"""), "version"),
             "python": (re.compile(r"""^(\s*)-\s*task:\s*(['"]?)(UsePythonVersion@\d+)\2\s*(?:#.*)?$"""),
                        "versionSpec")}
_CI_IMAGE_KEY = re.compile(r"^\s*(?:-\s*)?(image|container)\s*:(.*)$")
_CI_IMG_NAME = {"go": "golang", "python": "python"}


def _unquote(value: str) -> tuple[str, str, str]:
    """(ham, tırnak, iç) — satır sonu yorumu atılır."""
    raw = re.sub(r"\s+#.*$", "", value).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw, raw[0], raw[1:-1]
    return raw, "", raw


def _library_patch(img: str, lang: str, to: str, resolver) -> str | None:
    """CI / kod reposu üç parçalı genel imaj: yama sürümü çözülür ve yeni etiket Hub'da varsa yeni imaj, yoksa None."""
    name = _LANG_IMG_NAME.get(lang)
    full = resolver.latest_patch(name, to) if resolver and name and is_library_image(img, lang) else None
    new = image_for_full(img, lang, full) if full else img
    return new if new != img and _library_tag_ok(new, lang, resolver) else None


def _ci_pin(src: dict, path, line, **kw) -> dict:
    """Sabitleme: dosyanın durduğu repo / proje / dal tanımdan (ya da enjeksiyondan) gelir."""
    return {"repo": src["repo"], "project": src["project"], "branch": src["branch"], "path": path, "line": line, **kw}


def _ci_value_pin(out, pins, value, path, i, src, to, *, lang=None, resolver=None) -> None:
    """`phpVersion: '8.2'` / GoTool `version:` / `versionSpec:` değeri."""
    raw, q, inner = _unquote(value)
    mm = re.fullmatch(r"(\d+)\.(\d+)(\.\d+)?", inner)
    if not mm:
        # `3.x`, `$(var)`, `8` …: sürüm okunamıyor — kapı kapalı kalır, satırı insan düzenler
        pins.append(_ci_pin(src, path, i, version="", unparsed=inner[:40]))
        return
    ver = f"{mm.group(1)}.{mm.group(2)}"
    pins.append(_ci_pin(src, path, i, version=ver))
    if _v(ver) < _v(to):
        if mm.group(3):   # 1.22.5: rakam-dizisi kuralı 1.25.5'e izin verirdi — yanlış; yalnız yama çözücüyle yazılır
            full = resolver.latest_patch(_LANG_IMG_NAME.get(lang, ""), to) if resolver and lang else None
            out.append(_site("ci", src["repo"], src["project"], src["branch"], path, i, raw,
                             f"{q}{full}{q}" if full else raw, "" if full else CI_HAND))
        else:
            out.append(_site("ci", src["repo"], src["project"], src["branch"], path, i, f"{q}{ver}{q}", f"{q}{to}{q}"))


def _ci_image_pin(out, pins, key, value, lang, path, i, src, to, *, resolver=None) -> None:
    """`image:` / `container:` değeri dilin imajıysa sabitlemedir (`golang:1.21.4`, `…/golang:1.21-alpine`,
    `python:3.11-slim`, `…/php-8-2:1.0`). `container:` etiketsizse kaynak takma adıdır (`container: golang`)."""
    _raw, _q, img = _unquote(value)
    if not img or " " in img:
        return
    if lang == "php":
        if not re.search(r"(?:^|/)php(?:[-:]|$)", img):
            return
        v = pm.parse_php_image(img)
    else:
        name = _CI_IMG_NAME.get(lang)
        m = re.search(rf"(?:^|/){name}(?::([^\s/]*))?$", img) if name else None
        if not m:
            return
        tag = (m.group(1) or "").split("-")[0]
        tm = re.fullmatch(r"(\d+)\.(\d+)(\.\d+)?", tag)
        v = (int(tm.group(1)), int(tm.group(2))) if tm else None
    if key == "container" and ":" not in img:
        return
    if not v:
        pins.append(_ci_pin(src, path, i, version="", unparsed=img[:40]))
        return
    ver = _fmt(v)
    pins.append(_ci_pin(src, path, i, version=ver))
    if v < _v(to):
        new = image_for(img, lang, to)
        if _three_part(img, lang):
            new = _library_patch(img, lang, to, resolver)
            if new:
                out.append(_site("ci", src["repo"], src["project"], src["branch"], path, i, img, new))
            else:
                out.append(_site("ci", src["repo"], src["project"], src["branch"], path, i, img, img, CI_HAND))
        elif new == img:
            out.append(_site("ci", src["repo"], src["project"], src["branch"], path, i, img, img, CI_HAND))
        else:
            out.append(_site("ci", src["repo"], src["project"], src["branch"], path, i, img, new))


def _range_hard(constraint: str) -> bool:
    """Üst sınır, dışlama ya da tire aralığı içeren kısıt yeniden yazılırsa yapısı bozulur."""
    return bool(re.search(r"[<!]|\s-\s", constraint))


def _code_sites(repo, root: Path, lang, to, *, resolver=None) -> list[dict]:
    root = Path(root)
    out = []

    def rd(name):
        try:
            return (root / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    def add(path, line, old, new, blocked=""):
        out.append(_site("code", repo, "", "", path, line, old, new, blocked))

    if lang == "php":
        text = rd("composer.json")
        try:
            doc = json.loads(text) or {}
        except ValueError:
            doc = {}
        cand = ((doc.get("require") or {}).get("php"), ((doc.get("config") or {}).get("platform") or {}).get("php"))
        for val in sorted({str(v) for v in cand if v}):
            nums = re.findall(r"\d+\.\d+", val)
            if not nums or min(_v(n) for n in nums) >= _v(to):
                continue
            hard = _range_hard(val)
            new = val if hard else (re.sub(r"\d+\.\d+", to, val, count=1) if len(nums) == 1 else f">={to}")
            for i, ln in enumerate(text.splitlines(), 1):
                if re.search(rf'"php"\s*:\s*"{re.escape(val)}"', ln) and ln.count(val) == 1:
                    add("composer.json", i, val, new, RANGE_BLOCK if hard else "")
    elif lang == "go":
        text = rd("go.mod")
        for pat, fn in ((r"^go\s+(\d+\.\d+(?:\.\d+)?)\s*$", lambda o: to + (".0" if o.count(".") == 2 else "")),
                        (r"^toolchain\s+(go\d+\.\d+(?:\.\d+)?)\s*$", lambda o: f"go{to}.0")):
            for i, ln in enumerate(text.splitlines(), 1):
                m = re.match(pat, ln.strip())
                if m and _v(m.group(1).removeprefix("go")) < _v(to):
                    add("go.mod", i, m.group(1), fn(m.group(1)))
    elif lang == "python":
        for i, ln in enumerate(rd("pyproject.toml").splitlines(), 1):
            m = re.match(r"""\s*requires-python\s*=\s*(['"])([^'"\d]*)(\d+\.\d+)([^'"]*)\1""", ln)
            if m and _v(m.group(3)) < _v(to):
                old = m.group(2) + m.group(3) + m.group(4)
                if _range_hard(old):
                    add("pyproject.toml", i, old, old, RANGE_BLOCK)
                else:
                    add("pyproject.toml", i, old, m.group(2) + to + m.group(4))
        for i, ln in enumerate(rd(".python-version").splitlines(), 1):
            m = re.match(r"^\s*(\d+\.\d+(?:\.\d+)?)\s*$", ln)
            if m and _v(m.group(1)) < _v(to):
                add(".python-version", i, m.group(1), to)
    if lang in ("php", "python", "go"):
        for i, ln in enumerate(rd("Dockerfile").splitlines(), 1):
            m = re.match(r"(?i)\s*FROM\s+(?:--\S+\s+)?(\S+)", ln)
            v = _image_version(m.group(1), lang) if m else None
            if v and v < _v(to):
                if _three_part(m.group(1), lang):   # `golang:1.24.4-alpine`: etiket uydurulmaz — Hub'da varsa yazılır
                    new = _library_patch(m.group(1), lang, to, resolver)
                    add("Dockerfile", i, m.group(1), new or m.group(1), "" if new else CI_HAND)
                else:
                    add("Dockerfile", i, m.group(1), image_for(m.group(1), lang, to))
    return out


def sites(client, repo: str, root: Path, lang: str, current: str, to: str, index: dict, *,
          read=None, yml_files: dict | None = None, per_src: list | None = None, resolver=None) -> list[dict]:
    """Sürümün sabitlendiği yerler: FLODevOps Dockerfile'ları, yml-files test hattı, kod reposu.
    per_src: hedef aramasının servis listesi (app_map + build_index birleşimi)."""
    return (_devops_sites(client, repo, lang, to, index, read, per_src, resolver=resolver)
            + _ci_sites(client, repo, lang, to, yml_files, resolver=resolver)
            + _code_sites(repo, root, lang, to, resolver=resolver))


def plan(client, repo: str, lang: str, to: str | None = None, *, deps=None, index=None, root=None,
         read=None, yml_files=None, app_map_cache: dict | None = None, resolver=None) -> dict:
    from agile_sdlc_crew.quality import modern
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.quality import upgrade_registry
    if resolver is None and pc.get("CREW_TECHDEBT_UPGRADE_PATCH"):
        resolver = upgrade_registry.default()
    deps = deps or pm.default_deps()
    if root is None:
        root = pm._root(deps.cfg(), repo, deps)
    if index is None:
        try:
            index = modern.build_index(client) if client is not None else {}
        except Exception as e:  # noqa: BLE001
            log.warning(f"⚠️ upgrade: FLODevOps build dizini okunamadı: {e}")
            index = {}
    if lang == "php" and (client is None or app_map_cache is not None):
        # ağsız (test) yol ya da çağıranın verdiği app_map: eşleme yüklenmez, önbellek okunmaz/yazılmaz
        tv = pm.target_version(client, repo, app_map_cache=app_map_cache or {}, repo_root=Path(root),
                               write_cache=False, max_age_h=0, index=index)
    else:
        tv = modern.target(client, repo, Path(root), lang, index=index)
    current = tv.get("version")
    out = {"repo": repo, "lang": lang, "current": current, "target": None, "target_source": None,
           "selectable": [], "sites": [], "ci_pins": [], "ci_source": None, "per_src": tv.get("per_src") or [],
           "up_to_date": False}
    if not current:
        return out
    sel = selectable(lang, current, index, _devops_services(repo, lang, index, out["per_src"]), resolver=resolver)
    if to is not None:
        if not isinstance(to, str) or not _VER_RE.match(to) or to not in {s["version"] for s in sel}:
            raise ValueError(f"Hedef sürüm seçilemez: {to!r}")
        target, source = to, "seçim"
    else:
        target, source = default_target(lang, current, index), "varsayılan"
    out.update(selectable=sel, target=target, target_source=source if target else None)
    if not target or _v(current) >= _v(target):
        out["up_to_date"] = True
        return out
    ci = _ci_read(client, repo, lang, target, yml_files, resolver=resolver)
    out["sites"] = (_devops_sites(client, repo, lang, target, index, read, out["per_src"], resolver=resolver)
                    + ci["sites"] + _code_sites(repo, Path(root), lang, target, resolver=resolver))
    out["ci_pins"], out["ci_source"] = ci["pins"], ci["source"]
    if ci["live"]:
        # Çözülmüş `<repo>-test` tanımı (ya da None): kanıt aynı tanımla aranır, ikinci ad araması yapılmaz.
        out["ci_test_def"] = ci["definition"]
    return out


# ── Hedef sürümün aracıyla doğrulama ─────────────────────────────────────

PROBE_PY = Path(__file__).resolve().parent / "upgrade_probe.py"
_PY_SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "env", "site-packages", "__pycache__", "build", "dist",
                 ".tox", ".mypy_cache"}
_PHP_MSG = re.compile(r"^(?:PHP )?(Parse error|Fatal error|Deprecated):\s*(.*?)(?: in (.+?))? on line (\d+)\s*$")
_GO_MSG = re.compile(r"^(?:vet: )?((?:[\w.@-]+/)*[\w.@-]+\.go):(\d+)(?::\d+)?:\s*(.*)$")


def _hit(rule: str, rel: str, line: int, text: str, message: str) -> dict:
    return {"rule": rule, "file": rel, "line": int(line or 0), "text": (text or "").strip()[:200],
            "message": (message or "").strip()[:300]}


def _src_line(root: Path, rel: str, n: int) -> str:
    try:
        lines = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[n - 1] if 0 < n <= len(lines) else ""
    except OSError:
        return ""


def _inside(root: Path, rel: str) -> bool:
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        return False
    try:
        (root / rel).resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _walk_files(root: Path, suffix: str, lang: str) -> list[str]:
    """Probe dosya listesi. PHP: `php_modern` taramasının dışlamaları (ad ya da yol öneki); Python: sanal ortam /
    üretilmiş dizinler. Sembolik bağlar izlenmez."""
    if lang == "php":
        ex = [e.strip("/") for e in pm.load_cfg().get("php_exclude", []) if e.strip("/")]
    else:
        ex = []
    out: list[str] = []
    for dp, dns, fns in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dp, root)
        rel_dir = "" if rel_dir == "." else rel_dir

        def skip(d: str) -> bool:
            rel = (rel_dir + "/" + d).lstrip("/")
            if d == ".git" or (lang == "python" and d in _PY_SKIP_DIRS):
                return True
            return any(rel == e or rel.startswith(e + "/") or ("/" not in e and d == e) for e in ex)
        dns[:] = sorted(d for d in dns if not skip(d))
        for fn in sorted(fns):
            p = Path(dp) / fn
            if fn.endswith(suffix) and not p.is_symlink():
                out.append((rel_dir + "/" + fn).lstrip("/"))
    return out


def _probe_php(root: Path, binp: str, files, timeout: int) -> tuple[list[dict], dict]:
    from agile_sdlc_crew import verify
    rels = [f for f in (files if files is not None else _walk_files(root, ".php", "php")) if _inside(root, f)]
    hits: list[dict] = []
    seen: set = set()
    deadline = time.monotonic() + timeout
    done = 0
    with tempfile.TemporaryDirectory(prefix="tempo-probe-") as cwd:
        for rel in rels:
            left = deadline - time.monotonic()
            if left <= 1:
                break
            try:
                _, out = verify._run([binp, "-d", "error_reporting=-1", "-d", "display_errors=1", "-l",
                                      str((root / rel).resolve())], Path(cwd), int(min(30, left)))
            except (OSError, subprocess.SubprocessError):
                done += 1
                continue
            done += 1
            for ln in out.splitlines():
                m = _PHP_MSG.match(ln.strip())
                if not m:
                    continue
                kind, msg, _p, num = m.group(1), m.group(2), m.group(3), int(m.group(4))
                rule = "probe:deprecation" if kind == "Deprecated" else "probe:error"
                key = (rule, rel, num, msg)
                if key in seen:
                    continue
                seen.add(key)
                hits.append(_hit(rule, rel, num, _src_line(root, rel, num), f"{kind}: {msg}"))
    st = {"ran": True, "tool": binp, "reason": ""}
    if done < len(rels):
        st["reason"] = f"süre doldu: {done}/{len(rels)} dosya denetlendi"
    return hits, st


def _probe_python(root: Path, binp: str, files, timeout: int) -> tuple[list[dict], dict]:
    from agile_sdlc_crew import verify
    rels = [f for f in (files if files is not None else _walk_files(root, ".py", "python")) if _inside(root, f)]
    hits: list[dict] = []
    deadline = time.monotonic() + timeout
    done = 0
    with tempfile.TemporaryDirectory(prefix="tempo-probe-") as cwd:
        for i in range(0, len(rels), 100):
            left = deadline - time.monotonic()
            if left <= 1:
                break
            chunk = rels[i:i + 100]
            try:
                _, out = verify._run([binp, "-I", str(PROBE_PY), str(root.resolve()), *chunk], Path(cwd), int(left))
            except (OSError, subprocess.SubprocessError):
                break
            done += len(chunk)
            for ln in out.splitlines():
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(d, dict) and d.get("rule") in ("probe:error", "probe:deprecation"):
                    hits.append(_hit(d["rule"], str(d.get("file") or ""), d.get("line") or 0, str(d.get("text") or ""),
                                     str(d.get("message") or "")))
    st = {"ran": True, "tool": binp, "reason": ""}
    if done < len(rels):
        st["reason"] = f"süre doldu: {done}/{len(rels)} dosya denetlendi"
    return hits, st


def _probe_go(root: Path, to: str, timeout: int) -> tuple[list[dict], dict]:
    from agile_sdlc_crew import verify
    if not (root / "go.mod").is_file():
        return [], {"ran": False, "tool": "go", "reason": "go.mod yok"}
    if not _tool_ok("go", to):
        return [], {"ran": False, "tool": "go", "reason": f"yerel Go {to} sürümünden eski — hedef sürüm aracı yok"}
    git = ["git", "-c", "core.hooksPath=/dev/null"]
    tmp = Path(tempfile.mkdtemp(prefix="tempo-gprobe-"))
    wt = tmp / "wt"
    hits: list[dict] = []
    reason = ""
    try:
        code, out = verify._run([*git, "worktree", "add", "-q", "--detach", str(wt), "HEAD"], root, 120)
        if code != 0:
            return [], {"ran": False, "tool": "go", "reason": f"geçici worktree açılamadı: {out[:200]}"}
        gm = wt / "go.mod"
        text = gm.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"(?m)^toolchain\s+.*\n?", "", text)
        text = re.sub(r"(?m)^go\s+\S+\s*$", f"go {to}", text) if re.search(r"(?m)^go\s+\S+\s*$", text) \
            else text.rstrip("\n") + f"\n\ngo {to}\n"
        gm.write_text(text, encoding="utf-8")
        mod = "-mod=vendor" if (wt / "vendor" / "modules.txt").is_file() else "-mod=readonly"
        deadline = time.monotonic() + timeout
        for cmd, rule in ((["go", "build", mod, "./..."], "probe:error"), (["go", "vet", mod, "./..."], "probe:deprecation")):
            left = deadline - time.monotonic()
            if left <= 1:
                reason = "süre doldu"
                break
            try:
                rc, out = verify._run(cmd, wt, int(left))
            except subprocess.TimeoutExpired:
                reason = "süre doldu"
                break
            found = 0
            for ln in out.splitlines():
                m = _GO_MSG.match(ln.strip())
                if m:
                    rel = m.group(1).removeprefix("./")
                    if _inside(wt, rel):
                        hits.append(_hit(rule, rel, int(m.group(2)), _src_line(wt, rel, int(m.group(2))), m.group(3)))
                        found += 1
            if rc != 0 and not found:
                # Konumsuz hata (ör. go.mod güncellenmeli): derleme bir yere varmadı → doğrulanamadı, temiz değil.
                return [], {"ran": False, "tool": "go",
                            "reason": f"doğrulanamadı: {cmd[1]} konumsuz hata verdi: {out[:200]}"}
            if rc != 0 and rule == "probe:error":
                break  # vet, derleme hatalarını `vet:` önekiyle tekrarlar; hatalı derlemede çalıştırılmaz
    finally:
        verify._run([*git, "worktree", "remove", "--force", str(wt)], root, 60)
        verify._run([*git, "worktree", "prune"], root, 60)
        shutil.rmtree(tmp, ignore_errors=True)
    return hits, {"ran": True, "tool": "go", "reason": reason}


def probe(root: Path, lang: str, current: str, to: str, *, files: list[str] | None = None,
          timeout: int = 300) -> tuple[list[dict], dict]:
    """Hedef sürümün KENDİ aracıyla depoyu doğrula (depo kodu çalıştırılmaz).
    Dönüş: (hit'ler, {"ran", "tool", "reason"}). Araç yoksa ([], ran=False)."""
    root = Path(root)
    none = {"ran": False, "tool": "", "reason": "hedef sürüm için araç yok — yalnız katalog"}
    if not isinstance(to, str) or not _VER_RE.match(to) or not root.is_dir():
        return [], {**none, "reason": "geçersiz hedef sürüm ya da repo dizini"}
    if lang in ("php", "python"):
        binp = _target_bin(lang, to)
        if not binp:
            return [], none
        fn = _probe_php if lang == "php" else _probe_python
        return fn(root, binp, files, timeout)
    if lang == "go":
        return _probe_go(root, to, timeout)
    return [], {"ran": False, "tool": "", "reason": "araç yok"}


def probe_rules(hits: list[dict], *, to: str | None = None) -> list[pm.Rule]:
    """probe hit'lerinden genel kurallar (kural anahtarı başına bir tane; başlık/özet ilk mesajdan)."""
    since = _v(to) if to else (0, 0)
    out: list[pm.Rule] = []
    seen: set[str] = set()
    for h in hits:
        key = h.get("rule")
        if key not in ("probe:error", "probe:deprecation") or key in seen:
            continue
        seen.add(key)
        err = key == "probe:error"
        msg = str(h.get("message") or "")
        out.append(pm.Rule(
            key=key, since=since, kind="deprecation" if err else "structural",
            title=f"{key}: {msg[:80]}", summary=msg,
            advantages=["Hedef sürümde çalışmayı doğrular."],
            risks=(["Düzeltme hedef sürümün aracının mesajına göre yapılır."] if err else
                   ["Aracın otomatik düzeltmesi yok — her yer elle değerlendirilmeli."]),
            before="", after="", ref="", auto=err, sp_unit=1, line_local=True,
            breaks="error" if err else "deprecation"))
    return out


# ── Bağımlılık engelleri ─────────────────────────────────────────────────

_INF = (10 ** 6, 0, 0)


def _nums(s: str) -> tuple[list[int], bool]:
    """'8.1.0' → ([8,1,0], wildcard?). Anlaşılmazsa ValueError."""
    s = s.strip().lstrip("vV")
    s = re.sub(r"[-+@].*$", "", s) if re.match(r"^\d", s) else s
    parts = s.split(".")
    wild = False
    nums: list[int] = []
    for p in parts:
        if p in ("*", "x", "X"):
            wild = True
            break
        if not p.isdigit():
            raise ValueError(s)
        nums.append(int(p))
    if not nums and not wild:
        raise ValueError(s)
    return nums, wild


def _t(nums: list[int]) -> tuple[int, int, int]:
    n = (list(nums) + [0, 0, 0])[:3]
    return n[0], n[1], n[2]


def _bump(nums: list[int], idx: int) -> tuple[int, int, int]:
    n = (list(nums) + [0, 0, 0])[:3]
    n[idx] += 1
    for j in range(idx + 1, 3):
        n[j] = 0
    return n[0], n[1], n[2]


def _atom(tok: str) -> list[tuple]:
    """Tek kısıt parçası → [lo, hi) aralıkları (üçlü sürüm demetleri)."""
    tok = tok.strip()
    if tok in ("", "*", "x", "X"):
        return [((0, 0, 0), _INF)]
    m = re.match(r"^(\^|~|>=|<=|==|!=|=|>|<)?\s*(.+)$", tok)
    if not m:
        raise ValueError(tok)
    op, rest = m.group(1) or "", m.group(2)
    nums, wild = _nums(rest)
    n = len(nums)
    if op == "^":
        lo = _t(nums)
        if n == 0:
            raise ValueError(tok)
        idx = next((i for i, x in enumerate(nums) if x), n - 1)
        return [(lo, _bump(nums, min(idx, 2)))]
    if op == "~":
        if n == 0:
            raise ValueError(tok)
        return [(_t(nums), _bump(nums, 0 if n <= 2 else 1))]
    if op in ("", "=", "==") or wild:
        if wild or (op == "" and n < 3 and wild):
            if n == 0:
                return [((0, 0, 0), _INF)]
            return [(_t(nums), _bump(nums, n - 1))]
        return [(_t(nums), _bump(nums, 2))]
    if op == ">=":
        return [(_t(nums), _INF)]
    if op == ">":
        return [(_bump(nums, n - 1) if n < 3 else _bump(nums, 2), _INF)]
    if op == "<":
        return [((0, 0, 0), _t(nums))]
    if op == "<=":
        return [((0, 0, 0), _bump(nums, n - 1) if n < 3 else _bump(nums, 2))]
    if op == "!=":
        lo, hi = _t(nums), (_bump(nums, n - 1) if n < 3 else _bump(nums, 2))
        return [((0, 0, 0), lo), (hi, _INF)]
    raise ValueError(tok)


def _intersect(a: list[tuple], b: list[tuple]) -> list[tuple]:
    out = []
    for lo1, hi1 in a:
        for lo2, hi2 in b:
            lo, hi = max(lo1, lo2), min(hi1, hi2)
            if lo < hi:
                out.append((lo, hi))
    return out


def php_constraint_allows(constraint: str, version: str) -> bool:
    """Composer `require.php` kısıtı `version` (N.N serisi, herhangi bir yama) sürümüne izin veriyor mu.
    Anlaşılmayan kısıt `True` döner (engel sayılmaz)."""
    try:
        a, b = _nums(version)[0][:2]
        series = [((a, b, 0), (a, b + 1, 0))]
        c = (constraint or "").strip()
        if not c:
            return True
        for branch in re.split(r"\s*\|\|?\s*", c):
            branch = re.sub(r"(\^|~|>=|<=|==|!=|=|>|<)\s+", r"\1", branch.strip())
            m = re.match(r"^(\S+)\s+-\s+(\S+)$", branch)
            if m:
                lo_n, _w = _nums(m.group(1))
                hi = _atom("<=" + m.group(2))
                cur = _intersect([(_t(lo_n), _INF)], hi)
            else:
                cur = [((0, 0, 0), _INF)]
                for tok in [t for t in re.split(r"[\s,]+", branch) if t]:
                    cur = _intersect(cur, _atom(tok))
            if _intersect(cur, series):
                return True
        return False
    except (ValueError, IndexError):
        return True


def _nextjs_peer_blockers(root: Path, to: str) -> list[dict]:
    """react / react-dom bildirilen sürümü, hedef Next majörünün istediği en düşük react'in altındaysa."""
    try:
        data = yaml.safe_load(TARGETS_PATH.read_text(encoding="utf-8")) or {}
        minimum = str((data.get("nextjs_peer_react") or {}).get(to) or "")
        pkg = json.loads((root / "package.json").read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, yaml.YAMLError, AttributeError):
        return []
    if not _VER_RE.match(minimum) or not isinstance(pkg, dict):
        return []
    deps = {}
    for k in ("devDependencies", "dependencies"):
        if isinstance(pkg.get(k), dict):
            deps.update(pkg[k])
    out = []
    for name in ("react", "react-dom"):
        declared = deps.get(name)
        m = re.search(r"\d+\.\d+", declared) if isinstance(declared, str) else None
        if m and _v(m.group(0)) < _v(minimum):
            out.append({"kind": "peer", "name": name, "constraint": declared,
                        "detail": f"Next {to} en az react {minimum} ister"})
    return out


def blockers(root: Path, lang: str, to: str) -> list[dict]:
    """Yükseltmeyi engelleyen bağımlılıklar (iş açmaz, hazır olmayı bekletir).
    PHP: `composer.lock` paketinin `require.php` kısıtı hedefi dışlıyorsa."""
    if not isinstance(to, str) or not _VER_RE.match(to):
        return []
    if lang == "nextjs":
        return _nextjs_peer_blockers(Path(root), to)
    if lang != "php":
        return []
    try:
        doc = json.loads((Path(root) / "composer.lock").read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return []
    out = []
    for p in doc.get("packages") or []:
        if not isinstance(p, dict):
            continue
        c = (p.get("require") or {}).get("php") if isinstance(p.get("require"), dict) else None
        name = str(p.get("name") or "")
        if c and name and not php_constraint_allows(str(c), to):
            out.append({"kind": "dependency", "name": name, "constraint": str(c),
                        "detail": f"{name} {p.get('version', '')}: PHP {c} — {to} sürümünü dışlıyor".replace("  ", " ")})
    return out


# ── Yükseltme taraması ve alımı ──────────────────────────────────────────
# Modernleştirmenin üçüncü kardeşi: aynı gruplama (build_groups), board dedup'ı, Task açma
# (_open_tasks) ve Rector kuyruğu. Fark yalnız kural kümesi (`breaks` dolu, current < since ≤ hedef,
# + hedef aracın bulguları), başlık öneki, `tempo-upgrade` etiketi ve araç kapsamındaki `upgrade_to`.
# Kaynak ve parmak izi dilin mevcut kaynağıdır: repo yükseldikten sonra aynı kural modernleştirme
# taramasında da çıkar, board ikisini tek iş sayar.

TAG = "tempo-upgrade"
_TTL_S = 3600
_PROBE_CACHE: dict[tuple, dict] = {}
_PY_MAX_BYTES = 2_000_000
_COUNT_KEYS = ("error", "behavior", "deprecation")


def _py_matchers() -> dict[str, dict]:
    """Python yükseltme kataloğunun eşleşme alanları (load_rules bunları taşımaz)."""
    try:
        data = yaml.safe_load(PY_UPGRADE_RULES.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return {str(r["key"]): {"modules": [str(m) for m in r.get("modules") or []],
                            "attrs": [str(a) for a in r.get("attrs") or []]} for r in data.get("rules") or []}


def _dotted(node) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def python_hits(root: Path, keys: set[str]) -> list[dict]:
    """Yükseltme kataloğundaki modül / API kullanımları — `ast` ile, depo kodu ÇALIŞTIRILMADAN.
    Ayrıştırılamayan dosya atlanır (sözdizimini hedef yorumlayıcıyla probe ayrıca denetler)."""
    from agile_sdlc_crew.quality import modern
    from agile_sdlc_crew.quality.render import mask_secrets
    root = Path(root)
    match = {k: v for k, v in _py_matchers().items() if k in keys}
    mods = {m: k for k, v in match.items() for m in v["modules"]}
    attrs = [(a, k) for k, v in match.items() for a in v["attrs"]]
    hits: list[dict] = []
    if not mods and not attrs:
        return hits
    for dp, dns, fns in os.walk(root, followlinks=False):
        dns[:] = sorted(d for d in dns if d not in modern.SKIP_DIRS and not d.startswith("."))
        for fn in sorted(fns):
            p = Path(dp) / fn
            if not fn.endswith(".py") or p.is_symlink():
                continue
            try:
                if p.stat().st_size > _PY_MAX_BYTES:
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(text, filename=fn)
            except (OSError, SyntaxError, ValueError, RecursionError, MemoryError):
                continue
            rel = str(p.relative_to(root))
            lines = text.splitlines()

            def add(key: str, n: int) -> None:
                src = lines[n - 1].strip() if 0 < n <= len(lines) else ""
                hits.append({"rule": key, "file": rel, "line": n, "text": mask_secrets(src)[:200],
                             "module": pm.module_of(rel)})
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    names = [node.module]
                for name in names:
                    if name.split(".")[0] in mods:
                        add(mods[name.split(".")[0]], node.lineno)
                if isinstance(node, ast.Attribute) and attrs:
                    d = _dotted(node)
                    for a, key in attrs:
                        if d == a or d.endswith("." + a):
                            add(key, node.lineno)
    return hits


def _go_report_hits(root: Path, keys: set[str]) -> list[dict]:
    """Go'nun yükseltme kuralları yalnız rapor: go.mod'daki `go` satırı hedefe çekilince değişen
    varsayılanlar. Bulgu o satıra düşer (kartta sayı; iş açılmaz)."""
    try:
        lines = (Path(root) / "go.mod").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    n = next((i for i, ln in enumerate(lines, 1) if re.match(r"^go\s+\d", ln.strip())), 1)
    text = lines[n - 1].strip() if lines else ""
    return [{"rule": k, "file": "go.mod", "line": n, "text": text, "module": "."} for k in sorted(keys)]


def _next_hits(root: Path, current: str, keys: set[str], force: bool) -> tuple[list[dict], list[str]]:
    """Next.js katalog desenlerinin `breaks` dolu olanları (modern._hits, önbellekli) + package.json
    kuralları (`next lint` betiği — modern.next_hits yalnız JS/TS dosyalarını gezer)."""
    from agile_sdlc_crew.quality import modern
    found, errors, _sha = modern._hits(root, "nextjs", current, force)
    hits = [dict(h) for h in found if h.get("rule") in keys]
    try:
        pkg = (Path(root) / "package.json").read_text(encoding="utf-8", errors="replace")
    except OSError:
        pkg = ""
    for r in modern._raw_rules("nextjs"):
        if r.get("key") not in keys or not r.get("pattern") or not r.get("file_pattern") \
                or not re.search(r["file_pattern"], "package.json"):
            continue
        for m in re.finditer(r["pattern"], pkg, re.S):
            n = pkg.count("\n", 0, m.start()) + 1
            hits.append({"rule": r["key"], "file": "package.json", "line": n,
                         "text": pkg.splitlines()[n - 1].strip()[:200], "module": "."})
    return hits, errors


def _probe_cached(repo: str, lang: str, root: Path, sha: str, current: str, to: str, force: bool,
                  probe_fn=None) -> tuple[list[dict], dict]:
    """Hedef araçla doğrulama, süreç içi önbellekli (repo × HEAD × hedef; *Yeniden tara* atlar)."""
    if probe_fn is not None:
        return probe_fn(root, lang, current, to)
    key = (lang, repo, sha, to)
    c = _PROBE_CACHE.get(key)
    if c and sha and not force and time.time() - c["at"] < _TTL_S:
        return c["hits"], dict(c["st"])
    hits, st = probe(root, lang, current, to)
    if sha:
        _PROBE_CACHE[key] = {"hits": hits, "st": dict(st), "at": time.time()}
    return hits, st


def _collect(repo: str, lang: str, root: Path, current: str, to: str, cfg: dict, *, force: bool,
             scan_fn=None, head_sha_fn=None, probe_fn=None) -> tuple[list[dict], list[pm.Rule], dict]:
    """Bulgular (katalog + hedef araç) ve kurallar. Katalog satırında zaten bulgu varken hedef aracın
    aynı dosya + satırdaki bulgusu aynı sorunun ikinci görünüşüdür → atılır."""
    from agile_sdlc_crew.quality.render import mask_secrets
    rules = upgrade_rules(catalog(lang), current, to)
    keys = {r.key for r in rules}
    errors: list[str] = []
    if lang == "php":
        with pm._scan_lock(repo):  # aynı repo için paralel php süreçleri açılmasın
            res = pm.scan_cached(repo, root, cache_dir=Path(cfg["scan_cache"]), scan_fn=scan_fn,
                                 head_sha_fn=head_sha_fn, exclude=cfg.get("php_exclude"),
                                 php_bin=cfg.get("php_bin", "php"), ttl_h=int(cfg.get("scan_ttl_h", 24)), force=force)
        hits = [h for h in res.get("hits") or [] if h.get("rule") in keys]
        errors, sha, files = list(res.get("errors") or []), res.get("sha", ""), int(res.get("files") or 0)
    else:
        sha = (head_sha_fn or pm._head_sha)(root)
        if lang == "python":
            hits = python_hits(root, keys)
        elif lang == "nextjs":
            hits, errors = _next_hits(root, current, keys, force)
        else:
            hits = _go_report_hits(root, {r.key for r in rules if r.kind == "report"})
        files = len({h["file"] for h in hits})
    phits, pst = _probe_cached(repo, lang, root, sha, current, to, force, probe_fn)
    seen = {(h.get("file"), int(h.get("line") or 0)) for h in hits}
    extra = []
    for h in phits:
        if (h.get("file"), int(h.get("line") or 0)) in seen:
            continue
        # Task'taki "bulunan yerler" satırı hedef aracın mesajını da göstersin
        text = f"{h.get('text') or ''} ⟵ {h.get('message') or ''}".strip(" ⟵")
        extra.append({**h, "text": mask_secrets(text)[:200], "module": pm.module_of(str(h.get("file") or ""))})
    # Hedef aracın bulgusu için düzeltmenin en düşük sürümü bilinmez; düzeltme şimdiki sürümde yazılır
    prules = [replace(r, fix_min=(0, 0)) for r in probe_rules(extra, to=to)]
    return hits + extra, rules + prules, {"sha": sha, "files": files, "errors": errors, "probe": pst}


def _mark_groups(groups: list, rules_by_key: dict, current: str, to: str) -> None:
    """Yükseltme grubu: başlık öneki + etiket; behavior ve `fix_min > şimdiki` bulguları insan işi."""
    cur = _v(current)
    for g in groups:
        r = rules_by_key.get(g.findings[0].rule) if g.findings else None
        reason = ""
        if r is not None and r.breaks == "behavior":
            reason = "Davranış değişikliği: otomatik düzeltilemez"
        elif r is not None and r.fix_min > cur:
            # düzeltilmiş hâl bugünkü üretimde çalışmaz → önceden düzeltilemez
            reason = f"Düzeltme yalnız {_fmt(r.fix_min)} ve sonrasında çalışır — sürüm adımıyla birlikte ele alınmalı"
        if reason:
            g.needs_human = True
            g.human_reason = f"{g.human_reason} · {reason}" if g.infra and g.human_reason else reason
        g.title = f"Sürüm yükseltme {current}→{to}: {g.title}"
        if TAG not in g.extra_tags:
            g.extra_tags.append(TAG)


def _analyse(client, repo: str, lang: str, root: Path, current: str, to: str, cfg: dict, deps, *, force: bool,
             scan_fn=None, head_sha_fn=None, probe_fn=None):
    from agile_sdlc_crew.quality import modern
    hits, rules, meta = _collect(repo, lang, root, current, to, cfg, force=force, scan_fn=scan_fn,
                                 head_sha_fn=head_sha_fn, probe_fn=probe_fn)
    rules_by_key = {r.key: r for r in rules}
    # Araç (Rector / kendi düzelticimiz / satır penceresi) yalnız PHP katalog kurallarına; hedef aracın
    # genel bulgusu (probe:*) ve Go/Python/Next.js yükseltme kuralları pipeline'a ya da insana gider.
    tools = set(pm.tool_rules([r for r in rules if not r.key.startswith("probe:")])) if lang == "php" else set()
    cur_v = _v(current)

    def keep_closed(rule_key: str) -> str | None:
        # İnsan işi (behavior / fix_min > şimdiki) bir insan Done'a çekince 'baktım' demektir (spec §5): yerler
        # aynı kaldıkça grace sonrası yeniden açılmaz, yükseltmeyi de bekletmez. Yeni dosya yine açılır.
        r = rules_by_key.get(rule_key)
        if r is not None and (r.breaks == "behavior" or r.fix_min > cur_v):
            return "insan baktı: WI"
        return None
    # Süzme upgrade_rules'ta yapıldı; build_groups'a sürüm kapısı uygulanmasın diye "99.0"
    groups, skipped, summary = pm.build_groups(
        {"hits": hits}, rules, "99.0", repo=repo, board_index=deps.board(client), now=deps.now(), cfg=cfg,
        ext_ok=pm._ext_checker(root) if lang == "php" else None, source=modern.LANGS[lang]["source"],
        tool_rules=tools, keep_closed=keep_closed, upgrade_lane=True)
    for k, s in summary.items():
        s["breaks"] = rules_by_key[k].breaks
    _mark_groups(groups, rules_by_key, current, to)
    return groups, skipped, summary, rules_by_key, tools, meta


def _rows(groups: list, rules_by_key: dict, tools: set, current: str) -> dict[str, list[dict]]:
    cur = _v(current)
    out: dict[str, list[dict]] = {"auto": [], "infra": [], "human": []}
    for g in groups:
        rule = g.findings[0].rule
        r = rules_by_key.get(rule)
        auto = not g.needs_human
        row = pm._row(g, rules_by_key)
        row.update({"breaks": r.breaks if r else "", "blocking": bool(r is not None and blocking(r, auto=auto)),
                    "pre_fixable": bool(r is not None and r.fix_min <= cur),
                    "tool": bool(auto and rule in tools)})
        out["auto" if auto else ("infra" if g.infra else "human")].append(row)
    return out


def _prepare(repo: str, lang: str, deps, root) -> tuple[dict, Path]:
    from agile_sdlc_crew.quality import modern
    if lang not in modern.LANGS:
        raise ValueError(f"Desteklenmeyen dil: {lang}")
    cfg = deps.cfg()
    root = Path(root) if root is not None else pm._root(cfg, repo, deps)
    if lang not in modern.detect_langs(root):
        raise ValueError(f"{root.name} bir {modern.LANGS[lang]['label']} reposu değil")
    return cfg, root


def scan(client, repo: str, lang: str, *, to: str | None = None, team: str = "", force: bool = False, deps=None,
         index=None, root=None, read=None, yml_files=None, app_map_cache: dict | None = None, scan_fn=None,
         head_sha_fn=None, probe_fn=None, resolver=None) -> dict:
    """Yazma yok: hedef sürüme geçince kırılan / davranışı değişen / kullanımdan kalkan yerler. modern.scan
    ile aynı anahtarlar + şimdiki/hedef sürüm, sürüm yerleri, engeller, probe durumu ve sayımlar."""
    from agile_sdlc_crew.quality import modern
    deps = deps or pm.default_deps()
    cfg, root = _prepare(repo, lang, deps, root)
    repo = root.name
    label = modern.LANGS[lang]["label"]
    fresh = pm.freshen_clone(root, force=force) if cfg.get("freshen", True) and scan_fn is None else {}
    pl = plan(client, repo, lang, to, deps=deps, index=index, root=root, read=read, yml_files=yml_files,
              app_map_cache=app_map_cache, resolver=resolver)
    out = {"repo": repo, "lang": lang, "label": label, "version": pl["current"], "current": pl["current"],
           "target": pl["target"], "target_source": pl["target_source"], "selectable": pl["selectable"],
           "sites": pl["sites"], "ci_pins": pl["ci_pins"], "ci_source": pl.get("ci_source"),
           "per_src": pl["per_src"], "up_to_date": pl["up_to_date"],
           "freshen": fresh,
           "sha": "", "files": 0, "auto": [], "infra": [], "human": [], "rules": {}, "skipped": [],
           "skipped_counts": {}, "errors": [], "blockers": [], "probe": None,
           "counts": {k: 0 for k in _COUNT_KEYS}, "blocking": 0}
    if "ci_test_def" in pl:
        out["ci_test_def"] = pl["ci_test_def"]
    if not pl["current"]:
        out["message"] = f"Şimdiki {label} sürümü bulunamadı — yükseltme taranamaz."
        return out
    if pl["up_to_date"]:
        out["message"] = "Repo hedef sürümde ya da seçilebilecek daha yüksek bir sürüm yok."
        return out
    cur, tgt = pl["current"], pl["target"]
    groups, skipped, summary, rules_by_key, tools, meta = _analyse(
        client, repo, lang, root, cur, tgt, cfg, deps, force=force, scan_fn=scan_fn, head_sha_fn=head_sha_fn,
        probe_fn=probe_fn)
    rows = _rows(groups, rules_by_key, tools, cur)
    every = rows["auto"] + rows["infra"] + rows["human"]
    counts = {k: sum(r["count"] for r in every if r["breaks"] == k) for k in _COUNT_KEYS}
    n_blocking = sum(1 for r in every if r["blocking"])
    # Board'da işi olduğu için atlanan bulgu kodda duruyor: işi Resolved / Ready for Production / grace içinde Done
    # olsa da dedup onu "açık WI" diye atlar, open_tasks ise bitmiş sayar — hiçbiri saymazsa aşama `hazır` olurdu.
    # Engelleyen kuralları (ayrı kural başına bir; satırlarda zaten sayılan kural ikinci kez değil) eklenir.
    # İptal edilmiş ve "insan baktı" atlamaları sayılmaz.
    fp_pre = f"{modern.LANGS[lang]['source']}:{repo}:".lower()
    counted = {r["rule"] for r in every if r["blocking"]}
    held: set[str] = set()
    for s in skipped:
        fp = str(s.get("fingerprint") or "")
        if not str(s.get("reason") or "").startswith("açık WI") or not fp.lower().startswith(fp_pre):
            continue
        rest = fp[len(fp_pre):]
        k = _fp_rule(rest, rules_by_key)
        r = rules_by_key.get(k) if k else None
        auto = bool(r is not None and r.auto and r.breaks != "behavior" and r.fix_min <= _v(cur))
        if _fp_blocking(rest, rules_by_key, auto):
            held.add(k or rest)
    n_blocking += len(held - counted)
    out.update(rows)
    out.update({"sha": meta["sha"], "files": meta["files"], "errors": meta["errors"], "probe": meta["probe"],
                "rules": summary, "skipped": skipped[:50],
                "skipped_counts": dict(Counter(s["reason"].split(" #")[0] for s in skipped)),
                "blockers": blockers(root, lang, tgt), "counts": counts,
                "blocking": n_blocking})
    if not groups:
        out["message"] = f"{cur} → {tgt} geçişinde açılacak iş yok."
    return out


def take(client, repo: str, lang: str, group_ids: list[str], *, to: str | None = None, team: str = "",
         sprint: str = "", deps=None, index=None, root=None, read=None, yml_files=None,
         app_map_cache: dict | None = None, scan_fn=None, head_sha_fn=None, probe_fn=None) -> dict:
    """Seçilen yükseltme gruplarını 'Teknik borç' story'si altında Task olarak aç (modern.take ile aynı
    kurallar ve yanıt). Rector/düzelticinin uyguladığı PHP grupları modern_fix işine, kapsamında
    `upgrade_to` ile; diğer otomatik gruplar pipeline'a; insan işleri kuyruğa alınmaz."""
    from agile_sdlc_crew.quality import intake, modern
    deps = deps or pm.default_deps()
    if not intake._LOCK.acquire(blocking=False):
        raise intake.Busy("Başka bir alım / kuyruk isteği sürüyor")
    try:
        cfg, root = _prepare(repo, lang, deps, root)
        repo = root.name
        label = modern.LANGS[lang]["label"]
        pl = plan(client, repo, lang, to, deps=deps, index=index, root=root, read=read, yml_files=yml_files,
                  app_map_cache=app_map_cache)
        if not pl["current"]:
            raise ValueError(f"Şimdiki {label} sürümü bilinmiyor — yükseltme işi açılamaz")
        if pl["up_to_date"]:
            raise ValueError("Repo hedef sürümde — açılacak yükseltme işi yok")
        cur, tgt = pl["current"], pl["target"]
        groups, _sk, _sum, _rbk, tools, _meta = _analyse(
            client, repo, lang, root, cur, tgt, cfg, deps, force=False, scan_fn=scan_fn, head_sha_fn=head_sha_fn,
            probe_fn=probe_fn)
        picked, skipped = pm._pick({pm.gid_of(g): g for g in groups}, group_ids, cfg.get("max_take", 8))
        if not picked:
            return pm._nothing_picked({"repo": repo, "lang": lang}, skipped, strict=True)
        if lang == "php":
            if deps.queue_tool is not None:
                base_tool = deps.queue_tool

                def _tool(wid, title, payload):  # onaylı kapsam + hedef sürüm INSERT'te kayda
                    return base_tool(wid, title, json.dumps({**json.loads(payload), "upgrade_to": tgt}))
                deps = deps._replace(queue_tool=_tool)
            deps = pm._rector_queue(deps, repo, [g for g in picked if not g.needs_human and g.findings[0].rule in tools])
        return pm._open_and_report(
            client, picked, skipped, repo=repo, source=modern.LANGS[lang]["source"], team=team, sprint=sprint,
            task_cfg={**cfg, "php_target": f"{cur} → {tgt} (sürüm yükseltme; düzeltme {cur} sürümünde de çalışmalı)"},
            deps=deps, head={"repo": repo, "lang": lang, "version": cur, "current": cur, "target": tgt},
            log_line=f"⬆️ {label} sürüm yükseltme {cur}→{tgt} ({repo})", tail={"tool_fix": False},
            devops_cfg=cfg if lang == "php" else None)
    finally:
        intake._LOCK.release()
