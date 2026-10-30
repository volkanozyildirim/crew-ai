"""Dil sürümüne göre modernleştirme — PHP, Go, Python, Next.js.

Repo hangi dildeyse o dilin hedef sürümüne göre taranır; bulgular aynı Group/Task
modeline (php_modern.build_groups) girer, aynı Teknik borç akışından iş açılır.

| Dil     | Hedef sürüm                                                         | Tespit / düzeltme |
|---------|---------------------------------------------------------------------|-------------------|
| PHP     | FLODevOps imajı → composer.json (php_modern)                        | token tarayıcı / developer |
| Go      | go.mod `go` satırı (imaj bilgi amaçlı)                               | Go ekibinin `modernize` analizörü (-json / -fix) |
| Python  | FLODevOps python imajı → repo Dockerfile → requires-python → .python-version | ruff UP kuralları (yalnız güvenli düzeltme otomatik) |
| Next.js | package.json `next` sürümü                                           | katalog desenleri / developer |

Go ve Python'da mekanik işi araç uygular, LLM yalnız inceler (modern_fix);
Next.js ve PHP'de düzeltmeyi pipeline'ın developer'ı yapar.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from collections import Counter
from pathlib import Path

import yaml

from agile_sdlc_crew.quality import php_modern

log = logging.getLogger("agile_sdlc_crew.pipeline")

_CONFIG = Path(__file__).resolve().parent.parent / "config"
LANGS = {
    "php": {"source": "php_modern", "label": "PHP", "rules": _CONFIG / "php_modern_rules.yaml", "tool_fix": False},
    "go": {"source": "go_modern", "label": "Go", "rules": _CONFIG / "modern_go_rules.yaml", "tool_fix": True},
    "python": {"source": "py_modern", "label": "Python", "rules": _CONFIG / "modern_python_rules.yaml", "tool_fix": True},
    "nextjs": {"source": "next_modern", "label": "Next.js", "rules": _CONFIG / "modern_nextjs_rules.yaml",
               "tool_fix": False},
}
SOURCE_LANG = {v["source"]: k for k, v in LANGS.items()}
BUILD_INDEX_CACHE = _CONFIG / "quality_build_index.yaml"
SKIP_DIRS = {"node_modules", ".next", "dist", "build", "out", "vendor", ".git", ".venv", "venv", "public", "coverage"}


def go_modernize_bin() -> str:
    return os.environ.get("CREW_GO_MODERNIZE_BIN") or str(Path("~/.crew_tools/bin/modernize").expanduser())


def _ruff_cmd() -> list[str]:
    import sys
    return [sys.executable, "-m", "ruff"]


# ── Katalog ──────────────────────────────────────────────────────────────

def rules_for(source: str) -> list[php_modern.Rule]:
    lang = SOURCE_LANG.get(source, "php")
    if lang == "php":
        return php_modern.load_rules()
    return php_modern.load_rules(LANGS[lang]["rules"])


def _raw_rules(lang: str) -> list[dict]:
    try:
        return (yaml.safe_load(LANGS[lang]["rules"].read_text(encoding="utf-8")) or {}).get("rules") or []
    except (OSError, yaml.YAMLError):
        return []


def _generic_rule(key: str, message: str, *, manual: bool) -> php_modern.Rule:
    """Katalogda olmayan analizör/kod: araç mesajıyla genel kural."""
    return php_modern.Rule(
        key=key, since=(0, 0), kind="structural" if manual else "mechanical",
        title=f"{key}: {message[:80]}", summary=message, advantages=["Hedef sürümün sunduğu yazıma geçiş."],
        risks=(["Aracın güvenli düzeltmesi yok — her yer elle değerlendirilmeli."] if manual else
               ["Araç yalnız eşdeğer dönüşümü uygular."]),
        before="", after="", ref="", auto=not manual, sp_unit=1)


# ── Dil ve hedef sürüm ───────────────────────────────────────────────────

def detect_langs(root: Path) -> list[str]:
    root = Path(root)
    out: list[str] = []
    if (root / "composer.json").is_file():
        out.append("php")
    if (root / "go.mod").is_file():
        out.append("go")
    pkg = _package_json(root)
    if "next" in {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}:
        out.append("nextjs")
    if any((root / f).is_file() for f in ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg")):
        out.append("python")
    return out


def _package_json(root: Path) -> dict:
    try:
        return json.loads((Path(root) / "package.json").read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def build_index(client, *, max_age_h: int = 24, cache_path: Path = BUILD_INDEX_CACHE) -> dict[str, list[dict]]:
    """FLODevOps production `build/<servis>/` → repo: [{src, images}] (Jenkinsfile gitOriginApp).
    app_map yalnız New Relic adı olan servisleri tutuyor; Go/Python servislerinin çoğu orada yok."""
    from agile_sdlc_crew.quality import app_map as am
    try:
        cached = yaml.safe_load(cache_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        cached = {}
    if cached.get("repos") and time.time() - float(cached.get("built_at") or 0) < max_age_h * 3600:
        return cached["repos"]
    idx: dict[str, list[dict]] = {}
    for devrepo in am.DEVOPS_REPOS:
        items = client.get_items_in_path(devrepo, "/build", branch=am.DEVOPS_BRANCH,
                                         recursion_level="oneLevel", project=am.DEVOPS_PROJECT)
        for it in items:
            path = (it.get("path") or "").rstrip("/")
            if not it.get("isFolder") or path in ("", "/build"):
                continue
            m = am._ORIGIN.search(am._read(client, devrepo, f"{path}/JenkinsfileBuild") or "")
            if not m:
                continue
            df = am._read(client, devrepo, f"{path}/Dockerfile") or ""
            idx.setdefault(m.group(2).lower(), []).append(
                {"src": f"{devrepo}{path}", "images": re.findall(r"(?im)^FROM\s+(\S+)", df)})
    try:
        cache_path.write_text(yaml.safe_dump({"built_at": time.time(), "repos": idx}, allow_unicode=True),
                              encoding="utf-8")
    except OSError:
        pass
    return idx


def _min_image_version(images: list[str], name: str) -> tuple[int, int] | None:
    vs = [(int(a), int(b)) for img in images for a, b in re.findall(rf"(?:^|/){name}:(\d+)\.(\d+)", img)]
    return min(vs) if vs else None


def target(client, repo: str, root: Path, lang: str, *, index: dict | None = None) -> dict:
    """{"version": "1.24"|None, "source": "...", "images": [...]}"""
    root = Path(root)
    if lang == "php":
        cfg = php_modern.load_cfg()
        return php_modern._target(client, repo, root, cfg, None)
    images: list[str] = []
    if index is not None:
        images = [i for e in index.get(repo.lower(), []) for i in e.get("images", [])]
    if lang == "go":
        try:
            m = re.search(r"(?m)^go\s+(\d+)\.(\d+)", (root / "go.mod").read_text(encoding="utf-8"))
        except OSError:
            m = None
        return {"version": f"{m.group(1)}.{m.group(2)}" if m else None, "source": "go.mod", "images": images}
    if lang == "python":
        v = _min_image_version(images, "python")
        if v:
            return {"version": f"{v[0]}.{v[1]}", "source": "flodevops", "images": images}
        try:
            v = _min_image_version(re.findall(r"(?im)^FROM\s+(\S+)", (root / "Dockerfile").read_text(encoding="utf-8")),
                                   "python")
        except OSError:
            v = None
        if v:
            return {"version": f"{v[0]}.{v[1]}", "source": "Dockerfile", "images": images}
        for f, pat in (("pyproject.toml", r"requires-python\s*=\s*['\"][^0-9]*(\d+)\.(\d+)"),
                       (".python-version", r"^\s*(\d+)\.(\d+)"), ("setup.cfg", r"python_requires\s*=\s*[^0-9]*(\d+)\.(\d+)")):
            try:
                m = re.search(pat, (root / f).read_text(encoding="utf-8"), re.M)
            except OSError:
                continue
            if m:
                return {"version": f"{m.group(1)}.{m.group(2)}", "source": f, "images": images}
        return {"version": None, "source": None, "images": images}
    if lang == "nextjs":
        pkg = _package_json(root)
        raw = str({**(pkg.get("devDependencies") or {}), **(pkg.get("dependencies") or {})}.get("next") or "")
        m = re.search(r"(\d+)\.(\d+)", raw)
        return {"version": f"{m.group(1)}.{m.group(2)}" if m else None, "source": "package.json", "images": images}
    return {"version": None, "source": None, "images": images}


# ── Tarayıcılar ──────────────────────────────────────────────────────────

def _rel(root: Path, path: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return path


def _line_text(root: Path, rel: str, line: int) -> str:
    try:
        lines = (Path(root) / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[line - 1].strip()[:200] if 0 < line <= len(lines) else ""
    except OSError:
        return ""


def _go_env(root: Path) -> dict:
    from agile_sdlc_crew.verify import _env
    return _env({"GOFLAGS": "-mod=vendor" if (Path(root) / "vendor" / "modules.txt").is_file() else "-mod=mod"})


def go_hits(root: Path, *, timeout: int = 300) -> tuple[list[dict], list[str]]:
    """modernize -json ./... → isabetler. Paket yüklenemezse hata listesine düşer."""
    r = subprocess.run([go_modernize_bin(), "-json", "./..."], cwd=str(root), capture_output=True, text=True,
                       timeout=timeout, env=_go_env(root))
    try:
        data = json.loads(r.stdout or "{}")
    except ValueError:
        return [], [(r.stderr or r.stdout or "modernize çıktısı okunamadı")[:300]]
    hits, errors = [], []
    for pkg, analyzers in (data or {}).items():
        if not isinstance(analyzers, dict):
            continue
        for name, diags in analyzers.items():
            if name == "error" or not isinstance(diags, list):
                if name == "error":
                    errors.append(f"{pkg}: {str(diags)[:200]}")
                continue
            for d in diags:
                m = re.match(r"^(.*?):(\d+)(?::\d+)?$", str(d.get("posn", "")))
                if not m:
                    continue
                rel = _rel(root, m.group(1))
                # cgo/üretilmiş dosyalar go-build önbelleğinden gelir (repo dışı mutlak yol)
                if rel.startswith(("vendor/", "/")) or not rel.endswith(".go"):
                    continue
                line = int(m.group(2))
                hits.append({"rule": name, "file": rel, "line": line, "text": _line_text(root, rel, line),
                             "message": str(d.get("message", ""))})
    if r.returncode not in (0, 3) and not hits:
        errors.append((r.stderr or "")[:300])
    return hits, errors


def py_hits(root: Path, version: str, *, timeout: int = 300) -> tuple[list[dict], list[str]]:
    """ruff UP kuralları; güvenli düzeltmesi olmayan bulgu `<kod>:manual`."""
    tv = "py" + version.replace(".", "")
    r = subprocess.run([*_ruff_cmd(), "check", "--select", "UP", "--target-version", tv, "--output-format", "json",
                        "--no-cache", "--exit-zero", "--extend-exclude", "vendor,node_modules", "."],
                       cwd=str(root), capture_output=True, text=True, timeout=timeout)
    try:
        data = json.loads(r.stdout or "[]")
    except ValueError:
        return [], [(r.stderr or "ruff çıktısı okunamadı")[:300]]
    hits = []
    for d in data:
        rel = _rel(root, d.get("filename", ""))
        line = int((d.get("location") or {}).get("row") or 0)
        safe = ((d.get("fix") or {}).get("applicability") == "safe")
        code = str(d.get("code") or "UP")
        hits.append({"rule": code if safe else f"{code}:manual", "file": rel, "line": line,
                     "text": _line_text(root, rel, line), "message": str(d.get("message", ""))})
    return hits, []


def _js_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            if f.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")) and not f.endswith((".min.js", ".d.ts")):
                yield Path(dirpath) / f


def next_hits(root: Path) -> tuple[list[dict], list[str]]:
    """Katalog desenleri (yorum satırları atlanır; çok satırlı desen tüm metinde aranır)."""
    root = Path(root)
    compiled = []
    for r in _raw_rules("nextjs"):
        compiled.append((r["key"], re.compile(r["pattern"], re.S) if r.get("pattern") else None,
                         re.compile(r["file_pattern"]) if r.get("file_pattern") else None))
    hits = []
    for path in _js_files(root):
        rel = str(path.relative_to(root))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(text) > 2_000_000:
            continue
        for key, pat, fpat in compiled:
            if fpat and not fpat.search(rel):
                continue
            if pat is None:
                hits.append({"rule": key, "file": rel, "line": 1, "text": _line_text(root, rel, 1), "message": ""})
                continue
            for m in pat.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                src = text.splitlines()[line - 1].lstrip() if text else ""
                if src.startswith(("//", "*", "/*")):
                    continue
                hits.append({"rule": key, "file": rel, "line": line, "text": src[:200], "message": ""})
    return hits, []


def _module(rel: str) -> str:
    return php_modern.module_of(rel)


# ── Tarama / liste / alım ────────────────────────────────────────────────

def repos(client, *, deps=None) -> dict:
    """Yerel repolar × dil: hedef sürüm ve taranabilirlik."""
    deps = deps or php_modern.default_deps()
    cfg = deps.cfg()
    base = Path(cfg["repos_dir"])
    try:
        index = build_index(client)
    except Exception as e:  # noqa: BLE001
        log.warning(f"⚠️ modern: FLODevOps build dizini okunamadı: {e}")
        index = {}
    php_items = {i["repo"]: i for i in php_modern.repos(client, deps=deps).get("items", [])}
    items = []
    for p in sorted(base.iterdir(), key=lambda x: x.name.lower()) if base.is_dir() else []:
        if not p.is_dir() or not (p / ".git").exists():
            continue
        for lang in detect_langs(p):
            if lang == "php":
                it = php_items.get(p.name)
                if it:
                    items.append({**it, "lang": "php", "label": "PHP"})
                continue
            if lang == "go" and not Path(go_modernize_bin()).exists():
                items.append({"repo": p.name, "lang": lang, "label": LANGS[lang]["label"], "version": None,
                              "source": None, "images": [], "scannable": False, "reason": "modernize aracı yok"})
                continue
            tv = target(client, p.name, p, lang, index=index)
            items.append({"repo": p.name, "lang": lang, "label": LANGS[lang]["label"], "version": tv["version"],
                          "source": tv["source"], "images": tv["images"], "scannable": bool(tv["version"])})
    return {"items": items}


_CACHE: dict[tuple, dict] = {}


def _hits(root: Path, lang: str, version: str, force: bool) -> tuple[list[dict], list[str], str]:
    sha = php_modern._head_sha(root)
    key = (str(root), lang, version, sha)
    if not force and key in _CACHE and time.time() - _CACHE[key]["at"] < 3600:
        c = _CACHE[key]
        return c["hits"], c["errors"], sha
    if lang == "go":
        hits, errors = go_hits(root)
    elif lang == "python":
        hits, errors = py_hits(root, version)
    else:
        hits, errors = next_hits(root)
    for h in hits:
        h.setdefault("module", _module(h["file"]))
    _CACHE[key] = {"hits": hits, "errors": errors, "at": time.time()}
    return hits, errors, sha


def _rules_with_generics(lang: str, hits: list[dict]) -> list[php_modern.Rule]:
    rules = rules_for(LANGS[lang]["source"])
    known = {r.key for r in rules}
    by_code = {r.key: r for r in rules}
    for h in hits:
        k = h["rule"]
        if k in known:
            continue
        base = by_code.get(k.split(":")[0])
        if base is not None and k.endswith(":manual"):
            rules.append(php_modern.Rule(**{**base.__dict__, "key": k, "kind": "structural", "auto": False,
                                            "title": base.title + " (elle)",
                                            "risks": ["ruff'ın güvenli düzeltmesi yok — her yer elle değerlendirilmeli."]}))
        else:
            rules.append(_generic_rule(k, h.get("message") or k, manual=k.endswith(":manual")))
        known.add(k)
    return rules


def scan(client, repo: str, lang: str, *, team: str = "", force: bool = False, deps=None) -> dict:
    if lang == "php":
        return {**php_modern.scan(client, repo, team=team, force=force, deps=deps), "lang": "php"}
    if lang not in LANGS:
        raise ValueError(f"Desteklenmeyen dil: {lang}")
    deps = deps or php_modern.default_deps()
    cfg = deps.cfg()
    root = php_modern._root(cfg, repo, deps)
    repo = root.name
    if lang not in detect_langs(root):
        raise ValueError(f"{repo} bir {LANGS[lang]['label']} reposu değil")
    try:
        index = build_index(client)
    except Exception:  # noqa: BLE001
        index = {}
    tv = target(client, repo, root, lang, index=index)
    out = {"repo": repo, "lang": lang, "label": LANGS[lang]["label"], "version": tv["version"],
           "version_source": tv["source"], "images": tv["images"], "auto": [], "infra": [], "human": [],
           "rules": {}, "skipped": [], "skipped_counts": {}, "errors": [], "files": 0, "adoption": {}}
    if not tv["version"]:
        out["message"] = f"Hedef {LANGS[lang]['label']} sürümü bulunamadı — öneri üretilmez."
        return out
    hits, errors, sha = _hits(root, lang, tv["version"], force)
    rules = _rules_with_generics(lang, hits)
    rules_by_key = {r.key: r for r in rules}
    # Go/Python'da sürüm kapısını araç uyguluyor; katalog `since`'i yalnız bilgi
    version = tv["version"] if lang == "nextjs" else "99.0"
    groups, skipped, summary = php_modern.build_groups({"hits": hits}, rules, version, repo=repo,
                                                       board_index=deps.board(client), now=deps.now(), cfg=cfg,
                                                       source=LANGS[lang]["source"])
    out.update({
        "sha": sha, "files": len({h["file"] for h in hits}), "errors": errors, "rules": summary,
        "auto": [php_modern._row(g, rules_by_key) for g in groups if not g.needs_human],
        "infra": [php_modern._row(g, rules_by_key) for g in groups if g.needs_human and g.infra],
        "human": [php_modern._row(g, rules_by_key) for g in groups if g.needs_human and not g.infra],
        "skipped": skipped[:50], "skipped_counts": dict(Counter(s["reason"].split(" #")[0] for s in skipped)),
        "tool_fix": bool(LANGS[lang]["tool_fix"]),
    })
    if not groups:
        out["message"] = "Hedef sürümün sunduğu özelliklerle ilgili öneri yok."
    return out


def take(client, repo: str, lang: str, group_ids: list[str], *, team: str = "", deps=None) -> dict:
    """Seçilenleri 'Teknik borç' story'si altında Task olarak aç. Go/Python otomatik işleri
    araç-düzeltme işine (modern_fix), diğerleri pipeline'a kuyruklanır."""
    if lang == "php":
        return {**php_modern.take(client, repo, group_ids, team=team, deps=deps), "lang": "php"}
    from agile_sdlc_crew import db, estimation
    from agile_sdlc_crew.quality import intake
    deps = deps or php_modern.default_deps()
    if not intake._LOCK.acquire(blocking=False):
        raise intake.Busy("Başka bir alım / kuyruk isteği sürüyor")
    try:
        cfg = deps.cfg()
        root = php_modern._root(cfg, repo, deps)
        repo = root.name
        tv = target(client, repo, root, lang, index=build_index(client))
        if not tv["version"]:
            raise ValueError(f"Hedef {LANGS[lang]['label']} sürümü bilinmiyor")
        hits, _e, _sha = _hits(root, lang, tv["version"], False)
        rules = _rules_with_generics(lang, hits)
        version = tv["version"] if lang == "nextjs" else "99.0"
        groups, _s, _sum = php_modern.build_groups({"hits": hits}, rules, version, repo=repo,
                                                   board_index=deps.board(client), now=deps.now(), cfg=cfg,
                                                   source=LANGS[lang]["source"])
        by_gid = {php_modern.gid_of(g): g for g in groups}
        picked, skipped = [], []
        max_take = max(1, int(cfg.get("max_take", 8)))
        for gid in dict.fromkeys(str(x) for x in group_ids or []):
            g = by_gid.get(gid)
            if g is None:
                skipped.append({"gid": gid, "reason": "tarama değişti ya da board'da açık — yeniden tarayın"})
            elif len(picked) >= max_take:
                skipped.append({"gid": gid, "reason": f"istek başı tavan ({max_take})"})
            else:
                picked.append(g)
        if not picked:
            return {"repo": repo, "lang": lang, "tasks": [], "human_tasks": [], "skipped": skipped, "story": None}
        story = intake.resolve_story(client, LANGS[lang]["source"], None, team, cfg.get("target_sprint") or "next",
                                     lane="techdebt")
        cfg2 = {**cfg, "php_target": f"{tv['version']} ({tv['source']})"}
        if LANGS[lang]["tool_fix"]:
            def _queue(wid, title):
                # tür INSERT'te: sonradan güncellenirse işçi işi araya girip pipeline'la koşturabilir
                return db.create_job(str(wid), False, wi_title=title[:200], job_kind="modern_fix")
            deps = deps._replace(queue=_queue)
        rows, added = intake._open_tasks(client, picked, story, cfg2, deps, source=LANGS[lang]["source"],
                                         lane="techdebt", queue=True)
        if added:
            estimation.write_story_points(client, str(story["id"]), int(story["sp"] + added), logger=log.info)
        tasks = [r for r in rows if not r["needs_human"]]
        human = [r for r in rows if r["needs_human"]]
        log.info(f"🧹 {LANGS[lang]['label']} modernleştirme ({repo}): story #{story['id']} · {len(rows)} Task · {added} SP")
        return {"repo": repo, "lang": lang, "version": tv["version"],
                "story": {"id": story["id"], "url": intake._wi_url(client, story["id"]), "created": story["created"]},
                "tasks": tasks, "human_tasks": human, "devops": [], "skipped": skipped,
                "total_sp": sum(r["sp"] for r in tasks if r.get("id")),
                "human_sp": sum(r["sp"] for r in human if r.get("id")), "tool_fix": bool(LANGS[lang]["tool_fix"])}
    finally:
        intake._LOCK.release()
