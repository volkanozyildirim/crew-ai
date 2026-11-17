"""Sürüm adımı: kayıttaki sitelerin tek satırlık sürüm değişikliği ve taslak PR'ı.

Üretim altyapı repolarına (FLODevOps, yml-files) yazan en riskli kod: bir site yalnız
`line` satırındaki `old` metnini `new` ile değiştirir, başka hiçbir bayt değişmez; değişmezse
(dosya o arada düzenlenmişse) yazılmaz. `old` ile `new` yalnız rakam dizilerinde ayrışabilir.
Önce HER dosya hedef daldan HAM bayt olarak okunup uygulanır, dal durumu doğrulanır, ancak ondan
sonra yazılmaya başlanır — geç bir hata yarım push bırakmaz. Yazılan dal yalnız Tempo'nun kendi
`tempo/upgrade-…` dalıdır; hedef dala (ör. production) asla doğrudan yazılmaz. Dal ya bu çalışmada
doğrulanan hedef commit'te YENİ açılır ya da hedefle aynı commit'te duran boş artıktır; içinde
başka commit olan hiçbir dala push yapılmaz (mevcut Tempo PR'ı yalnız içeriği doğrulanıp aynen
döndürülür). Dosyalar base64 ile gönderilir: baytlar (BOM/CRLF) aynen. Sunucuya ve db'ye bağımlı değil.
"""
from __future__ import annotations

import base64
import difflib
import re

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
