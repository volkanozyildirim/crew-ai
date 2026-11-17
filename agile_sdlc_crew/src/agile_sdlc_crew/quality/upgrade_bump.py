"""Sürüm adımı: kayıttaki sitelerin tek satırlık sürüm değişikliği ve taslak PR'ı.

Üretim altyapı repolarına (FLODevOps, yml-files) yazan en riskli kod: bir site yalnız
`line` satırındaki `old` metnini `new` ile değiştirir, başka hiçbir bayt değişmez; değişmezse
(dosya o arada düzenlenmişse) yazılmaz. `old` ile `new` yalnız rakam dizilerinde ayrışabilir.
Önce HER dosya hedef daldan HAM bayt olarak okunup uygulanır, dal durumu doğrulanır, ancak ondan
sonra yazılmaya başlanır — geç bir hata yarım push bırakmaz. Yazılan dal yalnız Tempo'nun kendi
`tempo/upgrade-…` dalıdır; hedef dala (ör. production) asla doğrudan yazılmaz. Dal ya bu çalışmada
doğrulanan hedef commit'te YENİ açılır ya da hedefle aynı commit'te duran boş artıktır; içinde
başka commit olan hiçbir dala push yapılmaz (mevcut Tempo PR'ı yalnız içeriği doğrulanıp aynen
döndürülür). Dosyalar base64 ile gönderilir: baytlar (BOM/CRLF) aynen. push_sites sunucuya ve db'ye
bağımlı değil; iş kaydı (queue_step / run) db'yi yalnız enjekte edilmediğinde, fonksiyon içinde açar.
"""
from __future__ import annotations

import base64
import difflib
import html
import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

import requests

from agile_sdlc_crew.quality.modern_fix import _REPO
from agile_sdlc_crew.quality.render import mask_secrets

# Yalnız sürüm benzeri metin: harf/rakam/_ . - : / ' " > = ^ ~ | , boşluk. Satır sonu, ; $ ` ( ) yok.
_SAFE_NEW = re.compile(r"[\w.\-:/'\">=^~|, ]+", re.ASCII)
# Tempo'nun kendi dalı: tempo/upgrade-<dil>-<repo>-<büyük>.<küçük>
_BRANCH = re.compile(r"tempo/upgrade-[a-z]+-[A-Za-z0-9][\w.-]*-\d+\.\d+", re.ASCII)
_NOT_PUSHED_MSG = "kısıt elle düzenlenmeli"


class SiteChanged(ValueError):
    """Site artık kayıttaki gibi değil (satır değişmiş / metin belirsiz / yeni metin güvensiz)."""


class _Denied(Exception):
    """Yazma yetkisi yok (dal oluşturma 200 + PermissionRequired dahil)."""


def branch_name(lang: str, repo: str, to: str) -> str:
    return f"tempo/upgrade-{lang}-{repo}-{to}"


def _digit_shape(s: str) -> str:
    return re.sub(r"\d+", "#", s)


def apply_site(text: str, site: dict) -> str:
    """`site["line"]` (1 tabanlı) satırında `site["old"]` tam bir kez geçmeli; yalnız o satır değişir."""
    old, new = site.get("old") or "", site.get("new") or ""
    try:
        line_no = int(site.get("line") or 0)
    except (TypeError, ValueError):
        line_no = 0
    lines = text.splitlines(keepends=True)
    if not old or "\n" in old or "\r" in old:
        raise SiteChanged("Sitenin eski metni geçersiz.")
    if old == new:
        raise SiteChanged("Eski ve yeni metin aynı; yapılacak değişiklik yok.")
    if not _SAFE_NEW.fullmatch(new):
        raise SiteChanged(f"Yeni metin yalnız sürüm benzeri karakterler içerebilir: {new[:60]!r}")
    if _digit_shape(old) != _digit_shape(new):
        raise SiteChanged(f"'{old}' → '{new}': eski ve yeni metin yalnız rakamlarda ayrışabilir.")
    if not 1 <= line_no <= len(lines):
        raise SiteChanged(f"{site.get('path')}: {line_no}. satır dosyada yok ({len(lines)} satır).")
    line = lines[line_no - 1]
    if line.count(old) != 1:
        raise SiteChanged(f"{site.get('path')}:{line_no}: '{old}' satırda {line.count(old)} kez geçiyor "
                          "(tam bir kez olmalı) — dosya değişmiş olabilir.")
    out = list(lines)
    out[line_no - 1] = line.replace(old, new)
    # Güvence: yalnız o satır farklı olmalı.
    diff = [i for i, (a, b) in enumerate(zip(lines, out)) if a != b]
    if len(out) != len(lines) or diff != [line_no - 1]:
        raise SiteChanged(f"{site.get('path')}: değişiklik tek satırın dışına taştı; yazılmadı.")
    return "".join(out)


def _norm_path(path: str) -> str:
    return "/" + str(path or "").lstrip("/")


def _validate_branch(branch: str, targets: set[str]) -> None:
    if (not isinstance(branch, str) or not _BRANCH.fullmatch(branch)
            or ".." in branch or "refs/" in branch):
        raise ValueError(f"Geçersiz dal adı: {str(branch)[:80]!r} "
                         "(tempo/upgrade-<dil>-<repo>-<sürüm> olmalı).")
    if branch in targets:
        raise ValueError(f"'{branch}' bir hedef dal; hedef dala doğrudan yazılmaz.")


def _validate(sites: list[dict], branch: str) -> None:
    if not sites:
        raise ValueError("Yazılacak site yok.")
    seen: set[tuple] = set()
    for s in sites:
        if s.get("blocked"):
            raise ValueError(f"Engelli site var ({s.get('repo')} {s.get('path')}): {s['blocked']}")
        if not s.get("line"):
            raise ValueError(f"Satırı bilinmeyen site var ({s.get('repo')} {s.get('path')}).")
        repo, path = str(s.get("repo") or ""), str(s.get("path") or "")
        if not _REPO.match(repo):
            raise ValueError(f"Geçersiz repo adı: {repo!r}")
        norm = _norm_path(path)
        parts = norm.split("/")[1:]
        if (not norm.strip("/") or any(p in ("", ".", "..") or p.startswith("-") for p in parts)
                or "\\" in path or any(ord(c) < 32 for c in path)):
            raise ValueError(f"Geçersiz dosya yolu: {path!r}")
        if not s.get("branch"):
            raise ValueError(f"Hedef dal yok ({repo}).")
        if _digit_shape(str(s.get("old") or "")) != _digit_shape(str(s.get("new") or "")):
            raise ValueError(f"{repo} {path}: '{s.get('old')}' → '{s.get('new')}' yalnız rakam değişimi değil — "
                             f"{_NOT_PUSHED_MSG}, otomatik yazılmaz.")
        key = (s.get("project") or "", repo, norm, int(s["line"]))
        if key in seen:
            raise ValueError(f"Aynı satır için iki site var ({repo} {path}:{s['line']}).")
        seen.add(key)
    _validate_branch(branch, {str(s["branch"]) for s in sites})


def _is_denied(exc: Exception) -> bool:
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) in (401, 403)


def _diff_text(files: list[tuple[str, str, str]]) -> str:
    """Birleşik diff (a/ b/ başlıklı); satır sonu olmayan satıra standart işaretçi; sırlar maskeli."""
    out: list[str] = []
    for path, old, new in files:
        p = path.lstrip("/")
        for line in difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                         fromfile=f"a/{p}", tofile=f"b/{p}"):
            out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return mask_secrets("".join(out))


def _read_text(client, repo: str, path: str, branch: str, proj) -> str | None:
    raw = client.get_file_bytes(repo, path, branch, project=proj)
    if not raw:
        return None
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise SiteChanged(f"{repo} {path}: dosya UTF-8 değil; yazılmadı.")


def _branch_has(client, repo: str, branch: str, files: list, proj) -> bool:
    """Dalda planlanan dosyaların hepsi zaten bizim yeni içeriğe eşit mi (bayt bayt)?"""
    for path, _old, new in files:
        raw = client.get_file_bytes(repo, _norm_path(path), branch, project=proj)
        if raw is None or raw != new.encode("utf-8"):
            return False
    return True


def push_sites(client, sites: list[dict], *, branch: str, title: str, body: str,
               wi_id: int | None) -> dict:
    """(proje, repo) başına tek commit + tek taslak PR. Yetki yoksa (401/403) PR yerine diff döner.
    Yazma aşamasındaki hata o repoyu `errors`'a düşürür, öncekilerin PR'ları kaybolmaz.
    -> {"prs": [{"repo","id","url"}], "fallback": [{"repo","project","diff"}],
        "errors": [{"repo","project","error"}]}"""
    _validate(sites, branch)

    groups: dict[tuple[str, str], list[dict]] = {}
    for s in sites:
        groups.setdefault((s.get("project") or "", s["repo"]), []).append(s)

    me_cache: list = []

    def _me():
        if not me_cache:
            try:
                me_cache.append(str((client.get_authenticated_user() or {}).get("id") or ""))
            except Exception:
                me_cache.append("")
        return me_cache[0]

    # 1) Hepsini oku + uygula + dal durumunu doğrula — henüz yazma yok.
    plans = []
    for (project, repo), grp in groups.items():
        target = grp[0]["branch"]
        if any(g["branch"] != target for g in grp):
            raise ValueError(f"{repo}: siteler farklı hedef dallara gidiyor.")
        proj = project or None
        by_path: dict[str, list[dict]] = {}
        for g in grp:
            by_path.setdefault(_norm_path(g["path"]), []).append(g)
        files = []
        for path, group_sites in by_path.items():
            old_text = _read_text(client, repo, path, target, proj)
            if old_text is None:
                raise SiteChanged(f"{repo} {path}: dosya hedef dalda yok ya da boş; yazılmadı.")
            new_text = old_text
            for g in sorted(group_sites, key=lambda x: -int(x["line"])):
                new_text = apply_site(new_text, dict(g, path=path))
            files.append((path, old_text, new_text))
        refs = {b.get("name"): b.get("objectId") for b in client.list_branches(repo, project=proj)}
        target_oid = refs.get(f"refs/heads/{target}")
        if not target_oid:
            raise ValueError(f"{repo}: hedef dal '{target}' bulunamadı.")
        create, push, existing = True, True, None
        if f"refs/heads/{branch}" in refs:
            create = False
            pr = client.find_pull_request(repo, branch, project=proj)
            if pr:
                if pr.get("status") != "active":
                    raise ValueError(f"{repo}: '{branch}' dalının PR'ı kapanmış ({pr.get('status')}); "
                                     "dokunulmadı.")
                if pr.get("target") != target:
                    raise ValueError(f"{repo}: '{branch}' dalındaki açık PR başka bir dala gidiyor "
                                     f"({pr.get('target')} ≠ {target}); dokunulmadı.")
                me = _me()
                if not me:
                    raise ValueError(f"{repo}: Tempo kimliği çözülemedi; mevcut '{branch}' dalı "
                                     "yeniden kullanılmadı.")
                if pr.get("created_by") != me:
                    raise ValueError(f"{repo}: '{branch}' dalındaki PR'ı Tempo açmamış; dokunulmadı.")
                # Mevcut dala ASLA push yok: içerik tam bizim planımızsa PR olduğu gibi döner.
                if not _branch_has(client, repo, branch, files, proj):
                    raise ValueError(f"{repo}: PR dalı güncel değil — PR #{pr.get('id')}'i kapatıp "
                                     f"'{branch}' dalını silin, yeniden başlatın; dala dokunulmadı.")
                existing, push = pr, False
            elif refs[f"refs/heads/{branch}"] == target_oid:
                push = True                      # önceki denemeden kalan boş dal
            else:
                raise ValueError(f"{repo}: '{branch}' dalı var, PR'ı yok ve hedef dalla aynı commit'te "
                                 f"değil (içinde başka commit olabilir); dala dokunulmadı. Kalan "
                                 f"'{branch}' dalını silin ve yeniden deneyin.")
        plans.append((project, repo, target, files, existing, create, push, target_oid))

    # 2) Yaz — her repo kendi hatasını yakalar; başarılılar kaybolmaz.
    prs: list[dict] = []
    fallback: list[dict] = []
    errors: list[dict] = []
    for project, repo, target, files, existing, create, push, base_oid in plans:
        proj = project or None
        changes = [{"changeType": "edit", "path": path, "contentType": "base64encoded",
                    "content": base64.b64encode(new.encode("utf-8")).decode("ascii")}
                   for path, _old, new in files]
        stage, written = "create", False   # hata nerede oldu / dal bizim tarafımızdan yazıldı mı
        try:
            if create:
                res = client.create_branch(repo, branch, target, project=proj, source_object_id=base_oid)
                vals = res.get("value") if isinstance(res, dict) else None
                first = vals[0] if vals else {}
                if not first.get("success"):
                    status = str(first.get("updateStatus") or "")
                    if "PermissionRequired" in status:
                        raise _Denied()
                    raise ValueError(f"Dal oluşturulamadı ({status or 'yanıt okunamadı'}).")
                written = True
            stage = "push"
            if push:
                client.push_changes(repo, branch, changes, title, project=proj, old_object_id=base_oid)
                written = True
            if existing is not None:
                prs.append({"repo": repo, "id": existing["id"], "url": existing.get("url", ""),
                            "reused": True})
                continue
            stage = "pr"
            res = client.create_pull_request(
                repo, branch, target, title=title, description=body,
                work_item_ids=[wi_id] if (wi_id and not project) else None,
                draft=True, project=proj)
            pid = res.get("pullRequestId")
            name = (res.get("repository") or {}).get("name") or repo
            url = res.get("url") or ""
            base = getattr(client, "org_url", None)  # gerçek istemcide var; sahte istemcide yok
            if base:
                url = f"{base}/{project or getattr(client, 'project', '')}/_git/{name}/pullrequest/{pid}"
            prs.append({"repo": repo, "id": pid, "url": url})
        except Exception as e:  # noqa: BLE001 — bir repo'nun hatası öncekilerin PR'ını kaybettirmesin
            where = {"stage": stage, "branch_written": written}
            if isinstance(e, _Denied) or (isinstance(e, requests.HTTPError) and _is_denied(e)):
                fallback.append({"repo": repo, "project": project, "diff": _diff_text(files), **where})
            else:
                errors.append({"repo": repo, "project": project, "error": mask_secrets(str(e))[:800],
                               **where})
    return {"prs": prs, "fallback": fallback, "errors": errors}


# ── Sürüm adımı işi (job_kind=upgrade_bump) ──────────────────────────────
# Bir iş TEK adım atar ve biter; beklemez, yoklamaz. Hangi adımın sırada olduğu ve ön koşulu
# `upgrade_track.status`'tan gelir: queue_step iş açmadan, run push'tan önce aynı hesabı yapar.

JOB_KIND = "upgrade_bump"
STEPS = ("ci", "prod", "code")
_QUEUE_LOCK = threading.Lock()
_VER = re.compile(r"^\d+\.\d+$")
log = logging.getLogger("pipeline")


class StepChanged(ValueError):
    """Kullanıcının onayladığı adım ile sunucunun şimdi hesapladığı sıradaki adım farklı (ya da aynı
    izleme işi için açık bir adım işi var); uç bunu 409'a çevirir."""


class StepFailed(ValueError):
    """Adım atılmadı (ön koşul / yeniden doğrulama / yazma hatası); neden izleme işine yazıldı."""


def pushable(site: dict) -> bool:
    """Site push_sites'a verilebilir mi: engelsiz, satırı bilinen, yalnız rakam dizisi değişen."""
    old, new = str(site.get("old") or ""), str(site.get("new") or "")
    return (not site.get("blocked") and bool(site.get("line")) and bool(old) and old != new
            and _digit_shape(old) == _digit_shape(new))


def _ts(s):
    """Azure zaman damgası (ISO, 7 haneye kadar kesir, Z/±hh:mm) → aware datetime; okunamazsa None."""
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$", str(s or "").strip())
    if not m:
        return None
    dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(
        microsecond=int((m.group(2) or "0")[:6].ljust(6, "0")))
    tz = m.group(3)
    if not tz or tz == "Z":
        return dt.replace(tzinfo=timezone.utc)
    sign = 1 if tz[0] == "+" else -1
    return dt.replace(tzinfo=timezone(sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[4:6]))))


def _partial_ok(client, b: dict) -> bool:
    """Kısmi başarılı build yalnız test dışı bir adımda uyarı verdiyse yeşil — pipeline kapısı ve
    modern_fix ile aynı kural (flow._build_issues_block). Okuma hatası yükselir (kanıt sayılmaz)."""
    from agile_sdlc_crew.flow import _build_issues_block
    return not _build_issues_block(client.get_build_issue_tasks(b.get("project"), int(b.get("build_id"))))


_UNSET = object()


def ci_evidence(client, repo: str, since: str, *, wait_h: int, now=None, definition=_UNSET) -> dict:
    """Test hattı dosyasının son değişikliğinden (`since`) SONRA kuyruğa girmiş `<repo>-test` build'inden kanıt.
    Kanıt build TANIMINA bağlıdır (lint / Sonar gibi başka PR hatları sayılmaz): tanım yok → no_pipeline;
    tanımın since'ten kesin sonra kuyruğa girmiş tamamlanmış build'i yok → none; en yenisi yeşil (ya da yalnız
    test dışı adımda uyarılı kısmi başarı) → green; değilse red + hata özeti. API hatası yükselir.
    `since=None`: saat yok (repoda test hattı sabitlemesi yok) — tanımın en yeni tamamlanmış build'i alınır.
    `definition`: taramanın çözdüğü tanım (`get_test_pipeline` sonucu ya da None) — verilirse ikinci ad araması
    yapılmaz; verilmezse `client.get_test_pipeline` ile çözülür.
    -> {"state", "build": {"id","url","queue_time"}|None, "summary", "overdue", "since"}"""
    t0 = None
    if since is not None:
        t0 = _ts(since)
        if t0 is None:
            raise ValueError(f"Kanıt saati okunamadı ({str(since)[:40]!r}).")
    name = f"{repo}-test"
    d = client.get_test_pipeline(repo) if definition is _UNSET else definition
    if not d:
        return {"state": "no_pipeline", "build": None, "overdue": False, "since": since,
                "summary": f"'{name}' build tanımı yok."}
    builds = client.list_definition_builds(d["project"], d["id"], min_time=since or "", top=50) or []
    done = sorted(((t, b) for b in builds
                   if (t := _ts(b.get("queue_time"))) is not None and (t0 is None or t > t0)  # minTime'a güvenilmez
                   and b.get("status") == "completed" and b.get("result") not in (None, "", "canceled")),
                  key=lambda x: x[0], reverse=True)
    if not done:
        overdue = False
        if t0 is not None:
            now = now or datetime.now(timezone.utc)
            overdue = (now - t0).total_seconds() > max(0, int(wait_h)) * 3600
        return {"state": "none", "build": None, "overdue": overdue, "since": since,
                "summary": (f"Test hattı değişikliğinden sonra kuyruğa girmiş tamamlanmış '{d.get('name')}' build'i yok."
                            if t0 is not None else f"'{d.get('name')}' tanımının tamamlanmış build'i yok.")}
    b = done[0][1]
    blk = {"id": b.get("build_id"), "url": b.get("url") or "", "queue_time": b.get("queue_time") or ""}
    if b.get("result") == "succeeded" or (b.get("result") == "partiallySucceeded" and _partial_ok(client, b)):
        return {"state": "green", "build": blk, "overdue": False, "since": since,
                "summary": f"Build #{b.get('build_id')} ({d.get('name')}) yeşil."}
    try:
        summ = client.get_build_failure_summary(b.get("project") or d["project"], int(b.get("build_id")), 3000) or ""
    except Exception as e:  # noqa: BLE001 — özet okunamasa da durum kırmızı kalır (kapı kapalı)
        summ = f"(özet okunamadı: {e})"
    summ = mask_secrets(summ or f"Build #{b.get('build_id')} ({d.get('name')}) {b.get('result')}")
    return {"state": "red", "build": blk, "summary": summ, "overdue": False, "since": since}


def step_key(lang: str, repo: str, to: str, step: str) -> str:
    """Adımın DevOps isteği anahtarı (devops_request `tempo-devops-key:` satırı); status bununla devri görür."""
    return f"upgrade:{lang}:{repo}:{to}:{step}"


def site_diffs(client, sites: list[dict]) -> list[dict]:
    """Push'suz (CREW_TECHDEBT_UPGRADE_BUMP kapalı): (proje, repo) başına sitelerin uygulanmış diff'i.
    Siteler apply_site'tan geçer; dosya değişmişse SiteChanged."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for s in sites:
        groups.setdefault((s.get("project") or "", s["repo"]), []).append(s)
    out = []
    for (project, repo), grp in groups.items():
        by_path: dict[str, list[dict]] = {}
        for g in grp:
            by_path.setdefault(_norm_path(g["path"]), []).append(g)
        files = []
        for path, ss in by_path.items():
            old_text = _read_text(client, repo, path, ss[0]["branch"], project or None)
            if old_text is None:
                raise SiteChanged(f"{repo} {path}: dosya hedef dalda yok ya da boş.")
            new_text = old_text
            for g in sorted(ss, key=lambda x: -int(x["line"])):
                new_text = apply_site(new_text, dict(g, path=path))
            files.append((path, old_text, new_text))
        out.append({"repo": repo, "project": project, "diff": _diff_text(files)})
    return out


def _payload(job: dict | None) -> dict:
    from agile_sdlc_crew.quality import modern
    if (job or {}).get("job_kind") != JOB_KIND:
        raise StepFailed(f"İş bir sürüm adımı değil ({(job or {}).get('job_kind')!r}).")
    try:
        p = json.loads(job.get("job_payload") or "")
    except ValueError:
        raise StepFailed("İşin kapsamı okunamadı.")
    ok = (isinstance(p, dict) and p.get("lang") in modern.LANGS and isinstance(p.get("repo"), str)
          and bool(_REPO.match(p["repo"])) and ".." not in p["repo"]
          and all(isinstance(p.get(k), str) and _VER.match(p[k]) for k in ("from", "to"))
          and p.get("step") in STEPS and isinstance(p.get("sites"), list)
          and all(isinstance(s, dict) for s in p["sites"]) and str(p.get("track_id") or "").isdigit())
    if not ok:
        raise StepFailed("İşin kapsamı geçersiz (dil / repo / sürüm / adım / siteler / izleme işi).")
    return p


def _site_key(s: dict) -> tuple:
    try:
        line = int(s.get("line") or 0)
    except (TypeError, ValueError):
        line = -1
    return (str(s.get("where") or ""), s.get("project") or "", str(s.get("repo") or "").lower(),
            _norm_path(s.get("path")), str(s.get("branch") or ""), line, str(s.get("old") or ""),
            str(s.get("new") or ""))


def _esc(s) -> str:
    return html.escape(mask_secrets(str(s)))


def _open_bump_jobs(track_id) -> list:
    """Aynı izleme işi için kuyrukta / koşan `upgrade_bump` işleri (yalnız okur)."""
    from agile_sdlc_crew import db
    with db.get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM jobs WHERE work_item_id=%s AND job_kind=%s AND status IN ('queued','running')",
                    (str(track_id), JOB_KIND))
        return list(cur.fetchall())


def last_bump_job(track_id, step: str, to: str) -> dict | None:
    """Bu izleme işi + adım + hedef için EN SON `upgrade_bump` işi ({"id","status"}) ya da None (yalnız okur).
    Adım ve hedef işin kaydındaki kapsamdan (job_payload) okunur."""
    from agile_sdlc_crew import db
    with db.get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, status, job_payload FROM jobs WHERE work_item_id=%s AND job_kind=%s "
                    "ORDER BY id DESC LIMIT 500", (str(track_id), JOB_KIND))
        rows = list(cur.fetchall())
    for r in rows:
        try:
            p = json.loads(r.get("job_payload") or "")
        except ValueError:
            continue
        if isinstance(p, dict) and p.get("step") == step and p.get("to") == to:
            return {"id": r.get("id"), "status": r.get("status")}
    return None


def queue_step(client, repo: str, lang: str, *, to: str | None = None, team: str, deps=None,
               create_job=None, status_fn=None, expect_step: str | None = None, open_jobs=None, **kw) -> dict:
    """Sıradaki sürüm adımını kuyruğa al. Ön koşul yoksa ValueError(next.reason). Kapsam
    (lang, repo, from, to, step, sites, track_id, team) INSERT'te işin kaydına yazılır."""
    from agile_sdlc_crew.quality import upgrade_track as ut
    ut._check(repo, lang)
    if expect_step and expect_step not in ("ci", "prod", "code"):
        raise ValueError(f"Geçersiz adım: {expect_step!r} (ci | prod | code)")
    tr = ut.find_open(client, lang, repo)
    if not tr:
        raise ValueError("Önce yükseltmeyi başlatın — açık izleme işi yok.")
    if to and to != tr["to"]:
        raise ValueError(f"İzleme işinin hedefi {tr['to']} (istenen {to}) — hedefi değiştirmek için durdurup "
                         "yeniden başlatın.")
    to = tr["to"]
    st = (status_fn or ut.status)(client, repo, lang, to=to, deps=deps, **kw)
    nx = st.get("next") or {}
    if not nx.get("step") or not nx.get("ready"):
        raise ValueError(nx.get("reason") or "Atılabilecek sürüm adımı yok.")
    if expect_step and expect_step != nx["step"]:
        raise StepChanged(f"Durum değişti: onaylanan adım {ut.STEP_LABEL.get(expect_step, expect_step)}, şimdi sıradaki "
                          f"adım {ut.STEP_LABEL.get(nx['step'], nx['step'])} — kart yenilendi, yeniden onaylayın.")
    # Açık iş koruması yalnız open_jobs'a bağlı: create_job enjekte eden çağıran (otomatik ilerletme,
    # testler) korumayı kaybetmez; DB'ye dokunmayacak test kendi open_jobs'unu verir.
    open_jobs = open_jobs or _open_bump_jobs
    repo = st.get("repo") or repo
    payload = {"lang": lang, "repo": repo, "from": st["current"], "to": to, "step": nx["step"],
               "sites": nx["sites"], "track_id": int(tr["id"]), "team": team or ""}
    title = f"[{repo}] Sürüm adımı ({ut.STEP_LABEL[nx['step']]}) {st['current']}→{to}"
    if create_job is None:
        from agile_sdlc_crew import db
        create_job = db.create_job
    # "açık adım işi var mı → iş aç" tek kilit altında: kart düğmesi ile otomatik ilerletme aynı anda aynı
    # izleme işi için iki adım işi açmasın (kontrol ile INSERT arası yarış).
    with _QUEUE_LOCK:
        if open_jobs(tr["id"]):
            raise StepChanged(f"İzleme işi #{tr['id']} için bir sürüm adımı zaten kuyrukta ya da çalışıyor — "
                              "bitmesini bekleyin.")
        job_id = create_job(str(tr["id"]), False, title, job_kind=JOB_KIND, job_payload=json.dumps(payload))
    log.info(f"⬆️ Sürüm adımı kuyrukta: #{job_id} {nx['step']} ({lang}:{repo}:{to})")
    return {"job_id": job_id, "step": nx["step"], "track_id": int(tr["id"]), "to": to, "sites": len(nx["sites"])}


def _bodies(p: dict, test_note: str) -> tuple[str, str]:
    """PR başlığı + açıklaması (Türkçe, maskeli). `test_note` varsa açıklamanın ilk satırıdır."""
    repo, cur, to, step = p["repo"], p["from"], p["to"], p["step"]
    title = (f"Sürüm yükseltme {cur + '→' if cur != to else '→ '}{to}: "
             + {"ci": "test hattı", "prod": "üretim imajı", "code": "kod reposu sürüm kısıtı"}[step] + f" ({repo})")
    if step == "ci":
        body = f"Merge edildiğinde `{repo}` PR'ları `{to}` ile test edilir; üretim hâlâ `{cur}`."
    elif step == "prod":
        olds = sorted({str(s.get("old")) for s in p["sites"]})
        body = (f"`{repo}` üretim imajı {cur} → {to}: hedefin altındaki bütün servislerin `FROM` satırı.\n\n"
                "Merge sırası: test hattı → üretim → kod reposu. Merge ve deploy DevOps'undur.\n\n"
                f"Geri alma: FROM satırını eski imaja çevirin ({', '.join(olds)}).")
    else:
        body = f"Üretim `{to}` sürümünde; kod reposundaki sürüm kısıtı `{to}` sürümüne sıkılaştırılır."
    if test_note:
        body = test_note + "\n\n" + body
    body += f"\n\nTempo — sürüm yükseltme adımı. İzleme işi: #{p['track_id']}."
    return mask_secrets(title), mask_secrets(body)


def _devops_body(p: dict, res: dict, test_note: str, *, bump_on: bool) -> str:
    repo, cur, to = p["repo"], p["from"], p["to"]
    prs = "".join(f"<li>{_esc(x['repo'])}: <a href=\"{_esc(x.get('url', ''))}\">PR #{_esc(x.get('id'))}</a></li>"
                  for x in res.get("prs") or [])
    diffs = "".join(f"<p><b>{_esc(f.get('project') or '')} / {_esc(f['repo'])}</b></p><pre>{_esc(f['diff'])}</pre>"
                    for f in res.get("fallback") or [])
    olds = sorted({str(s.get("old")) for s in p["sites"] if s.get("where") == "devops"})
    why = ("" if bump_on else
           "<p><i>Otomatik sürüm PR'ları kapalı (CREW_TECHDEBT_UPGRADE_BUMP) — değişiklik aşağıda diff olarak.</i></p>")
    return ((f"<p><b>{_esc(test_note)}</b></p>" if test_note else "")
            + f"<p><b>Repo:</b> {_esc(repo)} · <b>Sürüm:</b> {_esc(cur)} → {_esc(to)} · "
              f"<b>İzleme işi:</b> #{_esc(p['track_id'])} · <b>Adım:</b> {_esc(p['step'])}</p>" + why
            + (f"<p><b>PR'lar (taslak)</b></p><ul>{prs}</ul>" if prs else "")
            + (f"<p><b>Uygulanacak değişiklik</b> (PR açılmadı):</p>{diffs}" if diffs else "")
            + "<p><b>Merge sırası:</b> test hattı (yml-files) → üretim (FLODevOps) → kod reposu. "
              "Merge ve deploy DevOps'undur.</p>"
            + (f"<p><b>Geri alma:</b> FROM satırını eski imaja çevirin ({_esc(', '.join(olds))}).</p>"
               if p["step"] == "prod" else ""))


def _comment(client, track_id: int, text_html: str) -> None:
    from agile_sdlc_crew.quality.upgrade_track import SIGNATURE
    try:
        client.add_comment(int(track_id), text_html + SIGNATURE)
    except Exception as e:  # noqa: BLE001 — yorum yazılamaması adımın sonucunu değiştirmez
        log.warning(f"⚠️ upgrade_bump: izleme işine yorum yazılamadı (#{track_id}): {e}")


def run(job_id: int, *, load_job=None, client=None, deps=None, status_fn=None, push_fn=None, request_fn=None,
        **kw) -> dict:
    """Tek adım: yeniden doğrula → push_sites (ya da diff) → izleme işine yorum; `prod`'da DevOps isteği.
    Başarısızlıkta neden izleme işine yazılır ve StepFailed yükselir (işçi işi failed yapar)."""
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.quality import upgrade_track as ut
    if load_job is None:
        from agile_sdlc_crew import db
        load_job = db.get_job
    p = _payload(load_job(job_id))
    if client is None:
        from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient
        client = AzureDevOpsClient()
    lang, repo, to, step, track = p["lang"], p["repo"], p["to"], p["step"], int(p["track_id"])
    label = ut.STEP_LABEL[step]

    def fail(reason: str):
        _comment(client, track, f"<p><b>Sürüm adımı ({_esc(label)}) atılmadı</b> — iş #{_esc(job_id)}.</p>"
                                f"<p>{_esc(reason)}</p><p>Hiçbir şey yazılmadı.</p>")
        raise StepFailed(reason)

    # 1) Yeniden doğrula: izleme işi hâlâ açık ve aynı hedefte; adım hâlâ sıradaki ve hazır
    #    (engelleyen bulgu / engel / PR / build kanıtı taze); siteler kayıttakiyle birebir.
    tr = ut.find_open(client, lang, repo)
    if not tr or int(tr["id"]) != track or tr["to"] != to:
        fail(f"İzleme işi #{track} artık açık değil ya da hedefi değişti.")
    try:
        st = (status_fn or ut.status)(client, repo, lang, to=to, deps=deps, **kw)
    except Exception as e:  # noqa: BLE001
        fail(f"Durum yeniden hesaplanamadı: {mask_secrets(str(e))[:500]}")
    nx = st.get("next") or {}
    if st.get("current") != p["from"]:
        fail(f"Üretim sürümü değişti ({p['from']} → {st.get('current')}); kayıttaki kapsam geçersiz.")
    if nx.get("step") != step or not nx.get("ready"):
        fail(f"Adım artık atılamaz: {nx.get('reason') or 'sıradaki adım ' + str(nx.get('step'))}")
    fresh = {_site_key(s): s for s in nx.get("sites") or []}
    mine = {_site_key(s): s for s in p["sites"]}
    if (not mine or set(fresh) != set(mine) or any(not pushable(s) for s in mine.values())
            or any(not pushable(s) for s in fresh.values())):
        gone = [f"{s.get('repo')} {s.get('path')}:{s.get('line')}" for k, s in mine.items() if k not in fresh]
        new = [f"{s.get('repo')} {s.get('path')}:{s.get('line')}" for k, s in fresh.items() if k not in mine]
        fail("Sürüm yerleri kayıttaki gibi değil (dosya değişmiş olabilir)."
             + (f" Kayıtta olup artık olmayan: {', '.join(gone)}." if gone else "")
             + (f" Yeni çıkan: {', '.join(new)}." if new else ""))
    sites = [fresh[k] for k in sorted(fresh, key=str)]  # yazılan, taze hesaplanan (kayıtla birebir) siteler
    test_note = nx.get("test_note") or ""
    title, body = _bodies(p, test_note)

    # 2) Yaz — ya da (BUMP kapalı) yalnız diff üret; push_sites hiç çağrılmaz.
    bump_on = bool(pc.get("CREW_TECHDEBT_UPGRADE_BUMP"))
    try:
        if bump_on:
            res = (push_fn or push_sites)(client, sites, branch=branch_name(lang, repo, to), title=title,
                                          body=body, wi_id=track)
        else:
            res = {"prs": [], "fallback": site_diffs(client, sites), "errors": []}
    except ValueError as e:
        fail(f"Sürüm satırı yazılmadı: {mask_secrets(str(e))[:800]}")

    # 3) DevOps isteği: üretim adımında her zaman; diğerlerinde yalnız diff (403 / BUMP kapalı) varsa.
    #    Yazma hatası varken açılmaz (eksik PR listesiyle açılırsa yeniden denemede güncellenemezdi).
    req, req_err = None, ""
    if not res.get("errors") and (step == "prod" or res.get("fallback")):
        if pc.get("CREW_DEVOPS_REQUESTS"):
            from agile_sdlc_crew import devops_request as dr
            key = step_key(lang, repo, to, step)  # status açık isteği bu anahtarla görür → adım yeniden kuyruğa girmez
            try:
                req = (request_fn or dr.open_request)(
                    client, team=pc.get("CREW_DEVOPS_TEAM") or "DevOps",
                    title=mask_secrets(f"[{repo}] Sürüm yükseltme {p['from']} → {to}: {label}"),
                    body_html=_devops_body(p, res, test_note, bump_on=bump_on), source_wi=track, key=key)
            except Exception as e:  # noqa: BLE001
                req_err = mask_secrets(str(e))[:500]
        else:
            req_err = "DevOps istekleri kapalı (CREW_DEVOPS_REQUESTS)"

    # 4) İzleme işine yorum: PR bağlantıları, diff'ler, aşamasıyla hatalar.
    lines = [f"<p><b>Sürüm adımı ({_esc(label)})</b> {_esc(p['from'])} → {_esc(to)} — iş #{_esc(job_id)}</p>"]
    if test_note:
        lines.append(f"<p><b>{_esc(test_note)}</b></p>")
    for x in res.get("prs") or []:
        lines.append(f"<p>PR: {_esc(x['repo'])} <a href=\"{_esc(x.get('url', ''))}\">#{_esc(x.get('id'))}</a>"
                     + (" (mevcut PR)" if x.get("reused") else " (taslak)") + "</p>")
    for f in res.get("fallback") or []:
        lines.append(f"<p>{_esc(f['repo'])}: PR açılmadı — diff "
                     + ("DevOps isteğinde.</p>" if req else f"aşağıda.</p><pre>{_esc(f['diff'])}</pre>"))
    for e in res.get("errors") or []:
        lines.append(f"<p>Hata · {_esc(e.get('repo'))} (aşama: {_esc(e.get('stage'))}"
                     + (", dal yazıldı" if e.get("branch_written") else "") + f"): {_esc(e.get('error'))}</p>")
    if req:
        lines.append(f"<p>DevOps isteği: #{_esc(req.get('id'))}" + ("" if req.get("created") else " (mevcut)")
                     + "</p>")
    if req_err:
        lines.append(f"<p>DevOps isteği açılamadı: {_esc(req_err)}</p>")
    _comment(client, track, "".join(lines))
    out = {"job_id": job_id, "step": step, "prs": res.get("prs") or [], "fallback": res.get("fallback") or [],
           "errors": res.get("errors") or [], "devops_request": (req or {}).get("id")}
    log.info(f"⬆️ Sürüm adımı #{job_id} ({step}, {lang}:{repo}:{to}): {len(out['prs'])} PR, "
             f"{len(out['fallback'])} diff, {len(out['errors'])} hata")
    if out["errors"]:
        raise StepFailed(f"{len(out['errors'])} repoda yazma hatası: "
                         + "; ".join(f"{e.get('repo')} ({e.get('stage')})" for e in out["errors"]))
    return out
