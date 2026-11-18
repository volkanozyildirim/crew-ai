"""Yayınlanmamış taban imaj için `base-images` şablon PR'ı (spec: language-upgrade-design.md, "Ek 2026-11-18").

Üretim sitesi `imaj yayınlanmamış` ise ve imaj kendi taban imajımızsa (`…/php-8-5:1.0`), en yakın alt sürüm dizini
kopyalanıp sürüm satırı güncellenir ve `staging`'e taslak PR açılır. İmaj üretilmez / yayınlanmaz. Durum saklanmaz:
dizin hedef dalda var mı + sabit adlı dalın PR'ı. Her bilinmeyen kapalı kalır (`belirsiz`)."""
from __future__ import annotations

import base64
import logging
import re
import threading
from pathlib import Path

import requests
import yaml

from agile_sdlc_crew.quality.render import mask_secrets
from agile_sdlc_crew.quality.upgrade import UNPUBLISHED

log = logging.getLogger("pipeline")

CFG_PATH = Path(__file__).resolve().parent.parent / "config" / "upgrade_image.yaml"
_LOCK = threading.Lock()
_NAMES = {"php": "php", "python": "python"}          # dil -> taban imaj adı (Go'nun taban imajı yok)
_LABEL = {"php": "PHP", "python": "Python"}
_DIR_RE = re.compile(r"^([a-z]+)-(\d+)-(\d+)(?:-(\d+))?$")
_TAG_RE = re.compile(r"[\w][\w.\-]*")
SITES_PER_REGION = 10


class ImageTemplateError(Exception):
    """Şablon hazırlanamadı; mesaj Türkçe ve kullanıcıya gösterilir."""


def load_cfg(path=CFG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("repo", "base-images")
    cfg.setdefault("branch", "staging")
    cfg.setdefault("regions", {})
    cfg.setdefault("fixes", {})
    return cfg


def branch_name(dir_name: str) -> str:
    return f"tempo/base-image-{dir_name}"


def needed(sites: list[dict], lang: str) -> list[dict]:
    """Yayınlanmamış, kendi taban imajımız olan devops siteleri dizine göre gruplanır."""
    name = _NAMES.get(lang)
    if not name:
        return []
    pat = re.compile(rf"^(?:[^/\s]+/)+({re.escape(name)}-\d+-\d+(?:-\d+)?):([\w.\-]+)$")
    groups: dict[str, dict] = {}
    for s in sites:
        if s.get("where") != "devops" or s.get("blocked") != UNPUBLISHED:
            continue
        m = pat.match(str(s.get("new") or ""))
        if not m:
            continue
        g = groups.setdefault(m.group(1), {"dir": m.group(1), "tags": [], "regions": [], "sites": []})
        if m.group(2) not in g["tags"]:
            g["tags"].append(m.group(2))
        region = str(s.get("repo") or "")
        if region not in g["regions"]:
            g["regions"].append(region)
        g["sites"].append(s)
    out = []
    for g in groups.values():
        tags = sorted(g.pop("tags"))
        g["tag"] = tags[0]
        g["error"] = f"bölgeler farklı etiket istiyor: {', '.join(tags)}" if len(tags) > 1 else ""
        out.append(g)
    return out


def _flags(entry: dict, cfg: dict) -> str:
    regs = cfg.get("regions") or {}
    return ", ".join(f"{r} → {regs.get(r, '?')}" for r in entry.get("regions") or [])


def _pr_view(pr: dict | None) -> dict | None:
    return {"id": pr.get("id"), "url": pr.get("url", "")} if pr else None


def _folders(client, cfg: dict) -> set[str]:
    """`cfg.branch` kökündeki dizin adları (dal yoksa boş küme)."""
    proj = cfg.get("project") or None
    refs = {b.get("name") for b in client.list_branches(cfg["repo"], project=proj)}
    if f"refs/heads/{cfg['branch']}" not in refs:
        return set()
    items = client.get_items_in_path(cfg["repo"], "/", branch=cfg["branch"], recursion_level="oneLevel",
                                     project=proj)
    return {str(i.get("path") or "").rstrip("/").rpartition("/")[2] for i in items if i.get("isFolder")}


def state(client, entry: dict, *, cfg=None) -> dict:
    cfg = cfg or load_cfg()
    out = {"dir": entry["dir"], "tag": entry.get("tag", ""), "regions": list(entry.get("regions") or []),
           "state": "belirsiz", "reason": "", "pr": None}
    if entry.get("error"):
        out["reason"] = entry["error"]
        return out
    try:
        if entry["dir"] in _folders(client, cfg):
            out.update(state="şablon var", reason=f"DevOps imajı üretip yayınlamalı: {_flags(entry, cfg)}")
            return out
        pr = client.find_pull_request(cfg["repo"], branch_name(entry["dir"]), project=cfg.get("project") or None)
    except Exception as e:  # noqa: BLE001 — okunamayan durum PR açtırmaz
        out["reason"] = f"okunamadı: {mask_secrets(str(e))[:200]}"
        return out
    out["pr"] = _pr_view(pr)
    status = (pr or {}).get("status")
    if pr is None:
        out["state"] = "yok"
    elif status == "active":
        out["state"] = "pr açık"
    elif status == "abandoned":
        out["state"] = "pr reddedildi"
    else:
        out["reason"] = f"PR #{pr.get('id')} '{status}' ama dizin '{cfg['branch']}' dalında yok"
    return out


def _lines(text: str) -> list[str]:
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def _pick_source(client, name: str, to: str, cfg: dict) -> str:
    want = tuple(int(x) for x in to.split("."))
    best = None
    for d in _folders(client, cfg):
        m = _DIR_RE.match(d)
        if not m or m.group(1) != name:
            continue
        ver = (int(m.group(2)), int(m.group(3)), int(m.group(4) or 0))
        if ver[:2] < want and (best is None or ver > best[0]):
            best = (ver, d)
    if not best:
        raise ImageTemplateError(f"Kaynak dizin bulunamadı: '{cfg['branch']}' dalında {to} sürümünden küçük "
                                 f"bir '{name}-A-B' dizini yok.")
    return best[1]


def build(client, lang: str, to: str, entry: dict, *, cfg=None, resolver=None) -> dict:
    cfg = cfg or load_cfg()
    name = _NAMES.get(lang)
    if not name:
        raise ImageTemplateError(f"{lang} için taban imaj şablonu desteklenmiyor.")
    if not re.fullmatch(r"\d+\.\d+", to or ""):
        raise ImageTemplateError(f"Hedef sürüm okunamadı: {to!r}.")
    target = entry["dir"]
    tag = str(entry.get("tag") or "")
    if not _TAG_RE.fullmatch(tag):
        raise ImageTemplateError(f"İmaj etiketi geçersiz: {tag!r}.")
    tm = _DIR_RE.match(target)
    if not tm or tm.group(1) != name or f"{tm.group(2)}.{tm.group(3)}" != to:
        raise ImageTemplateError(f"Dizin adı '{target}' hedef sürümle ({to}) uyuşmuyor.")
    source = _pick_source(client, name, to, cfg)
    if tm.group(4):
        full = f"{tm.group(2)}.{tm.group(3)}.{tm.group(4)}"
    else:
        full = resolver.latest_patch(name, to) if resolver else None
        if not full:
            raise ImageTemplateError(f"{name} {to} için yama sürümü bulunamadı (Docker Hub okunamadı ya da etiket yok).")
    proj = cfg.get("project") or None
    items = client.get_items_in_path(cfg["repo"], "/" + source, branch=cfg["branch"], recursion_level="full",
                                     project=proj)
    files: dict[str, bytes] = {}
    prefix = f"/{source}/"
    for i in items:
        p = str(i.get("path") or "")
        if i.get("isFolder") or not p.startswith(prefix):
            continue
        data = client.get_file_bytes(cfg["repo"], p, branch=cfg["branch"], project=proj)
        if data is None:
            raise ImageTemplateError(f"Kaynak dosya okunamadı: {p}.")
        files[f"/{target}/{p[len(prefix):]}"] = data
    docker = [p for p in files if p[len(target) + 2:].lower() == "dockerfile"]
    if len(docker) != 1:
        raise ImageTemplateError(f"Kaynak dizinde ('{source}') tek bir Dockerfile yok.")
    dpath = docker[0]
    try:
        lines = _lines(files[dpath].decode("utf-8"))
    except UnicodeDecodeError:
        raise ImageTemplateError("Dockerfile UTF-8 olarak okunamadı.") from None
    notes = [f"Kaynak: {source} → {target} (sürüm {full}, etiket {tag})."]

    def one(rx: str, what: str) -> tuple[int, re.Match]:
        hits = [(n, m) for n, ln in enumerate(lines) if (m := re.match(rx, ln.rstrip("\r\n")))]
        if len(hits) != 1:
            raise ImageTemplateError(f"Dockerfile'da {what} sürüm satırı tam bir olmalı, {len(hits)} bulundu.")
        return hits[0]
    if lang == "php":
        n, _ = one(r"ARG PHP_VERSION=\d+\.\d+\.\d+\s*$", "'ARG PHP_VERSION=…'")
        lines[n] = re.sub(r"(?<==)\d+\.\d+\.\d+", full, lines[n], count=1)
        _, fm = one(r"FROM\s+php:\$\{PHP_VERSION\}(\S*)\s*$", "'FROM php:${PHP_VERSION}…'")
        suffix = fm.group(1)
    else:
        n, fm = one(r"FROM python:(\d+\.\d+\.\d+)(-\S+)?\s*$", "'FROM python:…'")
        suffix = fm.group(2) or ""
        lines[n] = lines[n].replace(fm.group(1), full, 1)
    ok = resolver.tag_exists(name, full + suffix) if resolver else None
    if ok is not True:
        raise ImageTemplateError(f"Docker Hub'da {name}:{full + suffix} etiketi "
                                 f"{'yok' if ok is False else 'doğrulanamadı'}.")
    for fx in (cfg.get("fixes") or {}).get(lang, {}).get(to) or []:
        rx = re.compile(fx["drop"])
        hit = {k for k, ln in enumerate(lines) if rx.search(ln.rstrip("\r\n"))}
        bad = [k for k in sorted(hit) if not lines[k].rstrip().endswith("\\")]
        if bad:
            raise ImageTemplateError("Düzeltme satırı '\\' ile bitmiyor (RUN zincirinin ortası değil): "
                                     f"{lines[bad[0]].strip()[:120]}")
        if not hit:
            notes.append(f"düzeltme uygulanmadı: {fx.get('note', fx['drop'])}")
            continue
        lines = [ln for k, ln in enumerate(lines) if k not in hit]
        notes.append(str(fx.get("note") or fx["drop"]))
    files[dpath] = "".join(lines).encode("utf-8")
    files[f"/{target}/tag.txt"] = tag.encode("utf-8")
    return {"source": source, "full": full, "files": files, "notes": notes}


def _description(entry: dict, built: dict, cfg: dict, repo: str) -> str:
    regs = cfg.get("regions") or {}
    out = ["Tempo bu şablonu sürüm yükseltme için hazırladı. **İmaj üretilmedi / yayınlanmadı.**", "",
           f"Kaynak dizin: `{built['source']}` → `{entry['dir']}` (etiket `{entry['tag']}`).", "", "Değişiklikler:"]
    out += [f"- {n}" for n in built["notes"]]
    out += ["", "Derleme doğrulanmadı: imaj derlenip denenmeden bu PR birleştirilmemeli.", "", "## Yayın", "",
            f"Jenkins `baseImage={entry['dir']}` ile derlenir; bekleyen bölgeler ve bayrakları:"]
    for r in entry.get("regions") or []:
        paths = [str(s.get("path")) for s in entry["sites"] if s.get("repo") == r]
        out.append(f"- **{r}** → `{regs.get(r, '?')}` — bekleyen servisler:")
        out += [f"  - `{p}`" for p in paths[:SITES_PER_REGION]]
        if len(paths) > SITES_PER_REGION:
            out.append(f"  - … ve {len(paths) - SITES_PER_REGION} servis daha")
    if "russia" in (entry.get("regions") or []):
        out += ["", "RU registry'sine itilmezse russia servisleri geçemez."]
    if repo:
        out += ["", f"İsteyen repo: `{repo}`."]
    return "\n".join(out)


def _denied(e: Exception) -> bool:
    return getattr(getattr(e, "response", None), "status_code", None) in (401, 403)


def open_pr(client, lang: str, to: str, entry: dict, *, cfg=None, resolver=None, repo: str = "") -> dict:
    cfg = cfg or load_cfg()
    proj = cfg.get("project") or None
    with _LOCK:
        st = state(client, entry, cfg=cfg)
        if st["state"] != "yok":
            return {"state": st["state"], "pr": st["pr"], "reason": st["reason"], "notes": []}
        branch = branch_name(entry["dir"])
        head = f"refs/heads/{cfg['branch']}"
        refs = {b.get("name"): b.get("objectId") for b in client.list_branches(cfg["repo"], project=proj)}
        oid = refs.get(head)
        if not oid:
            raise ImageTemplateError(f"Hedef dal '{cfg['branch']}' bulunamadı.")
        built = build(client, lang, to, entry, cfg=cfg, resolver=resolver)
        again = {b.get("name"): b.get("objectId") for b in client.list_branches(cfg["repo"], project=proj)}
        if again.get(head) != oid:
            raise ImageTemplateError(f"'{cfg['branch']}' dalı hazırlık sırasında ilerledi; yeniden deneyin.")
        create = True
        if f"refs/heads/{branch}" in refs:
            if refs[f"refs/heads/{branch}"] != oid:
                raise ImageTemplateError(f"'{branch}' dalı var, PR'ı yok ve '{cfg['branch']}' ile aynı commit'te "
                                         "değil; dala dokunulmadı. Kalan dalı silip yeniden deneyin.")
            create = False                                  # önceki denemeden kalan boş dal
        title = f"{entry['dir']} taban imaj şablonu ({_LABEL[lang]} {built['full']})"
        changes = [{"changeType": "add", "path": p, "contentType": "base64encoded",
                    "content": base64.b64encode(b).decode("ascii")} for p, b in sorted(built["files"].items())]
        try:
            if create:
                res = client.create_branch(cfg["repo"], branch, cfg["branch"], project=proj, source_object_id=oid)
                vals = res.get("value") if isinstance(res, dict) else None
                if not (vals and vals[0].get("success")):
                    why = (vals or [{}])[0].get("updateStatus") or "yanıt okunamadı"
                    raise ImageTemplateError(f"Dal oluşturulamadı ({why}).")
            client.push_changes(cfg["repo"], branch, changes, title, project=proj, old_object_id=oid)
            res = client.create_pull_request(cfg["repo"], branch, cfg["branch"], title=title,
                                             description=mask_secrets(_description(entry, built, cfg, repo)),
                                             draft=True, project=proj)
        except requests.HTTPError as e:
            if _denied(e):
                raise ImageTemplateError("base-images reposuna yazma yetkisi yok (401/403); "
                                         "PR tamamlanamadı.") from e
            raise
    pid = res.get("pullRequestId")
    url = res.get("url") or ""
    base = getattr(client, "org_url", None)
    if base:
        name = (res.get("repository") or {}).get("name") or cfg["repo"]
        url = f"{base}/{cfg.get('project')}/_git/{name}/pullrequest/{pid}"
    return {"state": "pr açık", "pr": {"id": pid, "url": url}, "notes": built["notes"]}
