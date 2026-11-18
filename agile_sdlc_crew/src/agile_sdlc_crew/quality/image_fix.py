"""Taban imaj güvenlik uyarısının gidermesi (spec: language-upgrade-design.md, "Ek 2026-11-18: taban imaj güvenlik
uyarısı", "Giderme uyarıyla birlikte başlar").

`image_vuln.report` kaydındaki işarete göre, başka ekiplerin repolarına YAZAN bir giderme planlar ve uygular:

- `taban-yama`: kendi iki parçalı taban dizinimiz (`php-8-4`) → `base-images@staging`'de tek sürüm satırı, taslak PR.
- `taban-dizin`: kendi üç parçalı dizinimiz (`python-3-13-7`) → aynı minörün yeni yamasıyla dizin şablonu
  (`upgrade_image.open_pr`).
- `imaj-yama`: genel üç parçalı imaj (`golang:1.24.4`) → kullanan her bölge reposunda `FROM` satırı, bölge başına PR.
- `yükseltme`: desteği biten dil → `upgrade_track.start` ile sürüm yükseltme izlemesi.
- `yeniden-üret`: yalnız işletim sistemi bulgusu → yazma yok, DevOps'tan ister.

Her yazma yolu kapalı kalır: bilinmeyen / belirsiz / elle olan hiçbir şey yazılmaz. Durum saklanmaz; sabit dal adı,
açık / terk edilmiş PR ve açık izleme işi üzerinden her çağrıda yeniden türetilir. Terk edilen PR yeniden açılmaz."""
from __future__ import annotations

import html
import logging
import re
import threading

from agile_sdlc_crew.quality import app_map, image_vuln
from agile_sdlc_crew.quality.render import mask_secrets
from agile_sdlc_crew.quality.upgrade_image import _DIR_RE, _clean, _pr_view

log = logging.getLogger("pipeline")

_LOCK = threading.Lock()
TRACKED_LANGS = ("php", "python", "go")           # Tempo'nun sürüm yükseltmesini izlediği diller
_OWN_NAMES = {"php": "php", "python": "python"}   # taban imaj şablonu olan diller (Go'nun taban imajı yok)
_THREE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_TAG3 = re.compile(r"^(\d+\.\d+\.\d+)(.*)$", re.S)
_ARG_PHP = re.compile(r"^ARG PHP_VERSION=(\d+\.\d+\.\d+)\s*$")
_FROM_PHP = re.compile(r"^FROM\s+php:\$\{PHP_VERSION\}(\S*)\s*$")
_FROM_PY = re.compile(r"^FROM\s+python:(\d+\.\d+\.\d+)(-\S+)?\s*$")
BODY_MAX = 3900                                    # Azure PR açıklaması sınırı 4000
_RANK = ("hata", "sırada", "açıldı")


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


def _guard(kind: str, fn, *a, **kw) -> dict:
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001 — okunamayan durum yazdırmaz
        return _act(kind, "belirsiz", f"okunamadı: {_why(e)}")


# ── Plan ─────────────────────────────────────────────────────────────────

def plan(client, entry: dict, *, index: dict, resolver, cfg=None, find_open_fn=None, find_any_fn=None,
         auto: bool = False) -> list[dict]:
    """Yazma yok. Rapor kaydı (`image_vuln.report` öğesi) → eylem listesi (modül belgesine bakın). `auto` (yoklayıcı):
    daha önce HİÇ izleme açılmış repo (kapatılmışı dahil) `durduruldu` sayılır — kullanıcının durdurduğu izleme
    kendiliğinden yeniden açılmaz; düğme (auto=False) yeniden başlatabilir."""
    from agile_sdlc_crew.quality import upgrade_image
    cfg = cfg or upgrade_image.load_cfg()
    out: list[dict] = []
    patch, eol = entry.get("patch"), entry.get("eol")
    sc = entry.get("scan") or {}
    bump_on = _on("CREW_TECHDEBT_UPGRADE_BUMP")

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
                    out.append(pr_action("taban-yama", _plan_own, client, entry, d, resolver, cfg))
        else:
            out.append(pr_action("imaj-yama", _plan_public, client, entry, index, resolver, cfg))
    if eol:
        out.append(_guard("yükseltme", _plan_upgrade, client, entry, index, find_open_fn, find_any_fn, auto))
    if not patch and not eol and sc.get("state") == "tamam" and (sc.get("critical") or sc.get("high")):
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
    state, pr, why = _pr_state(client, repo, branch_name, proj)
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
    if not raws:
        return _act(kind, "elle", f"'{d}' dizinini kullanan servis taze index'te bulunamadı.", dir=new_dir)
    ent = {"dir": new_dir, "tag": tags[0], "regions": regions, "sites": [{"repo": r[1], "path": "/" + r[2]} for r in raws],
           "error": "" if len(tags) == 1 else f"servisler farklı etiket istiyor: {', '.join(tags)}"}
    st = upgrade_image.state(client, ent, cfg=cfg)
    return _act(kind, st["state"], st["reason"], pr=st["pr"], dir=new_dir, target=new_dir, entry=ent,
                to=f"{m.group(2)}.{m.group(3)}", lang=lang)


def _slug(image: str) -> str:
    """Dal adı gövdesi: `library/golang:1.24.4-alpine` → `golang-1.24.4-alpine` (eski etiket dahil: aynı yamaya giden iki
    ayrı eski etiket aynı dalı paylaşmaz)."""
    name, _, tag = image.partition(":")
    return re.sub(r"[^a-z0-9.]+", "-", f"{name.rsplit('/', 1)[-1]}-{tag}".lower()).strip("-.")


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
            u["state"], u["pr"], u["reason"] = _pr_state(client, region, branch, proj)
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


def _plan_upgrade(client, entry: dict, index: dict, find_open_fn, find_any_fn=None, auto: bool = False) -> dict:
    """Desteği biten imajı kullanan her repo için sürüm yükseltme izlemesi (varsayılan hedefle). Hedef, imajın
    üretimdeki minörünün üstünde olmalıdır (`default_target`); yükseltme özelliği kapalıyken hiç planlanmaz."""
    from agile_sdlc_crew.quality import intake, upgrade, upgrade_track
    kind, lang = "yükseltme", entry.get("lang") or ""
    if lang not in TRACKED_LANGS:
        label = "Node" if lang == "node" else (lang or "Bu dilin")
        return _act(kind, "elle", f"{label} sürümü izlenmiyor — elle")
    off = next((k for k in ("CREW_TECHDEBT_UPGRADE", "CREW_TECHDEBT_PHP_MODERN") if not _on(k)), None)
    if off:
        return _act(kind, "elle", f"sürüm yükseltme kapalı ({off})")
    find_open = find_open_fn or upgrade_track.find_open
    find_any = find_any_fn or upgrade_track.find_any
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
                    old = find_any(client, lang, repo)
                except Exception as e:  # noqa: BLE001 — geçmiş okunamazsa yoklayıcı açmaz
                    u["state"], u["reason"] = "belirsiz", f"izleme geçmişi okunamadı: {_why(e)}"
                    continue
                if old:
                    u.update(state="durduruldu", wi=old.get("id"),
                             reason="izleme daha önce kapatılmış — yeniden başlatma karttan")
                    continue
            u["state"] = "yok"
    states = [u["state"] for u in units]
    state = next((s for s in ("yok", "belirsiz", "izleniyor", "durduruldu") if s in states), "hedef yok")
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
    if res.get("prs"):
        p = res["prs"][0]
        unit.update(state="açıldı", pr={"id": p.get("id"), "url": p.get("url", "")}, reason="")
    elif res.get("fallback"):
        unit.update(state="elle", reason="yazma yetkisi yok (401/403) — PR açılamadı, değişiklik elle yapılmalı")
    else:
        err = (res.get("errors") or [{}])[0].get("error") or "PR açılamadı (yanıt boş)"
        unit.update(state="hata", reason=_why(Exception(err)))


def _push(client, unit: dict, sites: list[dict], *, push_fn, branch: str, title: str, body: str) -> None:
    try:
        _from_push(unit, push_fn(client, sites, branch=branch, title=title, body=body, wi_id=None))
    except Exception as e:  # noqa: BLE001 — bir bölgenin hatası ötekini durdurmaz
        log.warning(f"⚠️ image_fix: {unit.get('repo', 'base-images')} yazılamadı: {_why(e)}")
        unit.update(state="hata", reason=_why(e))


def _agg_run(units: list[dict], prior: str) -> str:
    states = {u["state"] for u in units}
    for s in _RANK:
        if s in states:
            return s
    return "elle" if "elle" in states else prior


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
    out += [f"- {_clean(f)}" for f in flags]
    if any(f.startswith("russia") for f in flags):
        out += ["", "RU registry'sine itilmezse russia servisleri geçemez."]
    return "\n".join(out)[:BODY_MAX]


def _body_public(entry: dict, a: dict) -> str:
    old, new = entry["image"], a["target"]
    out = [f"Tempo, `{_clean(old)}` imajının yama sürümü geride kaldığı için bu PR'ı açtı (Taban imaj güvenliği "
           "uyarısı).", "",
           f"Tetikleyen işaret: yama geride — `{_clean(old)}` → `{_clean(new)}`. Üç parçalı etiket donmuştur: imaj "
           f"yeniden çekilse de yeni yamayı getirmez.{_scan_text(entry)}", "",
           "Bu giderme şu bölge repolarını kapsar (her biri için ayrı PR):"]
    for u in a["repos"]:
        paths = ", ".join(f"`{_clean(s['path'])}`" for s in u.get("sites") or [])
        out.append(f"- **{_clean(u['repo'])}**" + (f": {paths}" if paths else ""))
    out += ["", "Derleme / test bu PR'da doğrulanmadı: birleştirmeden önce hattın sonucuna bakın."]
    return "\n".join(out)[:BODY_MAX]


def apply(client, entry: dict, *, team: str, index: dict, resolver, deps=None, push_fn=None, open_pr_fn=None,
          start_fn=None, max_open: int | None = None, cfg=None, find_open_fn=None, find_any_fn=None,
          budget: list | None = None, auto: bool = False) -> list[dict]:
    """Modül kilidi altında `plan` yeniden hesaplanır; yalnız `yok` durumundaki eylemler yürür. Bir eylemin (ya da
    bölgenin) hatası ötekilerini durdurmaz. Her eylem `ran` alanıyla döner (bu çağrıda yazma denendi mi).
    `budget`: izleme başlatma sayacı `[kalan]` (yoklayıcı turu tek sayacı paylaşır); yalnız BAŞARILI yeni başlatma
    tüketir. `auto`: yoklayıcı — durdurulan izleme yeniden açılmaz."""
    from agile_sdlc_crew.quality import upgrade_bump, upgrade_image, upgrade_track
    cfg = cfg or upgrade_image.load_cfg()
    push_fn = push_fn or upgrade_bump.push_sites
    open_pr_fn = open_pr_fn or upgrade_image.open_pr
    start_fn = start_fn or upgrade_track.start
    if budget is None:
        budget = [_max_open_default() if max_open is None else max(0, int(max_open))]
    with _LOCK:
        acts = plan(client, entry, index=index, resolver=resolver, cfg=cfg, find_open_fn=find_open_fn,
                    find_any_fn=find_any_fn, auto=auto)
        out = []
        for a in acts:
            if a["state"] != "yok":
                out.append(a)
                continue
            a["ran"] = True
            try:
                if a["kind"] == "taban-yama":
                    _run_own(client, entry, a, cfg, push_fn)
                elif a["kind"] == "taban-dizin":
                    _run_dir(client, a, cfg, resolver, open_pr_fn)
                elif a["kind"] == "imaj-yama":
                    _run_public(client, entry, a, push_fn)
                elif a["kind"] == "yükseltme":
                    _run_upgrade(client, entry, a, team, deps, start_fn, budget)
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
    a.update(state=unit["state"], pr=unit.get("pr"), reason=unit.get("reason", ""))


def _run_dir(client, a: dict, cfg: dict, resolver, open_pr_fn) -> None:
    r = open_pr_fn(client, a["lang"], a["to"], a["entry"], cfg=cfg, resolver=resolver, repo="")
    pr = r.get("pr")
    a.update(state="açıldı" if r.get("state") == "pr açık" and pr else str(r.get("state") or "belirsiz"), pr=pr,
             reason=str(r.get("reason") or ""))


def _run_public(client, entry: dict, a: dict, push_fn) -> None:
    body = _body_public(entry, a)
    title = f"Taban imaj yaması: {entry['image']} → {a['target']}"
    for u in a["repos"]:
        if u["state"] == "yok":
            _push(client, u, u["sites"], push_fn=push_fn, branch=a["branch"], title=title, body=body)
    a["state"] = _agg_run(a["repos"], a["state"])
    a["reason"] = "; ".join(f"{u['repo']}: {u['reason']}" for u in a["repos"] if u["state"] in ("hata", "elle") and u["reason"])


def _run_upgrade(client, entry: dict, a: dict, team: str, deps, start_fn, budget: list) -> None:
    lang = entry.get("lang")
    for u in a["repos"]:
        if u["state"] != "yok":
            continue
        if not (team or "").strip():
            u.update(state="hata", reason="takım bilinmiyor — izleme açılamadı")
            continue
        if budget[0] <= 0:
            u.update(state="sırada", reason="CREW_TECHDEBT_UPGRADE_MAX_OPEN sınırı — sonraki turda açılır")
            continue
        try:
            r = start_fn(client, u["repo"], lang, to=u["to"], team=team, deps=deps)
            created = bool(r.get("created", True))
            if created:
                budget[0] -= 1                  # yalnız başarılı YENİ başlatma sınırı tüketir
            u.update(state="açıldı" if created else "izleniyor", wi=r.get("id"), reason="")
            try:
                from agile_sdlc_crew.quality import intake
                u["url"] = intake._wi_url(client, int(r["id"]))
            except Exception:  # noqa: BLE001
                u["url"] = ""
        except Exception as e:  # noqa: BLE001
            u.update(state="hata", reason=_why(e))
    a["state"] = _agg_run(a["repos"], a["state"])
    a["reason"] = "; ".join(f"{u['repo']}: {u['reason']}" for u in a["repos"] if u["state"] in ("hata", "sırada") and u["reason"])


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
