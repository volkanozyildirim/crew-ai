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
CI_REPO = "yml-files"
CI_PROJECT = "EcomBackend"
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
        pub = sorted(published(lang, index), key=_v)
        return pub[-1] if pub and _v(pub[-1]) > _v(current) else None
    kv = known_versions(lang)
    return kv[-1] if kv else None


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


def selectable(lang: str, current: str, index: dict) -> list[dict]:
    pub = published(lang, index)
    out = []
    for v in known_versions(lang):
        if _v(current) < _v(v):
            out.append({"version": v, "published": True if lang not in ("php", "python") else v in pub,
                        "catalog": _has_catalog(lang, v), "tool": _tool_ok(lang, v)})
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


def _devops_sites(client, repo, lang, to, index, read, per_src=None) -> list[dict]:
    from agile_sdlc_crew.quality import app_map as am
    by_devrepo: dict[str, set[str]] = {}
    for es in (index or {}).values():
        for e in es:
            by_devrepo.setdefault(str(e.get("src") or "").split("/")[0], set()).update(e.get("images", []))
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
            new = image_for(img, lang, to)
            lines = [i for i, ln in enumerate(text.splitlines(), 1)
                     if (m := re.match(r"(?i)\s*FROM\s+(?:--\S+\s+)?(\S+)", ln)) and m.group(1) == img]
            if not lines:
                out.append(_site("devops", devrepo, am.DEVOPS_PROJECT, am.DEVOPS_BRANCH, path, 0, img, new, NO_LINE))
                continue
            blocked = "" if new in by_devrepo.get(devrepo, set()) else UNPUBLISHED
            for line in lines:
                out.append(_site("devops", devrepo, am.DEVOPS_PROJECT, am.DEVOPS_BRANCH, path, line, img, new, blocked))
    return out


def _ci_sites(client, repo, lang, to, yml_files) -> list[dict]:
    return _ci_scan(client, repo, lang, to, yml_files)[0]


def _ci_scan(client, repo, lang, to, yml_files) -> tuple[list[dict], list[dict] | None]:
    """(siteler, sabitlemeler). Sabitleme: repoyu checkout eden test hattı dosyasında dilin sürüm satırı, sürümü ne
    olursa olsun — `{repo, project, branch, path, line, version}`; site yalnız hedefin altındakiler. yml-files
    okunamazsa sabitlemeler None (bilinmiyor): "test hattı sabitlemesi yok" ile karışmasın diye."""
    key = {"php": "phpVersion", "python": "pythonVersion", "go": "goVersion"}.get(lang)
    if not key:
        return [], []
    branch = "main"
    if yml_files is None:
        try:
            branch = (client.get_repository(CI_REPO).get("defaultBranch") or "refs/heads/main").replace("refs/heads/", "")
            items = client.get_items_in_path(CI_REPO, "/" + CI_DIR, branch=branch, recursion_level="oneLevel",
                                             project=CI_PROJECT)
            yml_files = {}
            for it in items:
                p = (it.get("path") or "").lstrip("/")
                if not it.get("isFolder") and p.lower().endswith((".yml", ".yaml")):
                    yml_files[p] = client.get_file_content(CI_REPO, "/" + p, branch=branch, project=CI_PROJECT) or ""
        except Exception as e:  # noqa: BLE001
            log.warning(f"⚠️ upgrade: yml-files okunamadı: {e}")
            return [], None
    mention = re.compile(rf"(?im)^\s*(?:-\s*)?name:\s*['\"]?(?:[\w.-]+/)?{re.escape(repo)}['\"]?\s*$")
    keyline = re.compile(rf"^\s*{key}\s*:(.*)$")
    task_re, task_key = _CI_TASKS.get(lang, (None, None))
    out, pins = [], []
    for path in sorted(yml_files):
        if not path.startswith(CI_DIR + "/") or not mention.search(yml_files[path] or ""):
            continue
        pending = None   # açık sürüm görevi (GoTool / UsePythonVersion) bloğunda aranan anahtar
        for i, ln in enumerate((yml_files[path] or "").splitlines(), 1):
            if re.match(r"^\s*#", ln):
                continue
            if task_re is not None and task_re.match(ln):
                pending = task_key
                continue
            if _CI_BLOCK_END.match(ln):
                pending = None
                continue
            if pending and (m := re.match(rf"^\s*{pending}\s*:(.*)$", ln)):
                pending = None
                _ci_value_pin(out, pins, m.group(1), path, i, branch, to)
                continue
            if (m := keyline.match(ln)):
                _ci_value_pin(out, pins, m.group(1), path, i, branch, to)
                continue
            if (m := _CI_IMAGE_KEY.match(ln)):
                _ci_image_pin(out, pins, m.group(1), m.group(2), lang, path, i, branch, to)
    return out, pins


# Sürüm görevi → bloğundaki sürüm anahtarı (gerçek test hatları: gocrons GoTool@0 version, Python
# UsePythonVersion@0 versionSpec). Blok, sonraki adım/iş/aşama satırında biter.
_CI_TASKS = {"go": (re.compile(r"^\s*-\s*task:\s*GoTool@\d+\s*$"), "version"),
             "python": (re.compile(r"^\s*-\s*task:\s*UsePythonVersion@\d+\s*$"), "versionSpec")}
_CI_BLOCK_END = re.compile(r"^\s*-\s*(?:task|script|bash|pwsh|powershell|checkout|job|stage|template|deployment)\s*:")
_CI_IMAGE_KEY = re.compile(r"^\s*(?:-\s*)?(image|container)\s*:(.*)$")
_CI_IMG_NAME = {"go": "golang", "python": "python"}


def _unquote(value: str) -> tuple[str, str, str]:
    """(ham, tırnak, iç) — satır sonu yorumu atılır."""
    raw = re.sub(r"\s+#.*$", "", value).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw, raw[0], raw[1:-1]
    return raw, "", raw


def _ci_pin(path, line, branch, **kw) -> dict:
    return {"repo": CI_REPO, "project": CI_PROJECT, "branch": branch, "path": path, "line": line, **kw}


def _ci_value_pin(out, pins, value, path, i, branch, to) -> None:
    """`phpVersion: '8.2'` / GoTool `version:` / `versionSpec:` değeri."""
    raw, q, inner = _unquote(value)
    mm = re.fullmatch(r"(\d+)\.(\d+)(\.\d+)?", inner)
    if not mm:
        # `3.x`, `$(var)`, `8` …: sürüm okunamıyor — kapı kapalı kalır, satırı insan düzenler
        pins.append(_ci_pin(path, i, branch, version="", unparsed=inner[:40]))
        return
    ver = f"{mm.group(1)}.{mm.group(2)}"
    pins.append(_ci_pin(path, i, branch, version=ver))
    if _v(ver) < _v(to):
        if mm.group(3):   # 1.22.5: rakam-dizisi kuralı 1.25.5'e izin verirdi — yanlış; otomatik yazılmaz
            out.append(_site("ci", CI_REPO, CI_PROJECT, branch, path, i, raw, raw, CI_HAND))
        else:
            out.append(_site("ci", CI_REPO, CI_PROJECT, branch, path, i, f"{q}{ver}{q}", f"{q}{to}{q}"))


def _ci_image_pin(out, pins, key, value, lang, path, i, branch, to) -> None:
    """`image:` / `container:` değeri dilin imajıysa sabitlemedir (`golang:1.21.4`, `…/golang:1.21-alpine`,
    `python:3.11-slim`, `…/php-8-2:1.0`). `container:` etiketsizse kaynak takma adıdır (`container: golang`)."""
    _raw, _q, img = _unquote(value)
    if not img or " " in img:
        return
    if lang == "php":
        if not re.search(r"(?:^|/)php(?:[-:]|$)", img):
            return
        v = pm.parse_php_image(img)
        three = re.search(r"(?:^|/)php:\d+\.\d+\.\d+", img)
    else:
        name = _CI_IMG_NAME.get(lang)
        m = re.search(rf"(?:^|/){name}(?::([^\s/]*))?$", img) if name else None
        if not m:
            return
        tag = (m.group(1) or "").split("-")[0]
        tm = re.fullmatch(r"(\d+)\.(\d+)(\.\d+)?", tag)
        v = (int(tm.group(1)), int(tm.group(2))) if tm else None
        three = tm and tm.group(3)
    if key == "container" and ":" not in img:
        return
    if not v:
        pins.append(_ci_pin(path, i, branch, version="", unparsed=img[:40]))
        return
    ver = _fmt(v)
    pins.append(_ci_pin(path, i, branch, version=ver))
    if v < _v(to):
        new = image_for(img, lang, to)
        if three or new == img:
            out.append(_site("ci", CI_REPO, CI_PROJECT, branch, path, i, img, img, CI_HAND))
        else:
            out.append(_site("ci", CI_REPO, CI_PROJECT, branch, path, i, img, new))


def _range_hard(constraint: str) -> bool:
    """Üst sınır, dışlama ya da tire aralığı içeren kısıt yeniden yazılırsa yapısı bozulur."""
    return bool(re.search(r"[<!]|\s-\s", constraint))


def _code_sites(repo, root: Path, lang, to) -> list[dict]:
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
                add("Dockerfile", i, m.group(1), image_for(m.group(1), lang, to))
    return out


def sites(client, repo: str, root: Path, lang: str, current: str, to: str, index: dict, *,
          read=None, yml_files: dict | None = None, per_src: list | None = None) -> list[dict]:
    """Sürümün sabitlendiği yerler: FLODevOps Dockerfile'ları, yml-files test hattı, kod reposu.
    per_src: hedef aramasının servis listesi (app_map + build_index birleşimi)."""
    return (_devops_sites(client, repo, lang, to, index, read, per_src)
            + _ci_sites(client, repo, lang, to, yml_files)
            + _code_sites(repo, root, lang, to))


def plan(client, repo: str, lang: str, to: str | None = None, *, deps=None, index=None, root=None,
         read=None, yml_files=None, app_map_cache: dict | None = None) -> dict:
    from agile_sdlc_crew.quality import modern
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
           "selectable": [], "sites": [], "ci_pins": [], "per_src": tv.get("per_src") or [], "up_to_date": False}
    if not current:
        return out
    sel = selectable(lang, current, index)
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
    ci_sites, ci_pins = _ci_scan(client, repo, lang, target, yml_files)
    out["sites"] = (_devops_sites(client, repo, lang, target, index, read, out["per_src"]) + ci_sites
                    + _code_sites(repo, Path(root), lang, target))
    out["ci_pins"] = ci_pins
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
        tool_rules=tools, keep_closed=keep_closed)
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
         head_sha_fn=None, probe_fn=None) -> dict:
    """Yazma yok: hedef sürüme geçince kırılan / davranışı değişen / kullanımdan kalkan yerler. modern.scan
    ile aynı anahtarlar + şimdiki/hedef sürüm, sürüm yerleri, engeller, probe durumu ve sayımlar."""
    from agile_sdlc_crew.quality import modern
    deps = deps or pm.default_deps()
    cfg, root = _prepare(repo, lang, deps, root)
    repo = root.name
    label = modern.LANGS[lang]["label"]
    fresh = pm.freshen_clone(root, force=force) if cfg.get("freshen", True) and scan_fn is None else {}
    pl = plan(client, repo, lang, to, deps=deps, index=index, root=root, read=read, yml_files=yml_files,
              app_map_cache=app_map_cache)
    out = {"repo": repo, "lang": lang, "label": label, "version": pl["current"], "current": pl["current"],
           "target": pl["target"], "target_source": pl["target_source"], "selectable": pl["selectable"],
           "sites": pl["sites"], "ci_pins": pl["ci_pins"], "per_src": pl["per_src"], "up_to_date": pl["up_to_date"],
           "freshen": fresh,
           "sha": "", "files": 0, "auto": [], "infra": [], "human": [], "rules": {}, "skipped": [],
           "skipped_counts": {}, "errors": [], "blockers": [], "probe": None,
           "counts": {k: 0 for k in _COUNT_KEYS}, "blocking": 0}
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
    out.update(rows)
    out.update({"sha": meta["sha"], "files": meta["files"], "errors": meta["errors"], "probe": meta["probe"],
                "rules": summary, "skipped": skipped[:50],
                "skipped_counts": dict(Counter(s["reason"].split(" #")[0] for s in skipped)),
                "blockers": blockers(root, lang, tgt), "counts": counts,
                "blocking": sum(1 for r in every if r["blocking"])})
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
