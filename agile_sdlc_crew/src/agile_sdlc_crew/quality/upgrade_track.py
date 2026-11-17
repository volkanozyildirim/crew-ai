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


def stop(client, repo: str, lang: str, *, to: str | None = None, team: str) -> dict:
    """Açık izleme işini bir yorumla kapat."""
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
    client.add_comment(prev["id"], f"<p>Yükseltme izlemesi durduruldu{who}. Açık sürüm PR'ları ve hazırlık işleri "
                                   "olduğu gibi kalır; yeniden başlatmak yeni bir izleme işi açar.</p>" + SIGNATURE)
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
                    "blocking": any(_fp_blocking(fp[len(fp_pre):], rules, auto) for fp in mine)})
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


def _blocked_dv_reason(dv_blocked: list[dict]) -> str:
    parts = "; ".join(f"{s.get('repo')} {s.get('path')}: {s.get('blocked')}" for s in dv_blocked)
    return f"Üretim imajı güncellenemiyor ({parts}) — üretim adımı hiçbir bölge için açılmaz."


_PR_KNOWN = ("active", "completed", "abandoned")


def _pins_since(client, pins: list[dict], ci_pr: dict | None = None) -> str | None:
    """Kanıt saati: sabitleme dosyalarına dokunan en son commit (first-parent; hepsinin en yenisi) ile
    Tempo'nun test hattı PR'ı tamamlanmışsa onun kapanış zamanının en geçi. Biri okunamazsa None (kapı kapalı):
    commit araması merge'den önceki bir zamanı verse bile merge'den önce koşan build kanıt sayılmaz."""
    from agile_sdlc_crew.quality import upgrade_bump as bump
    best = None
    if ci_pr and ci_pr.get("status") == "completed":
        t = bump._ts(ci_pr.get("closed_at"))
        if t is None:
            log.warning(f"⚠️ upgrade: test hattı PR'ı #{ci_pr.get('id')} kapanış zamanı okunamadı")
            return None
        best = (t, ci_pr.get("closed_at"))
    for proj, repo, branch, path in sorted({(p.get("project") or "", p["repo"], p["branch"], p["path"]) for p in pins}):
        try:
            d = client.get_last_commit_date(repo, "/" + str(path).lstrip("/"), branch, project=proj or None)
        except Exception as e:  # noqa: BLE001 — zaman bilinmeden kanıt okunmaz (kapı kapalı)
            log.warning(f"⚠️ upgrade: {repo} {path} son commit zamanı okunamadı: {e}")
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
                    "handed_off": None}}

    def nxt(step, ready, reason="", sites=(), manual=(), test_note=""):
        out["next"] = {"step": step, "ready": bool(ready), "reason": reason, "sites": list(sites),
                       "manual": list(manual), "test_note": test_note, "handed_off": None}
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
        man_txt = "; ".join(f"{s.get('path')}: {s.get('old')} — sürüm kısıtı elle düzenlenmeli" for s in manual)
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
            why.append(_blocked_dv_reason(dv_blocked))
        why += [f"{s.get('repo')} {s.get('path')}: {s.get('blocked')}" for s in ci_blocked]
        if not_pre:
            why.append(f"{len(not_pre)} bulgu yalnız hedef sürümde düzeltilebilir (insan işi)")
        base_reason = "Engel var: " + "; ".join(why)
    else:
        base, base_reason = "hazır", ""
    out["stage"] = base
    ok = base == "hazır"

    ci_pr = out["prs"]["ci"] = _pr(client, upgrade.CI_REPO, br, upgrade.CI_PROJECT)
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
    wh = wait_h if wait_h is not None else int(pc.get("CREW_TECHDEBT_UPGRADE_CI_WAIT_H") or 72)
    if pins and not below and not unparsed:
        since = _pins_since(client, pins, ci_pr)
        if since:
            ev = out["evidence"] = bump.ci_evidence(client, repo, since, wait_h=wh)
    elif pins == [] and dv:
        # Sabitleme yok (ya da tanınmadı değil — tanınmayan satır `unparsed`tır): `<repo>-test` tanımı varsa
        # en yeni tamamlanmış build'i yeşil olmalı; tanım yoksa kanıt yok ("Test kanıtı yok").
        ev = out["evidence"] = bump.ci_evidence(client, repo, None, wait_h=wh)
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
        return nxt("ci" if below else "prod", False, "Kapı kapalı — test hattı sürüm satırı okunamadı: " + ", ".join(
            f"{p['path']}:{p['line']} ({p.get('unparsed', '')!r})" for p in unparsed)
                   + " — satır elle düzenlenmeli.", ci if below else dv)
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
        return nxt("prod", False, _blocked_dv_reason(dv_blocked), dv)
    if not ok:
        return nxt("prod", False, base_reason, dv)
    if pins:
        if not since:
            return nxt("prod", False, "Test hattı dosyasının son değişiklik zamanı okunamadı (son commit ya da "
                                      "test hattı PR'ının kapanışı) — kanıt aranamadı, üretim adımı açılmaz.", dv)
        state = (ev or {}).get("state")
        if state == "red":
            return nxt("prod", False, "Test hattı değişikliğinden sonraki build kırmızı — üretim adımı açılmaz. "
                       + (ev.get("summary") or "")[:1500], dv)
        if state == "none":
            return nxt("prod", False, "Test hattı hedef sürümde; ondan sonra kuyruğa girmiş tamamlanmış bir "
                                      "build henüz yok" + (" (bekleme süresi doldu — bir PR'ı yeniden tetikleyin)"
                                                           if ev.get("overdue") else "") + ".", dv)
        if state == "no_pipeline":
            test_note = NO_EVIDENCE
        elif state != "green":
            return nxt("prod", False, f"Test kanıtı okunamadı ({state!r}) — üretim adımı açılmaz.", dv)
    else:
        state = (ev or {}).get("state")
        if state == "no_pipeline":
            test_note = NO_EVIDENCE
        elif state == "green":
            test_note = ""
        elif state == "red":
            return nxt("prod", False, f"Test hattı sabitlemesi yok ama '{repo}-test' tanımının en yeni build'i "
                                      "kırmızı — üretim adımı açılmaz. " + (ev.get("summary") or "")[:1500], dv)
        elif state == "none":
            return nxt("prod", False, f"Test hattı sabitlemesi yok; '{repo}-test' tanımı var ama tamamlanmış build'i "
                                      "yok — üretim adımı açılmaz.", dv)
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
