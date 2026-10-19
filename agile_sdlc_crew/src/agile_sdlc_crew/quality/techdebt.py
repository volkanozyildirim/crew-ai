"""Teknik borç hattı — elle giriş, takımın tech-debt listesi, kuyruğa alma.

Kayıt kaynağı Azure'dur: elle girilen madde doğrudan sonraki sprintteki tek
"Teknik borç — <sprint>" story'sinin altında `tech-debt` etiketli Task olarak
açılır; liste Azure'daki etiketli WI'lardan okunur. Tempo DB'de backlog yok.
Kalite alımının ortak yolları kullanılır (intake.resolve_story(lane=),
intake._open_tasks, board dedup).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

from agile_sdlc_crew.quality import dedup, intake, render
from agile_sdlc_crew.quality.model import Group

log = logging.getLogger("pipeline")

TITLE_MAX = 120
TEXT_MAX = 4000
MAX_SP = 3
BIG_SP = 5


class Duplicate(RuntimeError):
    """Aynı teknik borç maddesi board'da zaten açık (ya da iptal edilmiş)."""


def repos_root() -> Path:
    return Path(os.environ.get("CREW_REPOS_DIR", "~/.crew_repos")).expanduser()


def manual_fingerprint(repo: str, title: str, problem: str) -> str:
    digest = hashlib.sha1(f"{title.strip()}\n{problem.strip()}".encode("utf-8")).hexdigest()[:12]
    return f"manual:{repo}:{digest}"


_REPO_NAME = re.compile(r"^[A-Za-z0-9][\w.-]*$")


def _repo_dir(root: Path, repo: str) -> Path | None:
    """Klon kökündeki repo dizini; ad yalnız [\w.-], kökten çıkamaz ('../x', '.')."""
    if not _REPO_NAME.match(repo or "") or repo in (".", ".."):
        return None
    p = root / repo
    if p.is_dir() and p.resolve().parent == root.resolve():
        return p
    low = repo.lower()
    return next((d for d in root.iterdir() if d.is_dir() and d.name.lower() == low), None) if root.is_dir() else None


def validate_manual(form: dict, *, repo_ok, repo_root: Path) -> tuple[Group | None, list[str]]:
    """Formu doğrula → (Group, []) ya da (None, [hatalar]). Hatalar Türkçe."""
    errs: list[str] = []
    repo = str(form.get("repo") or "").strip()
    title = str(form.get("title") or "").strip()
    problem = str(form.get("problem") or "").strip()
    expected = str(form.get("expected") or "").strip()
    if not repo or not _repo_dir(repo_root, repo) or not repo_ok(repo):
        errs.append("repo yerel klonlarda yok")
    if not title:
        errs.append("başlık zorunlu")
    elif len(title) > TITLE_MAX:
        errs.append(f"başlık en çok {TITLE_MAX} karakter")
    if not problem:
        errs.append("sorun açıklaması zorunlu")
    if not expected:
        errs.append("beklenen davranış / kabul kriteri zorunlu")
    if len(problem) > TEXT_MAX or len(expected) > TEXT_MAX:
        errs.append(f"metin alanları en çok {TEXT_MAX} karakter")
    files: list[str] = []
    rdir = _repo_dir(repo_root, repo) if repo and repo_ok(repo) else None
    for raw in form.get("files") or []:
        f = str(raw or "").strip()
        if not f:
            continue
        if f.startswith(("/", "\\")) or ".." in Path(f).parts or ":" in f:
            errs.append(f"geçersiz dosya yolu: {f}")
            continue
        target = (rdir / f).resolve() if rdir is not None else None
        if target is None or not target.is_relative_to(rdir.resolve()) or not target.is_file():
            errs.append(f"dosya repoda yok: {f}")
            continue
        files.append(f)
    try:
        sp = int(form.get("sp") or 1)
    except (TypeError, ValueError):
        sp = 1
    needs_human = bool(form.get("needs_human"))
    reason = "Elle insan işi olarak işaretlendi" if needs_human else ""
    if sp > MAX_SP:
        needs_human, sp, reason = True, BIG_SP, f"{MAX_SP} SP'den büyük — insan planlamalı"
    sp = max(1, sp)
    if errs:
        return None, errs
    g = Group(source="manual", repo=(rdir.name if rdir else repo), title=title,
              fingerprints=[manual_fingerprint(repo, title, problem)], findings=[], sp=sp,
              needs_human=needs_human, human_reason=reason,
              meta={"problem": problem, "expected": expected, "files": files})
    return g, []


def create_manual(client, form: dict, *, team: str = "", deps: intake.Deps | None = None,
                  repo_root: Path | None = None) -> dict:
    """Elle girilen maddeyi sonraki sprintteki 'Teknik borç' story'sinin altına aç.
    Kuyruğa ALMAZ — listede 'Kuyruğa al' ile alınır."""
    from agile_sdlc_crew import estimation
    deps = deps or intake.default_deps()
    root = repo_root or repos_root()
    g, errs = validate_manual(form, repo_ok=deps.repo_ok, repo_root=root)
    if errs:
        raise ValueError("; ".join(errs))
    if not intake._LOCK.acquire(blocking=False):
        raise intake.Busy("Başka bir alım sürüyor")
    try:
        cfg = deps.cfg()
        fp = g.fingerprints[0]
        v, prev = dedup.verdict(deps.board(client).get(fp), last_seen=0.0, now=deps.now(),
                                grace_days=int(cfg.get("grace_days", 3)))
        if v in ("blocked", "suppressed"):
            raise Duplicate(f"Bu madde board'da zaten var: #{prev}")
        story = intake.resolve_story(client, "manual", None, team, cfg.get("target_sprint") or "next",
                                     lane="techdebt")
        rows, added = intake._open_tasks(client, [g], story, cfg, deps, source="manual",
                                         lane="techdebt", queue=False)
        if added:
            estimation.write_story_points(client, str(story["id"]), int(story["sp"] + added), logger=log.info)
        row = rows[0]
        if row.get("error"):
            raise RuntimeError(row["error"])
        log.info(f"🧾 Teknik borç (elle): story #{story['id']} · Task #{row['id']} · {g.sp} SP")
        return {"task": row, "story": {"id": story["id"], "url": intake._wi_url(client, story["id"]),
                                       "created": story["created"]}}
    finally:
        intake._LOCK.release()


# ── Liste + kuyruk ────────────────────────────────────────────────────────

CLOSED_STATES = ("Done", "Closed", "Removed", "Canceled", "Cancelled", "Resolved", "Ready for Production")


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
    """Takımın alan yolundaki açık `tech-debt` etiketli WI'lar (story'ler hariç)."""
    from agile_sdlc_crew.sprint_planning import OPEN_JOB_STATUSES
    area = (client.get_team_area_path(team) or "").replace("'", "''")
    closed = ", ".join(f"'{s}'" for s in CLOSED_STATES)
    wiql = ("SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
            + (f"AND [System.AreaPath] UNDER '{area}' " if area else "")
            + f"AND [System.Tags] CONTAINS '{render.TECHDEBT_PUBLIC_TAG}' "
            f"AND [System.State] NOT IN ({closed}) AND [System.WorkItemType] <> 'User Story' "
            "ORDER BY [System.Id] DESC")
    wis = client.query_work_items(wiql, limit=300) or []
    jobs = (job_status or _job_status)([str(w.get("id")) for w in wis])
    rows = []
    for w in wis:
        f = w.get("fields") or {}
        tags = _tags(f)
        job = jobs.get(str(w.get("id")))
        human = render.HUMAN_TAG in tags
        busy = bool(job and job.get("status") in OPEN_JOB_STATUSES)
        rows.append({
            "id": int(w.get("id")), "title": f.get("System.Title", ""), "type": f.get("System.WorkItemType", ""),
            "state": f.get("System.State", ""),
            "iteration": (f.get("System.IterationPath") or "").rsplit("\\", 1)[-1],
            "sp": _sp(f), "repo": render.repo_from_description(f.get("System.Description") or ""),
            "source": "manual" if render.src_tag("manual") in tags else "azure",
            "needs_human": human, "job": job, "queueable": not human and not busy,
        })
    rows.sort(key=lambda r: (not r["queueable"], -r["id"]))
    return {"items": rows, "area": area.replace("''", "'")}


def queue(client, ids: list, *, team: str = "", max_queue: int = 5, job_status=None, queue_fn=None) -> dict:
    """Listede görünen WI'ları kuyruğa al. İnsan işi ve liste dışı reddedilir; açık
    işi olanı sprint_planning.queue_selected atlar; istek başı en çok max_queue."""
    if queue_fn is None:
        from agile_sdlc_crew.sprint_planning import queue_selected as queue_fn
    listed = {r["id"]: r for r in list_items(client, team=team, job_status=job_status)["items"]}
    skipped, take = [], []
    for raw in ids or []:
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
        elif len(take) >= max(1, int(max_queue)):
            skipped.append({"wi": str(wid), "reason": f"istek başı tavan ({max_queue})"})
        else:
            take.append({"id": wid, "title": row["title"]})
    res = queue_fn(take) if take else {"queued": [], "skipped": []}
    return {"queued": res.get("queued", []), "skipped": skipped + list(res.get("skipped", []))}
