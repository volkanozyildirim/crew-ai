"""Sürüm adımı: kayıttaki sitelerin tek satırlık sürüm değişikliği ve taslak PR'ı.

Üretim altyapı repolarına (FLODevOps, yml-files) yazan en riskli kod: bir site yalnız
`line` satırındaki `old` metnini `new` ile değiştirir, başka hiçbir bayt değişmez; değişmezse
(dosya o arada düzenlenmişse) yazılmaz. Önce HER dosya hedef daldan okunup uygulanır, ancak
ondan sonra yazılmaya başlanır — geç bir hata yarım push bırakmaz. Sunucuya ve db'ye bağımlı değil.
"""
from __future__ import annotations

import difflib
import re

import requests

from agile_sdlc_crew.quality.modern_fix import _REPO

# Yalnız sürüm benzeri metin: harf/rakam/_ . - : / ' " > = ^ ~ | , boşluk. Satır sonu, ; $ ` ( ) yok.
_SAFE_NEW = re.compile(r"[\w.\-:/'\">=^~|, ]+", re.ASCII)


class SiteChanged(ValueError):
    """Site artık kayıttaki gibi değil (satır değişmiş / metin belirsiz / yeni metin güvensiz)."""


def branch_name(lang: str, repo: str, to: str) -> str:
    return f"tempo/upgrade-{lang}-{repo}-{to}"


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


def _validate(sites: list[dict]) -> None:
    if not sites:
        raise ValueError("Yazılacak site yok.")
    for s in sites:
        if s.get("blocked"):
            raise ValueError(f"Engelli site var ({s.get('repo')} {s.get('path')}): {s['blocked']}")
        if not s.get("line"):
            raise ValueError(f"Satırı bilinmeyen site var ({s.get('repo')} {s.get('path')}).")
        repo, path = str(s.get("repo") or ""), str(s.get("path") or "")
        if not _REPO.match(repo):
            raise ValueError(f"Geçersiz repo adı: {repo!r}")
        parts = path.split("/")
        if not path.strip("/") or any(p == ".." or p.startswith("-") for p in parts):
            raise ValueError(f"Geçersiz dosya yolu: {path!r}")
        if not s.get("branch"):
            raise ValueError(f"Hedef dal yok ({repo}).")


def _is_denied(exc: Exception) -> bool:
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) in (401, 403)


def push_sites(client, sites: list[dict], *, branch: str, title: str, body: str,
               wi_id: int | None) -> dict:
    """(proje, repo) başına tek commit + tek taslak PR. Yetki yoksa (401/403) PR yerine diff döner.
    -> {"prs": [{"repo","id","url"}], "fallback": [{"repo","project","diff"}]}"""
    _validate(sites)

    groups: dict[tuple[str, str], list[dict]] = {}
    for s in sites:
        groups.setdefault((s.get("project") or "", s["repo"]), []).append(s)

    # 1) Hepsini oku + uygula + dal durumunu doğrula — henüz yazma yok.
    plans = []
    for (project, repo), grp in groups.items():
        target = grp[0]["branch"]
        if any(g["branch"] != target for g in grp):
            raise ValueError(f"{repo}: siteler farklı hedef dallara gidiyor.")
        proj = project or None
        by_path: dict[str, list[dict]] = {}
        for g in grp:
            by_path.setdefault(g["path"], []).append(g)
        files = []
        for path, group_sites in by_path.items():
            old_text = client.get_file_content(repo, path, target, project=proj)
            new_text = old_text
            for g in sorted(group_sites, key=lambda x: -int(x["line"])):
                new_text = apply_site(new_text, g)
            files.append((path, old_text, new_text))
        names = {b.get("name") for b in client.list_branches(repo, project=proj)}
        existing = None
        if f"refs/heads/{branch}" in names:
            pr = client.find_pull_request(repo, branch, project=proj)
            if not pr or pr.get("status") != "active":
                raise ValueError(f"{repo}: '{branch}' dalı zaten var ve açık bir PR'ı yok; "
                                 "başkasının dalına dokunulmadı.")
            existing = pr
        plans.append((project, repo, target, files, existing))

    # 2) Yaz.
    prs: list[dict] = []
    fallback: list[dict] = []
    for project, repo, target, files, existing in plans:
        proj = project or None
        changes = [{"changeType": "edit", "path": "/" + path.lstrip("/"), "content": new}
                   for path, _old, new in files]
        try:
            if existing is None:
                client.create_branch(repo, branch, target, project=proj)
            client.push_changes(repo, branch, changes, title, project=proj)
            if existing is not None:
                prs.append({"repo": repo, "id": existing["id"], "url": existing.get("url", "")})
                continue
            res = client.create_pull_request(
                repo, branch, target, title=title, description=body,
                work_item_ids=[wi_id] if (wi_id and not project) else None,
                draft=True, project=proj)
        except requests.HTTPError as e:
            if not _is_denied(e):
                raise
            diff = "".join(
                "".join(difflib.unified_diff(o.splitlines(keepends=True), n.splitlines(keepends=True),
                                             fromfile=f"a/{p.lstrip('/')}", tofile=f"b/{p.lstrip('/')}"))
                for p, o, n in files)
            fallback.append({"repo": repo, "project": project, "diff": diff})
            continue
        pid = res.get("pullRequestId")
        name = (res.get("repository") or {}).get("name") or repo
        url = res.get("url") or ""
        try:
            base = client.org_url  # gerçek istemcide var; sahte istemcide yok
            url = f"{base}/{project or getattr(client, 'project', '')}/_git/{name}/pullrequest/{pid}"
        except AttributeError:
            pass
        prs.append({"repo": repo, "id": pid, "url": url})
    return {"prs": prs, "fallback": fallback}
