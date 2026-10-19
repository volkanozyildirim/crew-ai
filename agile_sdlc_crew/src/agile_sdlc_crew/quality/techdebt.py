"""Teknik borç hattı — takımın tech-debt listesi ve kuyruğa alma.

Kayıt kaynağı Azure'dur: liste takımın alan yolundaki açık `tech-debt` etiketli
Task / Bug / Improvement'lardan okunur; Tempo DB'de backlog tutulmaz. Seçilen
iş sprint_planning.queue_selected ile kuyruğa alınır. (Elle giriş formu
kullanıcı kararıyla kaldırıldı — madde Azure'da etiketlenerek eklenir.)
Taramalardan gelen maddeler (Sonar smell, php_modern…) sonraki aşamalarda
kalite alımının `techdebt` hattıyla (intake.resolve_story(lane=)) açılacak.
"""

from __future__ import annotations

import logging

from agile_sdlc_crew.quality import intake, render

log = logging.getLogger("pipeline")


# ── Liste + kuyruk ────────────────────────────────────────────────────────

CLOSED_STATES = ("Done", "Closed", "Removed", "Canceled", "Cancelled", "Resolved", "Ready for Production")
LIST_TYPES = ("Task", "Bug", "Improvement")
# Son işi bu durumlardaysa yeniden kuyruğa alınmaz: koşuyor / PR'ı açık / insan
# bekliyor. Tekrar denemeye yalnız 'failed' (ve hiç işi olmayan) açık.
NOT_QUEUEABLE_JOB = {"queued", "running", "completed", "needs_info", "needs_human"}


def _tags(f: dict) -> set[str]:
    return {t.strip().lower() for t in (f.get("System.Tags") or "").split(";") if t.strip()}


def _sp(f: dict):
    for k in ("Custom.StoryPoints", "Microsoft.VSTS.Scheduling.StoryPoints"):
        if f.get(k) not in (None, ""):
            try:
                return float(f[k])
            except (TypeError, ValueError):
                pass
    return None


def _job_status(ids: list[str]) -> dict:
    from agile_sdlc_crew.sprint_planning import latest_job_status
    return latest_job_status(ids)


def list_items(client, *, team: str = "", job_status=None) -> dict:
    """Takımın alan yolundaki açık `tech-debt` etiketli Task/Bug/Improvement'lar."""
    area = (client.get_team_area_path(team) or "").replace("'", "''")
    if not area:
        # Boş alan = filtresiz WIQL = proje geneli; başka takımların işleri listelenir.
        raise ValueError("Takım seçin — takımın alan yolu çözülemedi")
    closed = ", ".join(f"'{s}'" for s in CLOSED_STATES)
    types = ", ".join(f"'{t}'" for t in LIST_TYPES)
    wiql = ("SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
            f"AND [System.AreaPath] UNDER '{area}' "
            f"AND [System.Tags] CONTAINS '{render.TECHDEBT_PUBLIC_TAG}' "
            f"AND [System.State] NOT IN ({closed}) AND [System.WorkItemType] IN ({types}) "
            "ORDER BY [System.Id] DESC")
    wis = client.query_work_items(wiql, limit=300) or []
    jobs = (job_status or _job_status)([str(w.get("id")) for w in wis])
    rows = []
    for w in wis:
        f = w.get("fields") or {}
        tags = _tags(f)
        job = jobs.get(str(w.get("id")))
        human = render.HUMAN_TAG in tags
        busy = bool(job and job.get("status") in NOT_QUEUEABLE_JOB)
        rows.append({
            "id": int(w.get("id")), "title": f.get("System.Title", ""), "type": f.get("System.WorkItemType", ""),
            "state": f.get("System.State", ""),
            "iteration": (f.get("System.IterationPath") or "").rsplit("\\", 1)[-1],
            "sp": _sp(f), "repo": render.repo_from_description(f.get("System.Description") or ""),
            "needs_human": human, "job": job, "queueable": not human and not busy,
        })
    rows.sort(key=lambda r: (not r["queueable"], -r["id"]))
    return {"items": rows, "area": area.replace("''", "'")}


def queue(client, ids: list, *, team: str = "", max_queue: int = 5, job_status=None, queue_fn=None) -> dict:
    """Listede görünen WI'ları kuyruğa al. İnsan işi ve liste dışı reddedilir; açık
    işi olanı sprint_planning.queue_selected atlar; istek başı en çok max_queue."""
    if queue_fn is None:
        from agile_sdlc_crew.sprint_planning import queue_selected as queue_fn
    # Çift tıklama / aynı id'nin tekrarı aynı WI için birden çok iş (paralel PR) açmasın
    if not intake._LOCK.acquire(blocking=False):
        raise intake.Busy("Başka bir alım / kuyruk isteği sürüyor")
    try:
        return _queue_locked(client, list(dict.fromkeys(ids or [])), team, max_queue, job_status, queue_fn)
    finally:
        intake._LOCK.release()


def _queue_locked(client, ids, team, max_queue, job_status, queue_fn) -> dict:
    listed = {r["id"]: r for r in list_items(client, team=team, job_status=job_status)["items"]}
    skipped, take = [], []
    for raw in ids:
        try:
            wid = int(raw)
        except (TypeError, ValueError):
            skipped.append({"wi": str(raw), "reason": "geçersiz id"})
            continue
        row = listed.get(wid)
        if row is None:
            skipped.append({"wi": str(wid), "reason": "teknik borç listesinde yok"})
        elif row["needs_human"]:
            skipped.append({"wi": str(wid), "reason": "insan işi (needs-human) — kuyruğa alınmaz"})
        elif not row["queueable"]:
            j = row.get("job") or {}
            skipped.append({"wi": str(wid), "reason": f"son iş #{j.get('id')} {j.get('status')} — yeniden alınmaz"})
        elif len(take) >= max(1, int(max_queue)):
            skipped.append({"wi": str(wid), "reason": f"istek başı tavan ({max_queue})"})
        else:
            take.append({"id": wid, "title": row["title"]})
    res = queue_fn(take) if take else {"queued": [], "skipped": []}
    return {"queued": res.get("queued", []), "skipped": skipped + list(res.get("skipped", []))}
