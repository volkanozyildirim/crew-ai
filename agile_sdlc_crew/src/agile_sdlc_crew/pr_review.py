"""PR inceleme katkısı — insanın geliştirdiği PR'a danışma niteliğinde review.

Pipeline yalnızca başlanmamış (Proposed) WI'ları alır; "In Progress / Code
Review"daki işlere sahiplik kuralı gereği dokunmaz (KN-35, KN-41). Ama o
işlerin **review sürecine** katkı verebilir: WI'a bağlı aktif PR'ı bulur,
değişen dosyaları context'e hazırlar (pipeline'ın kendi pre-fetch deseni),
code_reviewer ajanını bir kez çalıştırır ve bulguları PR'a (genel özet +
blocker/major için satır yorumları) ve WI'a yorum olarak yazar.

Sınırlar — bilinçli:
  * Kod değiştirmez, push etmez, PR oyu (vote) vermez, WI durumuna dokunmaz.
  * Tek LLM çağrısı (reviewer); retry/düzeltme döngüsü yok.
  * `pr_fix.py` gibi bağımsız modül: kuyruk worker'ından geçmez, sunucuda
    thread'de koşar; iş kaydı `jobs.job_kind='pr_review'` ile izlenir,
    maliyet `llm_calls`'a job_id ile yazılır.
"""

from __future__ import annotations

import logging
import re
from html import unescape

log = logging.getLogger("pipeline")

_PR_LINK_RE = re.compile(r"PullRequestId/[^%]+%2f([^%]+)%2f(\d+)", re.IGNORECASE)
INLINE_SEVERITIES = ("blocker", "major")
MAX_INLINE_COMMENTS = 8


# ── Saf yardımcılar ──────────────────────────────────────────────────────

def parse_pr_links(relations: list[dict] | None) -> list[dict]:
    """WI relations → [{repo_id, pr_id}] (en yeni PR önce). Ayraç %2F/%2f olabilir."""
    out = []
    for rel in relations or []:
        if (rel.get("attributes") or {}).get("name") != "Pull Request":
            continue
        m = _PR_LINK_RE.search(rel.get("url", "") or "")
        if m:
            out.append({"repo_id": m.group(1), "pr_id": int(m.group(2))})
    out.sort(key=lambda x: x["pr_id"], reverse=True)
    return out


def _plain(html: str) -> str:
    s = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", html or "", flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = unescape(s)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", s)).strip()


def wi_requirements_text(fields: dict) -> str:
    """WI'dan doğrudan gereksinim metni (BA çağrısı yok — insan işinin kendi tarifi)."""
    title = fields.get("System.Title", "") or ""
    desc = _plain(fields.get("System.Description", "") or "")
    ac = _plain(fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", "") or "")
    parts = [f"# Iş Kalemi\n{title}"]
    if desc:
        parts.append(f"\n## Açıklama\n{desc[:6000]}")
    parts.append(f"\n## Kabul Kriterleri\n{ac[:3000] if ac else '(WI alanı boş — açıklamadaki maddeler kabul kriteri sayılır)'}")
    return "\n".join(parts)


def changed_paths(change_entries: list[dict]) -> list[str]:
    """PR iteration changeEntries → silinmemiş dosya yolları (klasörler hariç)."""
    out = []
    for ch in change_entries or []:
        item = ch.get("item") or {}
        if item.get("isFolder") or item.get("gitObjectType") == "tree":
            continue
        if str(ch.get("changeType", "")).lower() == "delete":
            continue
        p = item.get("path")
        if p and p not in out:
            out.append(p)
    return out


def review_summary_markdown(*, verdict: str, issues: list[dict], pr_id: int, pr_url: str,
                            work_item_id: str, files: int, text_tail: str = "") -> str:
    mark = {"APPROVE": "✅ Onay", "CHANGES_REQUIRED": "🔴 Değişiklik gerekli"}.get(verdict, "⚪ Karar belirsiz")
    L = [f"## 🔍 Pipeline İnceleme Katkısı — PR #{pr_id}", "",
         f"**Karar (danışma):** {mark} · **İncelenen dosya:** {files} · **WI:** #{work_item_id}", ""]
    if issues:
        L += ["| # | Önem | Dosya | Sorun | Gerekli düzeltme |", "|---|---|---|---|---|"]
        for it in issues[:20]:
            loc = it.get("file", "") + (f":{it['line']}" if it.get("line") else "")
            L.append(f"| {it.get('id')} | {it.get('severity')} | `{loc}` | {it.get('problem', '')[:160]} | {it.get('required_fix', '')[:160]} |")
        L.append("")
    elif verdict == "APPROVE":
        L += ["Bloklayan ya da önemli madde bulunmadı.", ""]
    if text_tail:
        L += ["<details><summary>Reviewer notu</summary>", "", text_tail[:2500], "", "</details>", ""]
    L += ["---", f"*Bu inceleme danışma niteliğindedir: PR insan tarafından geliştirildi; pipeline kodu değiştirmez, "
          f"oy vermez, WI durumuna dokunmaz. [PR]({pr_url})*"]
    return "\n".join(L)


def inline_comment_text(issue: dict) -> str:
    return (f"🤖 **[{issue.get('id')}] {str(issue.get('severity', '')).upper()}** (pipeline inceleme katkısı, danışma)\n\n"
            f"{issue.get('problem', '')}\n\n**Önerilen düzeltme:** {issue.get('required_fix', '') or '—'}")


# ── Azure tarafı ─────────────────────────────────────────────────────────

def resolve_pr(client, *, work_item_id: str = "", pr_id: int | None = None, repo_name: str = "") -> tuple[str, int, dict]:
    """(repo_name, pr_id, pr_json). WI verildiyse relations'daki en yeni AKTİF PR."""
    if pr_id and repo_name:
        return repo_name, int(pr_id), client.get_pull_request(repo_name, int(pr_id))
    if not work_item_id:
        raise ValueError("work_item_id ya da (repo_name + pr_id) gerekli")
    wi = client.get_work_item(int(work_item_id))
    links = parse_pr_links(wi.get("relations") or [])
    if not links:
        raise LookupError(f"WI #{work_item_id} üzerinde bağlı PR yok")
    fallback = None
    for link in links:
        try:
            rname = client.get_repository(link["repo_id"]).get("name") or link["repo_id"]
            pr = client.get_pull_request(rname, link["pr_id"])
        except Exception as e:  # noqa: BLE001
            log.info(f"  PR #{link['pr_id']} okunamadı: {e}")
            continue
        status = (pr.get("status") or "").lower()
        if status == "active":
            return rname, link["pr_id"], pr
        if status == "completed" and fallback is None:
            fallback = (rname, link["pr_id"], pr)
    if fallback:
        log.info(f"  Aktif PR yok — tamamlanmış PR #{fallback[1]} inceleniyor")
        return fallback
    raise LookupError(f"WI #{work_item_id} için aktif ya da tamamlanmış PR bulunamadı (hepsi abandoned)")


def build_pr_context(client, repo_name: str, pr: dict, *, max_files: int = 12, per_file: int = 6000) -> tuple[str, list[str]]:
    pr_id = pr.get("pullRequestId")
    branch = (pr.get("sourceRefName") or "").replace("refs/heads/", "")
    paths = changed_paths(client.get_pull_request_changes(repo_name, int(pr_id)))
    parts = [
        f"\n# PR DEĞİŞİKLİKLERİ (PR #{pr_id}, repo {repo_name}, branch {branch} — feature branch içerikleri HAZIR)",
        "⚡ Aşağıdaki dosya içerikleri context'te zaten var. get_pr_changes / browse_repo ÇAĞIRMA — "
        "doğrudan bu içerikleri iş kalemine ve kabul kriterlerine göre incele.",
        f"PR başlığı: {pr.get('title', '')}",
        f"PR açıklaması: {_plain(pr.get('description', '') or '')[:1500] or '—'}",
    ]
    used = []
    for p in paths[:max_files]:
        try:
            content = client.get_file_content(repo_name, p, branch)
        except Exception:
            continue
        if not content or not content.strip():
            continue
        trunc = content[:per_file] + ("\n... (kısaltıldı)" if len(content) > per_file else "")
        parts.append(f"\n## {p}\n```\n{trunc}\n```")
        used.append(p)
    if len(paths) > max_files:
        parts.append(f"\n(+{len(paths) - max_files} dosya daha değişti; context'e alınmadı)")
    return "\n".join(parts), used


def run_pr_review(*, work_item_id: str = "", pr_id: int | None = None, repo_name: str = "",
                  job_id: int | None = None, post: bool = True) -> dict:
    """Uçtan uca: PR bul → context → reviewer → PR/WI yorumları. Sonuç dict."""
    from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient
    from agile_sdlc_crew.tools import claude_cli_llm as _acct
    from agile_sdlc_crew import db
    from agile_sdlc_crew.crew import AgileSDLCCrew
    from agile_sdlc_crew.flow import _parse_review_issues, _review_approved, _review_rejected
    from agile_sdlc_crew.main import _add_wi_comment

    client = AzureDevOpsClient()
    rname, pid, pr = resolve_pr(client, work_item_id=work_item_id, pr_id=pr_id, repo_name=repo_name)
    branch = (pr.get("sourceRefName") or "").replace("refs/heads/", "")
    project = ((pr.get("repository") or {}).get("project") or {}).get("name", "")
    pr_url = f"{client.org_url}/{project}/_git/{rname}/pullrequest/{pid}" if project else ""
    if not work_item_id:
        m = re.search(r"#(\d+)", pr.get("title", "") or "")
        work_item_id = m.group(1) if m else ""
    log.info(f"\n-- PR İNCELEME KATKISI: PR #{pid} ({rname}, {branch}) · WI #{work_item_id or '?'} --")

    wi_fields = {}
    if work_item_id:
        try:
            wi_fields = client.get_work_item(int(work_item_id)).get("fields", {}) or {}
        except Exception as e:  # noqa: BLE001
            log.info(f"  WI okunamadı: {e}")
    requirements = wi_requirements_text(wi_fields) if wi_fields else f"# Iş Kalemi\n{pr.get('title', '')}"
    pr_ctx, files = build_pr_context(client, rname, pr)
    ctx = (requirements + "\n\n# İNCELEME KAPSAMI\nBu PR insan tarafından geliştirildi. İncelemen DANIŞMA "
           "niteliğindedir: kodu sen değiştirmeyeceksin; bulgular PR yorumu olarak geliştiriciye gidecek. "
           "Somut, dosya/satır referanslı, uygulanabilir yaz." + pr_ctx)
    if job_id:
        db.update_job(job_id, repo_name=rname, branch_name=branch, pr_id=str(pid), pr_url=pr_url,
                      wi_title=(wi_fields.get("System.Title", "") or pr.get("title", "") or "")[:200],
                      current_step="review_pr_task")
        try:
            _acct.set_call_context(job_id, "review_pr_task", "code_reviewer")
            if getattr(_acct, "_call_sink", None) is None:
                _acct.register_call_sink(db.record_llm_call)
        except Exception:
            pass
    try:
        crew = AgileSDLCCrew().create_review_crew()
        result = crew.kickoff(inputs={
            "work_item_id": work_item_id or "?", "requirements": requirements[:3000],
            "target_repo": rname, "target_branch": branch, "pr_id": str(pid), "pr_url": pr_url,
            "previous_context": ctx, "scrum_master_feedback": "",
        })
    finally:
        try:
            _acct.clear_call_context()
        except Exception:
            pass
    text = (result.raw or "") if result else ""
    verdict = "APPROVE" if _review_approved(text) else ("CHANGES_REQUIRED" if _review_rejected(text) else "UNKNOWN")
    issues = _parse_review_issues(text)
    log.info(f"  Karar: {verdict} · {len(issues)} madde · {len(files)} dosya")

    posted = {"pr_summary": False, "inline": 0, "wi": False}
    summary = review_summary_markdown(verdict=verdict, issues=issues, pr_id=pid, pr_url=pr_url,
                                      work_item_id=work_item_id or "?", files=len(files),
                                      text_tail="" if issues else text[:2500])
    if post:
        try:
            client.add_pr_comment(rname, pid, summary)
            posted["pr_summary"] = True
        except Exception as e:  # noqa: BLE001
            log.warning(f"  PR özet yorumu yazılamadı: {e}")
        for it in [i for i in issues if i.get("severity") in INLINE_SEVERITIES][:MAX_INLINE_COMMENTS]:
            try:
                fp = it.get("file") or ""
                fp = fp if fp.startswith("/") else "/" + fp
                line = int(it.get("line") or 1)
                client.add_pr_comment(rname, pid, inline_comment_text(it), file_path=fp, line_number=line)
                posted["inline"] += 1
            except Exception as e:  # noqa: BLE001
                log.info(f"  Satır yorumu yazılamadı ({it.get('id')}): {e}")
        if work_item_id:
            try:
                _add_wi_comment(client, work_item_id, summary)
                posted["wi"] = True
            except Exception as e:  # noqa: BLE001
                log.info(f"  WI yorumu yazılamadı: {e}")
    if job_id:
        try:
            db.complete_step(job_id, "review_pr_task", text[:50_000])
        except Exception:
            pass
    return {"repo": rname, "pr_id": pid, "pr_url": pr_url, "work_item_id": work_item_id, "branch": branch,
            "verdict": verdict, "issues": len(issues), "files": len(files), "posted": posted, "summary": summary}
