"""Kalite işi alımı — orkestratör.

  gather (ağ) → build_candidates (saf: repo filtresi, eşik, dedup, birleştirme/paket)
  → rank.select_budget → [take] story bul/aç → child Task'lar → kuyruk → quality_items

Seçimde LLM yok. take() tam otonomdur (kullanıcı kararı): onay listesi yok;
yanlış tıklamaya karşı tek koruma CREW_QUALITY_INTAKE anahtarı ve bütçe/Task
tavanlarıdır. Aynı anda tek alım (_LOCK) — ikisi aynı parmak izini açmasın.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Callable, NamedTuple

from agile_sdlc_crew import pipeline_config
from agile_sdlc_crew.quality import app_map, dedup, newrelic, rank, render, sonar
from agile_sdlc_crew.quality.model import Finding, Group

log = logging.getLogger("pipeline")

SOURCES = ("newrelic", "sonar")
TASK_FIELDS_PATH = Path(__file__).resolve().parent.parent / "config" / "quality_task_fields.yaml"
_LOCK = threading.Lock()
_DONE = {"done", "closed", "completed", "removed", "canceled", "cancelled"}


class Busy(RuntimeError):
    """Başka bir kalite alımı sürüyor."""


class NoSprint(RuntimeError):
    """Takımın hedef (sonraki ya da aktif) sprinti yok ve story_id verilmedi."""


class Deps(NamedTuple):
    gather: Callable
    board: Callable
    repo_ok: Callable
    queue: Callable
    record: Callable
    now: Callable
    cfg: Callable


def load_cfg() -> dict:
    g = pipeline_config.get
    return {
        "nr_days": int(g("CREW_QUALITY_NR_LOOKBACK_DAYS") or 7),
        "nr_min_errors": int(g("CREW_QUALITY_NR_MIN_ERRORS") or 100),
        "pack_sp": int(g("CREW_QUALITY_SONAR_PACK_SP") or 3),
        "pack_files": int(g("CREW_QUALITY_SONAR_PACK_FILES") or 5),
        "human_max": int(g("CREW_QUALITY_HUMAN_MAX") if g("CREW_QUALITY_HUMAN_MAX") is not None else 5),
        "max_tasks": int(g("CREW_QUALITY_MAX_TASKS") or 8),
        "grace_days": int(g("CREW_QUALITY_REOPEN_GRACE_DAYS") if g("CREW_QUALITY_REOPEN_GRACE_DAYS") is not None else 3),
        "sonar_url": sonar.base_url(),
        "task_fields": load_task_fields(),
        "target_sprint": str(g("CREW_QUALITY_TARGET_SPRINT") or "next").strip().lower(),
    }


def load_task_fields(path: Path = TASK_FIELDS_PATH) -> dict:
    """Org'un Task tipindeki zorunlu özel alanları (ör. Custom.ARGEType)."""
    import yaml
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return {str(k): v for k, v in data.items()} if isinstance(data, dict) else {}


def repo_is_local(repo: str, base: Path | None = None) -> bool:
    """Pipeline yalnızca ~/.crew_repos altında klonu olan repoda çalışabilir."""
    base = base or Path(os.environ.get("CREW_REPOS_DIR", "~/.crew_repos")).expanduser()
    if not repo or not base.is_dir():
        return False
    if (base / repo).is_dir():
        return True
    low = repo.lower()
    return any(p.is_dir() and p.name.lower() == low for p in base.iterdir())


def gather(source: str, client, cfg: dict) -> tuple[list[Finding], list[dict]]:
    skipped: list[dict] = []
    if source == "newrelic":
        mapping = app_map.load_map(client)
        found = newrelic.fetch(cfg["nr_days"],
                               is_prod=lambda a: newrelic.is_prod_name(a) or a.lower() in mapping)
        out: list[Finding] = []
        unmapped: Counter = Counter()
        for f in found:
            repo = app_map.resolve(f.app, mapping)
            if not repo:
                unmapped[f.app] += f.count
                continue
            f.repo = repo
            out.append(f)
        skipped += [{"app": a, "reason": "eşlenemedi", "count": n} for a, n in unmapped.most_common()]
        return out, skipped
    issues, rules = sonar.fetch_issues()
    found = sonar.issues_to_findings(issues, rules, base_url=cfg["sonar_url"])
    projects = list(getattr(client, "repo_projects", []) or [])
    out = [f for f in found if sonar.in_scope(f.project, projects)]
    skipped += [{"project": p, "reason": "kapsam dışı proje"}
                for p in sorted({f.project for f in found if not sonar.in_scope(f.project, projects)})]
    return out, skipped


def build_candidates(source: str, findings: list[Finding], *, board_index: dict, now: float,
                     repo_ok: Callable, cfg: dict) -> tuple[list[Group], list[dict]]:
    skipped: list[dict] = []
    kept: list[Finding] = []
    missing: set[str] = set()
    for f in findings:
        if repo_ok(f.repo):
            kept.append(f)
        elif f.repo not in missing:
            missing.add(f.repo)
            skipped.append({"repo": f.repo, "reason": "repo yerelde yok"})
    if source == "newrelic":
        units = []
        for u in rank.nr_units(kept):
            if u.total < cfg["nr_min_errors"]:
                skipped.append({"fingerprint": u.fingerprints[0],
                                "reason": f"eşik altı (<{cfg['nr_min_errors']})"})
            else:
                units.append(u)
    else:
        units = rank.sonar_units(kept)
    live: list[Group] = []
    for u in units:
        fp = u.fingerprints[0]
        v, prev = dedup.verdict(board_index.get(fp), last_seen=u.last_seen, now=now,
                                grace_days=cfg["grace_days"])
        if v == "blocked":
            skipped.append({"fingerprint": fp, "reason": f"açık WI #{prev}"})
            continue
        if v == "suppressed":
            skipped.append({"fingerprint": fp, "reason": f"iptal edilmiş WI #{prev}"})
            continue
        if v == "recurring":
            u.recurring_of = prev
        live.append(u)
    if source == "newrelic":
        groups = rank.merge_undefined(live)
    else:
        groups = rank.pack_sonar(live, pack_sp=cfg["pack_sp"], pack_files=cfg["pack_files"])
    return groups, skipped


_GATHER_TTL = 120
_gather_cache: dict = {}


def _gather_cached(source: str, client, cfg: dict):
    """Proje listesi → önizleme → alım aynı birkaç dakikada aynı veriyi ister; NR
    sorgusu ~40 sn. Kısa TTL'li önbellek (dedup her seferinde board'dan taze)."""
    hit = _gather_cache.get(source)
    if hit and time.time() - hit[0] < _GATHER_TTL:
        return [Finding(**vars(f)) for f in hit[1]], list(hit[2])
    found, skipped = gather(source, client, cfg)
    _gather_cache[source] = (time.time(), [Finding(**vars(f)) for f in found], list(skipped))
    return found, skipped


def default_deps() -> Deps:
    from agile_sdlc_crew import db
    return Deps(
        gather=_gather_cached,
        board=lambda client: dedup.index_board(dedup.fetch_board(client)),
        repo_ok=repo_is_local,
        queue=lambda wid, title: db.create_job(str(wid), False, wi_title=title[:200]),
        record=db.record_quality_item,
        now=time.time,
        cfg=load_cfg,
    )


def _row(g: Group) -> dict:
    return {"id": None, "title": render.task_title(g), "repo": g.repo, "sp": g.sp,
            "score": round(g.score, 2), "needs_human": g.needs_human, "human_reason": g.human_reason,
            "fingerprints": g.fingerprints,
            "job_id": None}


def _filter_repos(findings: list[Finding], repos: list[str] | None) -> tuple[list[Finding], list[str]]:
    """Kullanıcının seçtiği projeler (repo adları, harf duyarsız). Boş = hepsi.
    Döner: (kalan bulgular, dışarıda kalan repolar)."""
    want = {r.strip().lower() for r in (repos or []) if r and r.strip()}
    if not want:
        return findings, []
    kept = [f for f in findings if (f.repo or "").lower() in want]
    dropped = sorted({f.repo for f in findings if (f.repo or "").lower() not in want and f.repo})
    return kept, dropped


def _plan(client, source: str, sp: int, deps: Deps, repos: list[str] | None = None) -> tuple[list[Group], dict, dict]:
    if source not in SOURCES:
        raise ValueError(f"Geçersiz kaynak: {source}")
    cfg = deps.cfg()
    findings, skipped = deps.gather(source, client, cfg)
    findings, out_of_sel = _filter_repos(findings, repos)
    skipped = list(skipped) + [{"repo": r, "reason": "seçim dışı proje"} for r in out_of_sel]
    groups, sk2 = build_candidates(source, findings, board_index=deps.board(client), now=deps.now(),
                                   repo_ok=deps.repo_ok, cfg=cfg)
    skipped += sk2
    # Bütçe ve Task tavanı yalnız OTOMATİK işlere; insan işleri ayrı grup, skora
    # göre en çok human_max (bütçeden düşmez). Eskiden aynı havuzdaydılar: 30 SP'lik
    # Sonar alımında yüksek skorlu secret'lar 8 Task tavanının 7'sini doldurdu.
    auto = [g for g in groups if not g.needs_human]
    human = sorted((g for g in groups if g.needs_human), key=lambda g: (-g.score, g.fingerprints[0]))
    human = human[:max(0, int(cfg.get("human_max", 5)))]
    max_tasks = int(cfg["max_tasks"])
    picked_auto = rank.select_budget(auto, int(sp), human_share=0.0, max_tasks=max_tasks)
    picked = picked_auto + human
    total = sum(g.sp for g in picked_auto)
    cap_reached = len(picked_auto) >= max_tasks and total < int(sp) and len(auto) > len(picked_auto)
    result = {
        "source": source, "budget_sp": int(sp), "total_sp": total,
        "candidates": len(auto), "human_candidates": sum(1 for g in groups if g.needs_human),
        "tasks": [_row(g) for g in picked_auto], "human_tasks": [_row(g) for g in human],
        "human_sp": sum(g.sp for g in human), "cap_reached": cap_reached,
        "skipped": skipped[:50],
        "skipped_counts": dict(Counter(s["reason"] if not s["reason"].startswith(("açık WI", "iptal"))
                                       else s["reason"].split(" #")[0] for s in skipped)),
        "story": None,
    }
    if cap_reached:
        result["cap_note"] = (f"Task tavanı {max_tasks} (CREW_QUALITY_MAX_TASKS) doldu: bütçenin "
                              f"{total}/{int(sp)} SP'si kullanıldı. Her Task bir pipeline işi.")
    if repos:
        result["repos"] = list(repos)
    if not picked:
        result["message"] = "Bütçeye uyan aday yok."
    return picked, result, cfg


def preview(client, source: str, sp: int, *, team: str = "", deps: Deps | None = None,
            repos: list[str] | None = None) -> dict:
    _picked, result, _cfg = _plan(client, source, sp, deps or default_deps(), repos=repos)
    return result


def projects(client, source: str, *, deps: Deps | None = None) -> dict:
    """Seçim listesi: bu kaynakta bulgusu olan projeler (repo), bulgu sayısına göre."""
    if source not in SOURCES:
        raise ValueError(f"Geçersiz kaynak: {source}")
    deps = deps or default_deps()
    findings, _sk = deps.gather(source, client, deps.cfg())
    cnt: Counter = Counter()
    for f in findings:
        if f.repo:
            cnt[f.repo] += 1
    return {"source": source, "items": [{"repo": r, "findings": n} for r, n in cnt.most_common()]}


def _wi_url(client, wid: int) -> str:
    return f"{client.org_url}/{client.project}/_workitems/edit/{wid}"


def target_iteration(iterations: list[dict], target: str = "next") -> dict | None:
    """'next': başlangıcı en erken gelecek sprint (aktif sprinte dokunulmaz;
    isteyen işi elle aktif sprinte çeker). 'current': aktif sprint."""
    def tf(i):
        return ((i.get("attributes") or {}).get("timeFrame") or "").lower()
    if target == "current":
        return next((i for i in iterations if tf(i) == "current"), None)
    future = [i for i in iterations if tf(i) == "future"]
    return min(future, key=lambda i: (i.get("attributes") or {}).get("startDate") or "9999", default=None)


def resolve_story(client, source: str, story_id: int | None, team: str, target: str = "next",
                  lane: str = "quality") -> dict:
    if story_id:
        f = (client.get_work_item(int(story_id)) or {}).get("fields") or {}
        if f.get("System.WorkItemType") != "User Story":
            raise ValueError(f"#{story_id} bir User Story değil ({f.get('System.WorkItemType') or '?'})")
        if (f.get("System.State") or "").lower() in _DONE:
            raise ValueError(f"#{story_id} kapalı ({f.get('System.State')})")
        return {"id": int(story_id), "area": f.get("System.AreaPath", ""),
                "iteration": f.get("System.IterationPath", ""), "sp": float(f.get("Custom.StoryPoints") or 0),
                "created": False}
    cur = target_iteration(client.list_iterations(team), target)
    if not cur:
        raise NoSprint("Takımın " + ("aktif" if target == "current" else "sonraki") + " sprinti yok — story_id verin")
    ipath = cur.get("path") or ""
    esc = ipath.replace("'", "''")
    tag_filter = (f"AND [System.Tags] CONTAINS '{render.TECHDEBT_TAG}' " if lane == "techdebt" else
                  f"AND [System.Tags] CONTAINS '{render.TAG}' "
                  f"AND [System.Tags] CONTAINS '{render.src_tag(source)}' ")
    found = client.query_work_items(
        "SELECT [System.Id] FROM WorkItems WHERE [System.WorkItemType] = 'User Story' "
        f"AND [System.IterationPath] = '{esc}' " + tag_filter +
        "AND [System.State] NOT IN ('Done', 'Closed', 'Removed', 'Canceled') ORDER BY [System.Id] DESC",
        limit=1)
    if found:
        f = found[0].get("fields") or {}
        return {"id": int(found[0]["id"]), "area": f.get("System.AreaPath", ""),
                "iteration": f.get("System.IterationPath", ipath),
                "sp": float(f.get("Custom.StoryPoints") or 0), "created": False}
    area = client.get_team_area_path(team) or ""
    fields = {"System.Title": render.story_title(source, cur.get("name") or ipath, lane=lane),
              "System.Description": render.story_description(source, lane=lane),
              "System.Tags": render.story_tags(source, lane=lane),
              "System.IterationPath": ipath}
    if area:
        fields["System.AreaPath"] = area
    wi = client.create_work_item("User Story", fields)
    wf = wi.get("fields") or {}
    return {"id": int(wi["id"]), "area": area or wf.get("System.AreaPath", ""), "iteration": ipath,
            "sp": 0.0, "created": True}


def _assignee(client) -> str:
    try:
        return (client.get_authenticated_user() or {}).get("uniqueName", "")
    except Exception:  # noqa: BLE001
        return ""


AC_FIELD = "Microsoft.VSTS.Common.AcceptanceCriteria"


def _create_task(client, fields: dict, parent_id: int) -> int:
    """AC alanı tipte yoksa Azure 400 döner → alansız tekrar dene (kabul kriteri açıklamada da var)."""
    try:
        return int(client.create_work_item("Task", fields, parent_id=parent_id)["id"])
    except Exception as e:  # noqa: BLE001
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code != 400 and "400" not in str(e):
            raise
        rest = {k: v for k, v in fields.items() if k != AC_FIELD}
        return int(client.create_work_item("Task", rest, parent_id=parent_id)["id"])


def _open_tasks(client, groups: list[Group], story: dict, cfg: dict, deps: Deps, *, source: str,
                lane: str = "quality", queue: bool = True) -> tuple[list[dict], int]:
    """Grupları story'nin child Task'ı olarak aç; SP yaz; (queue ise) insan işi olmayanı
    kuyruğa al; parmak izi başına quality_items kaydı. Kalite ve teknik borç ortak yolu."""
    from agile_sdlc_crew import estimation
    me = _assignee(client)
    rows: list[dict] = []
    added = 0
    for g in groups:
        row = _row(g)
        fields = {"System.Title": row["title"],
                  "System.Description": render.task_description(g, sonar_url=cfg.get("sonar_url", ""),
                                                                nr_days=cfg.get("nr_days", 7)),
                  "System.Tags": render.task_tags(g, lane=lane),
                  "System.IterationPath": story["iteration"]}
        if story.get("area"):
            fields["System.AreaPath"] = story["area"]
        if me:
            fields["System.AssignedTo"] = me
        fields[AC_FIELD] = render.acceptance_block(g)
        for k, v in (cfg.get("task_fields") or {}).items():
            fields.setdefault(k, v)
        try:
            wid = _create_task(client, fields, story["id"])
        except Exception as e:  # noqa: BLE001
            row["error"] = f"Task açılamadı: {e}"
            rows.append(row)
            continue
        row["id"] = wid
        added += g.sp
        estimation.write_story_points(client, str(wid), g.sp, logger=log.info)
        if queue and not g.needs_human:
            try:
                row["job_id"] = deps.queue(wid, row["title"])
            except Exception as e:  # noqa: BLE001
                row["error"] = f"kuyruğa alınamadı: {e}"
        for fp in g.fingerprints:
            try:
                deps.record(fingerprint=fp, source=source, repo=g.repo, story_id=story["id"], wi_id=wid,
                            job_id=row["job_id"], sp=g.sp, score=g.score, needs_human=g.needs_human)
            except Exception as e:  # noqa: BLE001
                log.warning(f"⚠️ Kalite: quality_items yazılamadı ({fp[:60]}): {e}")
        rows.append(row)
    return rows, added


def take(client, source: str, sp: int, *, story_id: int | None = None, team: str = "",
         deps: Deps | None = None, repos: list[str] | None = None) -> dict:
    from agile_sdlc_crew import estimation
    deps = deps or default_deps()
    if not _LOCK.acquire(blocking=False):
        raise Busy("Başka bir kalite alımı sürüyor")
    try:
        picked, result, cfg = _plan(client, source, sp, deps, repos=repos)
        if not picked:
            return result
        story = resolve_story(client, source, story_id, team, cfg.get("target_sprint") or "next")
        result["story"] = {"id": story["id"], "url": _wi_url(client, story["id"]), "created": story["created"]}
        rows, added = _open_tasks(client, picked, story, cfg, deps, source=source)
        if added:
            estimation.write_story_points(client, str(story["id"]), int(story["sp"] + added), logger=log.info)
        result["tasks"] = [r for r in rows if not r["needs_human"]]
        result["human_tasks"] = [r for r in rows if r["needs_human"]]
        result["total_sp"] = sum(r["sp"] for r in result["tasks"] if r.get("id"))
        result["human_sp"] = sum(r["sp"] for r in result["human_tasks"] if r.get("id"))
        log.info(f"🧹 Kalite alımı ({source}): story #{story['id']} · {len(rows)} Task · {added} SP")
        return result
    finally:
        _LOCK.release()


def unmapped(client, source: str = "newrelic") -> dict:
    """Eşlenemeyen NR uygulamaları / kapsam dışı Sonar projeleri (yazma yok)."""
    deps = default_deps()
    _f, skipped = deps.gather(source, client, deps.cfg())
    return {"source": source, "items": skipped}
