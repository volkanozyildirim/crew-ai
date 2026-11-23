"""Taban imaj güvenlik uyarısının gidermesi (spec: language-upgrade-design.md, "Ek 2026-11-18: taban imaj güvenlik
uyarısı", "Giderme uyarıyla birlikte başlar").

`image_vuln.report` kaydındaki işarete göre, başka ekiplerin repolarına YAZAN bir giderme planlar ve uygular:

- `taban-yama`: kendi iki parçalı taban dizinimiz (`php-8-4`) → `base-images@staging`'de tek sürüm satırı, taslak PR.
- `taban-dizin`: kendi üç parçalı dizinimiz (`python-3-13-7`) → aynı minörün yeni yamasıyla dizin şablonu
  (`upgrade_image.open_pr`).
- `imaj-yama`: genel üç parçalı imaj (`golang:1.24.4`) → kullanan her bölge reposunda `FROM` satırı, bölge başına PR.
- `taban-taşıma`: düzeltme `base-images`'ın ilk dalında hazır ama sonraki dala taşınmamış → `flow` sırasındaki ilk
  farklı komşu dal çiftine taslak taşıma PR'ı (dal oluşturulmaz; dalın tamamını taşır, gövde bunu söyler).
- `taban-etiket`: üretim dalında `tag.txt` yükselmiş, bölge repoları eski etiketi çekiyor → bölge başına `FROM`
  etiketi PR'ı.
- `yükseltme`: desteği biten dil → `upgrade_track.start` ile sürüm yükseltme izlemesi.
- `yeniden-üret`: yalnız işletim sistemi bulgusu → yazma yok, DevOps'tan ister.

Her yazma yolu kapalı kalır: bilinmeyen / belirsiz / elle olan hiçbir şey yazılmaz. Durum saklanmaz; sabit dal adı,
açık / terk edilmiş PR ve açık izleme işi üzerinden her çağrıda yeniden türetilir. Terk edilen PR yeniden açılmaz."""
from __future__ import annotations

import hashlib
import html
import logging
import re
import threading

from agile_sdlc_crew.quality import app_map, image_vuln
from agile_sdlc_crew.quality.render import mask_secrets
from agile_sdlc_crew.quality.upgrade_image import _DIR_RE as _UI_DIR_RE
from agile_sdlc_crew.quality.upgrade_image import _clean, _pr_view

log = logging.getLogger("pipeline")

_LOCK = threading.Lock()
TRACKED_LANGS = ("php", "python", "go")           # Tempo'nun sürüm yükseltmesini izlediği diller


def _tracked() -> tuple[str, ...]:
    """İzlenen diller; Node yalnız CREW_TECHDEBT_UPGRADE_NODE açıkken (varsayılan kapalı)."""
    from agile_sdlc_crew.quality import modern
    return TRACKED_LANGS + (("node",) if modern.node_on() else ())
_OWN_NAMES = {"php": "php", "python": "python"}   # taban imaj şablonu olan diller (Go'nun taban imajı yok)
_DIR_RE = re.compile(_UI_DIR_RE.pattern, re.ASCII)   # ASCII dışı rakamlı ad (`８`) int()'e girmesin, dal adını bozmasın
_THREE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$", re.ASCII)
_TAG3 = re.compile(r"^(\d+\.\d+\.\d+)(.*)$", re.S | re.ASCII)
_ARG_PHP = re.compile(r"^ARG PHP_VERSION=(\d+\.\d+\.\d+)\s*$", re.ASCII)
_FROM_PHP = re.compile(r"^FROM\s+php:\$\{PHP_VERSION\}(\S*)\s*$", re.ASCII)
_FROM_PY = re.compile(r"^FROM\s+python:(\d+\.\d+\.\d+)(-\S+)?\s*$", re.ASCII)
BODY_MAX = 3900                                    # Azure PR açıklaması sınırı 4000
_RANK = ("hata", "sırada", "açıldı")


class SelectionError(ValueError):
    """Karttan gelen repo seçimi taze plandaki repolarla eşleşmiyor — hiçbir şey yazılmadan reddedilir."""


def _act(kind: str, state: str, reason: str = "", **kw) -> dict:
    return {"kind": kind, "state": state, "reason": reason, "target": kw.pop("target", None), "pr": kw.pop("pr", None),
            "ran": False, **kw}


def _ver(s: str) -> tuple[int, int, int]:
    m = _THREE.match(s)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)


def _why(e: Exception) -> str:
    return mask_secrets(str(e) or type(e).__name__)[:200]


def _https(url) -> str:
    u = str(url or "")
    return u if re.match(r"^https://[^\s\"'<>]+$", u) else ""


def _read(client, repo: str, path: str, branch: str, project: str | None) -> str | None:
    """Dosya yoksa (404) None; başka her okuma hatası yükselir (belirsiz kalır, yazılmaz)."""
    try:
        text = client.get_file_content(repo, path, branch=branch, project=project)
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        if getattr(getattr(e, "response", None), "status_code", None) == 404:
            return None
        raise
    return text if isinstance(text, str) and text else None


def _pr_state(client, repo: str, branch: str, project: str | None) -> tuple[str, dict | None, str]:
    pr = client.find_pull_request(repo, branch, project=project)
    if pr is None:
        return "yok", None, ""
    status = pr.get("status")
    state = {"active": "pr açık", "abandoned": "pr reddedildi"}.get(status, "pr kapandı")
    return state, _pr_view(pr), (f"PR #{pr.get('id')} '{status}'" if state == "pr kapandı" else "")


def _branch_ok(branch: str, target: str) -> str:
    """Hesaplanan dal adı push anında reddedilecekse (`_validate_branch`) şimdi neden döner; uygunsa ""."""
    from agile_sdlc_crew.quality import upgrade_bump
    try:
        upgrade_bump._validate_branch(branch, {target})
    except ValueError as e:
        return f"dal adı geçersiz: {_why(e)}"
    return ""


def _older_open_pr(client, repo: str, project: str | None, prefix: str, own: str,
                   tail: str = r"\d+\.\d+\.\d+") -> dict | None:
    """Aynı eski imaj için başka (daha eski bir yamaya giden) dalın AÇIK PR'ı var mı? Yalnız `list_branches` (zaten push
    öncesi okunan hafif çağrı) + öneke uyan her dal için tam ad aramasıyla; terk / tamamlanmış PR engellemez."""
    refs = [str(b.get("name") or "").removeprefix("refs/heads/") for b in client.list_branches(repo, project=project)]
    # TAM eşleşme: `…-golang-1.24.4-` öneki `…-golang-1.24.4-alpine-1.24.13` gibi kardeş imaj dallarına uymasın.
    pat = re.compile(re.escape(prefix) + tail, re.ASCII)
    for name in sorted(r for r in refs if pat.fullmatch(r) and r != own):
        pr = client.find_pull_request(repo, name, project=project)
        if pr and pr.get("status") == "active":
            return pr
    return None


def _pr_or_older(client, repo: str, project: str | None, branch: str, prefix: str,
                 tail: str = r"\d+\.\d+\.\d+"):
    state, pr, why = _pr_state(client, repo, branch, project)
    if state == "yok":
        older = _older_open_pr(client, repo, project, prefix, branch, tail)
        if older:
            return "pr açık", _pr_view(older), f"önceki yama PR'ı açık: #{older.get('id')} — önce o kapanmalı"
    return state, pr, why


# ── Kullananlar (taze index'ten) ─────────────────────────────────────────

def _users(index: dict, entry: dict) -> list[tuple[str, str, str, str]]:
    """İmajı kullanan (kod repo, bölge, yol, ham FROM) satırları. Kendi imajımız dizinle, genel imaj adıyla eşleşir."""
    dirs = set(entry.get("via") or [])
    image = entry["image"]
    out = []
    for repo, items in (index or {}).items():
        for item in items or []:
            region, _, path = str(item.get("src") or "").partition("/")
            for raw in item.get("images") or []:
                if not isinstance(raw, str):
                    continue
                m = image_vuln._OWN_RE.match(raw) if dirs else None
                if (m and m.group(1) in dirs) or (not dirs and raw == image):
                    out.append((str(repo), region, path, raw))
    return out


def _flags(entry: dict, cfg: dict) -> list[str]:
    regs = cfg.get("regions") or {}
    return [f"{r} → {regs.get(r, '?')}" for r in sorted((entry.get("users") or {}))]


def _scan_text(entry: dict) -> str:
    sc = entry.get("scan") or {}
    if sc.get("state") == "tamam" and (sc.get("critical") or sc.get("high")):
        return (f" Düzeltmesi yayınlanmış açık: {int(sc.get('critical') or 0)} kritik, "
                f"{int(sc.get('high') or 0)} yüksek.")
    return ""


def _on(key: str) -> bool:
    from agile_sdlc_crew import pipeline_config as pc
    try:
        return bool(pc.get(key))
    except KeyError:
        return False


def _unknown_regions(regions, cfg: dict) -> list[str]:
    """`upgrade_image.needed` ile aynı kural: `config/upgrade_image.yaml` `regions`'ta olmayan bölge (RU sessizce
    bayraksız kalmasın; `?` basılmaz)."""
    known = cfg.get("regions") or {}
    return sorted(r for r in regions if r not in known)


def _unknown_act(kind: str, regions, cfg: dict):
    unk = _unknown_regions(regions, cfg)
    return _act(kind, "elle", f"bilinmeyen bölge: {_clean(', '.join(unk))[:100]}") if unk else None


def _guard(kind: str, fn, *a, **kw) -> dict:
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001 — okunamayan durum yazdırmaz
        return _act(kind, "belirsiz", f"okunamadı: {_why(e)}")


# ── Plan ─────────────────────────────────────────────────────────────────

def plan(client, entry: dict, *, index: dict, resolver, cfg=None, find_open_fn=None, find_any_fn=None,
         auto: bool = False, current_fn=None) -> list[dict]:
    """Yazma yok. Rapor kaydı (`image_vuln.report` öğesi) → eylem listesi (modül belgesine bakın). `auto` (yoklayıcı):
    daha önce HİÇ izleme açılmış repo (kapatılmışı dahil) `durduruldu` sayılır — kullanıcının durdurduğu izleme
    kendiliğinden yeniden açılmaz; düğme (auto=False) yeniden başlatabilir."""
    from agile_sdlc_crew.quality import upgrade_image
    cfg = cfg or upgrade_image.load_cfg()
    out: list[dict] = []
    patch, eol = entry.get("patch"), entry.get("eol")
    sc = entry.get("scan") or {}
    bump_on = _on("CREW_TECHDEBT_UPGRADE_BUMP")
    # Taşıma PR'ı ve bölge etiket PR'ı ayrı anahtarla açılır (varsayılan kapalı): kapalıyken hiç planlanmaz.
    promote_on = _on("CREW_TECHDEBT_IMAGE_VULN_PROMOTE")

    def pr_action(kind: str, fn, *a):
        # Tempo'nun bütün sürüm PR'ları gibi `_BUMP` kapalıyken PR açılmaz (yazma yolu kapalı).
        if not bump_on:
            return _act(kind, "elle", "sürüm PR'ları kapalı (CREW_TECHDEBT_UPGRADE_BUMP)")
        return _guard(kind, fn, *a)
    if patch and patch.get("latest"):
        if entry.get("via"):
            for d in entry["via"]:
                m = _DIR_RE.match(d)
                if m and m.group(4):
                    out.append(pr_action("taban-dizin", _plan_dir, client, entry, d, index, resolver, cfg))
                else:
                    out.append(_unknown_act("taban-yama", entry.get("users") or {}, cfg)
                               or pr_action("taban-yama", _plan_own, client, entry, d, resolver, cfg))
                # Düzeltme ilk dalda hazırsa (yama güncel / yeni dizin şablonu var) sıradaki dala taşıma PR'ı.
                last = out[-1]
                if promote_on and {"taban-yama": "güncel", "taban-dizin": "şablon var"}.get(last["kind"]) == last["state"]:
                    mv = pr_action("taban-taşıma", _plan_promote, client, entry, last.get("dir") or d, cfg)
                    # aynı dal çifti için tek PR olur: ikinci dizin aynı çifti isterse ayrıca planlanmaz
                    if mv and (mv.get("src"), mv.get("dst")) not in {(x.get("src"), x.get("dst")) for x in out
                                                                    if x["kind"] == "taban-taşıma" and x.get("src")}:
                        out.append(mv)
        else:
            out.append(pr_action("imaj-yama", _plan_public, client, entry, index, resolver, cfg))
    for d in sorted((entry.get("tag_lag") or {}) if promote_on else ()):
        # Üretim dalında etiket yükselmiş, bölge repoları eskisini çekiyor (rapordaki işaret; etiket planda taze okunur).
        tg = pr_action("taban-etiket", _plan_tag, client, entry, d, index, cfg)
        if tg:
            out.append(tg)
    if eol:
        out.append(_guard("yükseltme", _plan_upgrade, client, entry, index, find_open_fn, find_any_fn, auto,
                           current_fn))
    if not patch and not eol and sc.get("state") == "tamam" and (sc.get("critical") or sc.get("high")):
        unk = _unknown_act("yeniden-üret", entry.get("users") or {}, cfg)
        if unk:
            out.append(unk)
            return out
        flags = ", ".join(_flags(entry, cfg)) or "bölge bilinmiyor"
        what = (f"Sürüm değişikliği gidermez: işletim sistemi paketlerinde düzeltmesi yayınlanmış bulgu var "
                f"({int(sc.get('critical') or 0)} kritik, {int(sc.get('high') or 0)} yüksek). ")
        if entry.get("via"):
            why = f"İmaj yeniden üretilmeli — DevOps üretmeli. Bölge bayrakları: {flags}."
        else:
            why = (f"Servis imajı yeniden derlendiğinde taze taban çekilir — servisi yeniden yayınla. "
                   f"Kullanan bölgeler: {flags}.")
        out.append(_act("yeniden-üret", "devops", what + why))
    return out


def _plan_own(client, entry: dict, d: str, resolver, cfg: dict) -> dict:
    """Kendi iki parçalı dizin: `base-images@staging`'deki TEK sürüm satırı."""
    kind = "taban-yama"
    lang, latest = entry.get("lang"), str(entry["patch"]["latest"])
    repo, proj, branch = cfg["repo"], cfg.get("project") or None, cfg["branch"]
    dm = _DIR_RE.match(d)
    if lang not in _OWN_NAMES or not _THREE.match(latest):
        return _act(kind, "elle", f"{lang or 'bu'} dili için taban dizin yaması desteklenmiyor.")
    if not dm or dm.group(4) or dm.group(1) != _OWN_NAMES[lang]:
        return _act(kind, "elle", f"'{_clean(d)[:60]}' dizin adı {lang} iki parçalı taban dizini değil; yazılmaz.")
    path = text = None
    for name in ("Dockerfile", "dockerfile"):
        text = _read(client, repo, f"/{d}/{name}", branch, proj)
        if text is not None:
            path = f"/{d}/{name}"
            break
    if text is None:
        return _act(kind, "elle", f"{repo}@{branch}'de {d}/Dockerfile yok.", dir=d)
    lines = text.splitlines()
    rx = _ARG_PHP if lang == "php" else _FROM_PY
    hits = [(n, m) for n, ln in enumerate(lines, 1) if (m := rx.match(ln))]
    if len(hits) != 1:
        return _act(kind, "elle", f"{path}: sürüm satırı tam bir olmalı, {len(hits)} bulundu.", dir=d)
    n, m = hits[0]
    cur = m.group(1)
    if lang == "php":
        fm = [x for ln in lines if (x := _FROM_PHP.match(ln))]
        if len(fm) != 1:
            return _act(kind, "elle", f"{path}: 'FROM php:${{PHP_VERSION}}…' satırı tam bir olmalı.", dir=d)
        suffix = fm[0].group(1)
    else:
        suffix = m.group(2) or ""
    if cur.rsplit(".", 1)[0] != latest.rsplit(".", 1)[0]:
        return _act(kind, "elle", f"{path}: staging sürümü ({cur}) başka bir minörde.", dir=d)
    branch_name = f"tempo/image-patch-{d}-{cur}-{latest}"      # eski → yeni çiftini adında taşır
    base = dict(dir=d, target=latest, branch=branch_name)
    if _ver(cur) >= _ver(latest):
        return _act(kind, "güncel", "staging'de güncel — DevOps üretip taşımalı", **base)
    ok = resolver.tag_exists(image_vuln._HUB[lang], latest + suffix) if resolver else None
    if ok is not True:
        return _act(kind, "elle", f"Docker Hub'da {image_vuln._HUB[lang]}:{latest + suffix} etiketi "
                                  f"{'yok' if ok is False else 'doğrulanamadı'}.", **base)
    site = {"where": "base", "repo": repo, "project": proj or "", "branch": branch, "path": path, "line": n,
            "old": cur, "new": latest, "blocked": ""}
    from agile_sdlc_crew.quality import upgrade_bump
    if not upgrade_bump.pushable(site):
        return _act(kind, "elle", f"{path}: sürüm satırı yalnız rakam değişimiyle yazılamıyor.", **base)
    try:
        upgrade_bump.apply_site(text, site)                    # kuru çalıştırma: yazma anında bozulacaksa şimdi elle
    except ValueError as e:
        return _act(kind, "elle", _why(e), **base)
    bad = _branch_ok(branch_name, branch)
    if bad:
        return _act(kind, "elle", bad, **base)
    state, pr, why = _pr_or_older(client, repo, proj, branch_name, f"tempo/image-patch-{d}-{cur}-")
    return _act(kind, state, why, pr=pr, sites=[site], **base)


def _plan_dir(client, entry: dict, d: str, index: dict, resolver, cfg: dict) -> dict:
    """Kendi üç parçalı dizin: aynı minörün yeni yamasıyla yeni dizin şablonu (şablon adımının yolu)."""
    from agile_sdlc_crew.quality import upgrade_image
    kind = "taban-dizin"
    lang, latest = entry.get("lang"), str(entry["patch"]["latest"])
    m = _DIR_RE.match(d)
    name = _OWN_NAMES.get(lang)
    if not name or not m or m.group(1) != name or not _THREE.match(latest) \
            or latest.rsplit(".", 1)[0] != f"{m.group(2)}.{m.group(3)}":
        return _act(kind, "elle", f"'{d}' dizini için aynı minörün yama dizini türetilemedi.")
    new_dir = f"{name}-{m.group(2)}-{m.group(3)}-{latest.rsplit('.', 1)[1]}"
    raws = _users(index, {"image": entry["image"], "via": [d]})
    tags = sorted({image_vuln._OWN_RE.match(r[3]).group(2) for r in raws})
    regions = sorted({r[1] for r in raws})
    unk = _unknown_act(kind, regions, cfg)
    if unk:
        return unk
    if not raws:
        return _act(kind, "elle", f"'{d}' dizinini kullanan servis taze index'te bulunamadı.", dir=new_dir)
    ent = {"dir": new_dir, "tag": tags[0], "regions": regions, "sites": [{"repo": r[1], "path": "/" + r[2]} for r in raws],
           "error": "" if len(tags) == 1 else f"servisler farklı etiket istiyor: {', '.join(tags)}"}
    st = upgrade_image.state(client, ent, cfg=cfg)
    return _act(kind, st["state"], st["reason"], pr=st["pr"], dir=new_dir, target=new_dir, entry=ent,
                to=f"{m.group(2)}.{m.group(3)}", lang=lang)


def _slug(image: str) -> str:
    """Dal adı gövdesi: `library/golang:1.24.4-alpine` → `library-golang-1.24.4-alpine` (eski etiket ve ad dahil: aynı
    yamaya giden iki ayrı eski etiket / `golang` ile `library/golang` aynı dalı paylaşmaz)."""
    name, _, tag = image.partition(":")
    return re.sub(r"[^a-z0-9.]+", "-", f"{name}-{tag}".lower()).strip("-.")


def _plan_public(client, entry: dict, index: dict, resolver, cfg: dict) -> dict:
    """Genel üç parçalı imaj: kullanan her bölge reposundaki `FROM` satırı; bir bölge bile belirsizse hiçbiri yazılmaz."""
    from agile_sdlc_crew.quality import upgrade_bump
    kind = "imaj-yama"
    image, lang, latest = entry["image"], entry.get("lang"), str(entry["patch"]["latest"])
    base, _, tag = image.rpartition(":")
    hub = image_vuln._HUB.get(lang)
    m = _TAG3.match(tag)
    if not hub or not base or not m or base.rsplit("/", 1)[-1] != hub or not _THREE.match(latest) \
            or latest.rsplit(".", 1)[0] != m.group(1).rsplit(".", 1)[0]:
        return _act(kind, "elle", f"'{image}' etiketi aynı minörün yamasına çevrilemiyor.")
    suffix = m.group(2)
    new = f"{base}:{latest}{suffix}"
    ok = resolver.tag_exists(hub, latest + suffix) if resolver else None
    if ok is not True:
        return _act(kind, "elle", f"Docker Hub'da {hub}:{latest + suffix} etiketi {'yok' if ok is False else 'doğrulanamadı'}.",
                    target=new)
    slug = _slug(image)
    if not slug or ".." in slug:
        return _act(kind, "elle", f"'{image}' için dal adı türetilemedi.", target=new)
    branch = f"tempo/image-patch-{slug}-{latest}"
    bad = _branch_ok(branch, app_map.DEVOPS_BRANCH)
    if bad:
        return _act(kind, "elle", bad, target=new)
    proj, dbranch = app_map.DEVOPS_PROJECT, app_map.DEVOPS_BRANCH
    fresh: dict[str, list[str]] = {}
    for _repo, region, path, _raw in _users(index, entry):
        if path not in fresh.setdefault(region, []):
            fresh[region].append(path)
    units = []
    for region in sorted(entry.get("users") or {}):
        u = {"repo": region, "state": "elle", "reason": "", "sites": [], "pr": None}
        units.append(u)
        if region not in app_map.DEVOPS_REPOS:
            u["reason"] = f"'{_clean(region)[:40]}' bir bölge reposu değil; yazılmaz."
            continue
        if not fresh.get(region):
            u["reason"] = "taze index'te bu bölgede imajı kullanan servis yok."
            continue
        try:
            sites, why = _region_sites(client, region, fresh[region], image, new, proj, dbranch)
            if why:
                u["reason"] = why
                continue
            if not all(upgrade_bump.pushable(s) for s in sites):
                u["reason"] = "FROM satırı yalnız rakam değişimiyle yazılamıyor."
                continue
            u["sites"] = sites
            u["state"], u["pr"], u["reason"] = _pr_or_older(client, region, proj, branch, f"tempo/image-patch-{slug}-")
        except Exception as e:  # noqa: BLE001
            u["state"], u["reason"] = "belirsiz", f"okunamadı: {_why(e)}"
    return _act(kind, _agg_plan(units), _bad_reason(units), repos=units, branch=branch, target=new)


def _region_sites(client, region: str, paths: list[str], old: str, new: str, proj: str, branch: str):
    from agile_sdlc_crew.quality import upgrade_bump
    sites: list[dict] = []
    for path in paths:
        p = f"/{path}/Dockerfile"
        text = _read(client, region, p, branch, proj)
        if text is None:
            return [], f"{p} okunamadı."
        hit = False
        for n, ln in enumerate(text.splitlines(), 1):
            fm = image_vuln._FROM_RE.match(ln)
            if not fm:
                continue
            tok = fm.group(1)
            if tok == old:
                hit = True
                site = {"where": "devops", "repo": region, "project": proj, "branch": branch, "path": p, "line": n,
                        "old": old, "new": new, "blocked": ""}
                try:
                    upgrade_bump.apply_site(text, site)       # kuru çalıştırma: satırda eski metin tek olmalı
                except ValueError as e:
                    return [], f"{p}:{n}: {_why(e)}"
                sites.append(site)
            elif "@" in tok and tok.split("@", 1)[0] == old:
                return [], f"{p}:{n}: özetle sabitlenmiş imaj yeniden yazılmaz."
        if not hit:
            return [], f"{p}: '{old}' FROM satırı bulunamadı."
    return sites, ""


def _bad_reason(units: list[dict]) -> str:
    return "; ".join(f"{u['repo']}: {u['reason']}" for u in units if u["state"] in ("elle", "belirsiz") and u["reason"])


def _agg_plan(units: list[dict]) -> str:
    """Bir bölge bile belirsiz / elle ise bütün eylem kapalı; yoksa açılabilir bölge varsa `yok`, sonra açık / terk."""
    states = [u["state"] for u in units]
    if not states:
        return "elle"
    if "elle" in states:
        return "elle"
    if "belirsiz" in states:
        return "belirsiz"
    for s in ("yok", "pr açık", "pr reddedildi"):
        if s in states:
            return s
    return states[0]


def _repo_current(client, repo: str, lang: str, index: dict) -> str | None:
    """Reponun GERÇEK şimdiki sürümü — `upgrade_track.start` → `upgrade.plan`'ın okuduğu kaynakla aynı
    (`modern.target`: yerel klon + FLODevOps index'i; tarama yok). Okunamazsa None."""
    from pathlib import Path
    from agile_sdlc_crew.quality import modern, php_modern as pm
    deps = pm.default_deps()
    return modern.target(client, repo, Path(pm._root(deps.cfg(), repo, deps)), lang, index=index).get("version")


def _plan_upgrade(client, entry: dict, index: dict, find_open_fn, find_any_fn=None, auto: bool = False,
                  current_fn=None) -> dict:
    """Desteği biten imajı kullanan her repo için sürüm yükseltme izlemesi (varsayılan hedefle). Hedef, imajın
    üretimdeki minörünün üstünde olmalıdır (`default_target`); yükseltme özelliği kapalıyken hiç planlanmaz."""
    from agile_sdlc_crew.quality import intake, upgrade, upgrade_track
    kind, lang = "yükseltme", entry.get("lang") or ""
    if lang not in _tracked():
        label = "Node" if lang == "node" else (lang or "Bu dilin")
        return _act(kind, "elle", f"{label} sürümü izlenmiyor — elle")
    off = next((k for k in ("CREW_TECHDEBT_UPGRADE", "CREW_TECHDEBT_PHP_MODERN") if not _on(k)), None)
    if off:
        return _act(kind, "elle", f"sürüm yükseltme kapalı ({off})")
    find_open = find_open_fn or upgrade_track.find_open
    find_any = find_any_fn or upgrade_track.find_stopped
    current_fn = current_fn or _repo_current
    ver = str(entry.get("version") or "")
    parts = ver.split(".")
    minor = ".".join(parts[:2]) if len(parts) >= 2 else ""
    repos = sorted({r[0] for r in _users(index, entry)})
    units = []
    for repo in repos:
        u = {"repo": repo, "to": None, "state": "hedef yok", "reason": "", "wi": None, "url": ""}
        units.append(u)
        to = upgrade.default_target(lang, minor, index) if minor else None
        if not to:
            u["reason"] = f"{lang} {minor or '?'} için yayınlanmış daha yeni bir hedef sürüm yok."
            continue
        u["to"] = to
        try:
            tr = find_open(client, lang, repo)
        except Exception as e:  # noqa: BLE001 — izleme durumu okunamazsa açılmaz
            u["state"], u["reason"] = "belirsiz", f"izleme durumu okunamadı: {_why(e)}"
            continue
        if tr:
            u["state"], u["wi"] = "izleniyor", tr.get("id")
            try:
                u["url"] = intake._wi_url(client, int(tr["id"]))
            except Exception:  # noqa: BLE001
                u["url"] = ""
        else:
            if auto:
                try:
                    old = find_any(client, lang, repo, to)
                except Exception as e:  # noqa: BLE001 — geçmiş okunamazsa yoklayıcı açmaz
                    u["state"], u["reason"] = "belirsiz", f"izleme geçmişi okunamadı: {_why(e)}"
                    continue
                if old:
                    u.update(state="durduruldu", wi=old.get("id"),
                             reason="izleme daha önce kapatılmış — yeniden başlatma karttan")
                    continue
            try:
                cur = current_fn(client, repo, lang, index)
            except Exception:  # noqa: BLE001 — okunamazsa `start` kendi okumasında aynı koşulu yakalar
                cur = None
            if cur and upgrade._v(cur) >= upgrade._v(to):
                u.update(state="güncel", reason="repo zaten hedef sürümde")
                continue
            u["state"] = "yok"
    states = [u["state"] for u in units]
    state = next((s for s in ("yok", "belirsiz", "izleniyor", "durduruldu", "güncel") if s in states), "hedef yok")
    return _act(kind, state, "" if units else "imajı kullanan repo taze index'te bulunamadı", repos=units,
                cap=_max_open_default(), target=", ".join(sorted({u["to"] for u in units if u["to"]})) or None)


# ── Uygulama ─────────────────────────────────────────────────────────────

def _max_open_default() -> int:
    from agile_sdlc_crew import pipeline_config as pc
    try:
        v = pc.get("CREW_TECHDEBT_UPGRADE_MAX_OPEN")
    except KeyError:
        return 3
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 3


def _from_push(unit: dict, res: dict) -> None:
    """`wrote`: bu çağrıda GERÇEKTEN bir şey yazıldı mı (yeni PR ya da dal / push). Yeniden kullanılan PR, yetkisiz
    (fallback) ve yazmadan biten hatalar yazma sayılmaz."""
    unit["wrote"] = False
    if res.get("prs"):
        p = res["prs"][0]
        pr = {"id": p.get("id"), "url": p.get("url", "")}
        if p.get("reused"):
            unit.update(state="pr açık", pr=pr, reason="")
        else:
            unit.update(state="açıldı", pr=pr, reason="")
            unit["wrote"] = True
    elif res.get("fallback"):
        unit.update(state="elle", reason="yazma yetkisi yok (401/403) — PR açılamadı, değişiklik elle yapılmalı")
        unit["wrote"] = bool(res["fallback"][0].get("branch_written"))
    else:
        err = (res.get("errors") or [{}])[0]
        unit.update(state="hata", reason=_why(Exception(err.get("error") or "PR açılamadı (yanıt boş)")))
        unit["wrote"] = bool(err.get("branch_written"))


def _push(client, unit: dict, sites: list[dict], *, push_fn, branch: str, title: str, body: str) -> None:
    try:
        _from_push(unit, push_fn(client, sites, branch=branch, title=title, body=body, wi_id=None))
    except Exception as e:  # noqa: BLE001 — bir bölgenin hatası ötekini durdurmaz
        log.warning(f"⚠️ image_fix: {unit.get('repo', 'base-images')} yazılamadı: {_why(e)}")
        unit.update(state="hata", reason=_why(e), wrote=False)        # push_sites'ın ön doğrulaması: hiçbir şey yazılmadı


def _agg_run(units: list[dict], prior: str) -> str:
    states = {u["state"] for u in units}
    for s in _RANK:
        if s in states:
            return s
    if "elle" in states:
        return "elle"
    if prior == "yok" and "yok" not in states:          # hiçbir birim yazılmadı (ör. hepsi güncel)
        return next((s for s in ("belirsiz", "izleniyor", "durduruldu", "güncel") if s in states), prior)
    return prior


def _body_own(entry: dict, a: dict, cfg: dict) -> str:
    site = a["sites"][0]
    flags = _flags(entry, cfg)
    out = [f"Tempo, `{_clean(entry['image'])}` taban imajının yama sürümü geride kaldığı için bu PR'ı açtı "
           "(Taban imaj güvenliği uyarısı).", "",
           f"Tetikleyen işaret: yama geride — `{_clean(a['dir'])}` dizini {site['old']}, aynı minörün son yaması "
           f"{site['new']}.{_scan_text(entry)}", "",
           f"Değişiklik: `{_clean(site['path'])}` dosyasında tek satır ({site['old']} → {site['new']}); "
           "başka dosya değişmez.", "",
           "Tempo imajı üretmez / yayınlamaz: birleştirdikten sonra DevOps imajı üretip yayınlamalı. Bu imajı kullanan "
           "bölgeler ve Jenkins bayrakları:"]
    out += [f"- {_clean(f)}" for f in flags] or ["- (şu an bu imajı kullanan servis yok; kullanılmaya başlamadan güncel tutuluyor)"]
    if any(f.startswith("russia") for f in flags):
        out += ["", "RU registry'sine itilmezse russia servisleri geçemez."]
    return "\n".join(out)[:BODY_MAX]


UPGRADE_NOTE = ("Bu imajı kullanan repolar için sürüm yükseltme izlemesi de var; yükseltme PR'ı aynı satırı değiştirir — "
                "bu PR önce birleştirilmeli ya da kapatılmalı.")


def _body_public(entry: dict, a: dict, upgrade_note: bool = False) -> str:
    old, new = entry["image"], a["target"]
    out = [f"Tempo, `{_clean(old)}` imajının yama sürümü geride kaldığı için bu PR'ı açtı (Taban imaj güvenliği "
           "uyarısı).", "",
           f"Tetikleyen işaret: yama geride — `{_clean(old)}` → `{_clean(new)}`. Üç parçalı etiket donmuştur: imaj "
           f"yeniden çekilse de yeni yamayı getirmez.{_scan_text(entry)}", "",
           "Bu giderme şu bölge repolarını kapsar (her biri için ayrı PR):"]
    for u in a["repos"]:
        paths = ", ".join(f"`{_clean(s['path'])}`" for s in u.get("sites") or [])
        out.append(f"- **{_clean(u['repo'])}**" + (f": {paths}" if paths else ""))
    if upgrade_note:
        out += ["", UPGRADE_NOTE]
    out += ["", "Derleme / test bu PR'da doğrulanmadı: birleştirmeden önce hattın sonucuna bakın."]
    return "\n".join(out)[:BODY_MAX]


def apply(client, entry: dict, *, team: str, index: dict, resolver, deps=None, push_fn=None, open_pr_fn=None,
          start_fn=None, max_open: int | None = None, cfg=None, find_open_fn=None, find_any_fn=None,
          budget: list | None = None, auto: bool = False, current_fn=None,
          repos: list[str] | None = None) -> list[dict]:
    """Modül kilidi altında `plan` yeniden hesaplanır; yalnız `yok` durumundaki eylemler yürür. Bir eylemin (ya da
    bölgenin) hatası ötekilerini durdurmaz. Her eylem `ran` alanıyla döner (bu çağrıda yazma denendi mi).
    `budget`: izleme başlatma sayacı `[kalan]` (yoklayıcı turu tek sayacı paylaşır); yalnız BAŞARILI yeni başlatma
    tüketir. `auto`: yoklayıcı — durdurulan izleme yeniden açılmaz.
    `repos`: karttaki repo seçimi — verilirse sürüm yükseltme izlemesi YALNIZ bu repolar için açılır, ötekiler `yok`
    kalır ("seçilmedi"; sonra ayrıca başlatılabilir). `None` = hepsi (yoklayıcı ve eski davranış). Seçim yalnız taze
    plandaki repo adlarıyla eşleştirilir; planda olmayan ad `SelectionError` (yazma yok). Paylaşılan dosyaya PR açan
    eylemler (base-images, bölge repoları) seçimden etkilenmez: onlar imaj düzeyindedir ve bölgeler birlikte yürür."""
    from agile_sdlc_crew.quality import upgrade_bump, upgrade_image, upgrade_track
    cfg = cfg or upgrade_image.load_cfg()
    push_fn = push_fn or upgrade_bump.push_sites
    open_pr_fn = open_pr_fn or upgrade_image.open_pr
    start_fn = start_fn or upgrade_track.start
    if budget is None:
        budget = [_max_open_default() if max_open is None else max(0, int(max_open))]
    with _LOCK:
        acts = plan(client, entry, index=index, resolver=resolver, cfg=cfg, find_open_fn=find_open_fn,
                    find_any_fn=find_any_fn, auto=auto, current_fn=current_fn)
        pick = None if repos is None else {str(r) for r in repos}
        if pick is not None:
            known = {u["repo"] for a in acts if a["kind"] == "yükseltme" for u in a.get("repos") or []}
            unknown = sorted(pick - known)
            if unknown:
                raise SelectionError("Seçilen repo bu imajın giderme planında yok: " + ", ".join(unknown)[:200])
        # Yama PR'ı ile aynı imajın yükseltme izlemesi birlikte planlıysa PR gövdesi çakışmayı söyler (I3).
        upgrade_note = any(u["state"] == "izleniyor" or (u["state"] == "yok" and (pick is None or u["repo"] in pick))
                           for a in acts if a["kind"] == "yükseltme" for u in a.get("repos") or [])
        out = []
        for a in acts:
            if a["state"] != "yok":
                out.append(a)
                continue
            try:        # `ran` = bu çağrıda bir şey YAZILDI; her _run_* kendisi ayarlar (red / hata yazma sayılmaz)
                if a["kind"] == "taban-yama":
                    _run_own(client, entry, a, cfg, push_fn)
                elif a["kind"] == "taban-dizin":
                    _run_dir(client, a, cfg, resolver, open_pr_fn)
                elif a["kind"] == "imaj-yama":
                    _run_public(client, entry, a, push_fn, upgrade_note)
                elif a["kind"] == "taban-taşıma":
                    _run_promote(client, entry, a, cfg)
                elif a["kind"] == "taban-etiket":
                    _run_tag(client, entry, a, push_fn)
                elif a["kind"] == "yükseltme":
                    _run_upgrade(client, entry, a, team, deps, start_fn, budget, index, pick)
            except Exception as e:  # noqa: BLE001
                log.warning(f"⚠️ image_fix: {a['kind']} başarısız: {_why(e)}")
                a.update(state="hata", reason=_why(e))
            out.append(a)
    return out


def _run_own(client, entry: dict, a: dict, cfg: dict, push_fn) -> None:
    site = a["sites"][0]
    unit: dict = {}
    _push(client, unit, a["sites"], push_fn=push_fn, branch=a["branch"],
          title=f"Taban imaj yaması: {a['dir']} {site['old']} → {site['new']}", body=_body_own(entry, a, cfg))
    a.update(state=unit["state"], pr=unit.get("pr"), reason=unit.get("reason", ""), ran=bool(unit.get("wrote")))


def _run_dir(client, a: dict, cfg: dict, resolver, open_pr_fn) -> None:
    from agile_sdlc_crew.quality.upgrade_image import ImageTemplateError
    try:
        r = open_pr_fn(client, a["lang"], a["to"], a["entry"], cfg=cfg, resolver=resolver, repo="")
    except ImageTemplateError as e:
        # Sıra yalnız bu denemede gerçekten yazıldıysa harcanır: hatanın `wrote` alanı (metin aranmaz). Dal zaten
        # hazırken yalnız PR açılamıyorsa `wrote` False'tur — aynı imaj her gece sıra tüketmez.
        a.update(state="hata", reason=_why(e), ran=bool(getattr(e, "wrote", False)))
        return
    pr = r.get("pr")
    opened = r.get("state") == "pr açık" and bool(pr)
    a.update(state="açıldı" if opened else str(r.get("state") or "belirsiz"), pr=pr, reason=str(r.get("reason") or ""),
             ran=opened)


def _run_public(client, entry: dict, a: dict, push_fn, upgrade_note: bool = False) -> None:
    body = _body_public(entry, a, upgrade_note)
    title = f"Taban imaj yaması: {entry['image']} → {a['target']}"
    for u in a["repos"]:
        if u["state"] == "yok":
            _push(client, u, u["sites"], push_fn=push_fn, branch=a["branch"], title=title, body=body)
    a["ran"] = any(u.get("wrote") for u in a["repos"])
    a["state"] = _agg_run(a["repos"], a["state"])
    a["reason"] = "; ".join(f"{u['repo']}: {u['reason']}" for u in a["repos"] if u["state"] in ("hata", "elle") and u["reason"])


def _run_upgrade(client, entry: dict, a: dict, team: str, deps, start_fn, budget: list, index: dict | None = None,
                 pick: set | None = None) -> None:
    lang = entry.get("lang")
    for u in a["repos"]:
        if u["state"] != "yok":
            continue
        if pick is not None and u["repo"] not in pick:      # kartta seçilmedi: açılmaz, sınırı da tüketmez
            u["reason"] = "seçilmedi"
            continue
        if not (team or "").strip():
            u.update(state="hata", reason="takım bilinmiyor — izleme açılamadı")
            continue
        if budget[0] <= 0:
            u.update(state="sırada", reason="CREW_TECHDEBT_UPGRADE_MAX_OPEN sınırı — sonraki turda açılır")
            continue
        try:
            # Aynı taze index start'a da gider: izleme Task gövdesi (sürüm yerleri) yazma kararıyla aynı veriden kurulur.
            r = start_fn(client, u["repo"], lang, to=u["to"], team=team, deps=deps, index=index)
            created = bool(r.get("created", True))
            if created:
                budget[0] -= 1                  # yalnız başarılı YENİ başlatma sınırı tüketir
            u.update(state="açıldı" if created else "izleniyor", wi=r.get("id"), reason="", wrote=created)
            try:
                from agile_sdlc_crew.quality import intake
                u["url"] = intake._wi_url(client, int(r["id"]))
            except Exception:  # noqa: BLE001
                u["url"] = ""
        except ValueError as e:          # start'ın yazmadan önceki reddi (yerel klon yok, sürüm bilinmiyor, alan…): yazma yok
            u.update(state="hata", reason=_why(e), wrote=False)
        except Exception as e:  # noqa: BLE001
            u.update(state="hata", reason=_why(e), wrote=False)
    a["ran"] = any(u.get("wrote") for u in a["repos"])           # tur sınırı / yorum yalnız gerçekten YAZILAN için
    a["state"] = _agg_run(a["repos"], a["state"])
    a["reason"] = "; ".join(f"{u['repo']}: {u['reason']}" for u in a["repos"] if u["state"] in ("hata", "sırada") and u["reason"])


# ── Taşıma (base-images dalları arası) ve bölge repolarında etiket ───────

_FLOW_BR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,60}", re.ASCII)
_TAG_DIR = re.compile(r"[a-z0-9][a-z0-9.-]*", re.ASCII)      # dal adına girebilen dizin adı (`_` yok)


def _flow(cfg: dict) -> list[str]:
    """`config/upgrade_image.yaml` `flow`: taşıma sırası. Geçersizse boş (yazılmaz)."""
    flow = cfg.get("flow")
    if (not isinstance(flow, list) or len(flow) < 2 or len(set(map(str, flow))) != len(flow)
            or not all(isinstance(b, str) and _FLOW_BR.fullmatch(b) and ".." not in b for b in flow)
            or flow[0] != cfg.get("branch")):
        return []
    return flow


def _norm(text) -> str | None:
    """Karşılaştırma için: satır sonu / sondaki boşluk farkı içerik farkı sayılmaz."""
    return None if text is None else "\n".join(ln.rstrip() for ln in str(text).replace("\r\n", "\n").split("\n")).strip()


def _dir_snap(client, repo: str, proj, d: str, branch: str) -> tuple:
    """Dizinin o daldaki içeriği: (Dockerfile, tag.txt), normalize. İkisi de yoksa (None, None)."""
    df = _read(client, repo, f"/{d}/Dockerfile", branch, proj)
    if df is None:
        df = _read(client, repo, f"/{d}/dockerfile", branch, proj)
    return _norm(df), _norm(_read(client, repo, f"/{d}/tag.txt", branch, proj))


def _dir_snap_at(client, repo: str, proj, d: str, commit: str) -> tuple:
    """Aynı içerik, dal adından değil sabit commit'ten (eski bir PR'ın taşıdığı içerik)."""
    def get(name: str):
        raw = client.get_file_bytes(repo, f"/{d}/{name}", project=proj, version=commit, version_type="commit")
        return None if raw is None else raw.decode("utf-8", "replace")
    df = get("Dockerfile")
    if df is None:
        df = get("dockerfile")
    return _norm(df), _norm(get("tag.txt"))


_SNAP_VER = re.compile(r"^(?:ARG PHP_VERSION=|FROM\s+python:)(\d+\.\d+\.\d+)", re.M | re.ASCII)


def _snap_rank(snap: tuple):
    """Dizin içeriğinin sırası: (sürüm, etiket). Okunamayan sürüm / etiket → None (yön bilinmez, yazılmaz)."""
    df, tag = snap
    m = _SNAP_VER.search(df or "")
    if not m or not image_vuln._TAGV_RE.fullmatch(tag or ""):
        return None
    return _ver(m.group(1)), tuple(int(x) for x in tag.split("."))


def _plan_promote(client, entry: dict, d: str, cfg: dict) -> dict | None:
    """Düzeltme ilk dalda hazır ama sonraki dallara taşınmamışsa: içeriği farklı olan İLK komşu dal çiftine taslak
    taşıma PR'ı (ör. staging → preprod, sonra preprod → production). Hepsi aynıysa None (eylem yok). Dal oluşturulmaz,
    push edilmez: yalnız var olan iki dal arasında PR açılır."""
    kind = "taban-taşıma"
    repo, proj = cfg["repo"], cfg.get("project") or None
    flow = _flow(cfg)
    if not flow:
        return _act(kind, "elle", "config/upgrade_image.yaml `flow` geçersiz (ilk dal `branch` olmalı, en az iki dal).", dir=d)
    if not _TAG_DIR.fullmatch(d or "") or ".." in d:
        return _act(kind, "elle", f"'{_clean(d)[:60]}' dizin adı beklenen biçimde değil; taşınmaz.")
    snaps = [_dir_snap(client, repo, proj, d, b) for b in flow]
    if snaps[0][0] is None:
        return _act(kind, "elle", f"{repo}@{flow[0]}'de {d}/Dockerfile yok.", dir=d)
    i = next((i for i in range(len(flow) - 1) if snaps[i] != snaps[i + 1]), None)
    if i is None:
        return None
    src, dst = flow[i], flow[i + 1]
    # Yön: hedef dal düzeltmeden GERİDE olmalı (dizin hiç yok ya da sürüm / etiket daha küçük). Hedef ileride ya da
    # ayrışmışsa (kendi düzeltmesi var) taşıma dalın tamamını geri yazar — önerilmez.
    if snaps[i + 1] != (None, None):
        a_rank, b_rank = _snap_rank(snaps[i]), _snap_rank(snaps[i + 1])
        if a_rank is None or b_rank is None or not (a_rank > b_rank):
            return _act(kind, "elle", f"'{dst}' dalı '{src}' dalından geride değil (ileride ya da ayrışmış); taşıma "
                                      "elle değerlendirilmeli.", dir=d, src=src, dst=dst, target=dst)
    refs = {str(b.get("name") or ""): b.get("objectId") for b in client.list_branches(repo, project=proj)}
    src_oid, dst_oid = refs.get(f"refs/heads/{src}"), refs.get(f"refs/heads/{dst}")
    base = dict(dir=d, src=src, dst=dst, target=dst, src_oid=src_oid or "", dst_oid=dst_oid or "")
    if not src_oid or not dst_oid:
        return _act(kind, "elle", f"{repo}: '{src if not src_oid else dst}' dalı bulunamadı.", **base)
    pr = client.find_pull_request_between(repo, src, dst, project=proj)
    if pr and pr.get("status") == "active":
        return _act(kind, "pr açık", "", pr=_pr_view(pr), **base)
    if pr and pr.get("status") in ("abandoned", "completed"):
        # Önceki PR bu dizinin AYNI içeriğini mi taşıyordu? Dal başı değil dizin içeriği karşılaştırılır: paylaşılan
        # dala başka bir commit gelmesi reddedilen / tamamlanan öneriyi yeniden açtırmaz. İçerik okunamazsa belirsiz.
        old_commit = str(pr.get("source_commit") or "")
        if not re.fullmatch(r"[0-9a-f]{7,64}", old_commit):
            return _act(kind, "belirsiz", f"önceki PR #{pr.get('id')} hangi içeriği taşıdığı okunamadı", pr=_pr_view(pr), **base)
        if _dir_snap_at(client, repo, proj, d, old_commit) == snaps[i]:
            if pr["status"] == "abandoned":
                return _act(kind, "pr reddedildi", f"aynı içerikle açılan PR #{pr.get('id')} reddedilmiş",
                            pr=_pr_view(pr), **base)
            return _act(kind, "pr kapandı", f"aynı içerik PR #{pr.get('id')} ile taşınmış ama '{dst}' hâlâ farklı — "
                                              "elle bakılmalı", pr=_pr_view(pr), **base)
    return _act(kind, "yok", "", **base)


def _body_promote(entry: dict, a: dict, cfg: dict, paths) -> str:
    flags = _flags(entry, cfg)
    out = [f"Tempo, `{_clean(entry['image'])}` taban imajının düzeltmesi `{a['src']}` dalında hazır olduğu için bu taşıma "
           "PR'ını açtı (Taban imaj güvenliği uyarısı).", "",
           f"Tetikleyen dizin: `{_clean(a['dir'])}` — `{a['dst']}` dalında eski içerik duruyor.{_scan_text(entry)}", "",
           f"DİKKAT: bu PR `{a['src']}` dalının TAMAMINI `{a['dst']}` dalına taşır, yalnız bu dizini değil. "
           "Birleştirmeden önce bütün farkı inceleyin."]
    if paths is None:
        out += ["", "Değişen dosyalar okunamadı; PR'ın dosya sekmesine bakın."]
    else:
        out += ["", f"Değişen dosyalar ({len(paths)}):"] + [f"- `{_clean(p)[:120]}`" for p in paths[:30]]
        if len(paths) > 30:
            out.append(f"- … ve {len(paths) - 30} dosya daha")
    out += ["", f"Tempo imajı üretmez / yayınlamaz: birleştirdikten sonra DevOps imajı `{a['dst']}` ortamı için üretip "
                "yayınlamalı. Bu imajı kullanan bölgeler ve Jenkins bayrakları:"]
    out += [f"- {_clean(f)}" for f in flags] or ["- (şu an bu imajı kullanan servis yok; kullanılmaya başlamadan güncel tutuluyor)"]
    if any(f.startswith("russia") for f in flags):
        out += ["", "RU registry'sine itilmezse russia servisleri geçemez."]
    return "\n".join(out)[:BODY_MAX]


def _run_promote(client, entry: dict, a: dict, cfg: dict) -> None:
    repo, proj = cfg["repo"], cfg.get("project") or None
    try:
        paths = client.get_commit_diff_paths(repo, a["dst_oid"], a["src_oid"])
    except Exception:  # noqa: BLE001 — liste yalnız bilgi; okunamazsa PR yine açılır, gövde bunu söyler
        paths = None
    title = f"Taban imaj taşıma: {a['src']} → {a['dst']} ({_clean(a['dir'])})"
    try:
        res = client.create_pull_request(repo, a["src"], a["dst"], title=title,
                                         description=mask_secrets(_body_promote(entry, a, cfg, paths)),
                                         draft=True, project=proj)
    except Exception as e:  # noqa: BLE001
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code in (401, 403):
            a.update(state="elle", reason="yazma yetkisi yok (401/403) — taşıma PR'ı elle açılmalı", ran=False)
        else:
            a.update(state="hata", reason=_why(e), ran=False)
        return
    pid = res.get("pullRequestId") if isinstance(res, dict) else None
    if not pid:
        a.update(state="belirsiz", reason="PR büyük olasılıkla açıldı ama Azure yanıtı okunamadı; Azure DevOps'ta kontrol edin",
                 ran=True)
        return
    base = getattr(client, "org_url", None)
    proj_name = ((res.get("repository") or {}).get("project") or {}).get("name") or cfg.get("project")
    url = f"{base}/{proj_name}/_git/{repo}/pullrequest/{pid}" if base and proj_name else ""
    a.update(state="açıldı", pr={"id": pid, "url": _https(url)}, reason="", ran=True)


def _plan_tag(client, entry: dict, d: str, index: dict, cfg: dict) -> dict | None:
    """Kendi taban imajımızın etiketi üretim dalında yükselmiş (`tag.txt`) ama bölge repoları eskisini çekiyor: kullanan
    her bölge reposunda `FROM …/<dizin>:<eski>` → `:<yeni>`, bölge başına taslak PR. Etiket üretim dalından TAZE okunur;
    bir bölge bile belirsizse hiçbiri yazılmaz. Geride servis yoksa None."""
    from agile_sdlc_crew.quality import upgrade_bump
    kind = "taban-etiket"
    repo, proj = cfg["repo"], cfg.get("project") or None
    if not _TAG_DIR.fullmatch(d or "") or ".." in d:
        return _act(kind, "elle", f"'{_clean(d)[:60]}' dizin adı dal adına çevrilemiyor; yazılmaz.")
    new = str(_read(client, repo, f"/{d}/tag.txt", image_vuln.PROD_BRANCH, proj) or "").strip()
    if not image_vuln._TAGV_RE.fullmatch(new):
        return _act(kind, "elle", f"{repo}@{image_vuln.PROD_BRANCH} {d}/tag.txt okunamadı ya da sayısal değil.", dir=d)
    lag: dict[str, list[tuple[str, str]]] = {}       # bölge → [(yol, ham FROM)]
    for _repo, region, path, raw in _users(index, {"image": entry["image"], "via": [d]}):
        m = image_vuln._OWN_RE.match(raw)
        if m and m.group(1) == d and image_vuln.tag_newer(m.group(2), new) and (path, raw) not in lag.setdefault(region, []):
            lag[region].append((path, raw))
    lag = {r: v for r, v in lag.items() if v}
    if not lag:
        return None
    base = dict(dir=d, target=f"{d}:{new}", tag=new)
    dproj, dbranch = app_map.DEVOPS_PROJECT, app_map.DEVOPS_BRANCH
    units = []
    for region in sorted(lag):
        u = {"repo": region, "state": "elle", "reason": "", "sites": [], "pr": None}
        units.append(u)
        if region not in app_map.DEVOPS_REPOS:
            u["reason"] = f"'{_clean(region)[:40]}' bir bölge reposu değil; yazılmaz."
            continue
        try:
            sites: list[dict] = []
            why = ""
            for path, raw in sorted(lag[region]):
                got, why = _region_sites(client, region, [path], raw, raw[:raw.rindex(":") + 1] + new, dproj, dbranch)
                if why:
                    break
                sites += got
            if why:
                u["reason"] = why
                continue
            if not sites or not all(upgrade_bump.pushable(s) for s in sites):
                u["reason"] = "FROM satırı yalnız rakam değişimiyle yazılamıyor."
                continue
            u["sites"] = sites
            # Dal adı yazılacak satır kümesini taşır: tamamlanan bir PR'dan sonra eski etikette yeni bir servis
            # çıkarsa yeni dal → yeni PR (kapanmış PR onu engellemez); açık PR varken küme değişirse önce o kapanmalı.
            key = hashlib.sha1("\n".join(sorted(f"{s['path']}:{s['line']}:{s['old']}" for s in sites)).encode()).hexdigest()[:8]
            u["branch"] = f"tempo/image-tag-{d}-{new}-{key}"
            bad = _branch_ok(u["branch"], dbranch)
            if bad:
                u["reason"] = bad
                continue
            u["state"], u["pr"], u["reason"] = _pr_or_older(client, region, dproj, u["branch"], f"tempo/image-tag-{d}-",
                                                            tail=r"\d+(?:\.\d+){0,3}-[0-9a-f]{8}")
        except Exception as e:  # noqa: BLE001
            u["state"], u["reason"] = "belirsiz", f"okunamadı: {_why(e)}"
    return _act(kind, _agg_plan(units), _bad_reason(units), repos=units, **base)


def _body_tag(entry: dict, a: dict) -> str:
    out = [f"Tempo, `{_clean(a['dir'])}` taban imajının yeni etiketi (`{a['tag']}`) base-images "
           f"`{image_vuln.PROD_BRANCH}` dalında olduğu için bu PR'ı açtı (Taban imaj güvenliği uyarısı).", "",
           f"Değişiklik: `FROM …/{_clean(a['dir'])}:<eski>` satırlarında etiket `{a['tag']}` olur; başka satır değişmez."
           f"{_scan_text(entry)}", "",
           f"ÖNEMLİ: `{_clean(a['dir'])}:{a['tag']}` imajı bu bölgenin kayıt deposuna basılmadan birleştirmeyin — derleme "
           "imajı bulamaz. Tempo kayıt deposunu göremez; imajın varlığını DevOps doğrulamalı.", "",
           "Bu giderme şu bölge repolarını kapsar (her biri için ayrı PR):"]
    for u in a["repos"]:
        paths = ", ".join(f"`{_clean(s['path'])}`" for s in u.get("sites") or [])
        out.append(f"- **{_clean(u['repo'])}**" + (f": {paths}" if paths else ""))
    if any(u["repo"] == "russia" for u in a["repos"]):
        out += ["", "RU (VK) kayıt deposu ayrıdır: imaj oraya da basılmış olmalı."]
    out += ["", "Bu repoda aynı `FROM` satırını değiştiren başka bir Tempo PR'ı (sürüm yükseltme) açıksa ikisi çakışır: "
                "biri önce birleştirilmeli ya da kapatılmalı.",
            "", "Derleme / test bu PR'da doğrulanmadı: birleştirmeden önce hattın sonucuna bakın."]
    return "\n".join(out)[:BODY_MAX]


def _run_tag(client, entry: dict, a: dict, push_fn) -> None:
    body = _body_tag(entry, a)
    title = f"Taban imaj etiketi: {a['dir']} → {a['tag']}"
    for u in a["repos"]:
        if u["state"] == "yok":
            _push(client, u, u["sites"], push_fn=push_fn, branch=u["branch"], title=title, body=body)
    a["ran"] = any(u.get("wrote") for u in a["repos"])
    a["state"] = _agg_run(a["repos"], a["state"])
    a["reason"] = "; ".join(f"{u['repo']}: {u['reason']}" for u in a["repos"] if u["state"] in ("hata", "elle") and u["reason"])


# ── Çıktı ────────────────────────────────────────────────────────────────

def public(acts: list[dict]) -> list[dict]:
    """Dışarı (kart / uç) çıkan görünüm: iç alanlar (`entry`) atılır."""
    return [{k: v for k, v in a.items() if k != "entry"} for a in acts]


def comment_html(acts: list[dict]) -> str:
    """Uyarı işine düşen yorum: eylem, durum, PR / izleme bağlantıları (yalnız https). Her metin kaçışlıdır."""
    def esc(s, n: int = 300) -> str:
        return html.escape(mask_secrets(str(s if s is not None else ""))[:n])     # önce kes, sonra kaçışla

    def link(url, text) -> str:
        u = _https(url)
        return f'<a href="{esc(u)}">{esc(text)}</a>' if u else esc(text)
    rows = []
    for a in acts:
        bits = [f"<b>{esc(a['kind'])}</b> — {esc(a['state'])}"]
        if a.get("pr"):
            bits.append(link(a["pr"].get("url"), f"PR #{a['pr'].get('id')}"))
        for u in a.get("repos") or []:
            ref = u.get("pr") or {}
            tail = (link(ref.get("url"), f"PR #{ref.get('id')}") if ref else
                    link(u.get("url"), f"izleme #{u.get('wi')}") if u.get("wi") else "")
            bits.append(f"{esc(u['repo'])}: {esc(u['state'])} {tail}".strip())
        if a.get("reason"):
            bits.append(f"<i>{esc(a['reason'])}</i>")
        rows.append("<li>" + " · ".join(bits) + "</li>")
    return "<p><b>Giderme (Tempo):</b> uyarıyla birlikte başlatıldı.</p><ul>" + "".join(rows) + "</ul>"
