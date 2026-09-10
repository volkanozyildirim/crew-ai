"""Sprint planlama — Scrum işlevleri, faz 4.

Board salt okunurdu: hangi WI pipeline'a verilecek, elle ve tek tek kuyruğa
giriyordu. Bu modül seçili sprintin iş kalemlerinden **aday listesi** çıkarır,
öncelik + büyüklük sırasına dizer, kapasiteye göre işaretler ve seçilenleri
toplu kuyruğa alır. LLM yok; kararı insan verir (checkbox), sistem sırayı,
toplamı ve tahmini maliyet/süreyi gösterir.

Aday kuralı (deterministik):
  * durum Proposed kategorisinde (Backlog / To Do…) — tipin süreç listesinden,
    yoksa ad tabanlı yedek küme
  * tip pipeline'ın işleyebildiği türlerden (Task, Bug, User Story, PBI)
  * bu WI için açık iş (queued/running) yok; tamamlanmış iş varsa "zaten
    yapıldı", needs_info/needs_human varsa "insan bekliyor" diye ayrılır;
    failed → yeniden denenebilir (not düşülür)
Sıra: öncelik ↑, story point ↑ (küçük önce — akış), id ↑.
Kapasite: kullanıcı girer; yoksa takım velocity'si (son 3 sprint Done SP
ortalaması, Analytics) referans olarak gösterilir. Maliyet/süre tahmini
retrospektif ortalamalarından (SP başına $ ve dk; yoksa iş başına ortalama).
"""

from __future__ import annotations

import logging
from collections import defaultdict

log = logging.getLogger("pipeline")

PIPELINE_TYPES = {"Task", "Bug", "User Story", "Product Backlog Item", "Improvement"}
PROPOSED_FALLBACK = {"new", "backlog", "to do", "todo", "approved", "proposed"}
OPEN_JOB_STATUSES = {"queued", "running"}


def is_proposed(state: str, type_states: list[dict] | None) -> bool:
    """Durum 'henüz başlanmamış' mı? Tipin süreç listesi varsa kategoriye, yoksa ada bakar."""
    s = (state or "").strip().lower()
    if type_states:
        for st in type_states:
            if (st.get("name") or "").lower() == s:
                return (st.get("category") or "") == "Proposed"
        return False
    return s in PROPOSED_FALLBACK


def latest_job_status(wi_ids: list[str]) -> dict[str, dict]:
    """WI → en son işin {id, status}. DB yoksa boş."""
    if not wi_ids:
        return {}
    try:
        from agile_sdlc_crew import db
        with db.get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT j.work_item_id, j.id, j.status FROM jobs j "
                "JOIN (SELECT work_item_id, MAX(id) AS mid FROM jobs "
                "      WHERE COALESCE(kickoff_only,0)=0 AND COALESCE(dry_run,0)=0 "
                "      AND work_item_id IN (" + ",".join(["%s"] * len(wi_ids)) + ") "
                "      GROUP BY work_item_id) m ON m.mid = j.id",
                [str(w) for w in wi_ids],
            )
            return {str(r["work_item_id"]): {"id": r["id"], "status": r["status"]} for r in cur.fetchall() or []}
    except Exception as e:  # noqa: BLE001
        log.warning(f"  Sprint plan: is durumlari okunamadi: {e}")
        return {}


def classify_candidates(items: list[dict], type_states: dict[str, list[dict]],
                        jobs: dict[str, dict]) -> list[dict]:
    """Sprint iş kalemleri → aday satırları (eligible + reason)."""
    out = []
    for it in items or []:
        wid = str(it.get("id") or "")
        if not wid:
            continue
        t = it.get("type") or ""
        state = it.get("state") or ""
        job = jobs.get(wid)
        row = {
            "id": int(wid), "title": it.get("title") or "", "type": t, "state": state,
            "priority": it.get("priority") if it.get("priority") is not None else 4,
            "sp": float(it.get("storyPoints") or 0.0), "assignedTo": it.get("assignedTo") or "",
            "url": it.get("url") or "", "job": job, "eligible": False, "reason": "",
        }
        if t not in PIPELINE_TYPES:
            row["reason"] = f"tip {t or '?'} pipeline dışı"
        elif not is_proposed(state, type_states.get(t)):
            row["reason"] = f"durum '{state}' — başlanmış"
        elif job and job.get("status") in OPEN_JOB_STATUSES:
            row["reason"] = f"açık iş #{job['id']} ({job['status']})"
        elif job and job.get("status") == "completed":
            row["reason"] = f"iş #{job['id']} tamamlandı"
        elif job and job.get("status") in ("needs_info", "needs_human"):
            row["reason"] = f"iş #{job['id']} {job['status']} — insan bekliyor"
        else:
            row["eligible"] = True
            if job and job.get("status") == "failed":
                row["reason"] = f"önceki iş #{job['id']} başarısız — yeniden denenebilir"
        out.append(row)
    return out


def order_candidates(rows: list[dict]) -> list[dict]:
    """Öncelik ↑, SP ↑ (0 = bilinmiyor → sona), id ↑. Uygun olmayanlar en sonda."""
    def key(r):
        sp = r.get("sp") or 0.0
        return (0 if r.get("eligible") else 1, int(r.get("priority") or 4), 1 if sp <= 0 else 0, sp, r["id"])
    return sorted(rows, key=key)


def select_by_capacity(rows: list[dict], capacity_sp: float | None) -> list[dict]:
    """Uygun adayları sırayla işaretle; kapasite verilmişse kümülatif SP aşmasın.
    SP'si bilinmeyen (0) iş kapasiteyi tüketmez ama işaretlenir (uyarı ile)."""
    total = 0.0
    for r in rows:
        r["selected"] = False
        if not r.get("eligible"):
            continue
        sp = r.get("sp") or 0.0
        if capacity_sp is None or total + sp <= capacity_sp + 1e-9:
            r["selected"] = True
            total += sp
    return rows


def effort_estimate(rows: list[dict], *, cost_per_sp: float | None, min_per_sp: float | None,
                    cost_per_job: float | None, min_per_job: float | None) -> dict:
    sel = [r for r in rows if r.get("selected")]
    sp = sum(r.get("sp") or 0.0 for r in sel)
    unknown = sum(1 for r in sel if not (r.get("sp") or 0.0))
    cost = minutes = None
    if sel:
        if cost_per_sp and sp:
            cost = cost_per_sp * sp + (cost_per_job or 0.0) * unknown
        elif cost_per_job:
            cost = cost_per_job * len(sel)
        if min_per_sp and sp:
            minutes = min_per_sp * sp + (min_per_job or 0.0) * unknown
        elif min_per_job:
            minutes = min_per_job * len(sel)
    return {"count": len(sel), "sp": sp, "sp_unknown": unknown, "cost_usd": cost, "minutes": minutes}


def team_velocity(client, team: str, n: int = 3) -> float | None:
    """Son n tamamlanmış sprintin Done SP ortalaması (Analytics Custom_StoryPoints)."""
    try:
        from agile_sdlc_crew import sprint_report
        v = sprint_report.velocity_data(client, team, n=n)
    except Exception as e:  # noqa: BLE001
        log.warning(f"  Sprint plan: velocity alinamadi: {e}")
        return None
    done = [d for d in ((v or {}).get("done") or []) if d]
    return (sum(done) / len(done)) if done else None


def build_plan(client, iteration_path: str, *, team: str = "", capacity_sp: float | None = None,
               with_velocity: bool = True, retro_days: int = 60) -> dict:
    """Uçtan uca plan: sprint WI'ları → aday → sıra → kapasite → tahmin."""
    items = client.get_iteration_work_items(iteration_path)
    types = sorted({it.get("type") or "" for it in items if it.get("type")})
    type_states: dict[str, list[dict]] = {}
    for t in types:
        try:
            type_states[t] = [{"name": s.get("name", ""), "category": s.get("category", "")}
                              for s in client.get_work_item_type_states(t)]
        except Exception:  # noqa: BLE001
            type_states[t] = []
    jobs = latest_job_status([str(it.get("id")) for it in items if it.get("id")])
    rows = order_candidates(classify_candidates(items, type_states, jobs))

    velocity = team_velocity(client, team) if (with_velocity and team) else None
    cap = capacity_sp if capacity_sp is not None else velocity
    rows = select_by_capacity(rows, cap)

    cost_per_sp = min_per_sp = cost_per_job = min_per_job = None
    try:
        from agile_sdlc_crew import retrospective as rt
        a = rt.analyze(rt.collect(since_days=retro_days))
        cost_per_sp, min_per_sp = a.get("cost_per_sp"), a.get("min_per_sp")
        cost_per_job, min_per_job = a.get("cost_avg_completed"), a.get("minutes_avg_completed")
    except Exception as e:  # noqa: BLE001
        log.warning(f"  Sprint plan: retro ortalamalari alinamadi: {e}")
    est = effort_estimate(rows, cost_per_sp=cost_per_sp, min_per_sp=min_per_sp,
                          cost_per_job=cost_per_job, min_per_job=min_per_job)
    return {
        "iteration_path": iteration_path,
        "sprint": iteration_path.replace("/", "\\").split("\\")[-1],
        "items_total": len(items),
        "eligible": sum(1 for r in rows if r.get("eligible")),
        "rows": rows,
        "capacity_sp": cap,
        "capacity_source": "kullanıcı" if capacity_sp is not None else ("takım velocity (son 3 sprint)" if velocity else "yok"),
        "team_velocity": velocity,
        "estimate": est,
        "rates": {"cost_per_sp": cost_per_sp, "min_per_sp": min_per_sp,
                  "cost_per_job": cost_per_job, "min_per_job": min_per_job},
    }


def queue_selected(wi_rows: list[dict], *, use_hal: bool = False) -> dict:
    """Seçilen WI'ları kuyruğa al; açık işi olanı atla. {queued:[{wi,job_id}], skipped:[{wi,reason}]}."""
    from agile_sdlc_crew import db
    ids = [str(r.get("id")) for r in wi_rows if r.get("id")]
    current = latest_job_status(ids)
    queued, skipped = [], []
    for r in wi_rows:
        wid = str(r.get("id"))
        cur = current.get(wid)
        if cur and cur.get("status") in OPEN_JOB_STATUSES:
            skipped.append({"wi": wid, "reason": f"açık iş #{cur['id']} ({cur['status']})"})
            continue
        try:
            jid = db.create_job(wid, use_hal, wi_title=(r.get("title") or "")[:200])
            queued.append({"wi": wid, "job_id": jid})
        except Exception as e:  # noqa: BLE001
            skipped.append({"wi": wid, "reason": f"kuyruğa alınamadı: {e}"})
    return {"queued": queued, "skipped": skipped}
