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


def _repo_dir(root: Path, repo: str) -> Path | None:
    p = root / repo
    if p.is_dir():
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
    if not repo or not repo_ok(repo):
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
