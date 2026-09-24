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

import difflib
import logging
import re
from html import unescape

log = logging.getLogger("pipeline")

_PR_LINK_RE = re.compile(r"PullRequestId/[^%]+%2f([^%]+)%2f(\d+)", re.IGNORECASE)
INLINE_SEVERITIES = ("blocker", "major")
MAX_INLINE_COMMENTS = 8
FOCUS_CONTEXT_LINES = 25   # degisen satirin etrafinda birakilan baglam
# Context'e enjekte edilen "  42| kod" onekini alintidan sokup atmak icin.
_LINENO_PREFIX_RE = re.compile(r"^\s*\d+\s*\|\s?")


def _key(path: str) -> str:
    """Dosya yolunu karsilastirilabilir hale getirir (flow._norm_path ile ayni kural)."""
    return (path or "").strip().replace("\\", "/").lstrip("/")


def _norm_ws(s: str) -> str:
    """Bosluklari tek bosluga indirger — LLM alintilari girintiyi sadik tasimiyor."""
    return re.sub(r"\s+", " ", s or "").strip()


def body_index(file_bodies: dict[str, str]) -> dict[str, str]:
    """Dosya govdelerini yol-normalize edilmis anahtarla indeksler.

    `changed_paths` '/app/X.php' dondururken `_parse_review_issues` yollari
    `_norm_path` ile 'app/X.php'e cevirir; duz sozluk aramasi bu yuzden HIC
    tutmuyordu — suggestion dogrulamasi her seferinde sessizce dusuyordu."""
    return {_key(k): v for k, v in (file_bodies or {}).items()}


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
            sev = it.get("severity")
            if it.get("anchor") == "absence":
                sev = f"{sev} · eksik"
                loc += " *(satır referansı: eksik olanın ait olduğu yer)*"
            L.append(f"| {it.get('id')} | {sev} | `{loc}` | {it.get('problem', '')[:160]} | {it.get('required_fix', '')[:160]} |")
        L.append("")
    elif verdict == "APPROVE":
        L += ["Bloklayan ya da önemli madde bulunmadı.", ""]
    if text_tail:
        L += ["<details><summary>Reviewer notu</summary>", "", text_tail[:2500], "", "</details>", ""]
    L += ["---", f"*Bu inceleme danışma niteliğindedir: PR insan tarafından geliştirildi; pipeline kodu değiştirmez, "
          f"oy vermez, WI durumuna dokunmaz. [PR]({pr_url})*"]
    return "\n".join(L)


def inline_comment_text(issue: dict, *, suggestion: dict | None = None) -> str:
    """Satır yorumu. `suggestion` verilirse Azure DevOps'un uygulanabilir
    ```suggestion bloğu eklenir (PR sahibine "Apply changes" butonu çıkar)."""
    absent = issue.get("anchor") == "absence"
    head = (f"🤖 **[{issue.get('id')}] {str(issue.get('severity', '')).upper()}"
            f"{' — EKSİK' if absent else ''}** (pipeline inceleme katkısı, danışma)\n\n"
            + ("_Bu satırdaki kodda hata yok — eksik olan şey buraya ait._\n\n" if absent else "")
            + f"{issue.get('problem', '')}")
    if suggestion and suggestion.get("code") is not None:
        # Bos blok = "bu satirlari sil" (Azure DevOps'un Apply'i bunu destekler).
        _c = suggestion["code"]
        _label = "**Önerilen düzeltme** (bu satırları siler):" if not _c.strip() else "**Önerilen düzeltme:**"
        return f"{head}\n\n{_label}\n\n```suggestion\n{_c}\n```"
    return f"{head}\n\n**Önerilen düzeltme:** {issue.get('required_fix', '') or '—'}"


def _numbered(lines: list[str], start: int = 1) -> list[str]:
    return [f"{i:>4}| {ln}" for i, ln in enumerate(lines, start)]


def focused_source(new_body: str, old_body: str | None = None, *,
                   per_file: int, context: int = FOCUS_CONTEXT_LINES) -> str:
    """Dosyayi satir numarali metne cevirir; sigmiyorsa DEGISEN yerleri secer.

    Onceki davranis dosyanin ilk `per_file` karakterini aliyordu. OrderLine.php
    (121 KB / 3098 satir) reviewer'a %5 olarak gitti: PR'in asil dokundugu metot
    context'e HIC girmedi, ajan da iki maddeyi `line: 0` diye isaretledi (job
    #208). Bir PR incelemesinde onemli olan dosyanin bas tarafi degil, DEGISEN
    tarafi — bu yuzden base surumle diff alinip degisen araliklarin etrafi
    pencerelenir. Atlanan yerler `... N satir atlandi` diye isaretlenir ki ajan
    gormedigi kod hakkinda madde acmasin.

    Satir numaralari HER ZAMAN gercek dosyadaki numaradir (pencere ofseti degil)
    — `anchor_issue_lines` ham govdeye bakar, ikisi ayni sayiyi gormeli.
    Base yoksa (yeni dosya / okunamadi) bas taraf kirpilir: eski davranis.
    """
    lines = new_body.splitlines()
    if len(new_body) <= per_file:
        return "\n".join(_numbered(lines))

    changed: list[tuple[int, int]] = []
    if old_body:
        sm = difflib.SequenceMatcher(None, old_body.splitlines(), lines, autojunk=False)
        changed = [(j1 + 1, j2) for tag, _i1, _i2, j1, j2 in sm.get_opcodes() if tag != "equal" and j2 > j1]
    if not changed:
        head = new_body[:per_file]
        n = len(head.splitlines())
        return "\n".join(_numbered(head.splitlines())
                          + [f"    | ... (kısaltıldı — {max(0, len(lines) - n)} satır daha var, "
                             f"context'e girmedi; buradan sonrası hakkında madde açma)"])

    for ctx in (context, context // 2, 5, 2):
        wins: list[list[int]] = []
        for a, b in changed:
            lo, hi = max(1, a - ctx), min(len(lines), b + ctx)
            if wins and lo <= wins[-1][1] + 1:
                wins[-1][1] = max(wins[-1][1], hi)
            else:
                wins.append([lo, hi])
        out, prev = [], 0
        for lo, hi in wins:
            if lo > prev + 1:
                out.append(f"    | ... ({lo - prev - 1} satır atlandı — değişmedi, context'e alınmadı)")
            out += _numbered(lines[lo - 1:hi], lo)
            prev = hi
        if prev < len(lines):
            out.append(f"    | ... ({len(lines) - prev} satır atlandı — değişmedi, context'e alınmadı)")
        text = "\n".join(out)
        if len(text) <= per_file or ctx == 2:
            return text
    return text  # pragma: no cover


def anchor_issue_lines(issues: list[dict], file_bodies: dict[str, str]) -> dict:
    """Maddenin `line` degerini evidence.quote'un dosyadaki GERCEK satirina oturtur.

    Reviewer'a dosya icerigi veriliyor ama satir numarasini kendisi sayiyor.
    Job #208 (PR #43642): R1 "86" yerine 95, R2 "98" yerine 106 dedi — +9/+8
    kayma. Alintilar ikisinde de HARFI HARFINE dogruydu: yani numara tahmin,
    kanit gercek. O yuzden numarayi Python yeniden hesaplar — Katman 0, LLM
    cagrisi yok, sadece elimizdeki dosya govdesi.

    Alinti dosyada bulunamazsa numara SIFIRLANIR. Yanlis ama kesin gorunen bir
    satir, numarasiz maddeden daha zararli: ozet tablosu okuru yanlis koda
    yollar, satir yorumu yanlis satira dusrer.

    Donen sozluk: {anchored, moved, cleared, kept} — log icin.
    """
    idx = body_index(file_bodies)
    stats = {"anchored": 0, "moved": 0, "cleared": 0, "kept": 0}
    for it in issues or []:
        ev = it.get("evidence") or {}
        first = next((ln for ln in (ev.get("quote") or "").splitlines() if ln.strip()), "")
        needle = _norm_ws(_LINENO_PREFIX_RE.sub("", first))
        body = idx.get(_key(it.get("file")))
        try:
            old_line = int(it.get("line") or 0)
        except (TypeError, ValueError):
            old_line = 0
        if not needle or body is None:
            stats["kept"] += 1          # dogrulayacak kanit ya da govde yok
            continue
        hits = [n for n, ln in enumerate(body.splitlines(), 1) if needle in _norm_ws(ln)]
        if not hits:
            it["line"] = 0
            if ev:
                ev["line"] = 0
            stats["cleared"] += 1
            continue
        best = min(hits, key=lambda n: abs(n - old_line)) if old_line else hits[0]
        it["line"] = best
        ev["line"] = best
        it["evidence"] = ev
        stats["anchored"] += 1
        if best != old_line:
            stats["moved"] += 1
    return stats


def verified_suggestion(issue: dict, file_bodies: dict[str, str]) -> dict:
    """Suggestion'i ANCAK dosya icerigiyle dogrulanirsa dondur, yoksa {}.

    Suggestion tek tikla commit'lenebiliyor; yanlis satira yazilan bir oneri
    sessizce kod bozar. Bu yuzden: dosyayi taniyor muyuz, satir araligi dosyanin
    icinde mi, ve onerilen kod zaten oradaki kodla ayni mi (ayniysa gurultu).
    Dogrulanamayan oneri dusurulur — yorum duz metne doner, bilgi kaybolmaz.
    """
    sug = issue.get("suggestion") or {}
    code = sug.get("code")
    if code is None:
        return {}   # alan yok → oneri yok ("" ise SILME onerisidir, gecerli)
    body = body_index(file_bodies).get(_key(issue.get("file")))
    if body is None:
        return {}
    lines = body.splitlines()
    start, end = sug.get("line_start"), sug.get("line_end")
    if not (1 <= start <= end <= len(lines)):
        return {}
    if "\n".join(lines[start - 1:end]).strip() == code.strip():
        return {}
    return sug


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


def build_pr_context(client, repo_name: str, pr: dict, *, max_files: int = 12, per_file: int = 6000,
                     bodies: dict | None = None) -> tuple[str, list[str]]:
    """PR baglami + degisen dosya listesi. `bodies` verilirse okunan dosya
    iceriklerini oraya yazar — suggestion dogrulamasi (verified_suggestion)
    icin gerekli, cunku oneri gercek satirlarla ortusmek zorunda."""
    pr_id = pr.get("pullRequestId")
    branch = (pr.get("sourceRefName") or "").replace("refs/heads/", "")
    base = (pr.get("targetRefName") or "").replace("refs/heads/", "")
    paths = changed_paths(client.get_pull_request_changes(repo_name, int(pr_id)))
    parts = []
    # Pipeline'daki inceleme adimiyla ayni referanslar: repoya uyan dil dosyasi
    # + Sonar kurallari. Insan PR'i da ayni olcute gore incelensin.
    try:
        from agile_sdlc_crew.skills import standards_context
        skill_ctx = standards_context(repo_name)
    except Exception:  # noqa: BLE001
        skill_ctx = ""
    if skill_ctx:
        parts.append(skill_ctx)
    parts += [
        f"\n# PR DEĞİŞİKLİKLERİ (PR #{pr_id}, repo {repo_name}, branch {branch} — feature branch içerikleri HAZIR)",
        "⚡ Aşağıdaki dosya içerikleri context'te zaten var. get_pr_changes / browse_repo ÇAĞIRMA — "
        "doğrudan bu içerikleri iş kalemine ve kabul kriterlerine göre incele.",
        "📍 Her satırın başındaki `NN| ` DOSYANIN PARÇASI DEĞİL, satır numarasıdır. Bir maddede "
        "`line` verirken bu numarayı kullan; `evidence.quote` ve `suggested_code` yazarken ise "
        "`NN| ` önekini ATMA — sadece kodun kendisini yaz.",
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
        if bodies is not None:
            # Suggestion dogrulamasi TAM icerik ister: prompt'a giren metin
            # kisaltilmis olabilir, satir numaralari kaymasin.
            bodies[p] = content
        # Satir numarasi ONEKLE: reviewer aksi halde numarayi kendisi sayiyor ve
        # kaydiriyor (job #208'de +8/+9). Govde `bodies`e HAM yazilir — numara
        # yalniz prompt metnindedir, anchor/suggestion dogrulamasi ham metne bakar.
        # Sigmayan dosyada bas taraf degil DEGISEN taraf secilir (focused_source).
        old_body = None
        if len(content) > per_file and base:
            try:
                old_body = client.get_file_content(repo_name, p, base)
            except Exception:  # noqa: BLE001 — yeni dosya: base'te yok, kirpmaya duseriz
                old_body = None
        parts.append(f"\n## {p}\n```\n{focused_source(content, old_body, per_file=per_file)}\n```")
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
    file_bodies: dict[str, str] = {}
    pr_ctx, files = build_pr_context(client, rname, pr, bodies=file_bodies)
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
    anch = anchor_issue_lines(issues, file_bodies)
    log.info(f"  Karar: {verdict} · {len(issues)} madde · {len(files)} dosya")
    if anch["moved"] or anch["cleared"]:
        log.info(f"  Satır düzeltme: {anch['moved']} madde doğru satıra oturtuldu, "
                 f"{anch['cleared']} maddenin alıntısı dosyada bulunamadı (numara düşürüldü)")

    posted = {"pr_summary": False, "inline": 0, "wi": False, "suggestions": 0}
    summary = review_summary_markdown(verdict=verdict, issues=issues, pr_id=pid, pr_url=pr_url,
                                      work_item_id=work_item_id or "?", files=len(files),
                                      text_tail="" if issues else text[:2500])
    if post:
        try:
            client.add_pr_comment(rname, pid, summary)
            posted["pr_summary"] = True
        except Exception as e:  # noqa: BLE001
            log.warning(f"  PR özet yorumu yazılamadı: {e}")
        # Satir yorumu: blocker/major HER ZAMAN, arti DOGRULANMIS ONERISI olan
        # her madde — onemi ne olursa olsun.
        #
        # Neden: mekanik ve satir-lokal duzeltmeler (olu kod silme, eksik log,
        # atomik olmayan artirim) tanimi geregi `minor` siniflanir — bloklamazlar.
        # Eski filtre yalnizca blocker/major'i satira yaziyordu, yani ONERI
        # TASIYAN maddeler tam da hic yazilmayanlardi. Job #211'de olculdu:
        # reviewer R5 icin gecerli bir SILME onerisi uretti (Product.php
        # 1843-1873, kullanilmayan @deprecated metot), dogrulamadan da gecti,
        # ama minor oldugu icin PR'a hic dusmedi → sayac yine 0. Tek tiklik bir
        # duzeltme, ozet tablosunda bir satir olmaktan daha degerli.
        _sugs = {id(i): verified_suggestion(i, file_bodies) for i in issues}
        _inline = [i for i in issues
                   if i.get("severity") in INLINE_SEVERITIES or _sugs.get(id(i))]
        for it in _inline[:MAX_INLINE_COMMENTS]:
            try:
                fp = it.get("file") or ""
                fp = fp if fp.startswith("/") else "/" + fp
                line = int(it.get("line") or 1)
                # Suggestion yalniz dogrulandiysa yazilir; thread araligi
                # onerilen kodun YERINI ALACAK satirlarla ortusmeli.
                sug = _sugs.get(id(it)) or {}
                if sug:
                    line, end = sug["line_start"], sug["line_end"]
                else:
                    end = None
                client.add_pr_comment(rname, pid, inline_comment_text(it, suggestion=sug),
                                      file_path=fp, line_number=line, end_line=end)
                posted["inline"] += 1
                if sug:
                    posted["suggestions"] = posted.get("suggestions", 0) + 1
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
