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
MAX_FILES = 20
MAX_BYTES = 2_000_000
MAX_COMPARE_FILES = 100            # dal/hedef karşılaştırması: bundan büyük ağaç → reddedilir
MAX_COMPARE_BYTES = 10_000_000
MAX_TAG = 64
PR_MAX = 4000
_CONT_RE = re.compile(r"\s*(?:&&|;)\s*\\\s*$")     # satır sonu: `&& \` ya da `; \` (zincir devam ediyor)


def _clean(text) -> str:
    """Depodan gelen metin PR açıklamasına girmeden: ters tırnak ve denetim karakterleri atılır."""
    return re.sub(r"[`\x00-\x1f\x7f]", "", str(text))


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


def needed(sites: list[dict], lang: str, *, cfg=None) -> list[dict]:
    """Yayınlanmamış, kendi taban imajımız olan devops siteleri dizine göre gruplanır. İmaj yalnız, sitenin
    `new`'i `old` ile AYNI kayıt/yol önekini taşıyorsa bizimdir (Tempo'nun kullanımdaki imajı yeniden yazması)."""
    name = _NAMES.get(lang)
    if not name:
        return []
    cfg = cfg or load_cfg()
    img = rf"((?:[^/\s]+/)+)({re.escape(name)}-\d+-\d+(?:-\d+)?):([^\n]*)"
    pat = re.compile(rf"^{img}$")
    groups: dict[str, dict] = {}
    for s in sites:
        if s.get("where") != "devops" or s.get("blocked") != UNPUBLISHED:
            continue
        m = pat.match(str(s.get("new") or ""))
        o = pat.match(str(s.get("old") or ""))
        if not m or not o or o.group(1) != m.group(1):
            continue
        g = groups.setdefault(m.group(2), {"dir": m.group(2), "tags": [], "regions": [], "sites": []})
        if m.group(3) not in g["tags"]:
            g["tags"].append(m.group(3))
        region = str(s.get("repo") or "")
        if region not in g["regions"]:
            g["regions"].append(region)
        g["sites"].append(s)
    out = []
    for g in groups.values():
        tags = sorted(g.pop("tags"))
        g["tag"] = tags[0]
        bad_tag = [t[:MAX_TAG] for t in tags if not _TAG_RE.fullmatch(t) or len(t) > MAX_TAG]
        unknown = [r for r in g["regions"] if r not in (cfg.get("regions") or {})]
        g["error"] = (f"geçersiz etiket: {_clean(', '.join(bad_tag))}" if bad_tag
                      else f"bilinmeyen bölge: {_clean(', '.join(unknown))}" if unknown
                      else f"bölgeler farklı etiket istiyor: {', '.join(tags)}" if len(tags) > 1 else "")
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
    out = {"dir": entry["dir"], "tag": _clean(entry.get("tag", "")), "regions": list(entry.get("regions") or []),
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


def _head(client, cfg: dict) -> str | None:
    refs = {b.get("name"): b.get("objectId")
            for b in client.list_branches(cfg["repo"], project=cfg.get("project") or None)}
    return refs.get(f"refs/heads/{cfg['branch']}")


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


def build(client, lang: str, to: str, entry: dict, *, cfg=None, resolver=None, commit: str | None = None) -> dict:
    cfg = cfg or load_cfg()
    name = _NAMES.get(lang)
    if not name:
        raise ImageTemplateError(f"{lang} için taban imaj şablonu desteklenmiyor.")
    if not re.fullmatch(r"\d+\.\d+", to or ""):
        raise ImageTemplateError(f"Hedef sürüm okunamadı: {to!r}.")
    target = entry["dir"]
    tag = str(entry.get("tag") or "")
    if not _TAG_RE.fullmatch(tag) or len(tag) > MAX_TAG:
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
    commit = commit or _head(client, cfg)
    if not commit:
        raise ImageTemplateError(f"Hedef dal '{cfg['branch']}' bulunamadı.")
    items = client.get_items_in_path(cfg["repo"], "/" + source, branch=cfg["branch"], recursion_level="full",
                                     project=proj)
    prefix = f"/{source}/"
    paths = [str(i.get("path") or "") for i in items
             if not i.get("isFolder") and str(i.get("path") or "").startswith(prefix)]
    if len(paths) > MAX_FILES:
        raise ImageTemplateError(f"Kaynak dizinde ('{source}') {len(paths)} dosya var; "
                                 f"en çok {MAX_FILES} kopyalanır.")
    files: dict[str, bytes] = {}
    total = 0
    for p in paths:
        data = client.get_file_bytes(cfg["repo"], p, project=proj, version=commit, version_type="commit")
        if data is None:
            raise ImageTemplateError(f"Kaynak dosya okunamadı: {p}.")
        total += len(data)
        if total > MAX_BYTES:
            raise ImageTemplateError(f"Kaynak dizin ('{source}') {MAX_BYTES // 1_000_000} MB'tan büyük; kopyalanmadı.")
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
        hit = set()
        for k, ln in enumerate(lines):
            raw = ln.rstrip("\r\n")
            if not rx.search(raw):
                continue
            cmd = _CONT_RE.sub("", raw.strip()) if _CONT_RE.search(raw) else None
            if cmd is None or not rx.fullmatch(cmd):
                raise ImageTemplateError(
                    "Düzeltme satırı yalnız ilgili komutu içermiyor ya da '&& \\' ile bitmiyor (RUN zincirinin "
                    f"ortası değil); elle bakılmalı: {_clean(raw.strip())[:120]}")
            hit.add(k)
        if not hit:
            notes.append(f"düzeltme uygulanmadı: {fx.get('note', fx['drop'])}")
            continue
        lines = [ln for k, ln in enumerate(lines) if k not in hit]
        notes.append(str(fx.get("note") or fx["drop"]))
    files[dpath] = "".join(lines).encode("utf-8")
    files[f"/{target}/tag.txt"] = tag.encode("utf-8")
    return {"source": source, "full": full, "files": files, "notes": notes}


def _description(entry: dict, built: dict, cfg: dict, repo: str) -> str:
    """PR açıklaması. Depodan gelen her metin `_clean`'den geçer; 4000 sınırı burada korunur: önce servis listeleri
    kısalır (10 → 5 → 3 → 1 → 0), bayrak bloğu ve RU uyarısı asla kesilmez."""
    regs = cfg.get("regions") or {}
    dir_, tag = _clean(entry["dir"]), _clean(entry["tag"])

    def render(limit: int, notes: list[str]) -> str:
        out = ["Tempo bu şablonu sürüm yükseltme için hazırladı. **İmaj üretilmedi / yayınlanmadı.**", "",
               f"Kaynak dizin: `{_clean(built['source'])}` → `{dir_}` (etiket `{tag}`).", "", "Değişiklikler:"]
        out += [f"- {_clean(n)[:300]}" for n in notes]
        out += ["", "Derleme doğrulanmadı: imaj derlenip denenmeden bu PR birleştirilmemeli.", "", "## Yayın", "",
                f"Jenkins `baseImage={dir_}` ile derlenir; bekleyen bölgeler ve bayrakları:"]
        for r in entry.get("regions") or []:
            paths = [_clean(s.get("path")) for s in entry["sites"] if s.get("repo") == r]
            out.append(f"- **{_clean(r)}** → `{_clean(regs.get(r, '?'))}` — {len(paths)} bekleyen servis:")
            out += [f"  - `{p[:200]}`" for p in paths[:limit]]
            if len(paths) > limit:
                out.append(f"  - … ve {len(paths) - limit} servis daha")
        if "russia" in (entry.get("regions") or []):
            out += ["", "RU registry'sine itilmezse russia servisleri geçemez."]
        if repo:
            out += ["", f"İsteyen repo: `{_clean(repo)[:100]}`."]
        return "\n".join(out)
    notes = list(built["notes"])[:5]
    for limit in (SITES_PER_REGION, 5, 3, 1, 0):
        text = render(limit, notes)
        if len(text) <= PR_MAX:
            return text
    return render(0, notes[:1])[:PR_MAX]


def _denied(e: Exception) -> bool:
    return getattr(getattr(e, "response", None), "status_code", None) in (401, 403)


def _branch_matches(client, cfg: dict, branch: str, head: str, built: dict, target_head: str) -> bool:
    """Tempo'nun dalı (PR'sız) SABİT hedef commit'ten (`target_head`) tam olarak şablon dosyalarıyla ayrışıyor mu:
    dosya kümesi = hedef + şablon, şablon dosyaları bayt bayt `built`, diğer HER dosya hedef commit'tekiyle bayt
    bayt aynı. İçerikler sabit commit'lerden okunur (dal başı `head`, hedef `target_head`); listeleme dal adıyla
    yapılır ama ardından iki başın da değişmediği doğrulanır. Büyük ağaç / okunamayan / farklı her şey False."""
    proj = cfg.get("project") or None
    try:
        def listing(ref):
            """yol -> öğe; düz dosya (blob) olmayan herhangi bir öğe (submodule, symlink…) → ValueError.
            Azure dosyalara `gitObjectType: blob`, klasörlere `tree` verir; alan yoksa blob sayılır."""
            out = {}
            for i in client.get_items_in_path(cfg["repo"], "/", branch=ref, recursion_level="full", project=proj):
                kind = i.get("gitObjectType")
                if kind not in (None, "", "blob", "tree") or (kind == "tree" and not i.get("isFolder")) \
                        or (i.get("isFolder") and kind not in (None, "", "tree")):
                    raise ValueError(f"düz dosya değil: {i.get('path')}")
                if not i.get("isFolder"):
                    out[str(i.get("path"))] = i
            return out
        mine_items, theirs_items = listing(branch), listing(cfg["branch"])
        mine, theirs = set(mine_items), set(theirs_items)
        refs = {b.get("name"): b.get("objectId") for b in client.list_branches(cfg["repo"], project=proj)}
        if refs.get(f"refs/heads/{branch}") != head or refs.get(f"refs/heads/{cfg['branch']}") != target_head:
            return False
        if mine != theirs | set(built["files"]) or len(mine) > MAX_COMPARE_FILES:
            return False
        total = 0
        for p in sorted(mine):
            a, b = mine_items[p].get("objectId"), (theirs_items.get(p) or {}).get("objectId")
            if p not in built["files"] and a and b:
                if a != b:
                    return False                 # nesne kimlikleri varsa bayt okumadan kesin karar
                continue
            got = client.get_file_bytes(cfg["repo"], p, project=proj, version=head, version_type="commit")
            want = built["files"][p] if p in built["files"] else client.get_file_bytes(
                cfg["repo"], p, project=proj, version=target_head, version_type="commit")
            total += len(got or b"")
            if got is None or got != want or total > MAX_COMPARE_BYTES:
                return False
        return True
    except Exception:  # noqa: BLE001
        return False


def open_pr(client, lang: str, to: str, entry: dict, *, cfg=None, resolver=None, repo: str = "") -> dict:
    """Taslak şablon PR'ı açar. Her başarısızlık `ImageTemplateError` olarak çıkar (ham ağ hatası sızmaz)."""
    try:
        return _open(client, lang, to, entry, cfg=cfg, resolver=resolver, repo=repo)
    except ImageTemplateError:
        raise
    except Exception as e:  # noqa: BLE001 — yazma aşamasındakiler `_open` içinde zaten dönüştürülür; buraya yazmadan önceki hatalar düşer
        raise ImageTemplateError(f"'{branch_name(entry.get('dir', ''))}' dalı, hazırlık aşamasında hata: "
                                 f"{mask_secrets(str(e))[:300]}. Push yapılmadı.") from e


def _open(client, lang: str, to: str, entry: dict, *, cfg=None, resolver=None, repo: str = "") -> dict:
    cfg = cfg or load_cfg()
    proj = cfg.get("project") or None
    with _LOCK:
        st = state(client, entry, cfg=cfg)
        if st["state"] != "yok":
            return {"state": st["state"], "pr": st["pr"], "reason": st["reason"], "notes": []}
        branch = branch_name(entry["dir"])
        oid = _head(client, cfg)
        if not oid:
            raise ImageTemplateError(f"Hedef dal '{cfg['branch']}' bulunamadı.")
        built = build(client, lang, to, entry, cfg=cfg, resolver=resolver, commit=oid)
        refs = {b.get("name"): b.get("objectId") for b in client.list_branches(cfg["repo"], project=proj)}
        if refs.get(f"refs/heads/{cfg['branch']}") != oid:
            raise ImageTemplateError(f"'{cfg['branch']}' dalı hazırlık sırasında ilerledi; yeniden deneyin.")
        create = push = True
        existing = refs.get(f"refs/heads/{branch}")
        if existing:
            if existing == oid:
                create = False                              # önceki denemeden kalan boş dal
            elif _branch_matches(client, cfg, branch, existing, built, oid):
                create = push = False                       # dal tam beklenen içerikte, yalnız PR eksik
            else:
                raise ImageTemplateError(f"'{branch}' dalı var, PR'ı yok ve içeriği beklenenden farklı; dala "
                                         "dokunulmadı. Kalan dalı silip yeniden deneyin.")
        title = f"{_clean(entry['dir'])} taban imaj şablonu ({_LABEL[lang]} {_clean(built['full'])})"
        changes = [{"changeType": "add", "path": p, "contentType": "base64encoded",
                    "content": base64.b64encode(b).decode("ascii")} for p, b in sorted(built["files"].items())]
        stage, pushed = "dal oluşturma", not push
        try:
            if create:
                res = client.create_branch(cfg["repo"], branch, cfg["branch"], project=proj, source_object_id=oid)
                vals = res.get("value") if isinstance(res, dict) else None
                if not (vals and vals[0].get("success")):
                    raise ImageTemplateError(f"dal oluşturulamadı ({(vals or [{}])[0].get('updateStatus') or 'yanıt okunamadı'})")
            if push:
                stage = "push"
                client.push_changes(cfg["repo"], branch, changes, title, project=proj, old_object_id=oid)
                pushed = True
            stage = "PR oluşturma"
            res = client.create_pull_request(cfg["repo"], branch, cfg["branch"], title=title,
                                             description=mask_secrets(_description(entry, built, cfg, repo)),
                                             draft=True, project=proj)
        except Exception as e:  # noqa: BLE001 — her hata dal adını ve push durumunu söyler
            why = ("yazma yetkisi yok (401/403)" if _denied(e) else mask_secrets(str(e))[:300])
            raise ImageTemplateError(f"'{branch}' dalı, {stage} aşamasında hata: {why}. "
                                     f"Push {'yapıldı' if pushed else 'yapılmadı'}.") from e
    pid = res.get("pullRequestId") if isinstance(res, dict) else None
    if not pid:
        raise ImageTemplateError(f"'{branch}' dalında PR büyük olasılıkla açıldı ama Azure yanıtı okunamadı; "
                                 "PR'ı Azure DevOps'ta kontrol edin (dal ve içerik hazır, push yapıldı).")
    # Web bağlantısı `AzureDevOpsClient.find_pull_request` ile aynı biçimde; yanıtın `url`'si REST adresidir,
    # yalnız org/proje bilinmiyorsa yedek olarak döner.
    base = getattr(client, "org_url", None)
    repo_res = res.get("repository") or {}
    proj_name = (repo_res.get("project") or {}).get("name") or cfg.get("project")
    if base and proj_name and pid:
        url = f"{base}/{proj_name}/_git/{repo_res.get('name') or cfg['repo']}/pullrequest/{pid}"
    else:
        url = res.get("url") or ""
    return {"state": "pr açık", "pr": {"id": pid, "url": url}, "notes": built["notes"]}
