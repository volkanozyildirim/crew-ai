"""Dil sürümü yükseltme: katalogdan yükseltmeyi etkileyen kuralları süzer."""
from __future__ import annotations

import json
import logging
import re
import subprocess
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


def _has_catalog(lang: str, version: str) -> bool:
    """O sürümde `breaks` dolu bir katalog kuralı var mı (Go/Python/Next.js'te henüz olmayabilir)."""
    try:
        from agile_sdlc_crew.quality import modern
        rules = pm.load_rules() if lang == "php" else modern.rules_for(modern.LANGS[lang]["source"])
        return any(r.breaks and r.since == _v(version) for r in rules)
    except Exception:  # noqa: BLE001
        return False


def _tool_ok(lang: str, version: str) -> bool:
    """Yerelde o sürümü karşılayan araç var mı. Go: yerel `go version`; PHP/Python: Görev 4 genişletir."""
    if lang != "go":
        return False
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


def _devops_sites(client, repo, lang, to, index, read) -> list[dict]:
    from agile_sdlc_crew.quality import app_map as am
    by_devrepo: dict[str, set[str]] = {}
    for es in (index or {}).values():
        for e in es:
            by_devrepo.setdefault(str(e.get("src") or "").split("/")[0], set()).update(e.get("images", []))
    rd = read or (lambda d, p: am._read(client, d, p))
    out = []
    for e in (index or {}).get(repo.lower(), []):
        devrepo, _, rest = str(e.get("src") or "").partition("/")
        path = "/" + rest.strip("/") + "/Dockerfile"
        text = None
        for img in e.get("images", []):
            v = _image_version(img, lang)
            if not v or v >= _v(to):
                continue
            if text is None:
                text = rd(devrepo, path) or ""
            line = next((i for i, ln in enumerate(text.splitlines(), 1)
                         if re.match(r"(?i)\s*FROM\s", ln) and ln.count(img) == 1), 0)
            if not line:
                continue
            new = image_for(img, lang, to)
            blocked = "" if new in by_devrepo.get(devrepo, set()) else UNPUBLISHED
            out.append(_site("devops", devrepo, am.DEVOPS_PROJECT, am.DEVOPS_BRANCH, path, line, img, new, blocked))
    return out


def _ci_sites(client, repo, lang, to, yml_files) -> list[dict]:
    key = {"php": "phpVersion", "python": "pythonVersion", "go": "goVersion"}.get(lang)
    if not key:
        return []
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
            return []
    mention = re.compile(rf"(?im)^\s*(?:-\s*)?name:\s*['\"]?(?:[\w.-]+/)?{re.escape(repo)}['\"]?\s*$")
    var = re.compile(rf"^\s*{key}:\s*(['\"]?)(\d+\.\d+)\1\s*(?:#.*)?$")
    out = []
    for path in sorted(yml_files):
        if not path.startswith(CI_DIR + "/") or not mention.search(yml_files[path] or ""):
            continue
        for i, ln in enumerate((yml_files[path] or "").splitlines(), 1):
            m = var.match(ln)
            if m and _v(m.group(2)) < _v(to):
                q = m.group(1)
                out.append(_site("ci", CI_REPO, CI_PROJECT, branch, path, i, f"{q}{m.group(2)}{q}", f"{q}{to}{q}"))
    return out


def _code_sites(repo, root: Path, lang, to) -> list[dict]:
    root = Path(root)
    out = []

    def rd(name):
        try:
            return (root / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    def add(path, line, old, new):
        out.append(_site("code", repo, "", "", path, line, old, new))

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
            new = re.sub(r"\d+\.\d+", to, val, count=1) if len(nums) == 1 else f">={to}"
            for i, ln in enumerate(text.splitlines(), 1):
                if re.search(rf'"php"\s*:\s*"{re.escape(val)}"', ln) and ln.count(val) == 1:
                    add("composer.json", i, val, new)
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
                add("pyproject.toml", i, m.group(2) + m.group(3) + m.group(4), m.group(2) + to + m.group(4))
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
          read=None, yml_files: dict | None = None) -> list[dict]:
    """Sürümün sabitlendiği yerler: FLODevOps Dockerfile'ları, yml-files test hattı, kod reposu."""
    return (_devops_sites(client, repo, lang, to, index, read)
            + _ci_sites(client, repo, lang, to, yml_files)
            + _code_sites(repo, root, lang, to))


def plan(client, repo: str, lang: str, to: str | None = None, *, deps=None, index=None, root=None,
         read=None, yml_files=None) -> dict:
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
    if lang == "php" and client is None:  # ağsız (test) yol: app_map yüklenmez, önbellek okunmaz
        tv = pm.target_version(None, repo, app_map_cache={}, repo_root=Path(root), write_cache=False,
                               max_age_h=0, index=index)
    else:
        tv = modern.target(client, repo, Path(root), lang, index=index)
    current = tv.get("version")
    out = {"repo": repo, "lang": lang, "current": current, "target": None, "target_source": None,
           "selectable": [], "sites": [], "per_src": tv.get("per_src") or [], "up_to_date": False}
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
    out["sites"] = sites(client, repo, Path(root), lang, current, target, index, read=read, yml_files=yml_files)
    return out
