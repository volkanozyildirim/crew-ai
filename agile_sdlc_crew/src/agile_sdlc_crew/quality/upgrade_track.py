"""Sürüm yükseltme durumu, izleme Task'ı ve sıradaki sürüm adımı (spec §5–6).

Durum saklanmaz, türetilir — Azure tek kayıttır: taze tarama (engelleyen bulgu, engel, sürüm
yerleri), board'daki açık yükseltme işleri, sabit dal adındaki (`tempo/upgrade-<dil>-<repo>-<hedef>`)
PR'lar ve CI PR'ının merge'inden SONRA kuyruğa girmiş build. Aktif yükseltmenin kaydı bir izleme
Task'ıdır: takımın alanında, sprint'e bağlı değil, etiket `tempo-upgrade-track`, açıklamada
`tempo-upgrade-key: <dil>:<repo>:<hedef>` satırı. Repo × dil başına en çok bir açık izleme işi.

Ön koşullar burada tek yerde: `upgrade_bump.queue_step` iş açmadan, `upgrade_bump.run` push'tan
önce aynı `status`'u çağırır. Bir adım ancak `next.ready` iken atılır.
"""
from __future__ import annotations

import html
import logging
import re
import threading
from pathlib import Path

from . import php_modern as pm
from . import upgrade

log = logging.getLogger("pipeline")

STAGES = ("hazırlık", "engelli", "hazır", "test hattı", "üretim", "tamam")
TRACK_TAG = "tempo-upgrade-track"
KEY_PREFIX = "tempo-upgrade-key:"
_KEY_RE = re.compile(r"tempo-upgrade-key:\s*([a-z]+):([A-Za-z0-9][\w.-]*):(\d+\.\d+)")
SIGNATURE = "<p><small><i>Tempo — Sürüm yükseltme</i></small></p>"
STEP_LABEL = {"ci": "test hattı", "prod": "üretim", "code": "kod reposu"}
NO_EVIDENCE = "Test kanıtı yok"
WEAK_EVIDENCE = ("Test kanıtı zayıf: test hattında sürüm sabitlemesi bulunamadı; son yeşil build hedef sürümle "
                 "koşmamış olabilir.")
_PLAN_KW = ("index", "root", "read", "yml_files", "app_map_cache")
_SCAN_KW = _PLAN_KW + ("scan_fn", "head_sha_fn", "probe_fn")
_LOCK = threading.Lock()


def _q(s) -> str:
    return str(s).replace("'", "''")


def _is_open(fields: dict) -> bool:
    from agile_sdlc_crew.aging import DONE_STATES
    return (fields.get("System.State") or "").strip().lower() not in DONE_STATES


def _check(repo: str, lang: str) -> None:
    from agile_sdlc_crew.quality import modern
    from agile_sdlc_crew.quality.modern_fix import _REPO
    if lang not in modern.LANGS:
        raise ValueError(f"Desteklenmeyen dil: {lang!r}")
    if not isinstance(repo, str) or not _REPO.match(repo) or ".." in repo:
        raise ValueError(f"Geçersiz repo adı: {repo!r}")


def _pick(kw: dict, keys) -> dict:
    return {k: kw[k] for k in keys if k in kw}


# ── İzleme Task'ı ────────────────────────────────────────────────────────

def _track_rows(client, extra: str = "") -> list[dict]:
    q = ("SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
         f"AND [System.Tags] CONTAINS '{TRACK_TAG}'" + extra)
    out = []
    for wi in client.query_work_items(q) or []:
        f = wi.get("fields") or {}
        tags = {t.strip().lower() for t in str(f.get("System.Tags") or "").split(";")}
        if not _is_open(f) or TRACK_TAG not in tags:     # etiket WIQL'e ek olarak burada da doğrulanır
            continue
        m = _KEY_RE.search(html.unescape(f.get("System.Description") or ""))
        if not m:
            continue
        out.append({"id": int(wi["id"]), "lang": m.group(1), "repo": m.group(2), "to": m.group(3),
                    "title": f.get("System.Title") or "", "area": f.get("System.AreaPath") or ""})
    return out


def find_open(client, lang: str, repo: str) -> dict | None:
    """Repo × dil için açık izleme işi (hedefi ne olursa olsun); proje genelinde aranır —
    iki takım aynı repoyu ayrı ayrı izlemeye başlamasın."""
    rows = _track_rows(client, f" AND [System.Description] CONTAINS '{KEY_PREFIX} {_q(lang)}:{_q(repo)}:'")
    rows = [r for r in rows if r["lang"] == lang and r["repo"].lower() == repo.lower()]
    return min(rows, key=lambda r: r["id"]) if rows else None


def tracked(client, team: str) -> list[dict]:
    """Takımın alanındaki açık izleme işleri: [{id, lang, repo, to}]."""
    area = client.get_team_area_path(team) or ""
    if not area:
        raise ValueError(f"Takımın alanı çözümlenemedi: {team!r}")
    rows = _track_rows(client, f" AND [System.AreaPath] UNDER '{_q(area)}'")
    return [{k: r[k] for k in ("id", "lang", "repo", "to")} for r in rows]


def _root_iteration(client, team: str) -> str:
    """Takımın sprintlerinin ortak üst yolu (kök iteration); bulunamazsa proje kökü."""
    try:
        its = client.list_iterations(team) or []
    except Exception:  # noqa: BLE001
        its = []
    parents = [str(i.get("path") or "").split("\\")[:-1] for i in its if "\\" in str(i.get("path") or "")]
    common: list[str] = []
    if parents:
        for seg in zip(*parents):
            if len(set(seg)) != 1:
                break
            common.append(seg[0])
    return "\\".join(common) or str(getattr(client, "project", "") or "")


def _esc(s) -> str:
    from agile_sdlc_crew.quality.render import mask_secrets
    return html.escape(mask_secrets(str(s)))


def _site_line(s: dict) -> str:
    where = f"{s.get('repo')} {s.get('path')}:{s.get('line')}"
    tail = f" — engelli: {s['blocked']}" if s.get("blocked") else ""
    return f"<li>{_esc(s.get('where'))} · <code>{_esc(where)}</code> {_esc(s.get('old'))} → {_esc(s.get('new'))}{_esc(tail)}</li>"


def _track_body(repo: str, label: str, cur: str, tgt: str, sites: list[dict], blockers: list[dict], key: str) -> str:
    sl = "".join(_site_line(s) for s in sites) or "<li>Sürüm yeri bulunamadı.</li>"
    bl = "".join(f"<li>{_esc(b.get('detail') or b.get('name') or b)}</li>" for b in blockers)
    return (f"<p><b>{_esc(label)} sürüm yükseltme:</b> {_esc(repo)} {_esc(cur)} → {_esc(tgt)}</p>"
            "<p>Bu Task yükseltmenin kaydıdır: aşama değişimleri ve açılan sürüm PR'ları buraya yorum olarak "
            "düşer. Sprint'e bağlı değildir (yükseltme birkaç sprint sürer); kapatmak izlemeyi durdurur. "
            "Sıra: test hattı → üretim imajı → kod reposundaki sürüm kısıtı.</p>"
            f"<p><b>Sürüm yerleri</b></p><ul>{sl}</ul>"
            + (f"<p><b>Engeller</b></p><ul>{bl}</ul>" if bl else "<p>Bağımlılık engeli yok.</p>")
            + f"<p><small>{KEY_PREFIX} {_esc(key)}</small></p>" + SIGNATURE)


def _repo_root(repo: str, deps, kw: dict) -> Path:
    if kw.get("root") is not None:
        return Path(kw["root"])
    return pm._root(deps.cfg(), repo, deps)


def start(client, repo: str, lang: str, *, to: str | None = None, team: str, deps=None, **kw) -> dict:
    """İzleme Task'ını aç; repo × dil için açık olan varsa onu döndür (hedefi farklı olsa da)."""
    from agile_sdlc_crew.quality import modern
    _check(repo, lang)
    if not (team or "").strip():
        raise ValueError("Takım seçin")
    deps = deps or pm.default_deps()
    root = _repo_root(repo, deps, kw)
    repo = root.name                        # kanonik ad: anahtar satırı ve parmak izleriyle aynı
    pl = upgrade.plan(client, repo, lang, to, deps=deps, **dict(_pick(kw, _PLAN_KW), root=root))
    label = modern.LANGS[lang]["label"]
    if not pl["current"]:
        raise ValueError(f"Şimdiki {label} sürümü bilinmiyor — yükseltme başlatılamaz")
    if pl["up_to_date"] or not pl["target"]:
        raise ValueError("Repo hedef sürümde — başlatılacak yükseltme yok")
    cur, tgt = pl["current"], pl["target"]
    with _LOCK:
        prev = find_open(client, lang, repo)
        if prev:
            return {"id": prev["id"], "created": False, "lang": prev["lang"], "repo": prev["repo"], "to": prev["to"]}
        area = client.get_team_area_path(team) or ""
        if not area:
            raise ValueError(f"Takımın alanı çözümlenemedi: {team!r}")
        try:
            blk = upgrade.blockers(root, lang, tgt)
        except Exception as e:  # noqa: BLE001 — özet için; durum her seferinde yeniden hesaplanır
            log.warning(f"⚠️ upgrade: engeller okunamadı ({repo}): {e}")
            blk = []
        key = f"{lang}:{repo}:{tgt}"
        fields = {
            "System.Title": f"[{repo}] {label} sürüm yükseltme {cur} → {tgt} (izleme)"[:250],
            "System.Description": _track_body(repo, label, cur, tgt, pl["sites"], blk, key),
            "System.AreaPath": area,
            "System.Tags": TRACK_TAG,
        }
        it = _root_iteration(client, team)
        if it:
            fields["System.IterationPath"] = it
        wi = client.create_work_item("Task", fields)
    log.info(f"⬆️ Yükseltme izleme işi açıldı: #{wi['id']} ({key})")
    return {"id": int(wi["id"]), "created": True, "lang": lang, "repo": repo, "to": tgt}


def stop(client, repo: str, lang: str, *, to: str | None = None, team: str, note: str | None = None) -> dict:
    """Açık izleme işini bir yorumla kapat. `note` (HTML) verilirse varsayılan 'durduruldu' yorumunun yerine
    yazılır (otomatik ilerletmenin 'tamamlandı' kapanışı)."""
    from agile_sdlc_crew import wi_lifecycle
    _check(repo, lang)
    area = (client.get_team_area_path(team) or "") if (team or "").strip() else ""
    if not area:
        raise ValueError(f"Takımın alanı çözümlenemedi: {team!r}")
    prev = find_open(client, lang, repo)
    if not prev:
        raise ValueError(f"{repo} · {lang} için açık yükseltme izleme işi yok")
    pa, ar = prev["area"].lower(), area.lower()
    if pa != ar and not pa.startswith(ar + "\\"):
        raise ValueError(f"İzleme işi #{prev['id']} bu takımın alanında değil ({prev['area']}) — kapatılmadı")
    if to and prev["to"] != to:
        raise ValueError(f"Açık izleme işinin hedefi {prev['to']} (istenen {to}) — o hedefle durdurun")
    state = wi_lifecycle.pick_state(client.get_work_item_type_states("Task"), "complete")
    if not state:
        raise ValueError("İzleme işini kapatacak durum bulunamadı (Done / Closed)")
    who = f" ({_esc(team)})" if (team or "").strip() else ""
    client.add_comment(prev["id"], (note or f"<p>Yükseltme izlemesi durduruldu{who}. Açık sürüm PR'ları ve hazırlık "
                                            "işleri olduğu gibi kalır; yeniden başlatmak yeni bir izleme işi açar.</p>")
                       + SIGNATURE)
    client.set_work_item_state(prev["id"], state)
    log.info(f"⬆️ Yükseltme izleme işi kapatıldı: #{prev['id']} ({lang}:{repo}:{prev['to']})")
    return {"id": prev["id"], "closed": True, "state": state, "lang": lang, "repo": prev["repo"], "to": prev["to"]}


# ── Durum ────────────────────────────────────────────────────────────────

def _fp_blocking(rest: str, rules: dict, auto: bool) -> bool:
    """Parmak izinin kural kısmı (`<kural>:<dosya|modül>`) yükseltmeyi bekletir mi.
    Bilinmeyen kural muhafazakâr olarak bekletir."""
    keys = [k for k in list(rules) + ["probe:error", "probe:deprecation"] if rest.startswith(k + ":")]
    if not keys:
        return True
    k = max(keys, key=len)
    if k == "probe:error":
        return True
    if k == "probe:deprecation":
        return False
    return upgrade.blocking(rules[k], auto=auto)


def open_tasks(client, repo: str, lang: str) -> list[dict]:
    """Board'daki açık `tempo-upgrade` işleri (bu repo × dil): etiket + açıklamada bu reponun parmak izi.
    Başlığa BAKILMAZ — tarama board'da işi olan bulguyu `skipped`'e atıp `blocking`'e saymaz, bu yüzden bu
    liste o bulgular için tek kapıdır; başlığı elle değişmiş ya da başka bir hedef için alınmış iş de sayılır.
    Bir iş, parmak izlerinden biri engelleyen kurala (spec §5 tablosu; needs-human etiketi = otomatik değil;
    bilinmeyen kural → engeller) çıkıyorsa engelleyendir."""
    from agile_sdlc_crew.quality import modern, render
    source = modern.LANGS[lang]["source"]
    fp_pre = f"{source}:{repo}:".lower()
    q = ("SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
         f"AND [System.Tags] CONTAINS '{upgrade.TAG}' "
         f"AND [System.Description] CONTAINS '{render.FP_PREFIX} {_q(source)}:{_q(repo)}:'")
    rules = {r.key: r for r in upgrade.catalog(lang)}
    out = []
    for wi in client.query_work_items(q) or []:
        f = wi.get("fields") or {}
        title = f.get("System.Title") or ""
        tags = {t.strip().lower() for t in (f.get("System.Tags") or "").split(";")}
        if not _is_open(f) or upgrade.TAG not in tags:
            continue
        mine = [fp for fp in render.parse_fingerprints(f.get("System.Description") or "")
                if fp.lower().startswith(fp_pre)]
        if not mine:
            continue
        auto = render.HUMAN_TAG not in tags
        out.append({"id": int(wi["id"]), "title": title, "state": f.get("System.State") or "",
                    "blocking": any(_fp_blocking(fp[len(fp_pre):], rules, auto) for fp in mine),
                    "needs_human": not auto})
    return out


def _pr(client, repo: str, branch: str, project: str | None) -> dict | None:
    pr = client.find_pull_request(repo, branch, project=project)
    return dict(pr, repo=repo, project=project or "") if pr else None


def _fresh_scan(client, repo, lang, to, deps, kw) -> dict:
    """Taze tarama. Hedef artık seçilebilir değilse (üretim hedefe geçti) sentetik 'güncel' tarama."""
    try:
        return upgrade.scan(client, repo, lang, to=to, deps=deps, **_pick(kw, _SCAN_KW))
    except ValueError:
        if not to:
            raise
        pl = upgrade.plan(client, repo, lang, None, deps=deps, **_pick(kw, _PLAN_KW))
        cur = pl.get("current")
        if not cur or upgrade._v(cur) < upgrade._v(to):
            raise
        return {"repo": repo, "lang": lang, "current": cur, "target": to, "up_to_date": True, "sites": [],
                "blocking": 0, "blockers": [], "auto": [], "infra": [], "human": []}


def _pushable(s: dict) -> bool:
    from agile_sdlc_crew.quality import upgrade_bump as bump
    return bump.pushable(s)


def _blocked_dv_reason(dv_blocked: list[dict], tgt: str = "") -> str:
    hedef = f" (hedef {tgt})" if tgt else ""
    parts = "; ".join(f"{s.get('repo')} {s.get('path')}:{s.get('line')}: {s.get('blocked')}{hedef}" for s in dv_blocked)
    return f"Üretim imajı güncellenemiyor ({parts}) — üretim adımı hiçbir bölge için açılmaz."


_PR_KNOWN = ("active", "completed", "abandoned")


def _pins_since(client, pins: list[dict], ci_pr: dict | None = None) -> str | None:
    """Kanıt saati: sabitleme dosyalarının dalına yapılan EN SON PUSH (hepsinin en yenisi) ile Tempo'nun test
    hattı PR'ı tamamlanmışsa onun kapanış zamanının en geçi. Commit zamanı kullanılmaz: pipeline repolarına
    doğrudan push yapılıyor; önce yazılıp saatler sonra push edilen commit, aradaki (hâlâ eski dosyayla koşmuş)
    build'leri kanıt saydırırdı. Biri okunamazsa None (kapı kapalı)."""
    from agile_sdlc_crew.quality import upgrade_bump as bump
    best = None
    if ci_pr and ci_pr.get("status") == "completed":
        t = bump._ts(ci_pr.get("closed_at"))
        if t is None:
            log.warning(f"⚠️ upgrade: test hattı PR'ı #{ci_pr.get('id')} kapanış zamanı okunamadı")
            return None
        best = (t, ci_pr.get("closed_at"))
    for proj, repo, branch in sorted({(p.get("project") or "", p["repo"], p["branch"]) for p in pins}):
        try:
            d = client.get_last_push_date(repo, branch, project=proj or None)
        except Exception as e:  # noqa: BLE001 — zaman bilinmeden kanıt okunmaz (kapı kapalı)
            log.warning(f"⚠️ upgrade: {proj}/{repo} {branch} son push zamanı okunamadı: {e}")
            return None
        t = bump._ts(d)
        if t is None:
            return None
        if best is None or t > best[0]:
            best = (t, d)
    return best[1] if best else None


def _handoff(client, out: dict, lang: str) -> dict:
    """Sıradaki adım DevOps'a devredilmişse (anahtarı adımı taşıyan AÇIK istek) yeniden kuyruğa alınmaz."""
    from agile_sdlc_crew import devops_request as dr
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.quality import upgrade_bump as bump
    nx = out["next"]
    nx.setdefault("handed_off", None)
    if not nx.get("step") or not nx.get("ready"):
        return out
    try:
        req = dr.find_request(client, team=pc.get("CREW_DEVOPS_TEAM") or "DevOps",
                              key=bump.step_key(lang, out["repo"], out["target"], nx["step"]))
    except Exception as e:  # noqa: BLE001 — devir bilinmeden adım atılmaz
        nx.update(ready=False, reason=f"DevOps isteği sorgulanamadı: {str(e)[:300]}")
        return out
    if req and req.get("open"):
        nx.update(ready=False, reason=f"DevOps isteği açık: #{req['id']}", handed_off=req["id"])
    return out


def status(client, repo: str, lang: str, *, to: str | None = None, deps=None, scan: dict | None = None,
           wait_h: int | None = None, **kw) -> dict:
    """Aşama + PR'lar + sıradaki adım. `scan` verilirse ikinci tarama yapılmaz.
    -> {stage, current, target, blocking, blockers, open_tasks, ci_pins, prs: {ci, devops, code},
        evidence: {state, build{id,url,queue_time}|None, summary, overdue, since}|None,
        next: {step, ready, reason, sites, manual, test_note, handed_off}}"""
    _check(repo, lang)
    out = _status(client, repo, lang, to=to, deps=deps or pm.default_deps(), scan=scan, wait_h=wait_h, kw=kw)
    return _handoff(client, out, lang)


def _status(client, repo, lang, *, to, deps, scan, wait_h, kw) -> dict:
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.quality import upgrade_bump as bump
    sc = scan if scan is not None else _fresh_scan(client, repo, lang, to, deps, kw)
    repo = sc.get("repo") or repo
    cur, tgt = sc.get("current"), sc.get("target")
    out = {"repo": repo, "lang": lang, "stage": "hazırlık", "current": cur, "target": tgt,
           "blocking": int(sc.get("blocking") or 0), "blockers": list(sc.get("blockers") or []), "open_tasks": [],
           "ci_pins": sc.get("ci_pins"), "prs": {"ci": None, "devops": [], "code": None}, "evidence": None,
           "next": {"step": None, "ready": False, "reason": "", "sites": [], "manual": [], "test_note": "",
                    "handed_off": None, "blocked_by_evidence": False}}

    def nxt(step, ready, reason="", sites=(), manual=(), test_note="", blocked_by_evidence=False):
        # blocked_by_evidence: üretim adımının kanıt DIŞINDAKİ bütün ön koşulları tutuyor, onu bekleten yalnız
        # test kanıtı (kırmızı / henüz yok). Otomatik ilerletme kırmızı build ve CI bekleme notunu yalnız bu
        # durumda yazar — hazırlıkta başka PR'ların kırmızı build'leri bildirim üretmez.
        out["next"] = {"step": step, "ready": bool(ready), "reason": reason, "sites": list(sites),
                       "manual": list(manual), "test_note": test_note, "handed_off": None,
                       "blocked_by_evidence": bool(blocked_by_evidence)}
        return out

    if not cur:
        return nxt(None, False, "Şimdiki sürüm bilinmiyor — yükseltme durumu türetilemez.")
    if not tgt:
        out["stage"] = "tamam"
        return nxt(None, False, "Seçilebilecek daha yüksek bir sürüm yok.")
    br = bump.branch_name(lang, repo, tgt)

    def code_step(sites):
        if any(not s.get("branch") for s in sites):  # kod reposu sitesi hedef dalı taşımaz: varsayılan dal
            try:
                dflt = str((client.get_repository(repo) or {}).get("defaultBranch") or "").replace("refs/heads/", "")
            except Exception as e:  # noqa: BLE001 — dal yoksa push_sites "Hedef dal yok" der, iş açılmaz
                log.warning(f"⚠️ upgrade: {repo} varsayılan dalı okunamadı: {e}")
                dflt = ""
            sites = [dict(s, branch=s.get("branch") or dflt) for s in sites]
        push = [s for s in sites if _pushable(s)]
        manual = [s for s in sites if not _pushable(s)]
        man_txt = "; ".join(f"{s.get('path')}:{s.get('line')}: {s.get('old')} — "
                            f"{s.get('blocked') or 'sürüm kısıtı elle düzenlenmeli'} (hedef {tgt})" for s in manual)
        cpr = out["prs"]["code"] = _pr(client, repo, br, None)
        if cpr and cpr.get("status") == "active":
            return nxt("code", False, f"Kod reposu PR'ı #{cpr.get('id')} açık — merge bekleniyor.", push, manual)
        if cpr and cpr.get("status") == "completed":
            return nxt(None, False, f"Kod reposu PR'ı #{cpr.get('id')} merge edildi.", (), manual)
        if cpr and cpr.get("status") == "abandoned":
            return nxt("code", False, f"Kod reposu PR'ı #{cpr.get('id')} terk edilmiş — '{br}' dalını silip "
                                      "yeniden deneyin.", push, manual)
        if cpr:
            return nxt("code", False, f"Kod reposu PR'ının durumu bilinmiyor (#{cpr.get('id')}: "
                                      f"{cpr.get('status')!r}) — adım atılmadı.", push, manual)
        if not push:
            if manual:
                return nxt("code", False, man_txt + ".", (), manual)
            return nxt(None, False, "Kod reposunda değişecek sürüm metni yok.")
        if any(not s.get("branch") for s in push):
            return nxt("code", False, f"{repo} reposunun varsayılan dalı okunamadı.", push, manual)
        return nxt("code", True, man_txt + "." if man_txt else "", push, manual)

    if sc.get("up_to_date"):
        out["stage"] = "tamam"
        try:
            csites = upgrade._code_sites(repo, _repo_root(repo, deps, kw), lang, tgt)
        except ValueError:
            csites = []
        if not csites:
            out["prs"]["code"] = _pr(client, repo, br, None)
            return nxt(None, False, "Yükseltme tamamlandı.")
        return code_step(csites)

    sites = list(sc.get("sites") or [])
    dv = [s for s in sites if s.get("where") == "devops"]
    ci = [s for s in sites if s.get("where") == "ci"]
    code = [s for s in sites if s.get("where") == "code"]
    dv_blocked = [s for s in dv if s.get("blocked")]
    ci_blocked = [s for s in ci if s.get("blocked")]
    out["open_tasks"] = open_tasks(client, repo, lang)
    not_pre = [r for k in ("auto", "infra", "human") for r in sc.get(k) or [] if r.get("pre_fixable") is False]
    if out["blocking"] > 0 or any(t["blocking"] for t in out["open_tasks"]):
        base = "hazırlık"
        n_open = sum(1 for t in out["open_tasks"] if t["blocking"])
        base_reason = (f"Hazırlık bitmedi: {out['blocking']} engelleyen bulgu, {n_open} açık engelleyen "
                       "yükseltme işi — önce bunlar kapanmalı.")
    elif out["blockers"] or dv_blocked or ci_blocked or not_pre:
        base = "engelli"
        why = [str(b.get("detail") or b.get("name") or b) for b in out["blockers"]]
        if dv_blocked:
            why.append(_blocked_dv_reason(dv_blocked, tgt))
        why += [f"{s.get('repo')} {s.get('path')}:{s.get('line')}: {s.get('blocked')} — {tgt} yapılmalı"
                for s in ci_blocked]
        if not_pre:
            why.append(f"{len(not_pre)} bulgu yalnız hedef sürümde düzeltilebilir (insan işi)")
        base_reason = "Engel var: " + "; ".join(why)
    else:
        base, base_reason = "hazır", ""
    out["stage"] = base
    ok = base == "hazır"

    # Test hattı PR'ı tanımın dosyasının durduğu repoda (ci_source); kaynak yoksa aranmaz.
    src = sc.get("ci_source") or None
    ci_pr = out["prs"]["ci"] = _pr(client, src["repo"], br, src.get("project") or None) if src else None
    dv_repos = sorted({(s.get("project") or "", s["repo"]) for s in dv})
    dv_prs = {}
    for proj, r in dv_repos:
        p = _pr(client, r, br, proj or None)
        if p:
            dv_prs[(proj, r)] = p
    out["prs"]["devops"] = list(dv_prs.values())

    # Test hattı kapısı dosyanın DURUMUNA bağlı (Tempo PR'ına değil): satır hedefin altındaysa ci adımı
    # sırada; hepsi hedefteyse (kim yaptıysa) kanıt, dosyanın son değişikliğinden SONRA kuyruğa giren
    # `<repo>-test` build'idir. Sabitlemeler okunamadıysa (None) hiçbir adım açılmaz.
    pins = sc.get("ci_pins")
    unparsed = [p for p in pins or [] if not p.get("version")]
    below = [p for p in pins or [] if p.get("version") and upgrade._v(p["version"]) < upgrade._v(tgt)]
    ev, since = None, None
    # Taramanın çözdüğü `<repo>-test` tanımı (canlı yol): kanıt aynı tanımla aranır, ikinci ad araması yok.
    ev_kw = {"definition": sc["ci_test_def"]} if "ci_test_def" in sc else {}
    wh = wait_h if wait_h is not None else int(pc.get("CREW_TECHDEBT_UPGRADE_CI_WAIT_H") or 72)
    if pins and not below and not unparsed:
        since = _pins_since(client, pins, ci_pr)
        if since:
            ev = out["evidence"] = bump.ci_evidence(client, repo, since, wait_h=wh, **ev_kw)
    elif pins == [] and dv:
        # Sabitleme yok (ya da tanınmadı değil — tanınmayan satır `unparsed`tır): `<repo>-test` tanımı varsa
        # en yeni tamamlanmış build'i yeşil olmalı; tanım yoksa kanıt yok ("Test kanıtı yok").
        ev = out["evidence"] = bump.ci_evidence(client, repo, None, wait_h=wh, **ev_kw)
    ci_status = (ci_pr or {}).get("status")
    if (below and ci_status == "active") or (pins and not below and not (ev and ev["state"] in ("green", "no_pipeline"))):
        out["stage"] = "test hattı"
    if any(p.get("status") == "active" for p in dv_prs.values()):
        out["stage"] = "üretim"

    if not dv:
        # Sürüm yalnız kod reposunda duruyor: tek adım kod reposu, `hazır`da açılır.
        if not ok:
            return nxt("code", False, base_reason, [s for s in code if _pushable(s)],
                       [s for s in code if not _pushable(s)])
        return code_step(code)

    if pins is None:
        return nxt("ci" if ci else "prod", False, "Test hattı (yml-files) dosyaları okunamadı — test hattının "
                                                   "durumu bilinmeden sürüm adımı atılmaz.", ci or dv)
    if unparsed:
        return nxt("ci" if below else "prod", False, "Kapı kapalı — test hattı sürüm satırı okunamadı: " + "; ".join(
            f"{p['path']}:{p['line']} ({p.get('unparsed', '')!r}) — {tgt} yapılmalı" for p in unparsed)
                   + " (satır elle düzenlenmeli).", ci if below else dv)
    if pins == [] and sc.get("ci_test_def") and not sc.get("ci_source"):
        # Zayıf kanıt yalnız "tanım var, dosyası okundu, repoyu anıyor, sürüm satırı ve şablonu yok" durumunda;
        # dosyası kaynak olarak okunmamış bir tanımın yeşil build'i kanıt sayılmaz.
        return nxt("prod", False, f"'{repo}-test' tanımı var ama test hattı dosyası okunup doğrulanmadı — "
                                  "üretim adımı açılmaz.", dv)
    if below:
        if ci_status == "active":
            return nxt("prod", False, f"Test hattı PR'ı #{ci_pr.get('id')} henüz merge edilmedi.", dv)
        if ci_status == "completed":
            return nxt("ci", False, f"Test hattı PR'ı #{ci_pr.get('id')} merge edildi ama "
                       + ", ".join(f"{p['path']}:{p['line']} ({p['version']})" for p in below)
                       + " hâlâ hedefin altında — dosyayı kontrol edin.", ci)
        if ci_status == "abandoned":
            return nxt("ci", False, f"Test hattı PR'ı #{ci_pr.get('id')} terk edilmiş — '{br}' dalını silip "
                                    "yeniden deneyin.", ci)
        if ci_pr:
            return nxt("ci", False, f"Test hattı PR'ının durumu bilinmiyor (#{ci_pr.get('id')}: {ci_status!r}) — "
                                    "adım atılmadı.", ci)
        if not ok:
            return nxt("ci", False, base_reason, ci)
        bad = [s for s in ci if not _pushable(s)]
        if not ci or bad:
            return nxt("ci", False, "Test hattı satırı otomatik değiştirilemez: "
                       + ("; ".join(f"{s.get('path')}:{s.get('line')}" for s in bad) or "site bulunamadı"), ci)
        return nxt("ci", True, "", ci)

    # Üretim adımı (test hattı hedefte ya da test hattı sabitlemesi hiç yok)
    test_note = "" if pins else NO_EVIDENCE
    abandoned = [p for p in dv_prs.values() if p.get("status") == "abandoned"]
    if abandoned:
        return nxt("prod", False, "Üretim PR'ı terk edilmiş: " + ", ".join(
            f"{p['repo']} #{p.get('id')}" for p in abandoned) + f" — '{br}' dalını silip yeniden deneyin.", dv)
    unknown = [p for p in dv_prs.values() if p.get("status") not in _PR_KNOWN]
    if unknown:
        return nxt("prod", False, "Üretim PR'ının durumu bilinmiyor: " + ", ".join(
            f"{p['repo']} #{p.get('id')} ({p.get('status')!r})" for p in unknown) + " — adım atılmadı.", dv)
    if len(dv_prs) == len(dv_repos):
        if any(p.get("status") == "active" for p in dv_prs.values()):
            return nxt("code", False, "Üretim PR'ları açık — merge ve deploy DevOps'ta; kod reposu adımı üretim "
                                      "hedef sürüme geçince açılır.", [s for s in code if _pushable(s)],
                       [s for s in code if not _pushable(s)])
        return nxt("code", False, "Üretim PR'ları merge edildi; kod reposu adımı üretim sürümü hedefte "
                                  "okunduğunda açılır.", [s for s in code if _pushable(s)],
                   [s for s in code if not _pushable(s)])
    if dv_blocked:
        return nxt("prod", False, _blocked_dv_reason(dv_blocked, tgt), dv)
    if not ok:
        return nxt("prod", False, base_reason, dv)
    if pins:
        if not since:
            return nxt("prod", False, "Test hattı dosyasının son değişiklik zamanı okunamadı (dalın son push'u ya da "
                                      "test hattı PR'ının kapanışı) — kanıt aranamadı, üretim adımı açılmaz.", dv)
        state = (ev or {}).get("state")
        if state == "red":
            return nxt("prod", False, "Test hattı değişikliğinden sonraki build kırmızı — üretim adımı açılmaz. "
                       + (ev.get("summary") or "")[:1500], dv, blocked_by_evidence=True)
        if state == "none":
            return nxt("prod", False, "Test hattı hedef sürümde; ondan sonra kuyruğa girmiş tamamlanmış bir "
                                      "build henüz yok" + (" (bekleme süresi doldu — bir PR'ı yeniden tetikleyin)"
                                                           if ev.get("overdue") else "") + ".", dv,
                       blocked_by_evidence=True)
        if state == "no_pipeline":
            test_note = NO_EVIDENCE
        elif state != "green":
            return nxt("prod", False, f"Test kanıtı okunamadı ({state!r}) — üretim adımı açılmaz.", dv)
    else:
        state = (ev or {}).get("state")
        if state == "no_pipeline":
            test_note = NO_EVIDENCE
        elif state == "green":
            # Sabitlemeye bağlı olmayan kanıt hedef sürüm kanıtı gibi sunulmaz: PR açıklamasının ilk satırı.
            test_note = WEAK_EVIDENCE
        elif state == "red":
            return nxt("prod", False, f"Test hattı sabitlemesi yok ama '{repo}-test' tanımının en yeni build'i "
                                      "kırmızı — üretim adımı açılmaz. " + (ev.get("summary") or "")[:1500], dv,
                       blocked_by_evidence=True)
        elif state == "none":
            return nxt("prod", False, f"Test hattı sabitlemesi yok; '{repo}-test' tanımı var ama tamamlanmış build'i "
                                      "yok — üretim adımı açılmaz.", dv, blocked_by_evidence=True)
        else:
            return nxt("prod", False, f"Test kanıtı okunamadı ({state!r}) — üretim adımı açılmaz.", dv)
    bad = [s for s in dv if not _pushable(s)]
    if bad:
        return nxt("prod", False, "Üretim imajı satırı otomatik değiştirilemez: "
                   + "; ".join(f"{s['repo']} {s.get('path')}" for s in bad), dv)
    return nxt("prod", True, "", dv, test_note=test_note)


def watch_view(client, repo: str, lang: str, *, to: str | None = None, deps=None, **kw) -> dict | None:
    """Tarama ucunun 'üretim izlenen hedefe ulaştı' yanıtı: repo × dil için açık izleme işi var ve üretim
    onun hedefinde (ya da üstünde) ise — tarama o hedef için artık yapılamaz (hedef seçilemez) ama kod
    reposu adımı sırada olabilir. Bulgu listeleri boş, `up_to_date: true`, `status` izleme hedefiyle.
    Koşul tutmuyorsa None (uç eski davranışını korur)."""
    from agile_sdlc_crew.quality import modern
    _check(repo, lang)
    tr = find_open(client, lang, repo)
    if not tr or (to and to != tr["to"]):
        return None
    deps = deps or pm.default_deps()
    pl = upgrade.plan(client, repo, lang, None, deps=deps, **_pick(kw, _PLAN_KW))
    cur = pl.get("current")
    if not cur or upgrade._v(cur) < upgrade._v(tr["to"]):
        return None
    st = status(client, repo, lang, to=tr["to"], deps=deps, **kw)
    return {"repo": st.get("repo") or repo, "lang": lang, "label": modern.LANGS[lang]["label"], "version": cur,
            "current": cur, "target": tr["to"], "target_source": "izleme", "selectable": pl.get("selectable") or [],
            "sites": [], "ci_pins": [], "per_src": pl.get("per_src") or [], "up_to_date": True, "auto": [],
            "infra": [], "human": [], "rules": {}, "skipped": [], "skipped_counts": {}, "errors": [], "blockers": [],
            "probe": None, "counts": {"error": 0, "behavior": 0, "deprecation": 0}, "blocking": 0,
            "watch_target": tr["to"], "track_id": tr["id"],
            "message": f"Üretim {cur} — izlenen hedef {tr['to']} sürümüne ulaşıldı.", "status": st}


# ── Otomatik ilerletme (spec §7) ─────────────────────────────────────────
# İnsan tıklamadan, karttaki düğmelerin yapabileceğinden FAZLASINI yapmaz: alım `upgrade.take`, durum
# `status`, sürüm adımı `upgrade_bump.queue_step`, kapanış `stop` üzerinden — onların bütün kapıları geçerli;
# burası yalnız tavan ekler. Her yazma (yorum / Task) izleme işinin yorumlarındaki bir işaretle tekilleşir,
# böylece 30 dakikada bir koşan tur kendini tekrarlamaz.

RED_MARK = "tempo-upgrade-red:"
RED_TRACK_MARK = "tempo-upgrade-red-track:"
WAIT_MARK = "tempo-upgrade-ci-wait:"
ERR_MARK = "tempo-upgrade-err:"
ERR_REPEAT_H = 24
POLL_MIN_FLOOR = 5
_AUTO_KEYS = ("CREW_TECHDEBT", "CREW_TECHDEBT_PHP_MODERN", "CREW_TECHDEBT_UPGRADE", "CREW_TECHDEBT_UPGRADE_AUTO")
_TAG_RE = re.compile(r"<[^>]+>")
_POLLER: threading.Thread | None = None
_POLLER_LOCK = threading.Lock()


def _plain(text) -> str:
    """Azure yorumu HTML döner (markdown'ımızı render eder): etiketler soyulur, varlıklar çözülür."""
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", str(text or "")))).strip()


def _has_mark(text: str, mark: str) -> bool:
    """`tempo-upgrade-red:33`, `…:334`'ün öneki sayılmasın."""
    return re.search(re.escape(mark) + r"(?![\w.:+-])", text or "") is not None


COMMENTS_MAX = 1000


class _Comments:
    """Bir WI'nin yorumları — en çok bir kez, BÜTÜN sayfalarıyla okunur (uzun yaşayan izleme işinde işaret
    ilk sayfanın dışında kalabilir). Sınır aşılırsa okuma eksiktir: işaret 'var' sayılır, yorum yazılmaz."""

    def __init__(self, client, wid: int):
        self.client, self.wid, self._rows, self.complete = client, int(wid), None, True

    def rows(self) -> list[tuple[str, object]]:
        from agile_sdlc_crew.quality import upgrade_bump as bump
        if self._rows is None:
            reader = getattr(self.client, "get_work_item_comments_all", None)
            if reader is not None:
                raw, self.complete = reader(self.wid, max_items=COMMENTS_MAX)
            else:
                raw = self.client.get_work_item_comments(self.wid) or []
            self._rows = [(_plain(c.get("metin")), bump._ts(c.get("tarih"))) for c in raw or []]
        return self._rows

    def has(self, mark: str, since=None) -> bool:
        """İşaret var mı; `since` verilirse yalnız o andan sonraki yorumlar (zamanı okunamayan yorum yeni sayılır —
        şüphede tekrar yazılmaz). Yorumlar eksik okunduysa karar verilemez → True."""
        rows = self.rows()
        if not self.complete:
            return True
        return any(_has_mark(t, mark) and (since is None or ts is None or ts >= since) for t, ts in rows)

    def add(self, text_html: str) -> None:
        self.client.add_comment(self.wid, text_html + SIGNATURE)
        if self._rows is not None:
            self._rows.append((_plain(text_html), None))


def _note(res: dict, text: str) -> None:
    res["note"] = f"{res['note']} · {text}" if res["note"] else text


def _auto_sprint(client, team: str) -> tuple[str, str]:
    """Otomatik alımların sprint'i (CREW_TECHDEBT_UPGRADE_SPRINT: next | current) → (yol, mod)."""
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.quality import intake
    mode = str(pc.get("CREW_TECHDEBT_UPGRADE_SPRINT") or "next").strip().lower()
    if mode not in ("next", "current"):
        mode = "next"
    it = intake.target_iteration(client.list_iterations(team) or [], mode)
    return str((it or {}).get("path") or ""), mode


def _open_work(client, repo: str, lang: str) -> int:
    """Repo için açık otomatik yükseltme işi (needs-human hariç). Kuyruktaki / koşan işleri o işlerin
    kendisidir (WI açık kaldıkça sayılır); modern_fix PR'ı açıkken WI Code Review'da, yine açık."""
    return sum(1 for t in open_tasks(client, repo, lang) if not t.get("needs_human"))


def _auto_take(client, tr: dict, sc: dict, res: dict, *, team: str, deps, open_jobs, take_fn, kw: dict) -> None:
    """§7.1: board'da olmayan otomatik gruplar (önce engelleyenler) — repo başına açık iş MAX_OPEN'ı
    aşmayacak kadar, istek başı max_take ayrıca; needs-human gruplar tavana sayılmaz (board dedup'ı bir kez açar)."""
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.quality import intake
    if sc.get("up_to_date"):
        return
    repo, lang = tr["repo"], tr["lang"]
    auto = sorted(sc.get("auto") or [], key=lambda r: not r.get("blocking"))
    human = list(sc.get("infra") or []) + list(sc.get("human") or [])
    if not auto and not human:
        return
    max_take = max(1, int(deps.cfg().get("max_take", 8)))
    mo = pc.get("CREW_TECHDEBT_UPGRADE_MAX_OPEN")
    max_open = 3 if mo is None or mo == "" else max(0, int(mo))
    if max_open == 0:
        _note(res, "CREW_TECHDEBT_UPGRADE_MAX_OPEN 0 — otomatik alım yapılmadı")
        return
    n_open = int(open_jobs(repo)) if auto else 0
    n_auto = min(max(0, max_open - n_open), max_take)
    ids = [r["gid"] for r in auto[:n_auto]] + [r["gid"] for r in human][:max_take - n_auto]
    if auto and n_auto == 0:
        _note(res, f"Tavan dolu: {n_open} açık yükseltme işi (en çok {max_open}) — yeni otomatik iş alınmadı")
    if not ids:
        return
    sprint, mode = _auto_sprint(client, team)
    if not sprint:
        _note(res, f"Takımın {'sonraki' if mode == 'next' else 'aktif'} sprinti yok — otomatik alım yapılmadı")
        log.info(f"⬆️ upgrade oto: #{tr['id']} ({lang}:{repo}) {mode} sprint yok, alım yok")
        return
    try:
        out = take_fn(client, repo, lang, ids, to=tr["to"], team=team, sprint=sprint, deps=deps, **_pick(kw, _SCAN_KW))
    except (intake.Busy, ValueError) as e:
        _note(res, f"Alım yapılmadı: {str(e)[:300]}")
        log.info(f"⬆️ upgrade oto: #{tr['id']} ({lang}:{repo}) alım yapılmadı: {e}")
        return
    made = [x for x in (out.get("tasks") or []) + (out.get("human_tasks") or []) if x.get("id")]
    fps = {fp for x in made for fp in x.get("fingerprints") or []}
    by_gid = {r["gid"]: r for r in auto + human}
    res["took"] = [g for g in ids if set(by_gid[g].get("fingerprints") or []) & fps]
    if any(x.get("job_id") for x in out.get("tasks") or []):
        res["queued"] = True
    log.info(f"⬆️ upgrade oto: #{tr['id']} ({lang}:{repo}) {len(res['took'])} iş alındı → {sprint}")


def _red_build(client, tr: dict, st: dict, ev: dict, comments: _Comments, *, team: str, deps) -> str:
    """Kırmızı build (spec §6), yalnız üretim adımını O bekletirken çağrılır. İzleme işi başına en çok BİR açık
    needs-human Task (`tempo-upgrade-red-track: <izleme id>` satırı): açıksa yeni build ona bir yorum olur,
    yoksa yeni Task açılır. İzleme işine build başına bir yorum (`tempo-upgrade-red:<build>`); o yorum varsa
    bu build için her şey yapılmıştır."""
    from agile_sdlc_crew.quality import modern, render
    b = ev.get("build") or {}
    bid = str(b.get("id") or "").strip()
    if not bid:
        return "Kırmızı build'in numarası okunamadı — bildirim yazılmadı"
    repo, lang, to, tid = tr["repo"], tr["lang"], tr["to"], int(tr["id"])
    mark = RED_MARK + bid
    if comments.has(mark):          # eksik okunduysa da True: karar verilemez, yazılmaz
        return f"Kırmızı build #{bid} zaten bildirildi"
    track_mark = f"{RED_TRACK_MARK} {tid}"
    url = str(b.get("url") or "")
    summary = render.mask_secrets(str(ev.get("summary") or ""))[:3000]
    label = modern.LANGS[lang]["label"]
    cur = st.get("current") or "?"
    link = f"<a href=\"{_esc(url)}\">#{_esc(bid)}</a>" if url else f"#{_esc(bid)}"
    q = ("SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
         f"AND [System.Tags] CONTAINS '{upgrade.TAG}' AND [System.Description] CONTAINS '{_q(track_mark)}'")
    mine = [w for w in client.query_work_items(q) or []
            if _has_mark(_plain((w.get("fields") or {}).get("System.Description")), track_mark)]
    open_ = sorted((w for w in mine if _is_open(w.get("fields") or {})), key=lambda w: int(w["id"]))
    wrote = []
    task_id = None
    if open_:
        task_id = int(open_[0]["id"])
        tc = _Comments(client, task_id)
        if not _has_mark(_plain(open_[0]["fields"].get("System.Description")), mark) and not tc.has(mark):
            tc.add(f"<p><b>Yeni kırmızı build</b> {link} — üretim adımı hâlâ açılmıyor.</p>"
                   f"<pre>{_esc(summary[:1500])}</pre><p><small>{_esc(mark)}</small></p>")
            wrote.append(f"açık insan işi #{task_id}'e yorum")
    elif any(_has_mark(_plain((w.get("fields") or {}).get("System.Description")), mark) for w in mine):
        # bu build için açılmış Task bir insan tarafından kapatılmış: aynı build için yeniden açılmaz
        task_id = next(int(w["id"]) for w in mine
                       if _has_mark(_plain((w.get("fields") or {}).get("System.Description")), mark))
    if task_id is None:
        sprint, _mode = _auto_sprint(client, team)
        fields = {
            "System.Title": f"[{repo}] {label} sürüm yükseltme {cur}→{to}: test build'i kırmızı (#{bid})"[:250],
            "System.Description": (
                f"<p><b>{_esc(label)} sürüm yükseltme</b> {_esc(repo)} {_esc(cur)} → {_esc(to)}: test hattı hedef "
                "sürümdeyken koşan build kırmızı — üretim adımı açılmaz.</p>"
                f"<p>Build: {link} · İzleme işi: #{tid}</p><pre>{_esc(summary)}</pre>"
                "<p>Hata yükseltmeden kaynaklanıyorsa düzeltin; ilgisizse (kararsız test) build'i yeniden tetikleyin. "
                "Yeşil build gelince üretim adımı açılır. Bu Task açıkken sonraki kırmızı build'ler buraya yorum "
                "olarak düşer.</p>"
                f"<p><small>{_esc(track_mark)}</small></p><p><small>{_esc(mark)}</small></p>" + SIGNATURE),
            "System.Tags": "; ".join([render.TAG, render.TECHDEBT_TAG, render.TECHDEBT_PUBLIC_TAG,
                                      render.src_tag(modern.LANGS[lang]["source"]), upgrade.TAG, render.HUMAN_TAG]),
            "System.IterationPath": sprint or _root_iteration(client, team),
        }
        area = client.get_team_area_path(team) or ""
        if area:
            fields["System.AreaPath"] = area
        for k, v in (deps.cfg().get("task_fields") or {}).items():
            fields.setdefault(k, v)
        task_id = int(client.create_work_item("Task", fields)["id"])
        wrote.append(f"insan işi #{task_id}")
        log.info(f"⬆️ upgrade oto: #{tid} ({lang}:{repo}) kırmızı build #{bid} → needs-human Task #{task_id}")
    comments.add(f"<p><b>Test build'i kırmızı</b> — {_esc(repo)} {_esc(cur)} → {_esc(to)}: üretim adımı açılmaz.</p>"
                 f"<p>Build: {link} · İnsan işi: #{task_id}</p><pre>{_esc(summary[:1500])}</pre>"
                 f"<p><small>{_esc(mark)}</small></p>")
    wrote.append("izleme işine yorum")
    return f"Kırmızı build #{bid}: " + ", ".join(wrote)


def _ci_wait(client, tr: dict, ev: dict, comments: _Comments) -> str:
    """CI_WAIT_H doldu, yeni sürümle build yok: aynı `since` için BİR kez not."""
    from agile_sdlc_crew import pipeline_config as pc
    since = str(ev.get("since") or "")
    mark = WAIT_MARK + since
    if comments.has(mark):
        return "CI bekleme notu zaten yazıldı"
    wait_h = int(pc.get("CREW_TECHDEBT_UPGRADE_CI_WAIT_H") or 72)
    comments.add(f"<p><b>Yeni sürümle koşmuş build yok</b> — bir PR'ı yeniden tetikleyin.</p>"
                 f"<p>Test hattı {_esc(tr['to'])} sürümünde (değişiklik: {_esc(since)}); {wait_h} saati aştığı hâlde "
                 f"'{_esc(tr['repo'])}-test' tanımının ondan sonra kuyruğa girmiş tamamlanmış build'i yok. Yeşil build "
                 "gelince üretim adımı açılır.</p>"
                 f"<p><small>{_esc(mark)}</small></p>")
    log.info(f"⬆️ upgrade oto: #{tr['id']} ({tr['lang']}:{tr['repo']}) CI bekleme notu ({since})")
    return "CI bekleme notu yazıldı"


def _error_comment(client, tid: int, msg: str, now) -> None:
    """Hata izleme işine — aynı metin son 24 saatte yazılmadıysa (her turda bir yorum olmasın)."""
    import hashlib
    from datetime import timedelta
    mark = ERR_MARK + hashlib.sha1(msg.encode("utf-8")).hexdigest()[:10]
    try:
        c = _Comments(client, tid)
        if c.has(mark, since=now - timedelta(hours=ERR_REPEAT_H)):
            return
        c.add("<p><b>Otomatik ilerletme bu yükseltmede hata verdi</b> — sonraki turda yeniden denenecek.</p>"
              f"<p>{_esc(msg)}</p><p><small>{_esc(mark)}</small></p>")
    except Exception as e:  # noqa: BLE001 — Azure erişilemiyorsa yorum da yazılamaz; log yeter
        log.warning(f"⚠️ upgrade oto: #{tid} hata yorumu yazılamadı: {str(e)[:200]}")


_RETRY_FREE = (None, "completed")   # son adım işi yok ya da tamamlandı → otomatik kuyruğa alınabilir


def _advance_one(client, tr: dict, res: dict, *, team: str, deps, create_job, open_jobs, bump_jobs, status_fn,
                 take_fn, last_bump_job, kw: dict) -> None:
    from agile_sdlc_crew.quality import upgrade_bump as bump
    repo, lang, to, tid = tr["repo"], tr["lang"], tr["to"], int(tr["id"])
    sc = _fresh_scan(client, repo, lang, to, deps, kw)
    st = status_fn(client, repo, lang, to=to, deps=deps, scan=sc, **_pick(kw, _SCAN_KW))
    nx = st.get("next") or {}
    if nx.get("handed_off"):
        # Adım DevOps'a devredildi: hiçbir şey yazılmaz / kuyruğa girmez (neden kartta görünüyor).
        _note(res, f"Sıradaki adım DevOps isteğinde (#{nx['handed_off']}) — bekleniyor")
        return
    _auto_take(client, tr, sc, res, team=team, deps=deps,
               open_jobs=open_jobs or (lambda r: _open_work(client, r, lang)), take_fn=take_fn, kw=kw)
    ev = st.get("evidence") or {}
    # Bildirim yalnız kanıt sıradaki adımı (prod) bekletirken: hazırlık / engelli aşamasında reponun herhangi bir
    # PR'ının kırmızı build'i yükseltmeyle ilgisizdir (her yeni build id'si yeni bir Task olurdu).
    if nx.get("step") == "prod" and nx.get("blocked_by_evidence"):
        comments = _Comments(client, tid)
        if ev.get("state") == "red":
            _note(res, _red_build(client, tr, st, ev, comments, team=team, deps=deps))
        elif ev.get("state") == "none" and ev.get("overdue"):
            _note(res, _ci_wait(client, tr, ev, comments))
    if nx.get("step") and nx.get("ready"):
        # Son çalışması başarısız olan adım otomatik olarak yeniden açılmaz (her turda bir başarısız iş + yorum
        # olurdu); yeniden deneme karttaki düğmenin, insanın.
        last = last_bump_job(tid, nx["step"], to)
        if (last or {}).get("status") not in _RETRY_FREE:
            label = STEP_LABEL.get(nx["step"], nx["step"])
            st_ = last.get("status")
            why = ("kuyrukta / çalışıyor" if st_ in ("queued", "running")
                   else f"son çalışması başarısız ({st_}) — adım insanın yeniden denemesini bekliyor")
            _note(res, f"{label} adımı otomatik kuyruğa alınmadı: iş #{last.get('id')} {why}")
            return
        # Tek adım işi; queue_step durumu yeniden hesaplar (bu turda açılan işler dahil) ve açık adım işi
        # varken reddeder — buradan atlanamaz.
        try:
            q = bump.queue_step(client, repo, lang, to=to, team=team, deps=deps, create_job=create_job,
                                status_fn=status_fn, open_jobs=bump_jobs, expect_step=nx["step"], scan=sc,
                                **_pick(kw, _SCAN_KW))
        except ValueError as e:     # StepChanged dahil
            _note(res, f"Sürüm adımı kuyruğa alınmadı: {str(e)[:300]}")
            log.info(f"⬆️ upgrade oto: #{tid} ({lang}:{repo}) adım kuyruğa alınmadı: {e}")
        else:
            res.update(queued_step=q["step"], job_id=q.get("job_id"), queued=True)
            log.info(f"⬆️ upgrade oto: #{tid} ({lang}:{repo}) {STEP_LABEL.get(q['step'], q['step'])} adımı kuyrukta "
                     f"(iş #{q.get('job_id')})")
        return
    if st.get("stage") == "tamam" and not nx.get("step"):
        cpr = (st.get("prs") or {}).get("code")
        if cpr and cpr.get("status") != "completed":
            _note(res, f"Kod reposu PR'ı #{cpr.get('id')} {cpr.get('status')} — izleme işi kapatılmadı")
            return
        pr_txt = f" Kod reposu PR'ı #{_esc(cpr.get('id'))} merge edildi." if cpr else ""
        stop(client, repo, lang, to=to, team=team,
             note=f"<p><b>Yükseltme tamamlandı:</b> {_esc(repo)} üretimde {_esc(st.get('current') or to)} sürümünde."
                  f"{pr_txt} İzleme işi otomatik ilerletme tarafından kapatıldı.</p>")
        res["closed"] = True
        log.info(f"⬆️ upgrade oto: #{tid} ({lang}:{repo}:{to}) tamamlandı, izleme işi kapatıldı")


def advance(client, *, team: str, track_ids=None, deps=None, create_job=None, open_jobs=None, bump_jobs=None,
            last_bump_job=None, status_fn=None, take_fn=None, now=None, index=None, read=None, yml_files=None,
            app_map_cache: dict | None = None, scan_fn=None, head_sha_fn=None, probe_fn=None) -> list[dict]:
    """Takımın açık her izleme işi için bir tur (spec §7): otomatik alım (tavanlı, sprint
    CREW_TECHDEBT_UPGRADE_SPRINT), kanıt sıradaki adımı bekletiyorsa kırmızı build / CI bekleme bildirimi (bir
    kez), hazırsa tek sürüm adımı (son çalışması başarısızsa değil), `tamam`da kapanış. Bir öğenin hatası notuna
    yazılır, sıradakine geçilir; CREW_TECHDEBT_UPGRADE_AUTO kapanırsa sıradaki öğeye geçilmez.
    `track_ids`: yalnız bu izleme işleri (yoklayıcı takımları gruplarken her işi bir kez verir).
    Enjeksiyon (testler): `open_jobs(repo) -> int` repodaki açık otomatik iş sayısı (varsayılan board);
    `bump_jobs(track_id)` queue_step'in açık adım işi kontrolü; `last_bump_job(track_id, step, to)` son adım işi
    (ikisinin varsayılanı jobs tablosu, yalnız okur); tarama/plan enjeksiyonları upgrade.scan'deki gibi.
    -> [{"id","repo","lang","to","took","queued_step","job_id","queued","closed","note"}]"""
    from datetime import datetime, timezone
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.quality import upgrade_bump as bump
    from agile_sdlc_crew.quality.render import mask_secrets
    kw = {k: v for k, v in dict(index=index, read=read, yml_files=yml_files, app_map_cache=app_map_cache,
                                scan_fn=scan_fn, head_sha_fn=head_sha_fn, probe_fn=probe_fn).items() if v is not None}
    deps = deps or pm.default_deps()
    now = now or datetime.now(timezone.utc)
    items = tracked(client, team)
    if track_ids is not None:
        want = {int(i) for i in track_ids}
        items = [t for t in items if int(t["id"]) in want]
    out = []
    for tr in items:
        if not pc.get("CREW_TECHDEBT_UPGRADE_AUTO"):
            log.info("⬆️ upgrade oto: CREW_TECHDEBT_UPGRADE_AUTO kapatıldı — tur durdu")
            break
        res = {"id": tr["id"], "repo": tr["repo"], "lang": tr["lang"], "to": tr["to"], "took": [],
               "queued_step": None, "job_id": None, "queued": False, "closed": False, "note": ""}
        try:
            _check(tr["repo"], tr["lang"])
            _advance_one(client, tr, res, team=team, deps=deps, create_job=create_job, open_jobs=open_jobs,
                         bump_jobs=bump_jobs, status_fn=status_fn or status, take_fn=take_fn or upgrade.take,
                         last_bump_job=last_bump_job or bump.last_bump_job, kw=kw)
        except Exception as e:  # noqa: BLE001 — bir izleme işinin hatası diğerlerini durdurmaz
            msg = mask_secrets(str(e) or type(e).__name__)[:500]
            log.warning(f"⚠️ upgrade oto: #{tr['id']} ({tr['lang']}:{tr['repo']}) hata: {msg}")
            _note(res, f"Hata: {msg}")
            _error_comment(client, int(tr["id"]), msg, now)
        out.append(res)
    return out


def _team_for_area(client, area: str, cache: dict) -> str:
    """İzleme işinin alanından takım: önce alanın son parçası (get_team_area_path takım adıyla eşleşen alanı
    tercih eder), değilse alanı kapsayan en dar alanlı takım."""
    key = (area or "").lower()
    if not key:
        return ""
    if key in cache:
        return cache[key]
    leaf = area.split("\\")[-1].strip()
    team = leaf if leaf and (client.get_team_area_path(leaf) or "").lower() == key else ""
    if not team:
        if "\0teams" not in cache:
            cache["\0teams"] = [(t["name"], (client.get_team_area_path(t["name"]) or "").lower())
                                for t in client.list_teams() or [] if t.get("name")]
        fits = [(n, a) for n, a in cache["\0teams"] if a and (key == a or key.startswith(a + "\\"))]
        team = max(fits, key=lambda x: len(x[1]))[0] if fits else ""
    cache[key] = team
    return team


def _tick(get_client, on_queued=None, *, advance_fn=None) -> list[dict]:
    """Yoklayıcının bir turu. Ayarlar her turda okunur; otomatik ilerletme kapalıysa Azure'a hiç gidilmez.
    Takımlar açık izleme işlerinin alanından çıkar (proje genelinde tek WIQL); her izleme işi bir kez işlenir."""
    from agile_sdlc_crew import pipeline_config as pc
    if not all(pc.get(k) for k in _AUTO_KEYS):
        return []
    client = get_client()
    groups: dict[str, set] = {}
    cache: dict = {}
    for r in _track_rows(client):
        team = _team_for_area(client, r["area"], cache)
        if not team:
            log.warning(f"⚠️ upgrade oto: izleme işi #{r['id']} alanının takımı bulunamadı ({r['area']}) — atlandı")
            continue
        groups.setdefault(team, set()).add(r["id"])
    out: list[dict] = []
    for team, ids in sorted(groups.items()):
        try:
            out += (advance_fn or advance)(client, team=team, track_ids=ids)
        except Exception as e:  # noqa: BLE001 — bir takımın hatası diğerlerini durdurmaz
            log.warning(f"⚠️ upgrade oto: '{team}' takımı turu hata verdi: {str(e)[:300]}")
    if on_queued is not None and any(r.get("queued") for r in out):
        on_queued()
    return out


def start_poller(get_client, *, interval_min, on_queued=None, sleep=None, tick=None,
                 stop_event: threading.Event | None = None) -> threading.Thread:
    """Süreç başına TEK daemon thread (ikinci çağrı aynısını döner). İlk tur bir aralık sonra — sunucu yeniden
    başlar başlamaz iş yapılmaz. `interval_min` sayı ya da her turda okunan çağrılabilir; en az 5 dakika.
    `on_queued`: kuyruğa iş girdiyse çağrılır (sunucunun `_ensure_worker`'ı — quality/ işçiyi bilmez).
    Bir turun hatası thread'i durdurmaz."""
    import time
    global _POLLER
    with _POLLER_LOCK:
        if _POLLER is not None and _POLLER.is_alive():
            return _POLLER
        sleep = sleep or time.sleep
        tick = tick or _tick
        stop_event = stop_event or threading.Event()

        def minutes() -> int:
            try:
                v = interval_min() if callable(interval_min) else interval_min
                return max(POLL_MIN_FLOOR, int(v or 30))
            except Exception:  # noqa: BLE001
                return 30

        def loop():
            while not stop_event.is_set():
                sleep(minutes() * 60)
                if stop_event.is_set():
                    break
                try:
                    tick(get_client, on_queued=on_queued)
                except Exception as e:  # noqa: BLE001
                    log.warning(f"⚠️ Yükseltme otomatik ilerletme turu hata verdi: {str(e)[:300]}")

        _POLLER = threading.Thread(target=loop, daemon=True, name="upgrade-advance-poller")
        _POLLER.start()
        return _POLLER
