"""Dil sürümü yükseltme: katalogdan yükseltmeyi etkileyen kuralları süzer."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
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
    out["sites"] = sites(client, repo, Path(root), lang, current, target, index, read=read, yml_files=yml_files,
                       per_src=out["per_src"])
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
