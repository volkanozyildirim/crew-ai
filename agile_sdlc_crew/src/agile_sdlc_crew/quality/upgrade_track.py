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
        if not _is_open(f):
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
    prev = find_open(client, lang, repo)
    if not prev:
        raise ValueError(f"{repo} · {lang} için açık yükseltme izleme işi yok")
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


def open_tasks(client, repo: str, lang: str, cur: str, tgt: str) -> list[dict]:
    """Board'daki açık `tempo-upgrade` işleri (bu repo × dil × geçiş): başlıkta
    `Sürüm yükseltme <şimdiki>→<hedef>:` ve açıklamada bu reponun parmak izi. Bir iş, parmak
    izlerinden biri engelleyen kurala (spec §5 tablosu; needs-human etiketi = otomatik değil)
    çıkıyorsa engelleyendir."""
    from agile_sdlc_crew.quality import modern, render
    source = modern.LANGS[lang]["source"]
    fp_pre = f"{source}:{repo}:".lower()
    prefix = f"Sürüm yükseltme {cur}→{tgt}:"
    q = ("SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
         f"AND [System.Tags] CONTAINS '{upgrade.TAG}' "
         f"AND [System.Description] CONTAINS '{render.FP_PREFIX} {_q(source)}:{_q(repo)}:'")
    rules = {r.key: r for r in upgrade.catalog(lang)}
    out = []
    for wi in client.query_work_items(q) or []:
        f = wi.get("fields") or {}
        title = f.get("System.Title") or ""
        if not _is_open(f) or prefix not in title:
            continue
        mine = [fp for fp in render.parse_fingerprints(f.get("System.Description") or "")
                if fp.lower().startswith(fp_pre)]
        if not mine:
            continue
        tags = {t.strip().lower() for t in (f.get("System.Tags") or "").split(";")}
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


def status(client, repo: str, lang: str, *, to: str | None = None, deps=None, scan: dict | None = None,
           wait_h: int | None = None, **kw) -> dict:
    """Aşama + PR'lar + sıradaki adım. `scan` verilirse ikinci tarama yapılmaz.
    -> {stage, current, target, blocking, blockers, open_tasks, prs: {ci, devops, code}, evidence,
        next: {step, ready, reason, sites, manual, test_note}}"""
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.quality import upgrade_bump as bump
    _check(repo, lang)
    deps = deps or pm.default_deps()
    sc = scan if scan is not None else _fresh_scan(client, repo, lang, to, deps, kw)
    repo = sc.get("repo") or repo
    cur, tgt = sc.get("current"), sc.get("target")
    out = {"repo": repo, "lang": lang, "stage": "hazırlık", "current": cur, "target": tgt,
           "blocking": int(sc.get("blocking") or 0), "blockers": list(sc.get("blockers") or []), "open_tasks": [],
           "prs": {"ci": None, "devops": [], "code": None}, "evidence": None,
           "next": {"step": None, "ready": False, "reason": "", "sites": [], "manual": [], "test_note": ""}}

    def nxt(step, ready, reason="", sites=(), manual=(), test_note=""):
        out["next"] = {"step": step, "ready": bool(ready), "reason": reason, "sites": list(sites),
                       "manual": list(manual), "test_note": test_note}
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
        if cpr:
            return nxt("code", False, f"Kod reposu PR'ı #{cpr.get('id')} terk edilmiş — '{br}' dalını silip "
                                      "yeniden deneyin.", push, manual)
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
    out["open_tasks"] = open_tasks(client, repo, lang, cur, tgt)
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
    ev = None
    if ci_pr and ci_pr.get("status") == "completed":
        wh = wait_h if wait_h is not None else int(pc.get("CREW_TECHDEBT_UPGRADE_CI_WAIT_H") or 72)
        ev = out["evidence"] = bump.ci_evidence(client, repo, ci_pr.get("closed_at") or "", wait_h=wh)
    if ci_pr and ci_pr.get("status") in ("active", "completed") and not (ev and ev["state"] in ("green", "no_pipeline")):
        out["stage"] = "test hattı"
    if any(p.get("status") == "active" for p in dv_prs.values()):
        out["stage"] = "üretim"

    if not dv:
        # Sürüm yalnız kod reposunda duruyor: tek adım kod reposu, `hazır`da açılır.
        if not ok:
            return nxt("code", False, base_reason, [s for s in code if _pushable(s)],
                       [s for s in code if not _pushable(s)])
        return code_step(code)

    has_ci = bool(ci) or bool(ci_pr and ci_pr.get("status") != "abandoned")
    if has_ci and (ci_pr is None or ci_pr.get("status") == "abandoned"):
        if ci_pr:
            return nxt("ci", False, f"Test hattı PR'ı #{ci_pr.get('id')} terk edilmiş — '{br}' dalını silip "
                                    "yeniden deneyin.", ci)
        if not ok:
            return nxt("ci", False, base_reason, ci)
        bad = [s for s in ci if not _pushable(s)]
        if bad:
            return nxt("ci", False, "Test hattı satırı otomatik değiştirilemez: "
                       + "; ".join(f"{s.get('path')}:{s.get('line')}" for s in bad), ci)
        return nxt("ci", True, "", ci)
    if has_ci and ci_pr.get("status") == "active":
        return nxt("prod", False, f"Test hattı PR'ı #{ci_pr.get('id')} henüz merge edilmedi.", dv)

    # Üretim adımı (CI PR'ı merge edilmiş ya da test hattı sitesi hiç yok)
    test_note = "" if has_ci else NO_EVIDENCE
    abandoned = [p for p in dv_prs.values() if p.get("status") == "abandoned"]
    if abandoned:
        return nxt("prod", False, "Üretim PR'ı terk edilmiş: " + ", ".join(
            f"{p['repo']} #{p.get('id')}" for p in abandoned) + f" — '{br}' dalını silip yeniden deneyin.", dv)
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
    if has_ci:
        if ev["state"] == "red":
            return nxt("prod", False, "Test hattı merge'inden sonraki build kırmızı — üretim adımı açılmaz. "
                       + (ev.get("summary") or "")[:1500], dv)
        if ev["state"] == "none":
            return nxt("prod", False, "Test hattı PR'ı merge edildi; ondan sonra kuyruğa girmiş tamamlanmış bir "
                                      "build henüz yok" + (" (bekleme süresi doldu — bir PR'ı yeniden tetikleyin)"
                                                           if ev.get("overdue") else "") + ".", dv)
        if ev["state"] == "no_pipeline":
            test_note = NO_EVIDENCE
    bad = [s for s in dv if not _pushable(s)]
    if bad:
        return nxt("prod", False, "Üretim imajı satırı otomatik değiştirilemez: "
                   + "; ".join(f"{s['repo']} {s.get('path')}" for s in bad), dv)
    return nxt("prod", True, "", dv, test_note=test_note)
