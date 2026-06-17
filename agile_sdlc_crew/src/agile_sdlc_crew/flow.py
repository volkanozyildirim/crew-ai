"""Agile SDLC Crew - CrewAI Flow ile 11 adimli pipeline orkestrasyonu.

run_pipeline() icindeki monolitik kontrol akisini event-driven Flow yapisina
donusturur. State yonetimi, HAL/CrewAI dallanmasi ve quality gate'ler
deklaratif olarak tanimlanir.
"""

import logging
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr

from crewai.flow import Flow, and_, listen, or_, router, start

from agile_sdlc_crew import context_budget as _cb

log = logging.getLogger("pipeline")


def _log(msg: str):
    log.info(msg)


# Repo adlarinda generic gecen parcalar — tek baslarina sinyal tasimaz,
# "stock_api_list" ekran adinin "stock-api" repo'suna haksiz puan vermesini
# engeller. Hem step0 (kickoff) hem step4 (architect) bu seti kullanir.
_REPO_PART_STOPWORDS = {
    "api", "app", "core", "client", "common", "frontend", "backend",
    "lib", "main", "server", "service", "services", "system", "tools",
    "ui", "utils", "web", "www",
}


def _select_repo_by_name(known_repos: list[str], wi_text: str) -> tuple[str, str]:
    """WI metninden repo adi tahmini.

    2 katman:
    - **tam isim**: Repo adi WI'da \\b...\\b siniriyla geciyorsa direkt o.
      Birden fazla varsa en uzun (en spesifik) olan kazanir.
    - **parca eslesmesi**: Repo adinin '-/_' ile parcalanmis kelimeleri,
      stop-word olmayan ve >2 harfli olanlari, WI metninde \\b...\\b ile
      esleshiyor mu? En yuksek skor kazanir.

    Returns: (yontem, repo_name) — eslesme yoksa ('', '').
    Yontem string'leri 'tam isim' / 'parca eslesmesi' (log mesajlarinda kullanilir).
    """
    import re as _re
    text = wi_text.lower()

    # Layer 0: tam isim
    full = [
        r for r in known_repos
        if _re.search(rf'\b{_re.escape(r.lower())}\b', text)
    ]
    if full:
        return ("tam isim", max(full, key=len))

    # Layer 1: parca eslesmesi
    def score(rname: str) -> int:
        s = 0
        for p in _re.split(r'[-_]', rname.lower()):
            if len(p) <= 2 or p in _REPO_PART_STOPWORDS:
                continue
            if _re.search(rf'\b{_re.escape(p)}\b', text):
                s += 1
        return s

    best = max(known_repos, key=score, default="")
    if best and score(best) > 0:
        return ("parca eslesmesi", best)
    return ("", "")


def _extract_code_block(lines: list[str], target_line: int, suffix: str = ".py") -> tuple[str, str]:
    """Hedef satirin bulundugu fonksiyon/class/method blogunu cikarir.

    Strateji: hedef satirdan yukari cik, blok baslangicini bul (def/function/class vb),
    sonra asagi in, blok bitisini bul (indent seviyesi geri geldiginde).
    Tum dillerde calisir — indent-based block detection.

    Returns: (snippet_with_line_numbers, label)
    """
    if not lines:
        return "", "bos"

    target_line = max(0, min(target_line, len(lines) - 1))

    # Blok baslangici pattern'leri (dil agnostik)
    import re as _re_blk
    block_start_re = _re_blk.compile(
        r'^\s*(def |function |class |public |private |protected |async |static |'
        r'@router\.|@app\.|module\.exports|const \w+ = |export )'
    )

    # Yukari cik — blok baslangicini bul
    block_start = target_line
    for i in range(target_line, max(-1, target_line - 100), -1):
        if block_start_re.match(lines[i]):
            block_start = i
            break
    else:
        # Bulunamadiysa +-40 satir al
        start = max(0, target_line - 40)
        end = min(len(lines), target_line + 40)
        snippet = "\n".join(f"{i+1}: {lines[i]}" for i in range(start, end))
        return snippet, f"satir {start+1}-{end}"

    # Blok baslangicinin indent seviyesi
    start_indent = len(lines[block_start]) - len(lines[block_start].lstrip())

    # Asagi in — blok bitisini bul
    block_end = block_start + 1
    for i in range(block_start + 1, min(len(lines), block_start + 200)):
        stripped = lines[i].strip()
        if not stripped:  # bos satir — devam
            block_end = i + 1
            continue
        current_indent = len(lines[i]) - len(lines[i].lstrip())
        if current_indent <= start_indent and stripped and not stripped.startswith(("#", "//", "/*", "*")):
            # Ayni veya daha az indent — blok bitti
            break
        block_end = i + 1

    # En az hedef satiri icersin
    block_end = max(block_end, target_line + 1)

    snippet = "\n".join(f"{i+1}: {lines[i]}" for i in range(block_start, min(block_end, len(lines))))

    # Cok buyukse kes
    if len(snippet) > 5000:
        center = target_line - block_start
        snippet_lines = snippet.split("\n")
        half = 40
        s = max(0, center - half)
        e = min(len(snippet_lines), center + half)
        snippet = "\n".join(snippet_lines[s:e])

    func_name = lines[block_start].strip()[:60]
    return snippet, f"blok: {func_name}"


def _extract_dev_output(code_result) -> str:
    """Developer agent output'undan TAM dosya icerigini al.
    Oncelik: pydantic.full_file_content (schema zorla), sonra raw kod blogu."""
    # 1. Pydantic output varsa (schema ile zorlanmis)
    pyd = getattr(code_result, "pydantic", None)
    if pyd is not None:
        content = getattr(pyd, "full_file_content", None)
        if content:
            return content
    # 2. Fallback: raw'dan kod blogu cikar
    from agile_sdlc_crew.main import _extract_code_from_output
    return _extract_code_from_output(code_result.raw or "")


def _review_rejected(review_text: str) -> bool:
    """Kod inceleme ciktisindan RED (CHANGES_REQUIRED) karari ver.

    DIKKAT: Gecmiste tum metinde substring aranıyordu. Reviewer task'i
    'CHANGES_REQUIRED' kelimesini kural metninde ~7 kez tekrarliyor ve
    expected_output sablonunun verdict satiri birebir 'APPROVE / CHANGES_REQUIRED'.
    Model sablonu/kurallari yansittiginda gercek bir APPROVE bile RED okunuyor
    ve bos yere review-retry dongusune giriliyordu. Bu fonksiyon SADECE acik
    karar tokenina bakar; belirsizlikte dongoyu tetiklememek icin RED degildir.
    """
    import re as _re_v
    text = review_text or ""

    # 1) En guclu sinyal: govdede hicbir yerde gecmeyen makine satiri.
    decisions = _re_v.findall(
        r"(?im)^[\s>*_#-]*REVIEW_DECISION\s*[:=]\s*([A-Za-z_]+)", text
    )
    if decisions:
        last = decisions[-1].strip().upper()
        return last.startswith("REJECT") or "CHANGES_REQUIRED" in last

    # 2) Geri donus: yalniz 'Verdict:' SATIRINI parse et (tum metni degil).
    vm = _re_v.search(
        r"(?im)^[\s>*_#-]*(?:final[\s_]*)?(?:verdict|karar)\s*[:=]\s*(.+)$", text
    )
    if vm:
        val = vm.group(1).upper()
        has_reject = any(tok in val for tok in (
            "CHANGES_REQUIRED", "CHANGES REQUIRED", "REJECT", "RED",
            "REDDED", "DEĞİŞİKLİK GEREKLİ", "DEGISIKLIK GEREKLI",
        ))
        has_approve = "APPROVE" in val or "ONAY" in val
        if has_reject and not has_approve:
            return True
        # Net APPROVE, ya da sablon echo'su (ikisi birden) / belirsiz → dongoye girme.
        return False

    # 3) Hic karar satiri yok → parse kacagi yuzunden pipeline'i bloklama.
    return False


def _coalesce_plan_changes(changes: list) -> list:
    """Ayni dosyayi hedefleyen birden fazla plan degisikligini TEK girise birlestir.

    Aksi halde implement her degisikligi bagimsiz full-file push olarak isler ve
    ikinci push birincinin duzeltmesini EZER (WI #66687 Kargoist.php: architect
    v2 dali + legacy dali icin iki ayri 'edit' uretti; ikincisi birincisini ezdi,
    reviewer hakli olarak 'v2 still * 1.20' dedi). Birlesik giris, current/new
    kodu temizlenmis holistic bir full-file pass'e gider: developer dosyayi bir
    kez okur, TUM degisiklikleri uygular, tek dosya dondurur.
    """
    groups: dict = {}
    order: list = []
    for c in changes:
        fp = (c.get("file_path") or "").strip()
        if not fp:
            continue
        if fp not in groups:
            groups[fp] = []
            order.append(fp)
        groups[fp].append(c)
    out = []
    for fp in order:
        grp = groups[fp]
        if len(grp) == 1:
            out.append(grp[0])
            continue
        descs = [c.get("description", "") for c in grp if c.get("description")]
        types = {c.get("change_type", "edit") for c in grp}
        ctype = "add" if types == {"add"} else "edit"
        out.append({
            "file_path": fp,
            "change_type": ctype,
            "description": (
                "Bu dosyada uygulanacak TÜM değişiklikler — HEPSİNİ uygula "
                "(dosyada birden fazla kod yolu/dal olabilir, hiçbirini atlama):\n"
                + "\n".join(f"{i + 1}. {d}" for i, d in enumerate(descs))
            ),
            "current_code": "",
            "new_code": "",
        })
    return out


# ── State Model ──────────────────────────────────────

class _KickoffOnlyStop(Exception):
    """Sentinel: kickoff-only modunda step0'dan sonra pipeline'i durdurur.
    main.run_kickoff_only tarafindan yakalanir, basari sayilir."""
    pass


class PipelineState(BaseModel):
    """Flow boyunca tasınan state. Her adim state'i gunceller."""
    work_item_id: str = ""
    use_hal: bool = False
    job_id: int | None = None
    # Kickoff-only debug: step0 sonrasi durdur. main.run_kickoff_only setler.
    kickoff_only: bool = False
    # Bu kickoff icin kullanicinin verdigi extra feedback (refine yolu).
    kickoff_feedback: str = ""
    # Dry-run: development stays local; no push, no PR, no review/test/UAT.
    # Set from DB row (jobs.dry_run) or env CREW_DRY_RUN in initialize().
    dry_run: bool = False
    requirements_text: str = ""
    repo_name: str = ""
    plan: dict = Field(default_factory=dict)
    known_repos: list[str] = Field(default_factory=list)
    branch_name: str = ""
    all_pushes: list[dict] = Field(default_factory=list)
    pr_id: str = ""
    pr_url: str = ""
    kickoff_text: str = ""
    review_text: str = ""
    test_text: str = ""
    uat_text: str = ""
    completion_text: str = ""
    # BA analizi sonrasi belirlenen kabul kriterleri — teknik tasarim,
    # kod gelistirme, inceleme ve UAT'ta bağlayıcı tek kaynak.
    acceptance_criteria: list[str] = Field(default_factory=list)


# ── Flow ─────────────────────────────────────────────

class AgileSDLCFlow(Flow[PipelineState]):
    """11 adimli Agile SDLC pipeline'i — event-driven orkestrasyon."""

    # Serializable olmayan nesneler
    _tracker: Any = PrivateAttr(default=None)
    _agile_crew: Any = PrivateAttr(default=None)
    _client: Any = PrivateAttr(default=None)
    _repo_mgr: Any = PrivateAttr(default=None)
    _vector_store: Any = PrivateAttr(default=None)
    _hal: Any = PrivateAttr(default=None)
    _db: Any = PrivateAttr(default=None)
    # E: Job budget tracking — her kickoff sonrasi token toplami guncellenir,
    # $CREW_MAX_JOB_COST asilirsa RuntimeError ile pipeline durdurulur
    _job_prompt_tokens: int = PrivateAttr(default=0)
    _job_completion_tokens: int = PrivateAttr(default=0)
    _job_total_tokens: int = PrivateAttr(default=0)
    # ── Helper Methods (dekoratorsuz) ────────────────

    def _build_step_context(self, step_key: str) -> str:
        """Step'e ozel, tipli bilgilerden derlenen yapisal context.
        Her agent, kendi adimi icin gereken bilgiyi burada alir."""
        s = self.state
        parts = []

        # Her step icin: is kalemi ozeti
        if s.work_item_id:
            parts.append(f"# Is Kalemi\nWI #{s.work_item_id}")

        # Kickoff Design Review tutanagi — Kritik Risk Tablosu + Backlog Adaylari
        # her adima tasiniyor. Teknik tasarim en genis pencereyi alir (risk + edge case
        # bilgisi tasarima yansimali). Test/UAT daha kisaltilmis.
        # NOT: requirements artik kickoff'tan ONCE calisiyor, dolayisiyla
        # requirements_analysis_task icin kickoff text enjekte edilmez (henuz yok).
        if s.kickoff_text:
            if step_key in ("technical_design_task",):
                # Architect: Risk Tablosu + Edge Case'ler + Kabul Kriterleri mutlaka gorusun
                parts.append(
                    f"\n# Kickoff Design Review Tutanagi (Tum Disiplinler)\n"
                    f"{s.kickoff_text[:_cb.cap('KICKOFF')]}\n"
                    f"⚠️ Teknik plan 'Kritik Risk Tablosu'ndaki TUM riskler ve "
                    f"'Edge Case'ler' icin somut kod degisiklikleri icermeli."
                )
            elif step_key in ("test_planning_task",):
                parts.append(
                    f"\n# Kickoff Design Review — Test Perspektifi\n"
                    f"{s.kickoff_text[:_cb.cap('KICKOFF_QA')]}"
                )
            elif step_key in ("uat_task",):
                parts.append(
                    f"\n# Kickoff Design Review — Backlog Adaylari ve Kabul Kriterleri\n"
                    f"{s.kickoff_text[:_cb.cap('KICKOFF_QA')]}"
                )
            elif step_key in ("review_pr_task",):
                # Reviewer: risk tablosunu bilerek PR'i incelesin
                parts.append(
                    f"\n# Kickoff Design Review — Kritik Riskler\n"
                    f"{s.kickoff_text[:_cb.cap('KICKOFF_REVIEW')]}"
                )

        # Requirements (step 1 sonrasi — kickoff dahil, artik requirements once calisiyor)
        if s.requirements_text and step_key != "requirements_analysis_task":
            parts.append(f"\n# Is Analizi (Gereksinimler)\n{s.requirements_text[:_cb.cap('REQUIREMENTS')]}")

        # Kabul kriterleri — BA analizinden sonra belirlenir, pipeline boyunca
        # baglayici tek kaynak: tasarim, gelistirme, inceleme ve UAT buna gore yapilir.
        if s.acceptance_criteria and step_key in (
            "kickoff_meeting_task", "technical_design_task", "implement_change_task",
            "review_pr_task", "uat_task", "completion_report_task",
        ):
            criteria_text = "\n".join(
                f"{i+1}. {c}" for i, c in enumerate(s.acceptance_criteria)
            )
            parts.append(
                f"\n# Acceptance Criteria (Binding Throughout Pipeline — Set by BA)\n"
                f"{criteria_text}\n"
                f"⚠️ Every step must align with these criteria:\n"
                f"- Technical Design: plan must cover every criterion\n"
                f"- Development: code must implement every criterion\n"
                f"- Code Review: verify each criterion is met\n"
                f"- UAT: evaluate each criterion as PASS/FAIL"
            )

        # Plan (step 4 sonrasi)
        if s.plan and step_key not in ("requirements_analysis_task", "technical_design_task"):
            changes_summary = []
            for ch in s.plan.get("changes", [])[:_cb.cap('PLAN_CHANGES')]:
                changes_summary.append(
                    f"- [{ch.get('change_type','edit')}] `{ch.get('file_path','?')}`: "
                    f"{ch.get('description','')[:120]}"
                )
            parts.append(
                f"\n# Teknik Tasarim\n"
                f"Repo: {s.repo_name}\n"
                f"Degisiklikler:\n" + "\n".join(changes_summary)
            )
            acs = s.plan.get("acceptance_criteria", [])
            if acs:
                parts.append("Kabul Kriterleri:\n" + "\n".join(f"- {a}" for a in acs[:10]))

        # Implementation bilgisi (step 7 sonrasi)
        if s.branch_name and step_key not in (
            "requirements_analysis_task", "discover_repos_task",
            "dependency_analysis_task", "technical_design_task",
            "create_branch_task",
        ):
            impl = [f"\n# Implementation\nBranch: {s.branch_name}"]
            if s.all_pushes:
                pushed = [p.get("file", "?") for p in s.all_pushes if p.get("file")]
                impl.append(f"Push edilen dosyalar ({len(pushed)}): {', '.join(pushed[:10])}")
            if s.pr_id and s.pr_url:
                impl.append(f"PR #{s.pr_id}: {s.pr_url}")
            parts.append("\n".join(impl))

        # Validation ciktilari (step 11 icin)
        if step_key == "completion_report_task":
            if s.review_text:
                parts.append(f"\n# Kod Inceleme\n{s.review_text[:_cb.cap('REVIEW')]}")
            if s.test_text:
                parts.append(f"\n# Test Planlama\n{s.test_text[:_cb.cap('TEST')]}")
            if s.uat_text:
                parts.append(f"\n# UAT Dogrulama\n{s.uat_text[:_cb.cap('UAT')]}")

        # Vector DB'den benzer onceki isler (step 4 icin)
        if step_key == "technical_design_task" and self._vector_store:
            try:
                similar = self._vector_store.find_similar_jobs(
                    f"WI#{s.work_item_id}: {s.requirements_text[:500]}",
                    limit=2,
                )
                rel = [x for x in similar if x.get("work_item_id") != s.work_item_id]
                if rel:
                    sim_text = "\n".join(
                        f"- WI#{x['work_item_id']} ({x['step']}): {x['content'][:_cb.cap('SIMILAR')]}"
                        for x in rel
                    )
                    parts.append(f"\n# Benzer Onceki Isler\n{sim_text}")
            except Exception:
                pass

        return _cb.measure(step_key, "\n".join(parts))

    def _step_start(self, step_key: str):
        if self.state.job_id:
            try:
                self._db.start_step(self.state.job_id, step_key)
                self._db.update_job(self.state.job_id, current_step=step_key)
            except Exception:
                pass

    def _try_resume_step(self, step_key: str) -> str | None:
        """Onceki job'dan bu step'in basarili ciktisi varsa dondurur.
        Pipeline tekrar calistirildiginda tamamlanmis adimlari atlamak icin.
        CREW_ENABLE_RESUME=0 veya dashboard 'Cache Resume' kapaliysa atlanir."""
        from agile_sdlc_crew import pipeline_config as _pc_resume
        import os as _os_resume
        enabled = _pc_resume.get("CREW_ENABLE_RESUME")
        # env override (geriye uyumluluk)
        if _os_resume.environ.get("CREW_ENABLE_RESUME", "1") == "0":
            enabled = False
        if not enabled:
            return None
        try:
            cached = self._db.get_cached_step_output(step_key, self.state.work_item_id)
            if cached and len(cached.strip()) > 20:
                return cached
        except Exception:
            pass
        return None

    def _resume_step(self, step_key: str, cached_output: str):
        """Cache'ten gelen ciktiyi loglayip step'i tamamlanmis olarak isaretle."""
        _log(f"  ⏩ {step_key} onceki job'dan resume edildi ({len(cached_output)} char)")
        self._step_start(step_key)
        self._step_done(step_key, cached_output[:50_000])

    def _step_done(self, step_key: str, output: str = ""):
        self._tracker.task_completed(step_key)
        if self.state.job_id:
            try:
                self._db.complete_step(self.state.job_id, step_key, output)
            except Exception:
                pass
        # Vector store'a da kaydet (benzer is arama icin)
        import os as _os_sd
        if (
            _os_sd.environ.get("CREW_SAVE_STEP_OUTPUT", "1") != "0"
            and self._vector_store and output and len(output.strip()) > 20
        ):
            try:
                self._vector_store.save_step_output(
                    self.state.work_item_id, step_key, output,
                    metadata={"repo": self.state.repo_name},
                )
            except Exception:
                pass

    def _step_fail(self, step_key: str, error: str):
        if self.state.job_id:
            try:
                self._db.fail_step(self.state.job_id, step_key, error)
            except Exception:
                pass

    def _run_discover_repos(self, candidate_repos: list[str], evidence: dict | None = None, repo_history: list[dict] | None = None) -> None:
        """LLM'e en alakali aday repo'larin summary'lerini ver, hedef repo'yu sec.

        Vector + BM25 hibrid arama genelde 0.02-0.03 araliginda skor verir
        (Flo monorepolari hepsi PHP/Butterfly + e-commerce semantigi); LLM
        tablo/model/migration kanıtlarına bakarak daha guvenilir karar verir.
        Sonuc DB'ye yazilir ve technical_design context'ine hint olarak eklenir.
        Karar verilemezse pipeline akisi etkilenmez — architect yine de calisir.
        """
        import json as _json
        self._step_start("discover_repos_task")

        if not candidate_repos:
            self._step_done("discover_repos_task", "Atlandı — aday repo yok")
            return

        # Aday'lar icin tam summary'leri toplayalim (architect'in karar
        # vermesi icin model + tablo + migration listesi onemli)
        adaylar = []
        for rname in candidate_repos:
            s = self._repo_mgr.get_repo_summary(rname)
            if not s:
                continue
            # Generic dizin ozetini at, geri kalan her sey: framework,
            # README, Domain, DB Tablolari, Migrationlar
            short_lines = []
            for line in s.split("\n"):
                if line.startswith("## Ust Seviye Dizinler"):
                    break
                short_lines.append(line)
            adaylar.append((rname, "\n".join(short_lines).strip()))

        if not adaylar:
            self._step_done("discover_repos_task", "Atlandı — summary'ler bos")
            return

        prompt_user = (
            f"# Is Kalemi\nWI #{self.state.work_item_id}\n\n"
            f"# Gereksinim Analizi (BA Ciktisi)\n{self.state.requirements_text[:3500]}\n\n"
            f"# Aday Repo Ozetleri (en alakali {len(adaylar)})\n"
        )
        for name, summary in adaylar:
            # Her aday icin ~10K char — buyuk monolithlerde (orkestra ~14KB)
            # tablo + model listesi cikti, LLM'in kanıt görmesi icin gerekli.
            # 20 aday × 10K = ~200KB; Claude Sonnet/Opus icin sorunsuz.
            prompt_user += f"\n=================== {name} ===================\n{summary[:10000]}\n"

        # Birebir kod kaniti — isim benzerliginden GUCLU sinyal.
        evidence_block = ""
        if evidence:
            ev_sorted = sorted(
                evidence.items(),
                key=lambda kv: (len(kv[1].get("exclusive", [])), len(kv[1].get("symbols", []))),
                reverse=True,
            )
            ev_lines = []
            for repo, e in ev_sorted:
                excl = e.get("exclusive", [])
                excl_txt = f"  ← YALNIZCA bu repoda: {excl}" if excl else ""
                ev_lines.append(f"- {repo}: {e.get('symbols', [])}{excl_txt}")
            evidence_block = (
                "\n\n# BIREBIR KOD KANITI (grep ile dogrulandi — repo ADI benzerliginden GUCLU)\n"
                "Asagidaki tanimlayicilar WI'da geciyor ve repolarin KODUNDA birebir bulundu.\n"
                "Bir sembol YALNIZCA tek repoda geciyorsa, o repo gercek SAHIPTIR. "
                "Repo ADI benzerligine ALDANMA: ornegin 'stock_api_list' sembolu "
                "'stock-api' adli microservice'te DEGIL, ekranin yasadigi baska bir "
                "repoda birebir geciyor olabilir — kod kaniti isim eslesmesini EZER.\n"
                + "\n".join(ev_lines)
            )
        prompt_user += evidence_block
        if repo_history:
            hist_lines = []
            for s in repo_history:
                hist_lines.append(
                    f"- {s['repo']} (skor {s['score']}, benzer iş: "
                    + ", ".join(f"#{w}" for w in s.get('supporting_wis', [])[:3])
                    + "; örnek dosyalar: "
                    + ", ".join(s.get('file_paths_evidence', [])[:3]) + ")"
                )
            prompt_user += (
                "\n\n# BENZER GEÇMİŞ İŞLER (başarılı PR'lar şu repolarda yapıldı — ADVISORY)\n"
                "Bu sinyal repo ADI benzerliğinden GÜÇLÜ, birebir KOD KANITINDAN zayıftır.\n"
                + "\n".join(hist_lines)
            )
        prompt_user += (
            "\n\nGOREV: Bu is kalemi (WI) yukaridaki aday repo'lardan HANGISINDE "
            "yapilmalidir? Tek bir repo sec. Oncelik sirasi:\n"
            "1) BIREBIR KOD KANITI'nda bir sembol YALNIZCA tek repoda geciyorsa, "
            "o repo cok guclu adaydir — repo adi baska bir seye benzese bile onu sec.\n"
            "2) Sonra WI'da gecen tablo/model/dosya yukaridaki aday'larin "
            "'DB Tablolari & Migrationlar' / 'Domain Bilesenleri > Model' "
            "listelerinde gectigine bak. Birden fazla aday'da geciyorsa modelin "
            "yasadigi (sahibi) repoyu sec.\n"
            "3) Repo adi benzerligi EN ZAYIF sinyaldir; tek basina yeterli degil.\n\n"
            "Cikti SADECE su JSON formatinda olsun (markdown kullanma):\n"
            '{"target_repo": "repo_adi", "reason": "kanit + neden (1-2 cumle, '
            'Turkce, tablo/model/dosya/sembol belirt)", "alternatives": ["ikinci_aday", "ucuncu_aday"]}'
        )

        try:
            from agile_sdlc_crew.llm import build_for_agent
            llm = build_for_agent("software_architect")
            raw = llm.call([
                {"role": "system", "content": (
                    "You are a senior software architect. Pick the single most likely "
                    "target repo for the given work item, citing concrete table/model/file "
                    "evidence from the candidate summaries. Respond with ONLY a JSON object."
                )},
                {"role": "user", "content": prompt_user},
            ])
        except Exception as e:
            _log(f"  discover_repos LLM hatasi: {e} — atlanyor")
            self._step_done("discover_repos_task", f"LLM hatasi: {e}")
            return

        raw_s = raw if isinstance(raw, str) else str(raw)
        start = raw_s.find("{")
        end = raw_s.rfind("}")
        parsed: dict = {}
        target = ""
        reason = ""
        if start >= 0 and end > start:
            try:
                parsed = _json.loads(raw_s[start:end + 1])
                target = (parsed.get("target_repo") or "").strip()
                reason = (parsed.get("reason") or "").strip()
            except Exception as e:
                _log(f"  discover_repos JSON parse hatasi: {e}")

        known = set(self.state.known_repos)
        if target and target not in known:
            _log(f"  discover_repos uyari: LLM '{target}' donduerdu, known_repos'ta yok — gormezden gelinecek")
            target = ""

        if target:
            # discover_repos GERCEK symbol-grep kaniti uzerinde akil yuruten LLM
            # adimi — repo karari icin en guvenilir sinyal. step4 bunu otorite
            # olarak kullanir (architect tool'suzken kor; kendi repo_name'ini
            # halusine edebiliyor — bkz. WI #66511 orkestra/webservice).
            self._discovered_repo = target
            self._discovered_alternatives = [
                a for a in (parsed.get("alternatives", []) or []) if a in known
            ]
            _log(f"  Discover repo onerisi: {target} — {reason[:200]}")
            self._step_done(
                "discover_repos_task",
                _json.dumps(parsed, ensure_ascii=False, indent=2),
            )
        else:
            self._step_done(
                "discover_repos_task",
                f"LLM secim yapamadi, raw cikti: {raw_s[:500]}",
            )

    def _grep_symbol_evidence(self, max_symbols: int = 8) -> dict:
        """WI'daki ayirt edici tanimlayicilari (snake_case, camelCase) TUM
        repolarin kodunda BIREBIR grep'le ara.

        Fuzzy isim eslesmesi tehlikeli: 'stock_api_list' WI'da gecince
        repo adi 'stock-api' (parca: stock+api) yuksek skor aliyor — ama
        sembol aslinda 'orkestra'da birebir geciyor. Bir sembol YALNIZCA
        tek repoda geciyorsa o repo gercek sahiptir; bu, isim benzerligini
        ezmeli.

        Returns: {repo: {"symbols": [...], "exclusive": [...]}}
                 exclusive = yalnizca o repoda gecen semboller (en guclu).
                 Eslesme yoksa {}.
        """
        import re as _re_se
        import subprocess as _sp_se
        from pathlib import Path as _P_se

        text = f"{self.state.requirements_text} {self.state.kickoff_text}"
        # BA/architect JSON sema anahtarlari ve pipeline jenerik terimleri —
        # WI icerigi DEGIL, gurultu. Bunlar repolarin kodunda da gectigi icin
        # (orn. project-hal'da 'acceptance_criteria') yanlis exclusive kaniti
        # uretir; sembol cikariminda atla.
        _SYMBOL_STOPWORDS = {
            "functional_requirements", "technical_requirements", "acceptance_criteria",
            "out_of_scope", "open_questions", "breaking_changes", "migration_notes",
            "security_perf_notes", "alternatives_considered", "covers_requirements",
            "current_code", "new_code", "change_type", "file_path", "work_item_id",
            "repo_name", "start_marker", "end_marker", "test_plan", "expected_output",
            "scrum_master_feedback", "previous_context", "target_repo",
        }
        symbols: list[str] = []
        seen: set[str] = set()
        # snake_case ≥2 segment, >=8 char: stock_api_list, scheduled_delivery_date_range
        for m in _re_se.finditer(r'\b([a-z][a-z0-9]+(?:_[a-z0-9]+){1,5})\b', text):
            s = m.group(1)
            if len(s) >= 8 and s.lower() not in seen and s.lower() not in _SYMBOL_STOPWORDS:
                seen.add(s.lower()); symbols.append(s)
        # camelCase/PascalCase ≥1 hump, >=6 char: getStocks, OrderAddress
        for m in _re_se.finditer(r'\b([A-Za-z][a-z0-9]+(?:[A-Z][a-z0-9]+){1,4})\b', text):
            s = m.group(1)
            if len(s) >= 6 and s.lower() not in seen and s.lower() not in _SYMBOL_STOPWORDS:
                seen.add(s.lower()); symbols.append(s)
        if not symbols:
            return {}
        symbols.sort(key=len, reverse=True)  # en spesifik once
        symbols = symbols[:max_symbols]

        base = self._repo_mgr.base_dir
        known = set(self.state.known_repos)
        sym_to_repos: dict[str, set] = {}
        for sym in symbols:
            try:
                res = _sp_se.run(
                    ["grep", "-rlF",
                     "--include=*.php", "--include=*.py", "--include=*.ts",
                     "--include=*.tsx", "--include=*.js", "--include=*.go",
                     "--include=*.cs", "--include=*.java", "--include=*.vue",
                     "--exclude-dir=vendor", "--exclude-dir=node_modules",
                     "--exclude-dir=.git",
                     sym, str(base)],
                    capture_output=True, text=True, timeout=15,
                )
            except Exception:
                continue
            if res.returncode != 0 or not res.stdout.strip():
                continue
            repos_for_sym: set[str] = set()
            for line in res.stdout.strip().split("\n"):
                try:
                    repo = _P_se(line).relative_to(base).parts[0]
                except Exception:
                    continue
                if repo in known:
                    repos_for_sym.add(repo)
            if repos_for_sym:
                sym_to_repos[sym] = repos_for_sym

        by_repo: dict[str, dict] = {}
        for sym, repos in sym_to_repos.items():
            is_exclusive = len(repos) == 1
            for repo in repos:
                e = by_repo.setdefault(repo, {"symbols": [], "exclusive": []})
                e["symbols"].append(sym)
                if is_exclusive:
                    e["exclusive"].append(sym)
        return by_repo

    # Budget'a dahil OLMAYAN adimlar — local Ollama LLM kullanan step'ler.
    # Bu step'ler bedava (local model), budget sadece harici API cagrilarini sayar.
    _LOCAL_STEPS = frozenset({
        "kickoff_meeting_task",
        "requirements_analysis_task",
        # review_retry_implement_* prefix'i asagida kontrol edilir
    })

    def _is_local_step(self, step_name: str) -> bool:
        """Step local Ollama model mi kullaniyor?"""
        if step_name in self._LOCAL_STEPS:
            return True
        # review_retry_implement_0, review_retry_implement_1 vb.
        # CREW_USE_LOCAL_LLM=1 ise developer da local → retry implement de local
        import os as _os_local
        if step_name.startswith("review_retry_implement_"):
            if _os_local.environ.get("CREW_USE_LOCAL_LLM", "").lower() in ("1", "true", "yes"):
                # Developer local ise retry implement de local
                if _os_local.environ.get("CREW_LOCAL_DEVELOPER", "1").lower() not in ("0", "false", "no"):
                    return True
        return False

    def _track_and_check_budget(self, crew_result, step_name: str = ""):
        """Her crew.kickoff() sonrasi token kullanimini topla ve budget check yap.
        CREW_MAX_JOB_COST (USD, default 5.0) asilirsa RuntimeError fırlat.
        SADECE harici LLM (Sonnet/o4 vb) cagrilarini sayar — local Ollama bedava."""
        import os as _os
        usage = getattr(crew_result, "token_usage", None)
        if not usage:
            return
        try:
            pt = int(getattr(usage, "prompt_tokens", 0) or 0)
            ct = int(getattr(usage, "completion_tokens", 0) or 0)
            tt = int(getattr(usage, "total_tokens", 0) or 0) or (pt + ct)
        except Exception:
            return

        is_local = self._is_local_step(step_name)

        if not is_local:
            self._job_prompt_tokens += pt
            self._job_completion_tokens += ct
            self._job_total_tokens += tt

        # Approximate USD cost (Sonnet 4: $3/M input, $15/M output)
        from agile_sdlc_crew import pipeline_config as _pc
        price_in = _pc.get("CREW_PRICE_INPUT_USD_PER_M")
        price_out = _pc.get("CREW_PRICE_OUTPUT_USD_PER_M")
        cost = (
            self._job_prompt_tokens * price_in + self._job_completion_tokens * price_out
        ) / 1_000_000.0

        max_cost = _pc.get("CREW_MAX_JOB_COST")
        if step_name:
            local_tag = " [LOCAL]" if is_local else ""
            _log(
                f"  💰 Token: {tt} (+{pt}i/{ct}o){local_tag} | Harici toplam: "
                f"{self._job_total_tokens} ≈ ${cost:.3f} / ${max_cost:.2f}"
            )
        if cost > max_cost:
            _log(
                f"  🚨 BUDGET ASILDI: ${cost:.2f} > ${max_cost:.2f} "
                f"(prompt:{self._job_prompt_tokens} + completion:{self._job_completion_tokens} token)"
            )
            # WI'ya yorum at
            try:
                from agile_sdlc_crew.main import _add_wi_comment
                _add_wi_comment(
                    self._client, self.state.work_item_id,
                    f"## 💰 Maliyet Limiti Asildi — Pipeline Durduruldu\n\n"
                    f"Bu is icin {self._job_total_tokens:,} token kullanildi "
                    f"(yaklasik **${cost:.2f}**), konfigure edilmis limit "
                    f"**${max_cost:.2f}**.\n\n"
                    f"Pipeline guvenlik icin `{step_name}` adiminda durduruldu. "
                    f"Is kaleminin karmasiklik/veri miktarini gozden gecirip tekrar kuyruga ekleyin "
                    f"veya `CREW_MAX_JOB_COST` env'ini artirip yeniden baslatin.\n\n"
                    f"---\n*Agile SDLC Crew - Budget Guard*"
                )
            except Exception:
                pass
            raise RuntimeError(
                f"Job budget exceeded at {step_name}: ${cost:.2f} (limit ${max_cost:.2f})"
            )

    def _scrum_review(self, step_name: str, output: str) -> tuple[bool, str]:
        """Scrum Master ciktiyi inceler. CREW_SM_REVIEW=1 ile aktif edilir (default: kapali).
        Her cagrida ayri API call yapar — token tasarrufu icin default kapali."""
        from agile_sdlc_crew import pipeline_config as _pc
        if not _pc.get("CREW_SM_REVIEW"):
            return True, ""
        try:
            review_crew = self._agile_crew.create_scrum_review_crew()
            result = review_crew.kickoff(inputs={
                "step_name": step_name,
                "step_output": (output or "")[:4000],
                "work_item_id": self.state.work_item_id,
            })
            raw = result.raw or ""
            up = raw.upper()
            # Accept both new (English) and legacy (Turkish) decision tokens
            rejected = any(tok in up for tok in ("IMPROVE", "IYILESTIR", "İYİLEŞTİR"))
            _log(f"  SM Review ({step_name}): {'IMPROVE' if rejected else 'APPROVE'}")
            return (not rejected), raw
        except Exception as e:
            _log(f"  SM Review hatasi: {e}")
            return True, ""

    def _prefetch_pr_changes_context(self, max_files: int = 12, per_file: int = 6000) -> str:
        """Reviewer'in get_pr_changes/dosya-okuma tool'larini (her biri ayri claude_cli
        subprocess'i) cagirmadan inceleyebilmesi icin: PR'da degisen dosyalarin
        feature-branch icerigini context'e hazirla. Architect pre-fetch deseni.
        Hata olursa bos string — reviewer yine tool'larla okuyabilir."""
        repo = self.state.repo_name
        branch = self.state.branch_name
        if not repo or not branch:
            return ""
        files: list[str] = []
        for ch in (self.state.plan.get("changes") or []):
            fp = ch.get("file_path")
            if fp and fp not in files:
                files.append(fp)
        for p in (self.state.all_pushes or []):
            fp = p.get("file")
            if fp and fp not in files:
                files.append(fp)
        if not files:
            return ""
        parts = [
            f"\n# PR DEĞİŞİKLİKLERİ (PR #{self.state.pr_id}, repo {repo}, branch {branch} — feature branch içerikleri HAZIR)",
            "⚡ Aşağıdaki dosya içerikleri context'te zaten var. get_pr_changes / browse_repo "
            "ÇAĞIRMA — doğrudan bu içerikleri WI gereksinimlerine ve kabul kriterlerine göre incele. "
            "Sadece context'te OLMAYAN bir dosyaya ihtiyaç duyarsan tool kullan.",
        ]
        n = 0
        for fp in files[:max_files]:
            content = ""
            try:
                content = self._client.get_file_content(repo, fp, branch)
            except Exception:
                try:
                    content = self._repo_mgr.get_file_content(repo, fp, branch)
                except Exception:
                    content = ""
            if not content or not content.strip():
                continue
            trunc = content[:per_file] + ("\n... (kısaltıldı)" if len(content) > per_file else "")
            parts.append(f"\n## {fp}\n```\n{trunc}\n```")
            n += 1
        if n == 0:
            return ""
        _log(f"  Review pre-fetch: {n} değişen dosya context'e eklendi (tool adımları kısaldı)")
        return "\n".join(parts)

    def _review_retry_loop(self):
        """Reviewer RED verdikten sonra: implement → push → review dongusune girer.
        Branch ve PR zaten var — sadece dosyalari duzeltip push eder, sonra tekrar review.
        Bu metod step8_code_review icerisinden cagrilir, max iteration kontrolu orada yapilir."""
        from agile_sdlc_crew.main import _extract_code_from_output, _validate_code, _add_wi_comment
        from agile_sdlc_crew.pipeline import push_file

        _log("\n-- REVIEW RETRY: Dosyalar duzeltiliyor --")

        plan = self.state.plan
        repo_name = self.state.repo_name
        branch = self.state.branch_name

        # Reviewer feedback'inden hangi dosyalarin sorunlu oldugunu cikar
        # Sadece bahsedilen dosyalari tekrar implement et — geri kalani dokunma
        import re as _re_retry
        review_feedback = self.state.review_text or ""
        review_lower = review_feedback.lower()
        changes_to_fix = []
        for change in plan.get("changes", []):
            fp = change.get("file_path", "")
            if not fp:
                continue
            # Dosya adi veya son parcasi reviewer metninde geciyorsa sorunlu
            fname = fp.rsplit("/", 1)[-1].lower()
            if fname in review_lower or fp.lower() in review_lower:
                changes_to_fix.append(change)
        # Eger reviewer spesifik dosya belirtmediyse tum dosyalari duzelt
        if not changes_to_fix:
            changes_to_fix = [c for c in plan.get("changes", []) if c.get("file_path")]
            _log(f"  Reviewer spesifik dosya belirtmedi, tum {len(changes_to_fix)} dosya duzeltilecek")
        else:
            _log(f"  Reviewer {len(changes_to_fix)}/{len(plan.get('changes', []))} dosyada sorun bildirdi")

        for i, change in enumerate(changes_to_fix):
            file_path = change.get("file_path", "")

            _log(f"  Retry implement [{i+1}/{len(changes_to_fix)}]: {file_path}")

            # Onceki push'taki icerigi al (mevcut dosya) — reviewer geri bildirimi ile duzelt
            existing_content = ""
            try:
                existing_content = self._client.get_file_content(repo_name, file_path, branch)
            except Exception:
                existing_content = change.get("new_code", "")

            ctx = self._build_step_context("implement_change_task")
            code_crew = self._agile_crew.create_code_crew()
            code_result = code_crew.kickoff(inputs={
                "work_item_id": self.state.work_item_id,
                "target_repo": repo_name,
                "target_file": file_path,
                "change_description": change.get("description", ""),
                "current_code": change.get("current_code", ""),
                "new_code": change.get("new_code", ""),
                "full_content": existing_content,
                "previous_context": ctx,
            })
            self._track_and_check_budget(code_result, f"review_retry_implement_{i}")

            new_content = _extract_dev_output(code_result)
            if not new_content or len(new_content.strip()) < 30:
                _log(f"    Developer bos/kisa cikti, atlaniyor")
                continue

            # ── Guvenlik Kontrolleri (push oncesi) — main implement ile ayni ──
            orig_len = len(existing_content.strip()) if existing_content else 0
            new_len = len(new_content.strip())
            orig_lines = existing_content.count("\n") if existing_content else 0
            new_lines = new_content.count("\n")

            # Cok az icerik — 3 satirdan kisa veya 50 char altinda
            if new_len < 50 or new_lines < 3:
                _log(f"    GUVENLIK: cok kisa icerik ({new_lines} satir, {new_len} char), retry push iptal")
                continue

            # Buyuk kod kaybi — orijinal >500 char ve %50'den kisa => agent bozuk cikti verdi
            if existing_content and orig_len > 500 and new_len < orig_len * 0.5:
                _log(
                    f"    🚨 GUVENLIK ALARMI (retry): dosya %{100 - int(100 * new_len / orig_len)} kuculdu "
                    f"({orig_lines} → {new_lines} satir, {orig_len} → {new_len} char). "
                    f"Agent timeout/truncate sonrasi tam dosya yerine parca dondu. Retry push IPTAL."
                )
                continue

            push_result = push_file(
                repo_name, branch, file_path, new_content,
                f"fix: review feedback - {change.get('description', '')[:60]} (WI #{self.state.work_item_id})",
                repo_mgr=self._repo_mgr, dry_run=self.state.dry_run,
            )
            if push_result.get("success"):
                _log(f"    Push OK: {file_path}")
            else:
                _log(f"    Push HATA: {push_result.get('error', '?')}")

        _log("  Review retry: dosyalar guncellendi, tekrar review yapiliyor")

        # Tekrar review
        ctx = self._build_step_context("review_pr_task")
        ctx += self._prefetch_pr_changes_context()
        ctx += self._test_requirement_note(self.state.repo_name)
        review_crew = self._agile_crew.create_review_crew()
        review_result = review_crew.kickoff(inputs={
            "work_item_id": self.state.work_item_id,
            "requirements": self.state.requirements_text[:3000],
            "target_repo": self.state.repo_name,
            "target_branch": self.state.branch_name,
            "pr_id": self.state.pr_id,
            "pr_url": self.state.pr_url,
            "previous_context": ctx,
            "scrum_master_feedback": "",
        })
        self._track_and_check_budget(review_result, "review_pr_task (retry)")
        review_text = review_result.raw or ""
        self.state.review_text = review_text

        # Re-check — still rejected? (yalniz acik karar tokenina bakar)
        still_rejected = _review_rejected(review_text)
        if still_rejected:
            # step8_code_review'daki max retry kontrolune don
            review_attempt = getattr(self, "_review_attempt", 0)
            from agile_sdlc_crew import pipeline_config as _pc_rev2
            max_review_retries = _pc_rev2.get("CREW_REVIEW_MAX_RETRIES")
            if review_attempt < max_review_retries:
                self._review_attempt = review_attempt + 1
                _log(f"  🔄 Reviewer hala RED — tekrar deneniyor (deneme {self._review_attempt}/{max_review_retries})")
                self._review_retry_loop()
                return
            else:
                _log(f"  🚨 Reviewer {max_review_retries} deneme sonrasi hala RED — pipeline durduruluyor")
                _add_wi_comment(self._client, self.state.work_item_id,
                    f"## ❌ Kod İnceleme — {max_review_retries} Düzeltme Sonrası Hâlâ Başarısız\n\n"
                    f"PR: [#{self.state.pr_id}]({self.state.pr_url})\n\n"
                    f"**Son Değerlendirme:**\n{review_text[:2000]}\n\n"
                    f"---\n*Agile SDLC Crew - Review Retry Exhausted*"
                )
                self._step_fail("review_pr_task", f"Reviewer: {max_review_retries} deneme sonrasi RED")
                raise RuntimeError(f"Reviewer {max_review_retries} deneme sonrasi reddediyor")

        # Onay geldi
        self._step_done("review_pr_task", review_text[:3000])
        _log(f"  ✅ Review retry basarili — kod onaylandi")
        _add_wi_comment(self._client, self.state.work_item_id,
            f"## ✅ Kod İnceleme (Düzeltme Sonrası Onay)\n\n"
            f"PR: [#{self.state.pr_id}]({self.state.pr_url})\n\n"
            f"{review_text[:2000]}\n\n"
            f"*Agile SDLC Crew - Review Retry Onay*"
        )

    # ── Flow Start ───────────────────────────────────

    def _reset_job_state(self):
        """Job basinda paylasimli/birikimli durumu sifirla (cross-job sizinti onleme)."""
        from agile_sdlc_crew.tools.tool_cache import reset_tool_cache
        reset_tool_cache()
        try:
            from agile_sdlc_crew.tools.claude_cli_llm import clear_repo_ctx
            clear_repo_ctx()
        except Exception:
            pass
        self._job_prompt_tokens = 0
        self._job_completion_tokens = 0
        self._job_total_tokens = 0
        # discover_repos'un kanit-temelli repo karari — step4 otoritesi
        self._discovered_repo = ""
        self._discovered_alternatives = []

    @start()
    def initialize(self):
        """Pipeline baslangici: client'lar olustur, tracker'i baslat."""
        from agile_sdlc_crew.crew import AgileSDLCCrew
        from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient
        from agile_sdlc_crew.tools.local_repo import LocalRepoManager
        from agile_sdlc_crew.tools.vector_store import VectorStore
        from agile_sdlc_crew import db as _db

        # Pipeline basi: birikimli durumu sifirla
        self._reset_job_state()

        self._db = _db
        self._agile_crew = AgileSDLCCrew()
        self._agile_crew.set_status_tracker(self._tracker)
        self._client = AzureDevOpsClient()
        self._vector_store = VectorStore()
        self._repo_mgr = LocalRepoManager()
        self._repo_mgr.vector_store = self._vector_store

        # Resolve dry_run from DB row (set when job was queued) or env override
        import os as _os_init
        _dry_env = _os_init.environ.get("CREW_DRY_RUN", "").lower() in ("1", "true", "yes")
        if self.state.job_id:
            try:
                _job = _db.get_job(self.state.job_id)
                if _job and bool(_job.get("dry_run")):
                    self.state.dry_run = True
            except Exception:
                pass
        if _dry_env:
            self.state.dry_run = True
        if self.state.dry_run:
            _log(f"  🔬 DRY-RUN modu aktif — push/PR/review/test/UAT atlanacak, sonuc lokal kalacak")

        # Tum repolari listele; fetch YAPMA — sadece eksik olanlari clone et.
        # Hedef repo'nun fetch'i step5_create_branch icinde yapilir.
        repos = self._client.list_repositories()
        self.state.known_repos = [r.get("name", "") for r in repos]
        new_clones = 0
        for repo in repos:
            name = repo.get("name", "")
            clone_url = repo.get("remoteUrl", "")
            if name and clone_url:
                # Zaten local'de varsa skip; yoksa clone
                repo_dir = self._repo_mgr.base_dir / name
                already_exists = repo_dir.exists() and (repo_dir / ".git").exists()
                try:
                    self._repo_mgr.ensure_repo(name, clone_url, fetch=False)
                    if not already_exists:
                        new_clones += 1
                except Exception as e:
                    _log(f"  Repo clone hatasi ({name}): {e}")
        if new_clones > 0:
            _log(f"  {new_clones} yeni repo clone edildi (diger repolar fetch edilmedi, hiz icin)")

        # Workspace cleanup (KESIF FAZI): sadece BU WI'nin onceki run'larindan
        # kalan artik feature branch'i olan repolari temizle. Tum repolari
        # taramak gereksiz: yeni job icin sadece kendi WI artigi yaniltici.
        # Aday repo (architect'in secip uzerinde calisacagi) step5'te tam
        # reset gorur; baska WI'lara ait branch'lere DOKUNULMAZ (kullanici
        # paralel calisma yapiyor olabilir).
        _stale_branch = f"feature/{self.state.work_item_id}"
        _log(f"  Workspace cleanup: '{_stale_branch}' artigi olan repolari ariyorum...")
        _cleaned = 0
        for _rname in self.state.known_repos:
            _rdir = self._repo_mgr.base_dir / _rname
            if not (_rdir / ".git").exists():
                continue
            try:
                _has_stale = self._repo_mgr._git(
                    ["rev-parse", "--verify", "--quiet", _stale_branch], cwd=_rdir,
                ).returncode == 0
                if not _has_stale:
                    continue
                # Bu repo onceki bir job'da bu WI icin dokunulmus → reset
                self._repo_mgr._git(["fetch", "origin", "main"], cwd=_rdir)
                self._repo_mgr._git(["checkout", "main"], cwd=_rdir)
                self._repo_mgr._git(["reset", "--hard", "origin/main"], cwd=_rdir)
                # Clean -fd: untracked dosyalari sil ama gitignored olanlari
                # KORU (vendor/, node_modules/, .env vb.). REPO_SUMMARY.md
                # gitignore'da olmadigi icin -e ile ayrica exclude ediyoruz.
                # Local feature branch'i SILINMEZ — step5'te taze yeniden
                # olusturulacak (kod duzenleme fazinin basi).
                self._repo_mgr._git(
                    ["clean", "-fd", "-e", "REPO_SUMMARY.md"], cwd=_rdir,
                )
                _log(f"  Onceki job artigi temizlendi: {_rname}")
                _cleaned += 1
            except Exception as _e:
                _log(f"  Repo cleanup hatasi ({_rname}): {_e}")
        if _cleaned:
            _log(f"  Workspace cleanup: WI #{self.state.work_item_id} artigi {_cleaned} repoda temizlendi")
        else:
            _log("  Workspace cleanup: bu WI'ya ait artik yok")

        # Tum REPO_SUMMARY.md'leri vector DB'ye embed et
        # (Agent 'hangi repo' sorusuna semantic arama ile cevap bulabilsin)
        # Sirayla embed et — Ollama'ya paralel istek gitmiyor ama model swap
        # sirasinda 500 verebilir. 0.1s araliklarla gondererek stabilize et.
        import time as _embed_time
        _log("  REPO_SUMMARY.md'ler vector DB'ye embed ediliyor...")
        indexed = 0
        regenerated = 0
        for name in self.state.known_repos:
            try:
                repo_dir = self._repo_mgr.base_dir / name
                summary_path = repo_dir / "REPO_SUMMARY.md"
                # Eksikse yeniden olustur (workspace cleanup sirasinda silinmis
                # veya repo yeni clone'lanmis olabilir).
                if not summary_path.exists():
                    self._repo_mgr.generate_repo_summary(name)
                    if summary_path.exists():
                        regenerated += 1
                if summary_path.exists():
                    self._vector_store.index_repo_summary(name, repo_dir)
                    indexed += 1
                    _embed_time.sleep(0.1)  # Ollama throttle
            except Exception as e:
                _log(f"  Summary index hatasi ({name}): {e}")
        msg = f"  {indexed}/{len(self.state.known_repos)} repo summary embed edildi"
        if regenerated:
            msg += f" ({regenerated} regenerate edildi)"
        _log(msg)

        # Geçmiş-iş repo önerisi açıksa ve indeks boşsa DB'den geri-doldur
        # (indeks boş kaldıkça her başlatmada denenir; boşken maliyet tek SQL sorgusu,
        #  ilk başarılı iş sonrası record_count ile erken çıkar)
        from agile_sdlc_crew import pipeline_config as _pc_bf
        if _pc_bf.get("CREW_REPO_HISTORY_SUGGEST"):
            try:
                _info = self._vector_store.storage.get_scope_info("/repo-decisions")
                _empty = not _info or _info.record_count == 0
            except Exception:
                _empty = True
            if _empty:
                try:
                    n = self._vector_store.backfill_repo_decisions(self._db)
                    _log(f"  Repo-decision indeksi geri-dolduruldu: {n} iş")
                except Exception as e:
                    _log(f"  Repo-decision backfill hatası: {e}")
            else:
                _log(f"  Repo-decision indeksi mevcut ({_info.record_count} kayıt), backfill atlandı")

        # repo_mgr ve vector_store'u crew'a aktar (agent tool'lari icin)
        self._agile_crew.local_repo_mgr = self._repo_mgr
        self._agile_crew.vector_store = self._vector_store

        self._tracker.start(self.state.work_item_id)
        _log(f"\n  Pipeline baslatildi: WI #{self.state.work_item_id}, {len(self.state.known_repos)} repo hazir")

    # ── Router: HAL vs CrewAI ────────────────────────

    @router(initialize)
    def route_planning_mode(self):
        """HAL modu veya CrewAI modu secimi."""
        if self.state.use_hal:
            return "hal_planning"
        return "crew_planning"

    # ── HAL Planning Path ────────────────────────────

    @listen("hal_planning")
    def hal_planning(self):
        """HAL modunda planlama: tek adimda analiz + tasarim."""
        from agile_sdlc_crew.hal_client import HALClient
        from agile_sdlc_crew.main import (
            _resolve_repo_name,
            _enrich_plan_with_agent,
            _add_wi_comment,
        )

        _log("\n-- PLANLAMA (HAL modu) --")
        hal = HALClient()
        hal.login()
        self._hal = hal
        _log("  HAL login basarili")

        hal_detail = hal.analyze_work_item(self.state.work_item_id)
        hal_parsed = hal.parse_analysis_response(hal_detail)

        repo_name = _resolve_repo_name(
            hal_parsed.get("repo_name", ""),
            self.state.known_repos,
            self._client,
            self.state.work_item_id,
        )
        self.state.repo_name = repo_name

        plan = {
            "work_item_id": self.state.work_item_id,
            "repo_name": repo_name,
            "summary": hal_parsed.get("summary", ""),
            "changes": [],
            "acceptance_criteria": [],
        }
        for hc in hal_parsed.get("changes", []):
            plan["changes"].append({
                "file_path": hc["path"],
                "change_type": hc.get("change_type", "edit"),
                "description": hc.get("description", ""),
                "current_code": hc.get("current_code", ""),
                "new_code": hc.get("code", ""),
            })
        self.state.requirements_text = hal_parsed.get("raw_response", "")
        _log(f"  HAL analiz tamamlandi: repo={repo_name}, {len(plan['changes'])} dosya")

        # Degisiklik yoksa ayni sohbette tekrar sor
        if not plan["changes"]:
            _log("  HAL degisiklik bulamadi, ayni sohbette detay isteniyor...")
            retry_detail = hal.followup(
                f"Dosya yollarini ve mevcut/yeni kod bloklarini goster. "
                f"Repo: {repo_name}"
            )
            retry_parsed = hal.parse_analysis_response(retry_detail)
            for hc in retry_parsed.get("changes", []):
                plan["changes"].append({
                    "file_path": hc["path"],
                    "change_type": hc.get("change_type", "edit"),
                    "description": hc.get("description", ""),
                    "current_code": hc.get("current_code", ""),
                    "new_code": hc.get("code", ""),
                })
            if retry_parsed.get("raw_response"):
                self.state.requirements_text = retry_parsed["raw_response"]
            _log(f"  HAL followup: {len(plan['changes'])} dosya")

        # HAL modunda ilk 3 adim atlanir
        hal_skip = ["requirements_analysis_task", "discover_repos_task", "dependency_analysis_task"]
        for task_key in hal_skip:
            self._step_done(task_key, "HAL modu ile atlandı")
        if self.state.job_id:
            self._db.skip_steps(self.state.job_id, hal_skip, reason="HAL modu ile atlandı")

        # Eksikleri tamamla
        plan = _enrich_plan_with_agent(
            plan, self._agile_crew, self._client, repo_name,
            self.state.work_item_id, self.state.requirements_text,
            self._tracker, hal=hal, repo_mgr=self._repo_mgr,
        )
        self.state.plan = plan
        self._step_done("technical_design_task", f"Repo: {repo_name}, {len(plan.get('changes', []))} dosya")

        # Planlama yorumu
        files_summary = "\n".join(
            f"- [{ch.get('change_type', 'edit')}] `{ch['file_path']}`: {ch.get('description', '')[:80]}"
            for ch in plan["changes"]
        )
        _add_wi_comment(self._client, self.state.work_item_id,
            f"## Analiz & Teknik Tasarim\n\n"
            f"**Repo:** {repo_name}\n"
            f"**Degisecek dosyalar:**\n{files_summary}\n\n"
            f"*Agile SDLC Crew - Planlama tamamlandi*"
        )

    # ── CrewAI Planning Path ─────────────────────────
    # Sira: Requirements (ön analiz) → Kickoff (teknik tartisma) → Technical Design
    # Kickoff'un anlamli olmasi icin once isin ne oldugu ve hangi repo'da
    # yapilacagi bilinmeli — kör tartisma olmaz.

    @listen("crew_planning")
    def crew_step1_requirements(self):
        """Adim 1: Is Analizi + Yetersizlik Kontrolu + Resim/Link Analizi.
        Pipeline'in ILK adimi — kickoff'tan ONCE calisir. Boylece kickoff
        toplantisinda agentlar is analizi, kabul kriterleri ve hedef repoyu
        zaten bilerek teknik tartisma yapabilir."""
        from agile_sdlc_crew.main import _add_wi_comment
        import re as _re
        import os as _os

        _log("\n-- ADIM 1: Is analizi (kickoff oncesi on analiz) --")

        # Resume: onceki job'dan BA ciktisi varsa atla
        cached_ba = self._try_resume_step("requirements_analysis_task")
        if cached_ba:
            self.state.requirements_text = cached_ba
            # Kabul kriterlerini cache'ten cikar
            import re as _re_resume
            import json as _json_resume
            try:
                jm = _re_resume.search(r'```(?:json)?\s*\n?(.*?)(?:\n?```|$)', cached_ba, _re_resume.DOTALL)
                jt = jm.group(1).strip() if jm else cached_ba
                ba_j = _json_resume.loads(jt)
                for ac in ba_j.get("acceptance_criteria", []):
                    if isinstance(ac, dict):
                        self.state.acceptance_criteria.append(f"{ac.get('id','')}: {ac.get('desc','')}")
                    elif isinstance(ac, str):
                        self.state.acceptance_criteria.append(ac)
            except Exception:
                pass
            self._resume_step("requirements_analysis_task", cached_ba)
            return

        self._step_start("requirements_analysis_task")

        ctx = self._build_step_context("requirements_analysis_task")

        # WI icerigini Python'da oku ve context'e ekle — agent tool
        # cagirmak zorunda kalmasin (local LLM'ler tool'u duzgun cagiramayabiliyor)
        wi_content_length = 0
        wi_ac_plain = ""
        wi_title_raw = ""
        wi_desc_clean = ""
        try:
            wi_full = self._client.get_work_item(int(self.state.work_item_id))
            wi_fields = wi_full.get("fields", {}) if wi_full else {}
            wi_desc_raw = wi_fields.get("System.Description", "") or ""
            wi_title_raw = wi_fields.get("System.Title", "") or ""
            wi_ac_raw = wi_fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", "") or ""
            wi_ac_plain = _re.sub(r'<[^>]+>', ' ', wi_ac_raw).strip()
            wi_desc_clean = _re.sub(r'<[^>]+>', ' ', wi_desc_raw).strip()
            # HTML stripleyip saf metin uzunlugu hesapla
            combined = f"{wi_title_raw} {wi_desc_raw} {wi_ac_raw}"
            combined_plain = _re.sub(r'<[^>]+>', ' ', combined)
            combined_plain = _re.sub(r'\s+', ' ', combined_plain).strip()
            wi_content_length = len(combined_plain)
            _log(f"  WI icerik uzunlugu: {wi_content_length} karakter (plain text)")
            # WI icerigini context'e ekle — BA agent bunu gorsun
            ctx += (
                f"\n\n# WORK ITEM DETAYI (#{self.state.work_item_id})\n"
                f"## Baslik\n{wi_title_raw}\n\n"
                f"## Aciklama\n{wi_desc_clean}\n\n"
                f"## Kabul Kriterleri\n{wi_ac_plain or '(Tanimsiz — description iceriginden cikarilmali)'}\n"
            )
        except Exception as e:
            _log(f"  WI icerik olcumu hatasi: {e}")

        # Mevcut PR varsa yorumlarini oku — onceki denemeden kalan feedback
        # Thread bilgisi state'e kaydedilir: implement sonrasi her yoruma yanit verilir
        _pr_threads_to_respond: list[dict] = []
        _pr_repo_for_threads = ""
        _pr_id_for_threads = 0
        try:
            import re as _re_pr

            # WI relations'dan PR baglantilarini cikar — repo'larda aramak yerine
            # tek API cagrisinda (get_work_item zaten yapildi) PR bilgisi gelir
            _wi_for_pr = self._client.get_work_item(int(self.state.work_item_id))
            _pr_links = []
            for rel in _wi_for_pr.get("relations", []):
                if rel.get("attributes", {}).get("name") == "Pull Request":
                    url = rel.get("url", "")
                    # vstfs:///Git/PullRequestId/{projectId}%2f{repoId}%2f{prId}
                    # NOT: ayrac %2F (buyuk) veya %2f olabiliyor → IGNORECASE sart
                    # (aksi halde %2F'li WI'larin mevcut PR'i/yorumlari bulunamaz)
                    pr_match = _re_pr.search(r'PullRequestId/[^%]+%2f([^%]+)%2f(\d+)', url, _re_pr.IGNORECASE)
                    if pr_match:
                        _pr_links.append({
                            "repo_id": pr_match.group(1),
                            "pr_id": int(pr_match.group(2)),
                        })
            if _pr_links:
                _log(f"  WI relations'da {len(_pr_links)} PR baglantisi bulundu")
                # En yeni PR'den eskiye dogru tara — ilk active'i al, yoksa
                # en son completed'i kullan, abandoned'lari hep atla.
                _pr_links.sort(key=lambda x: x["pr_id"], reverse=True)

                _active_match: tuple[str, int] | None = None
                _completed_match: tuple[str, int] | None = None
                _abandoned_count = 0

                for link in _pr_links:
                    for rname in self.state.known_repos:
                        try:
                            pr_data = self._client.get_pull_request(rname, link["pr_id"])
                        except Exception:
                            continue
                        status = (pr_data.get("status") or "").lower()
                        if status == "active" and _active_match is None:
                            _active_match = (rname, link["pr_id"])
                        elif status == "completed" and _completed_match is None:
                            _completed_match = (rname, link["pr_id"])
                        elif status == "abandoned":
                            _abandoned_count += 1
                        break  # repo bulundu, bu PR icin diger repolari deneme
                    if _active_match:
                        break  # active bulundu, daha eskilere bakma

                chosen = _active_match or _completed_match
                if chosen:
                    _pr_repo_for_threads, _pr_id_for_threads = chosen
                    pr_kind = "active" if _active_match else "completed (active yok)"
                    _log(
                        f"  Mevcut PR bulundu (WI relations): #{_pr_id_for_threads} "
                        f"({_pr_repo_for_threads}) [{pr_kind}]"
                    )
                    existing_pr = {"pr_id": _pr_id_for_threads}
                else:
                    existing_pr = None
                    if _abandoned_count == len(_pr_links):
                        _log(
                            f"  Tum PR baglantilari abandoned ({_abandoned_count}); "
                            "yeni PR olusturulacak"
                        )
                    else:
                        _log("  WI relations'da kullanilabilir PR bulunamadi")
            else:
                existing_pr = None
                _log(f"  WI relations'da PR baglantisi yok")

            if existing_pr and existing_pr.get("pr_id"):
                pr_id = _pr_id_for_threads
                rname = _pr_repo_for_threads

                # Thread'leri oku (resolve edilmemis, insan yorumlari)
                threads = self._client.get_pr_threads(rname, pr_id)
                for thread in threads:
                    if thread.get("properties", {}).get("CodeReviewThreadType"):
                        continue
                    status = thread.get("status", "")
                    if status in ("fixed", "closed", "wontFix", "byDesign"):
                        continue
                    thread_id = thread.get("id")
                    if not thread_id:
                        continue
                    for comment in thread.get("comments", []):
                        if comment.get("commentType") == "system":
                            continue
                        content = comment.get("content", "").strip()
                        author = comment.get("author", {}).get("displayName", "")
                        if content and "Agile SDLC Crew" not in content:
                            file_path = None
                            tc = thread.get("threadContext")
                            if tc:
                                file_path = tc.get("filePath")
                            _pr_threads_to_respond.append({
                                "thread_id": thread_id,
                                "author": author,
                                "content": content,
                                "file_path": file_path,
                            })
                            break  # thread basina ilk insan yorumu yeterli

                if _pr_threads_to_respond:
                    comment_text = "\n".join(
                        f"- [{t.get('file_path') or 'genel'}] {t['author']}: {t['content'][:200]}"
                        for t in _pr_threads_to_respond
                    )
                    ctx += (
                        f"\n\n# MEVCUT PR YORUMLARI (#{pr_id} — resolve edilmesi gereken)\n"
                        f"⚠️ Asagidaki her yorumu dikkate al:\n"
                        f"- Gecerli yorumlar icin plan'a dahil et\n"
                        f"- Gecersiz/yanlis yorumlar icin neden katilmadiginizi acikla\n\n"
                        f"{comment_text}"
                    )
                    _log(f"  {len(_pr_threads_to_respond)} resolve edilmemis PR yorumu context'e eklendi")
        except Exception as e:
            _log(f"  PR yorum okuma hatasi (atlaniyor): {e}")

        # PR thread bilgisini instance'a kaydet — implement sonrasi yanit vermek icin
        self._pr_threads_to_respond = _pr_threads_to_respond
        self._pr_repo_for_threads = _pr_repo_for_threads
        self._pr_id_for_threads = _pr_id_for_threads

        # Resim + Link analizi — description'daki inline media'yi textual'a cevir
        from agile_sdlc_crew import pipeline_config as _pc_media
        if _pc_media.get("CREW_ANALYZE_WI_MEDIA"):
            try:
                from agile_sdlc_crew.tools.wi_media import WIMediaAnalyzer
                wi = self._client.get_work_item(int(self.state.work_item_id))
                wi_desc_raw = wi.get("fields", {}).get("System.Description", "") or ""
                analyzer = WIMediaAnalyzer(self._client)
                enrichment = analyzer.enrich_description(wi_desc_raw)
                if enrichment:
                    ctx += f"\n\n# RESIM + LINK ICERIKLERI (description'dan otomatik cikarildi)\n{enrichment}"
                    _log(f"  WI media analizi: {len(enrichment)} karakter ek bilgi")
                    # Media varsa icerik zenginligi artar — ek karakter olarak say
                    wi_content_length += len(enrichment)
            except Exception as e:
                _log(f"  WI media analizi hatasi (atlaniyor): {e}")

        req_crew = self._agile_crew.create_requirements_crew()
        req_result = req_crew.kickoff(inputs={
            "work_item_id": self.state.work_item_id,
            "previous_context": ctx,
            "scrum_master_feedback": "",
        })
        requirements_text = req_result.raw or ""

        # 🚨 INSUFFICIENCY CHECK — Python-first.
        # We no longer ask the agent "say INSUFFICIENT" (small LLMs copy
        # the keyword from the prompt → wrong decision). Python decides
        # purely on WI content length.
        from agile_sdlc_crew import pipeline_config as _pc_minwi
        MIN_CONTENT_CHARS = _pc_minwi.get("CREW_MIN_WI_CONTENT_CHARS")
        if wi_content_length < MIN_CONTENT_CHARS:
            missing = (
                f"Work item has too little info (only {wi_content_length} characters of content). "
                f"Title + description + acceptance criteria must total at least {MIN_CONTENT_CHARS} characters."
            )
            _log(f"  🚨 WORK ITEM INSUFFICIENT: {missing}")
            _add_wi_comment(self._client, self.state.work_item_id,
                f"## ⚠️ Work Item Insufficient — Development Not Started\n\n"
                f"The work item description is too short ({wi_content_length} characters). "
                f"At least {MIN_CONTENT_CHARS} characters are needed for automated development.\n\n"
                f"Please clarify the following in the work item:\n"
                f"- Description: what will be done, why it's needed\n"
                f"- Acceptance criteria: conditions for success\n"
                f"- Technical detail: which repo/module/file is affected, example/reference\n\n"
                f"After adding the info, you can re-queue the work item.\n\n"
                f"---\n*Agile SDLC Crew - Insufficiency Check*"
            )
            self._step_fail("requirements_analysis_task", f"INSUFFICIENT: {missing}")
            raise RuntimeError(f"Work item insufficient for development: {missing}")

        # Backward compat: old prompts/agents may still emit "INSUFFICIENT:"
        # or "YETERSIZ:" — Python already said "sufficient", silently strip.
        if _re.search(r'(INSUFFICIENT|YETERSIZ)\s*:', requirements_text[:500], _re.IGNORECASE):
            _log(f"  ℹ️  Agent output contained insufficiency keyword, stripping (content is sufficient: {wi_content_length} char)")
            requirements_text = _re.sub(
                r'(INSUFFICIENT|YETERSIZ)\s*:\s*[^\n]*\n?', '', requirements_text, flags=_re.IGNORECASE
            ).lstrip()

        # SM Review
        approved, feedback = self._scrum_review("Is Analizi", requirements_text)
        if not approved:
            _log("  SM iyilestirme istedi, tekrar calistiriliyor...")
            req_crew = self._agile_crew.create_requirements_crew()
            req_result = req_crew.kickoff(inputs={
                "work_item_id": self.state.work_item_id,
                "previous_context": ctx,
                "scrum_master_feedback": f"SCRUM MASTER GERI BILDIRIMI:\n{feedback}",
            })
            requirements_text = req_result.raw or ""

        self.state.requirements_text = requirements_text

        # ── BA JSON Cikarimi ────────────────────────────────────
        # BA artik JSON cikti uretiyor — parse edip state'e kaydet.
        # Basarisiz olursa eski yonteme (serbest metin) dusulur.
        import json as _json_ba
        ba_json = None
        try:
            # Code fence icindeki JSON'u cikar
            json_match = _re.search(r'```(?:json)?\s*\n?(.*?)(?:\n?```|$)', requirements_text, _re.DOTALL)
            json_text = json_match.group(1).strip() if json_match else requirements_text.strip()
            # Brace match
            if not json_text.startswith("{"):
                brace_match = _re.search(r'\{.*\}', json_text, _re.DOTALL)
                if brace_match:
                    json_text = brace_match.group(0)
            ba_json = _json_ba.loads(json_text)
            _log(f"  BA JSON parse basarili: {list(ba_json.keys())}")
        except Exception as e:
            _log(f"  BA JSON parse basarisiz ({e}), serbest metin olarak devam ediliyor")

        # ── Kabul Kriterleri Cikarimi ────────────────────────────────────
        # Oncelik sirasi:
        # 1. BA JSON ciktisindaki acceptance_criteria (V2 format, ID'li)
        # 2. WI AcceptanceCriteria alani
        # 3. WI Description'daki maddeli listeler
        # 4. BA serbest metin ciktisindaki maddeler (son care)
        criteria: list[str] = []

        # 1. BA JSON'dan (V2 format — ID'li)
        if ba_json and ba_json.get("acceptance_criteria"):
            for ac in ba_json["acceptance_criteria"]:
                if isinstance(ac, dict):
                    criteria.append(f"{ac.get('id', '')}: {ac.get('desc', '')}")
                elif isinstance(ac, str) and len(ac) > 10:
                    criteria.append(ac)

        # 2. WI AC alanindan
        if not criteria and wi_ac_plain:
            for line in wi_ac_plain.replace("\r", "").split("\n"):
                line = line.strip()
                line = _re.sub(r'^[\-•*\d]+[.):\s]+', '', line).strip()
                if len(line) > 10:
                    criteria.append(line)
        # 3. Description'dan (AC alani bossa)
        if not criteria and wi_desc_clean:
            for line in wi_desc_clean.replace("\r", "").split("\n"):
                line = line.strip()
                line = _re.sub(r'^[\-•*\d]+[.):\s]+', '', line).strip()
                if len(line) > 15:
                    criteria.append(line)
        # 3. BA çıktısındaki numaralı/madde isaretli satırlar
        if not criteria and requirements_text:
            for line in requirements_text.split("\n"):
                stripped = line.strip()
                m = _re.match(r'^(?:[\-•*]|\d+[.):])\s+(.+)', stripped)
                if m and len(m.group(1)) > 10:
                    criteria.append(m.group(1).strip())
        self.state.acceptance_criteria = criteria[:15]  # En fazla 15 kriter
        if criteria:
            _log(f"  Kabul kriterleri belirlendi: {len(criteria)} kriter")
            for i, c in enumerate(criteria[:5], 1):
                _log(f"    {i}. {c[:80]}")
        else:
            _log("  Kabul kriteri bulunamadi (WI'de tanimsiz)")

        self._step_done("requirements_analysis_task", requirements_text[:3000])
        _log(f"  Is analizi tamamlandi")

    @listen(crew_step1_requirements)
    def step0_kickoff_meeting(self):
        """Kickoff toplantisi — requirements'tan SONRA calisir.
        Artik is analizi, kabul kriterleri ve hedef repo biliniyor.
        Agentlar bilgiye dayali teknik tartisma yapabilir.
        CREW_KICKOFF_MEETING=0 ile devre disi birakilabilir (default: aktif)."""
        import os as _os
        from agile_sdlc_crew import pipeline_config as _pc_ko
        from agile_sdlc_crew.main import _add_wi_comment

        if not _pc_ko.get("CREW_KICKOFF_MEETING"):
            _log("  Kickoff toplantisi devre disi (CREW_KICKOFF_MEETING=0)")
            self._step_done("kickoff_meeting_task", "Devre dışı (CREW_KICKOFF_MEETING=0)")
            return

        # Resume: onceki job'dan kickoff ciktisi varsa atla
        cached_kickoff = self._try_resume_step("kickoff_meeting_task")
        if cached_kickoff:
            self.state.kickoff_text = cached_kickoff
            self._resume_step("kickoff_meeting_task", cached_kickoff)
            return

        _log("\n-- KICKOFF TOPLANTISI (is analizi sonrasi) --")
        self._step_start("kickoff_meeting_task")

        # Kickoff context'i: requirements + acceptance criteria + repo bilgisi dahil
        ctx = self._build_step_context("kickoff_meeting_task")

        # Hedef repo tahmini — 4 katman:
        # 0/1. _select_repo_by_name (tam isim + parca eslesmesi)
        # 1.5. Geçmiş-iş önerisi (suggest_repo_from_history)
        # 2. Kod grep: WI'daki teknik terimler repo kodlarinda geciyorsa
        # 3. Vector semantic search (fallback)
        import re as _re_ko
        import subprocess as _sp_ko
        kickoff_repo = ""
        try:
            # WI bilgisini oku — requirements step'teki degiskenler burada yok
            _wi_ko_data = self._client.get_work_item(int(self.state.work_item_id))
            _wi_ko_fields = _wi_ko_data.get("fields", {}) if _wi_ko_data else {}
            _wi_title_ko = _wi_ko_fields.get("System.Title", "")
            _wi_desc_ko = _re_ko.sub(r'<[^>]+>', ' ', _wi_ko_fields.get("System.Description", "") or "").strip()
            wi_text_ko = f"{_wi_title_ko} {_wi_desc_ko} {self.state.requirements_text[:500]}".lower()

            # Katman 0/1: tam isim → parca eslesmesi (ortak helper)
            _method, _matched = _select_repo_by_name(self.state.known_repos, wi_text_ko)
            if _matched:
                kickoff_repo = _matched
                _log(f"  Kickoff hedef repo ({_method}): {kickoff_repo}")

            # Katman 1.5: Geçmiş-iş önerisi (isim eşleşmesi yoksa, grep'ten önce)
            if not kickoff_repo and _pc_ko.get("CREW_REPO_HISTORY_SUGGEST") and self._vector_store:
                try:
                    _sug = self._vector_store.suggest_repo_from_history(
                        self.state.requirements_text[:600],
                        exclude_wi=self.state.work_item_id,
                        known_repos=self.state.known_repos,
                    )
                    _min_score = _pc_ko.get("CREW_REPO_HISTORY_MIN_SCORE")
                    if _sug and _sug[0]["score"] >= _min_score:
                        kickoff_repo = _sug[0]["repo"]
                        _log(f"  Kickoff hedef repo (geçmiş-iş): {kickoff_repo} (skor {_sug[0]['score']})")
                except Exception as e:
                    _log(f"  Kickoff geçmiş-iş önerisi hatası: {e}")

            # Katman 2: Kod grep (teknik terimler)
            if not kickoff_repo:
                search_text_ko = f"{_wi_title_ko} {_wi_desc_ko}"
                tech_terms_ko = set()
                for m in _re_ko.finditer(r'\b([a-z]+[A-Z][a-zA-Z]{3,})\b', search_text_ko):
                    tech_terms_ko.add(m.group(1))
                for m in _re_ko.finditer(r'/api/(\w+)', search_text_ko, _re_ko.IGNORECASE):
                    tech_terms_ko.add(m.group(1))
                for m in _re_ko.finditer(r'\b(\w+\.(?:php|py|ts|js|go|cs|java))\b', search_text_ko):
                    tech_terms_ko.add(m.group(1).split('.')[0])

                if tech_terms_ko:
                    _log(f"  Kickoff kod grep terimleri: {list(tech_terms_ko)[:8]}")
                    repo_hits_ko: dict[str, int] = {}
                    for rname in self.state.known_repos:
                        repo_dir = self._repo_mgr.base_dir / rname
                        if not repo_dir.exists():
                            continue
                        hits = 0
                        for term in list(tech_terms_ko)[:5]:
                            try:
                                result = _sp_ko.run(
                                    ["grep", "-rl", "--include=*.php", "--include=*.py",
                                     "--include=*.ts", "--include=*.js", "--include=*.go",
                                     "-m", "1", term, str(repo_dir)],
                                    capture_output=True, text=True, timeout=5,
                                )
                                if result.returncode == 0 and result.stdout.strip():
                                    hits += 1
                            except Exception:
                                pass
                        if hits > 0:
                            repo_hits_ko[rname] = hits
                    if repo_hits_ko:
                        kickoff_repo = max(repo_hits_ko, key=repo_hits_ko.get)
                        _log(f"  Kickoff hedef repo (grep): {kickoff_repo} ({repo_hits_ko[kickoff_repo]} terim)")

            # Katman 3: Vector semantic search (son care)
            if not kickoff_repo and self._vector_store:
                query = f"{self.state.requirements_text[:500]}"
                relevant = self._vector_store.find_relevant_repos(query, limit=3)
                if relevant and relevant[0]["score"] >= 0.1:
                    kickoff_repo = relevant[0]["repo"]
                    _log(f"  Kickoff hedef repo (vector): {kickoff_repo} (score={relevant[0]['score']:.3f})")

            # Bulunan reponun summary + ust dizin dosya listesini context'e ekle
            if kickoff_repo:
                repo_summary = self._repo_mgr.get_repo_summary(kickoff_repo)
                if repo_summary:
                    ctx += f"\n\n# HEDEF REPO: {kickoff_repo}\n{repo_summary[:2500]}"
                # Ust seviye dizin listesi — architect hangi klasorde ne var bilsin
                try:
                    from pathlib import Path as _Path
                    repo_dir = self._repo_mgr.base_dir / kickoff_repo
                    if repo_dir.exists():
                        top_files = sorted([
                            f"  {p.relative_to(repo_dir)}"
                            for p in repo_dir.rglob("*")
                            if p.is_file()
                            and p.suffix.lower() in {".php",".py",".ts",".js",".go",".cs",".java"}
                            and not any(s in str(p) for s in ("vendor/","node_modules/",".git/","__pycache__"))
                        ])[:40]
                        if top_files:
                            ctx += f"\n\n# {kickoff_repo} DOSYA YAPISI (ilk 40)\n" + "\n".join(top_files)
                except Exception:
                    pass
        except Exception as e:
            _log(f"  Kickoff repo tahmini hatasi: {e}")

        try:
            import time as _kt
            # Ogrenilmis yonergeler (gecmis WI'lardan) + bu calistirma icin
            # kullanici feedback'i — kickoff context'inin basina enjekte edilir.
            from agile_sdlc_crew import kickoff_guidance as _kg
            _guidance_block = _kg.format_for_context()
            extra_prefix = ""
            if _guidance_block:
                extra_prefix += _guidance_block + "\n\n"
            if self.state.kickoff_feedback:
                extra_prefix += (
                    "# KULLANICIDAN BU CALISTIRMA ICIN GERI BILDIRIM\n"
                    f"{self.state.kickoff_feedback.strip()}\n"
                    "Bu feedback'i tum uzmanlar dikkate almali.\n\n"
                )
            if extra_prefix:
                ctx = extra_prefix + ctx

            _log(f"  Kickoff baslatiyor: 4 task, repo={kickoff_repo}")
            _kickoff_t0 = _kt.time()
            # Yeni varsayilan path: task-by-task + Haiku grading + retry.
            # Klasik tek-Crew calistirma icin CREW_KICKOFF_GRADING=0 ile
            # `run_kickoff_meeting` grading'i atlayarak ayni interface ile calisir.
            kickoff_result = self._agile_crew.run_kickoff_meeting(inputs={
                "work_item_id": self.state.work_item_id,
                "previous_context": ctx,
                "target_repo": kickoff_repo,
            })
            _kickoff_elapsed = _kt.time() - _kickoff_t0
            self._track_and_check_budget(kickoff_result, "kickoff_meeting_task")
            kickoff_text = kickoff_result.raw or ""
            _log(f"  Kickoff tamamlandi: {_kickoff_elapsed:.0f}s")
        except Exception as e:
            _log(f"  🚨 Kickoff toplantisi HATASI: {e}")
            self._step_fail("kickoff_meeting_task", str(e))
            raise RuntimeError(f"Kickoff toplantisi basarisiz: {e}")

        self.state.kickoff_text = kickoff_text
        _log(f"  📏 kickoff: {len(kickoff_text or '')} char")
        self._step_done("kickoff_meeting_task", kickoff_text[:3000])

        # Per-agent ciktilarini + grade gecmisini job_id basina JSON'a yaz
        # (debug UI bunu okur). flow.kickoff_meeting return objesinde varsa kaydet.
        try:
            grades_blob = getattr(kickoff_result, "kickoff_grades", None) or {}
            per_agent = getattr(kickoff_result, "kickoff_outputs", None) or {}
            self._persist_kickoff_debug(kickoff_text, grades_blob, per_agent, kickoff_repo)
        except Exception as e:
            _log(f"  Kickoff debug JSON yazma hatasi: {e}")

        _log("  Kickoff toplantisi tamamlandi")

    def _persist_kickoff_debug(
        self,
        kickoff_text: str,
        grades: dict,
        per_agent: dict,
        target_repo: str,
    ) -> None:
        """Per-agent kickoff cikti + grade gecmisini debug UI icin diske yazar.

        Dosya: /tmp/crew_kickoff/job_<job_id>.json
        """
        import json as _json
        from datetime import datetime as _ddt
        from pathlib import Path as _PP

        if not self.state.job_id:
            return
        out_dir = _PP("/tmp/crew_kickoff")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"job_{self.state.job_id}.json"

        # Plan'daki sirayi koru
        plan = [
            ("kickoff_ba_task",       "BA Analiz"),
            ("kickoff_arch_task",     "Architect"),
            ("kickoff_dev_task",      "Developer"),
            ("kickoff_sm_close_task", "SM Tutanak"),
        ]
        agents = []
        for key, label in plan:
            agents.append({
                "key": key,
                "label": label,
                "output": (per_agent.get(key) or "") if isinstance(per_agent, dict) else "",
                "attempts": (grades.get(key) or []) if isinstance(grades, dict) else [],
            })

        payload = {
            "job_id": self.state.job_id,
            "work_item_id": self.state.work_item_id,
            "target_repo": target_repo,
            "kickoff_only": bool(self.state.kickoff_only),
            "kickoff_feedback": self.state.kickoff_feedback or "",
            "saved_at": _ddt.now().isoformat(timespec="seconds"),
            "agents": agents,
            "final_text": kickoff_text,
        }
        tmp = out_path.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(out_path)

    @listen(step0_kickoff_meeting)
    def crew_step4_technical_design(self):
        """Adim 2-3 atlanir, repo ve bagimlilik bilgisi context'e eklenir.
        Teknik tasarim agent'i work item + repo summary ile calisir."""
        # Kickoff-only debug modunda: step0 ciktilari kaydedildi, durdur.
        if self.state.kickoff_only:
            _log("  Kickoff-only modu: step4 ve sonrasi atlanyor, pipeline durduruluyor.")
            raise _KickoffOnlyStop("kickoff_only mode")

        from agile_sdlc_crew.main import _parse_architect_output, _resolve_repo_name

        # Step 3 (dependency_analysis) hala atlanyor — repo summary context icin yeterli.
        self._step_done("dependency_analysis_task", "Atlandı — repo bilgisi local'den alınıyor")

        # Repo summary'lerini context'e ekle.
        # Vector search ile en ilgili repolari basa al — 66 repo varken
        # sadece ilk 15 context'e giriyor, hedef repo mutlaka icinde olmali.
        ordered_repos = list(self.state.known_repos)  # default: olduğu gibi
        if self._vector_store:
            try:
                query = f"{self.state.requirements_text[:500]} {self.state.kickoff_text[:300]}"
                relevant = self._vector_store.find_relevant_repos(query, limit=15)
                if relevant:
                    top_names = [r["repo"] for r in relevant]
                    rest = [r for r in self.state.known_repos if r not in top_names]
                    ordered_repos = top_names + rest
            except Exception:
                pass
        summaries = []
        summaries_repos = []
        for rname in ordered_repos:
            s = self._repo_mgr.get_repo_summary(rname)
            if s:
                short = []
                for line in s.split("\n"):
                    if line.startswith("## Dizin"):
                        break
                    short.append(line)
                summaries.append("\n".join(short).strip())
                summaries_repos.append(rname)

        # Adim 2: LLM'e en alakali 20 summary'yi vererek hedef repo icin
        # ONERI uretsin. Onerisi technical_design context'ine eklenir;
        # SON KARAR architect'in — kendi kanıtlarına dayanarak farkli bir
        # repo secebilir, override edebilir.
        #
        # Path/URL mining: vector search bazen WI'da net olarak gecen repo
        # adini (ornegin "/webservice/v1/meta/get" -> 'webservice') top 20'de
        # cikartmiyor (tum skorlar 0.025-0.030 araliginda sikisik). Bu yuzden
        # BA ciktisindaki path/URL token'larini known_repos'a karsi esleyip
        # eslesenleri candidate listesine ZORLA dahil ediyoruz.
        candidate_repos = list(summaries_repos[:20])
        try:
            import re as _re_pm
            haystack = f"{self.state.requirements_text} {self.state.kickoff_text}"
            # Path segment'lerinden ve "repo-name" benzeri token'lardan aday cikar
            # ('/foo/...', 'foo/bar', 'foo-repo') hepsi yakalanir
            path_tokens: set[str] = set()
            for m in _re_pm.finditer(r"[/\s\"'`(]([a-z][a-z0-9_-]{2,40})(?:[/\.])", haystack):
                path_tokens.add(m.group(1).lower())
            forced = []
            for tok in path_tokens:
                if tok in self.state.known_repos and tok not in candidate_repos:
                    forced.append(tok)
            if forced:
                # En basta — WI metninde gecen repo adlari en gucl sinyal
                candidate_repos = forced + [r for r in candidate_repos if r not in forced]
                _log(f"  Path mining: WI metninde gecen repo'lar candidate'e eklendi: {forced}")
        except Exception as e:
            _log(f"  Path mining hatasi: {e} — vector top 20 ile devam")

        # Birebir kod kaniti — WI sembollerini repolarin kodunda grep'le.
        # Fuzzy isim eslesmesinin (stock_api_list → stock-api) yanlis repoyu
        # secmesini engeller; exclusive eslesmeleri candidate'e ZORLA dahil et.
        symbol_evidence: dict = {}
        try:
            symbol_evidence = self._grep_symbol_evidence()
            if symbol_evidence:
                # Exclusive eslesmesi olan repolar once, sonra digerleri
                ev_ranked = sorted(
                    symbol_evidence.items(),
                    key=lambda kv: (len(kv[1].get("exclusive", [])), len(kv[1].get("symbols", []))),
                    reverse=True,
                )
                ev_repos = [r for r, _ in ev_ranked]
                candidate_repos = ev_repos + [r for r in candidate_repos if r not in ev_repos]
                _log(f"  Symbol-grep kaniti: " + ", ".join(
                    f"{r}({len(e.get('exclusive', []))}excl/{len(e.get('symbols', []))})"
                    for r, e in ev_ranked[:6]
                ))
        except Exception as e:
            _log(f"  Symbol-grep hatasi: {e} — atlaniyor")

        # Geçmiş-iş repo önerisi (advisory) — candidate'e zorla dahil + discover'a kanıt
        repo_history_suggestions: list[dict] = []
        from agile_sdlc_crew import pipeline_config as _pc_rh
        if _pc_rh.get("CREW_REPO_HISTORY_SUGGEST") and self._vector_store:
            try:
                _q_hist = f"{self.state.requirements_text[:500]} {self.state.kickoff_text[:300]}"
                repo_history_suggestions = self._vector_store.suggest_repo_from_history(
                    _q_hist, exclude_wi=self.state.work_item_id,
                    known_repos=self.state.known_repos,
                )
                if repo_history_suggestions:
                    _hist_repos = [s["repo"] for s in repo_history_suggestions]
                    candidate_repos = _hist_repos + [r for r in candidate_repos if r not in _hist_repos]
                    _log(f"  Geçmiş-iş repo önerisi: " + ", ".join(
                        f"{s['repo']}({s['score']})" for s in repo_history_suggestions
                    ))
            except Exception as e:
                _log(f"  Geçmiş-iş önerisi hatası: {e}")

        self._run_discover_repos(
            candidate_repos[:25], evidence=symbol_evidence,
            repo_history=repo_history_suggestions,
        )

        # Repo Ozetleri context'i: top 15 ozet (Discover karari context'e
        # ayri bir blok olarak zaten eklendi; burada tum adaylar kalir ki
        # architect kendi kararini ozgurce versin).
        if summaries:
            _log(f"  Repo ozet sirasi (ilk 5): {summaries_repos[:5]}")

        # Benzer onceki isleri bul ve context'e ekle
        _log("\n-- ADIM 4: Teknik tasarim --")
        self._step_start("technical_design_task")

        # Cache: ayni WI icin onceki completed job'dan plan var mi?
        # CREW_ENABLE_RESUME=False ise cache atlanir (vendor/yeni context icin taze calistirmak icin)
        from agile_sdlc_crew import pipeline_config as _pc_td
        _resume_enabled = _pc_td.get("CREW_ENABLE_RESUME")
        cached = (
            self._db.get_cached_step_output("technical_design_task", self.state.work_item_id)
            if _resume_enabled else None
        )
        # JSON balance check — truncate edilmis cache'i okuma denemesi bile yapmayalim
        def _looks_complete_json(s: str) -> bool:
            if not s or "{" not in s or "changes" not in s:
                return False
            # Brace balance: { == } olmali (en azindan JSON sonu kadar)
            open_c = s.count("{")
            close_c = s.count("}")
            return open_c > 0 and open_c == close_c
        if cached and not _looks_complete_json(cached):
            _log(f"  Cache eksik/truncate (brace count mismatch), temizleniyor")
            try:
                self._db.clear_cached_step_output(
                    "technical_design_task", self.state.work_item_id,
                )
            except Exception:
                pass
            cached = None
        if cached:
            try:
                plan = _parse_architect_output(cached)
                repo_name = plan["repo_name"]
                if repo_name not in self.state.known_repos:
                    repo_name = _resolve_repo_name(
                        repo_name, self.state.known_repos, self._client, self.state.work_item_id,
                    )
                    plan["repo_name"] = repo_name
                self.state.repo_name = repo_name
                self.state.plan = plan
                self.state.requirements_text = self.state.requirements_text or cached
                # Tam JSON'u sakla — sonraki job'lar da parse edebilsin
                self._step_done("technical_design_task", cached[:50_000])
                _log(f"  Onceki job'dan plan kullanildi: {len(plan['changes'])} dosya, repo={repo_name}")
                return
            except (ValueError, KeyError) as e:
                # Bozuk/truncate edilmis cache — DB'den sil ki bir daha okumasin
                _log(f"  Cache JSON bozuk/eksik ({e}) — DB'den temizleniyor, agent calisacak")
                try:
                    cleared = self._db.clear_cached_step_output(
                        "technical_design_task", self.state.work_item_id,
                    )
                    if cleared:
                        _log(f"  {cleared} bozuk cache kaydi silindi")
                except Exception as clear_err:
                    _log(f"  Cache temizleme hatasi (kritik degil): {clear_err}")

        ctx = self._build_step_context("technical_design_task")
        if repo_history_suggestions:
            _hist_txt = "\n".join(
                f"- {s['repo']} (skor {s['score']}; örnek dosyalar: "
                + ", ".join(s.get('file_paths_evidence', [])[:3]) + ")"
                for s in repo_history_suggestions
            )
            ctx += (
                "\n\n# BENZER GEÇMİŞ İŞLER (Repo Kararı — ADVISORY)\n"
                "Aşağıdaki repolarda benzer işler başarıyla tamamlandı. Repo adı "
                "benzerliğinden güçlü sinyaldir; yine de kendi kanıtınla karar ver.\n"
                f"{_hist_txt}\n"
            )

        # ── Python on-hazirlik: WI + repo + DOSYA ICERIKLERI ──────────────
        # Hedef: agent tool cagirmadan JSON plani uretsin.
        # Tool cagrilari conversation history'yi buyutur; 10 iterasyon = 100K+
        # input token. Dosyalari burada okuyup context'e eklemek bunu engeller.
        import re as _re
        wi_title = ""
        wi_desc_clean = ""
        wi_criteria_clean = ""
        try:
            wi = self._client.get_work_item(int(self.state.work_item_id))
            wi_title = wi.get("fields", {}).get("System.Title", "")
            wi_desc = wi.get("fields", {}).get("System.Description", "") or ""
            wi_criteria = wi.get("fields", {}).get("Microsoft.VSTS.Common.AcceptanceCriteria", "") or ""
            wi_desc_clean = _re.sub(r'<[^>]+>', ' ', wi_desc).strip()[:3000]
            wi_criteria_clean = _re.sub(r'<[^>]+>', ' ', wi_criteria).strip()[:1500]
            ctx += (
                f"\n\n# WORK ITEM DETAYI\n"
                f"## Baslik\n{wi_title}\n\n"
                f"## Aciklama\n{wi_desc_clean}\n\n"
                f"## Kabul Kriterleri\n{wi_criteria_clean}\n"
            )
        except Exception as e:
            _log(f"  WI on-hazirlik hatasi: {e}")

        # Hedef repo tahmini — 3 katman:
        # 0/1. _select_repo_by_name (tam isim + parca eslesmesi, helper)
        # 2. Kod grep (teknik terimler)
        # 3. Vector semantic search (fallback)
        prefetch_repo = ""
        relevant = []

        # Katman -2: discover_repos karari (EN GUCLU). Bu, symbol-grep kaniti +
        # repo ozetleri uzerinde akil yuruten ayri LLM adimi; naive tie-break'ten
        # daha guvenilir. WI #66511'de symbol-grep berabereydi (orkestra 1excl/4,
        # webservice 1excl/4) ve tie-break yanlis repoya (orkestra) gitti; oysa
        # discover_repos 'returnOffices yalnız webservice'te' diye dogru secmisti.
        if getattr(self, "_discovered_repo", "") in self.state.known_repos:
            prefetch_repo = self._discovered_repo
            _log(f"  Adim 4 hedef repo (discover_repos otoritesi): {prefetch_repo}")

        # Katman -1: BIREBIR KOD KANITI (en guclu). Bir sembol yalnizca tek
        # repoda geciyorsa, fuzzy isim eslesmesini EZER. 'stock_api_list' →
        # 'stock-api' adi yanilgisini engeller (asil sahip orkestra).
        if not prefetch_repo and symbol_evidence:
            ev_ranked = sorted(
                symbol_evidence.items(),
                key=lambda kv: (len(kv[1].get("exclusive", [])), len(kv[1].get("symbols", []))),
                reverse=True,
            )
            top_repo, top_e = ev_ranked[0]
            if top_e.get("exclusive"):
                prefetch_repo = top_repo
                _log(f"  Adim 4 hedef repo (symbol-grep exclusive {top_e['exclusive']}): {prefetch_repo}")

        # Katman 0/1: ortak helper (step0_kickoff_meeting ile birebir ayni mantik)
        # — yalnizca symbol kaniti karar vermediyse.
        if not prefetch_repo:
            wi_text_for_repo = f"{wi_title} {wi_desc_clean} {self.state.requirements_text[:500]}".lower()
            _method_td, _matched_td = _select_repo_by_name(
                self.state.known_repos, wi_text_for_repo,
            )
            if _matched_td:
                prefetch_repo = _matched_td
                _log(f"  Adim 4 hedef repo ({_method_td}): {prefetch_repo}")

        # grep_matched_files: repo tespitinde bulunan dosya yollari — pre-fetch'te okunur
        grep_matched_files: list[str] = []

        # Katman 1.5: Geçmiş-iş önerisi (isim eşleşmesi yoksa, grep'ten önce)
        if not prefetch_repo and repo_history_suggestions:
            _min_score = _pc_rh.get("CREW_REPO_HISTORY_MIN_SCORE")
            if repo_history_suggestions[0]["score"] >= _min_score:
                prefetch_repo = repo_history_suggestions[0]["repo"]
                _log(f"  Adim 4 hedef repo (geçmiş-iş): {prefetch_repo} (skor {repo_history_suggestions[0]['score']})")

        # Katman 2: Kod grep — teknik terimler repo kodlarinda geciyorsa
        if not prefetch_repo:
            try:
                search_text = f"{wi_title} {wi_desc_clean} {wi_criteria_clean}"
                tech_terms = set()
                for m in _re.finditer(r'\b([a-z]+[A-Z][a-zA-Z]{3,})\b', search_text):
                    tech_terms.add(m.group(1))
                for m in _re.finditer(r'/api/(\w+)', search_text, _re.IGNORECASE):
                    tech_terms.add(m.group(1))
                for m in _re.finditer(r'\b(\w+\.(?:php|py|ts|js|go|cs|java))\b', search_text):
                    tech_terms.add(m.group(1).split('.')[0])
                if tech_terms:
                    _log(f"  Kod grep terimleri: {list(tech_terms)[:8]}")
                    import subprocess
                    repo_hits: dict[str, int] = {}
                    repo_files: dict[str, list[str]] = {}
                    for rname in self.state.known_repos:
                        repo_dir = self._repo_mgr.base_dir / rname
                        if not repo_dir.exists():
                            continue
                        hits = 0
                        matched = []
                        for term in list(tech_terms)[:5]:
                            try:
                                result = subprocess.run(
                                    ["grep", "-rl", "--include=*.php", "--include=*.py",
                                     "--include=*.ts", "--include=*.js", "--include=*.go",
                                     "--include=*.cs", "--include=*.java",
                                     "-m", "1", term, str(repo_dir)],
                                    capture_output=True, text=True, timeout=5,
                                )
                                if result.returncode == 0 and result.stdout.strip():
                                    hits += 1
                                    for f in result.stdout.strip().split("\n"):
                                        if f and f not in matched:
                                            matched.append(f)
                            except Exception:
                                pass
                        if hits > 0:
                            repo_hits[rname] = hits
                            repo_files[rname] = matched
                    if repo_hits:
                        best_grep = max(repo_hits, key=repo_hits.get)
                        _log(f"  Kod grep sonucu: {best_grep} ({repo_hits[best_grep]} terim) — tum: {repo_hits}")
                        prefetch_repo = best_grep
                        grep_matched_files = repo_files.get(best_grep, [])
            except Exception as e:
                _log(f"  Kod grep hatasi: {e}")

        # Katman 3: Vector semantic search (son care)
        if not prefetch_repo and self._vector_store:
            try:
                wi_query = f"{wi_title} {wi_desc_clean[:500]}" if wi_title else self.state.requirements_text[:500]
                relevant = self._vector_store.find_relevant_repos(wi_query, limit=5)
                if relevant and relevant[0]["score"] >= 0.1:
                    prefetch_repo = relevant[0]["repo"]
                    _log(f"  Vector repo tahmini: {prefetch_repo} (score={relevant[0]['score']:.3f})")
            except Exception:
                pass

        if prefetch_repo:
            rel_text = "\n".join(f"- {r['repo']} (score: {r['score']:.3f})" for r in relevant)
            ctx += f"\n\n# ONERILEN REPOLAR (en uygun)\n- {prefetch_repo} ← TAHMİN (context'teki ozeti incele)\n{rel_text}\n"

        # ── Dosya pre-fetch: grep eslesen dosyalar + WI ipuclari ──────
        # Dosya icerikleri context'te olursa agent browse_repo cagirmaz →
        # conversation history buyumez → dramatik token tasarrufu.
        prefetch_file_count = 0
        try:
            # 0. Grep ile bulunan dosyalari oku (en degerli — WI'daki terimleri iceriyor)
            if prefetch_repo and grep_matched_files:
                from pathlib import Path as _Path
                repo_dir = self._repo_mgr.base_dir / prefetch_repo
                for fpath_str in grep_matched_files[:5]:
                    if prefetch_file_count >= 4:
                        break
                    try:
                        fpath = _Path(fpath_str)
                        if not fpath.exists() or fpath.stat().st_size > 50_000:
                            continue
                        # vendor/node_modules atla
                        if any(s in str(fpath) for s in ("vendor/", "node_modules/", ".git/")):
                            continue
                        content = fpath.read_text(encoding="utf-8", errors="replace")
                        rel_path = "/" + str(fpath.relative_to(repo_dir))
                        trunc = content[:4000] + ("\n... (truncated)" if len(content) > 4000 else "")
                        ctx += f"\n\n# DOSYA ICERIGI: {rel_path}\n```\n{trunc}\n```"
                        prefetch_file_count += 1
                        _log(f"  Pre-fetch (grep eslesmesi): {rel_path} ({len(content)} char)")
                    except Exception:
                        pass

            # 0b. PR yorumlarinda bahsedilen dosyalari pre-fetch et
            pr_threads_for_prefetch = getattr(self, "_pr_threads_to_respond", [])
            for t in pr_threads_for_prefetch:
                if prefetch_file_count >= 5:
                    break
                fp = t.get("file_path")
                if not fp:
                    # Yorum metninden dosya yolu + satir numarasi cikar (ornek: "azure_service.py:74")
                    import re as _re_prf
                    fm = _re_prf.search(r'([\w/]+\.(?:py|php|ts|js|go|cs))(?::(\d+))?', t.get("content", ""))
                    if fm:
                        fp = fm.group(1)
                        _ref_line = int(fm.group(2)) if fm.group(2) else None
                    else:
                        _ref_line = None
                else:
                    _ref_line = None
                if fp:
                    try:
                        if not fp.startswith("/"):
                            matches = list(repo_dir.rglob(fp.split("/")[-1]))
                            matches = [m for m in matches if not any(s in str(m) for s in ("vendor/", "node_modules/", ".git/"))]
                            if matches:
                                fpath = matches[0]
                            else:
                                continue
                        else:
                            fpath = repo_dir / fp.lstrip("/")
                        if fpath.exists():
                            content = fpath.read_text(encoding="utf-8", errors="replace")
                            rel_path = "/" + str(fpath.relative_to(repo_dir))
                            lines = content.split("\n")

                            if len(lines) <= 100:
                                # Kucuk dosya — tamami
                                snippet = content[:4000]
                                label = "tam"
                            elif _ref_line:
                                # Buyuk dosya + satir ref — ilgili blogu bul
                                snippet, label = _extract_code_block(lines, _ref_line - 1, fpath.suffix)
                            else:
                                # Buyuk dosya, satir ref yok — ilk 4K
                                snippet = content[:4000]
                                label = "ilk 4K"

                            ctx += f"\n\n# DOSYA ICERIGI: {rel_path} ({label}, PR ref)\n```\n{snippet}\n```"
                            prefetch_file_count += 1
                            _log(f"  Pre-fetch (PR ref): {rel_path} ({label}, {len(snippet)} char)")
                    except Exception:
                        pass

            search_text = " ".join(filter(None, [
                wi_desc_clean, wi_criteria_clean,
                self.state.requirements_text[:1000],
                self.state.kickoff_text[:500],
            ]))
            # Dosya adi/yolu iceren pattern'leri yakala
            file_name_re = _re.compile(
                r'\b([\w.-]+\.(?:php|ts|tsx|js|jsx|py|cs|java|rb|go|vue|html|scss|blade\.php))\b',
                _re.IGNORECASE,
            )
            raw_hints = list(dict.fromkeys(m.group(1) for m in file_name_re.finditer(search_text)))
            _log(f"  Dosya ipuclari: {raw_hints[:8]}")

            # Hedef repoda bu dosyalari bul (local filesystem glob ile)
            if prefetch_repo:
                from pathlib import Path as _Path
                repo_dir = self._repo_mgr.base_dir / prefetch_repo

                # 1. WI'dan gelen dosya ipuclari
                for hint in raw_hints[:6]:
                    if prefetch_file_count >= 5:
                        break
                    matches = list(repo_dir.rglob(hint)) if repo_dir.exists() else []
                    matches = [
                        m for m in matches
                        if not any(skip in str(m) for skip in ("vendor/", "node_modules/", ".git/"))
                    ]
                    if not matches:
                        continue
                    target = matches[0]
                    rel_path = "/" + str(target.relative_to(repo_dir))
                    try:
                        content = target.read_text(encoding="utf-8", errors="replace")
                        trunc = content[:4000] + ("\n... (truncated)" if len(content) > 4000 else "")
                        ctx += f"\n\n# DOSYA ICERIGI: {rel_path}\n```\n{trunc}\n```"
                        prefetch_file_count += 1
                        _log(f"  Pre-fetch (ipucu): {rel_path} ({len(content)} char)")
                    except Exception:
                        pass

                # 1.5. Repo biliniyor ama az/hic dosya icerigi cekildi: WI'nin
                #      teknik terimleriyle repo ICINDE grep yap, eslesen
                #      dosyalarin ICERIGINI cek. Dosya-adi ipucu olmayan
                #      WI'larda (ornegin "stock_api_list ekrani") handler'i
                #      context'e getirir; architect browse_repo cagirmak
                #      zorunda kalmaz (claude_cli ReAct tool-loop'unda bos donuyor).
                if prefetch_file_count < 3 and repo_dir.exists():
                    import subprocess as _sp_grep
                    # JSON anahtari / Turkce gurultu engellemek icin blocklist.
                    _STOP = {
                        "functional_requirements", "technical_requirements",
                        "acceptance_criteria", "out_of_scope", "open_questions",
                        "work_item_id", "current_code", "new_code", "file_path",
                        "change_type", "repo_name",
                    }
                    grep_terms: list[str] = []
                    # 1) EN DEGERLI: symbol_evidence'in bu repoda DOGRULADIGI semboller
                    #    (WI'dan cikarilip repolarda birebir grep'lenmis — returnOffices gibi).
                    _ev = symbol_evidence.get(prefetch_repo, {}) if symbol_evidence else {}
                    for s in (_ev.get("exclusive", []) + _ev.get("symbols", [])):
                        if s and s.lower() not in {x.lower() for x in grep_terms}:
                            grep_terms.append(s)
                    # 2) Sadece WI baslik+aciklamasindan gercek kod tanimlayicilari
                    #    (requirements_text JSON'unu DAHIL ETME; IGNORECASE YOK —
                    #     yoksa camelCase deseni her Turkce kelimeyi eslesir).
                    _term_src = f"{wi_title} {wi_desc_clean} {wi_criteria_clean}"
                    for pat in (
                        r'\b([a-z][a-z0-9]{2,}_[a-z0-9_]{2,})\b',   # snake_case: opening_hours
                        r'\b([a-z]+[A-Z][a-zA-Z]{3,})\b',           # camelCase: returnOffices
                        r'/api/(\w+)',                              # endpoint: /api/stock
                    ):
                        for m in _re.finditer(pat, _term_src):
                            t = m.group(1)
                            if not t.isascii():
                                continue
                            if t.lower() in _STOP:
                                continue
                            if t.lower() not in {x.lower() for x in grep_terms}:
                                grep_terms.append(t)
                    # symbol_evidence terimleri once kalsin; gerisinde uzun (spesifik) once
                    grep_terms.sort(key=lambda t: (t not in (_ev.get("exclusive", []) + _ev.get("symbols", [])), -len(t)))
                    seen_grep: set[str] = set()
                    if grep_terms:
                        _log(f"  Repo-ici grep terimleri: {grep_terms[:8]}")
                        for term in grep_terms[:6]:
                            if prefetch_file_count >= 4:
                                break
                            try:
                                res = _sp_grep.run(
                                    ["grep", "-rl",
                                     "--include=*.php", "--include=*.py", "--include=*.ts",
                                     "--include=*.tsx", "--include=*.js", "--include=*.go",
                                     "--include=*.cs", "--include=*.java",
                                     "-m", "1", term, str(repo_dir)],
                                    capture_output=True, text=True, timeout=5,
                                )
                            except Exception:
                                continue
                            if res.returncode != 0 or not res.stdout.strip():
                                continue
                            for fstr in res.stdout.strip().split("\n"):
                                if prefetch_file_count >= 4:
                                    break
                                if not fstr or fstr in seen_grep:
                                    continue
                                if any(s in fstr for s in ("vendor/", "node_modules/", ".git/", "/test", "_test.", "/mocks/")):
                                    continue
                                seen_grep.add(fstr)
                                try:
                                    p = _Path(fstr)
                                    content = p.read_text(encoding="utf-8", errors="replace")
                                    rel_path = "/" + str(p.relative_to(repo_dir))
                                    trunc = content[:4000] + ("\n... (truncated)" if len(content) > 4000 else "")
                                    ctx += f"\n\n# DOSYA ICERIGI: {rel_path}\n```\n{trunc}\n```"
                                    prefetch_file_count += 1
                                    _log(f"  Pre-fetch (repo-ici grep '{term}'): {rel_path} ({len(content)} char)")
                                except Exception:
                                    pass

                # 2. Ipucu yoksa veya az dosya bulunduysa: repo'nun temel yapisini ekle
                #    Architect'in tool cagirmadan plan yapabilmesi icin yeterli bilgi saglar.
                if prefetch_file_count < 2 and repo_dir.exists():
                    # Proje manifest dosyalari — teknoloji stacki ve dependency'ler
                    manifest_names = [
                        "package.json", "composer.json", "go.mod", "requirements.txt",
                        "pom.xml", "Cargo.toml", "tsconfig.json",
                    ]
                    for mf in manifest_names:
                        if prefetch_file_count >= 3:
                            break
                        mf_path = repo_dir / mf
                        if mf_path.exists():
                            try:
                                content = mf_path.read_text(encoding="utf-8", errors="replace")
                                trunc = content[:3000] + ("\n... (truncated)" if len(content) > 3000 else "")
                                ctx += f"\n\n# DOSYA ICERIGI: /{mf}\n```\n{trunc}\n```"
                                prefetch_file_count += 1
                                _log(f"  Pre-fetch (manifest): /{mf} ({len(content)} char)")
                            except Exception:
                                pass

                    # src/ dizin yapisi — architect hangi dosyalarin var oldugunu bilir
                    try:
                        src_files = sorted([
                            str(p.relative_to(repo_dir))
                            for p in repo_dir.rglob("*")
                            if p.is_file()
                            and p.suffix.lower() in {".php",".py",".ts",".tsx",".js",".jsx",".go",".cs",".java",".vue"}
                            and not any(s in str(p) for s in ("vendor/","node_modules/",".git/","__pycache__","dist/","build/",".next/"))
                        ])[:60]
                        if src_files:
                            ctx += f"\n\n# {prefetch_repo} KAYNAK DOSYALARI ({len(src_files)} dosya)\n" + "\n".join(f"  /{f}" for f in src_files)
                            # Dizin yapisi dosya ICERIGI degil — tool'suz moda gecis tetiklemez
                            _log(f"  Pre-fetch (dizin yapisi): {len(src_files)} dosya listelendi")
                    except Exception:
                        pass
        except Exception as e:
            _log(f"  Dosya pre-fetch hatasi (atlaniyor): {e}")

        # ── Tek architect: tool'lu, pre-fetch context ile ──────────────
        # Onceden tool'suz / tool'lu ikili yapi vardi — surekli sorun cikiyordu:
        # - Tool'suz: dosya icerigi yetersizse YETERSIZ diyordu (browse_repo yok)
        # - Tool'lu: max_iter bitince Thought:/Action: halusinasyonu final output oluyordu
        # Cozum: TEK architect, her zaman tool'lu, ama pre-fetch context ile destekli.
        # Context yeterliyse 1 iterasyonda JSON uretir (tool cagirmaz), yetmezse browse_repo ile okur.
        _log(f"  Pre-fetch sonucu: {prefetch_file_count} dosya icerigi context'te")
        if prefetch_file_count > 0:
            ctx_hint = (
                "⚡ Context'te dosya icerikleri ve WI detayi hazir ('DOSYA ICERIGI' basliklariyla). "
                "Bunlar yeterliyse direkt JSON plan uret, tool cagirma. "
                "Yetmezse browse_repo ile eksik dosyalari oku."
            )
        else:
            ctx_hint = (
                "Context'te henuz dosya icerigi yok. browse_repo ile hedef repo'daki "
                "ilgili dosyalari oku, sonra JSON plan uret."
            )

        # Test zorunlulugu notu (CREW_REQUIRE_TESTS + repoda test varsa):
        # architect plana test dosyalarini da dahil etsin.
        if prefetch_repo:
            ctx += self._test_requirement_note(prefetch_repo)

        # ── Part B: architect'i "gor kil" — claude -p'ye klonlanmis repoyu
        # --add-dir ile, Read/Grep/Glob'u --allowedTools ile ver. Boylece
        # gercek kodu kesfeder (returnOffices'i halusine etmek yerine bulur).
        # Env-toggle: CREW_CLI_REPO_TOOLS (default kapali — maliyet/sure etkisi var).
        from agile_sdlc_crew.tools import claude_cli_llm as _cli
        from agile_sdlc_crew import pipeline_config as _pc_cli
        _cli_repo_tools = False
        try:
            _cli_repo_tools = bool(_pc_cli.get("CREW_CLI_REPO_TOOLS"))
        except Exception:
            pass
        if _cli_repo_tools and prefetch_repo:
            _cand = [prefetch_repo] + [
                a for a in getattr(self, "_discovered_alternatives", []) if a != prefetch_repo
            ]
            _repo_dirs = [
                str(self._repo_mgr.base_dir / r) for r in _cand[:3]
                if (self._repo_mgr.base_dir / r).exists()
            ]
            if _repo_dirs:
                _cli.set_repo_ctx(_repo_dirs, "Read,Grep,Glob,LS")
                _log(f"  Architect repo araclari ACIK: --add-dir {len(_repo_dirs)} repo, Read/Grep/Glob")

        analysis_crew = self._agile_crew.create_analysis_crew()
        try:
            analysis_result = analysis_crew.kickoff(inputs={
                "work_item_id": self.state.work_item_id,
                "target_repo": prefetch_repo or "",
                "previous_context": ctx,
                "scrum_master_feedback": ctx_hint,
            })
        except Exception as e:
            # Guardrail retry'lari tukendi (veya kickoff hatasi) — guardrail'siz
            # crew ile manuel fallback. Mevcut parse onarimi yine devrede.
            # NOT: Tukenen guardrail retry'larinin token'lari exception ile dustugu
            # icin budget'a sayilmaz (hafif under-count); sadece guardrails ACIK +
            # exhaustion yolunda olur, cost guard yine fallback sonucunu sayar.
            _log(f"  Guardrail/kickoff hatasi ({e}), guardrail'siz fallback kickoff")
            analysis_crew = self._agile_crew.create_analysis_crew(with_guardrail=False)
            analysis_result = analysis_crew.kickoff(inputs={
                "work_item_id": self.state.work_item_id,
                "target_repo": prefetch_repo or "",
                "previous_context": ctx,
                "scrum_master_feedback": ctx_hint,
            })
        self._track_and_check_budget(analysis_result, "technical_design_task")
        raw_output = analysis_result.raw or ""

        # Parse hatasi — onceki ciktiyi context'e ekleyip tekrar dene
        # (Guardrail KAPALIYKEN birincil retry; ACIKKEN guardrail zaten parse
        #  garantiledigi icin bu blok genelde tetiklenmez — yine de fallback kalir.)
        try:
            plan = _parse_architect_output(raw_output)
        except ValueError as e:
            _log(f"  Parse hatasi ({e}), guardrail'siz architect ile retry")
            retry_ctx = ctx + (
                f"\n\n# ONCEKI DENEME CIKTISI\n"
                f"(Asagidaki bilgilerden JSON uret — tool cagirma, direkt JSON yaz)\n\n"
                f"{raw_output[:6000]}"
            )
            analysis_crew = self._agile_crew.create_analysis_crew(with_guardrail=False)
            analysis_result = analysis_crew.kickoff(inputs={
                "work_item_id": self.state.work_item_id,
                "target_repo": prefetch_repo or "",
                "previous_context": retry_ctx,
                "scrum_master_feedback": (
                    f"⚠️ Onceki denemende plan validation hatasi:\n{e}\n\n"
                    "Hatayi gider ve SADECE gecerli JSON plan uret. "
                    "Tool cagirma, aciklama yazma — SADE JSON. "
                    "Placeholder kullanma (timestamp/class/tablo adlari somut olmali)."
                ),
            })
            self._track_and_check_budget(analysis_result, "technical_design_task (retry)")
            raw_output = analysis_result.raw or ""
            plan = _parse_architect_output(raw_output)

        # SM Review
        approved, feedback = self._scrum_review("Teknik Tasarim", raw_output[:3000])
        if not approved:
            _log("  SM iyilestirme istedi, tekrar calistiriliyor...")
            analysis_crew = self._agile_crew.create_analysis_crew()
            analysis_result = analysis_crew.kickoff(inputs={
                "work_item_id": self.state.work_item_id,
                "target_repo": "",
                "previous_context": ctx,
                "scrum_master_feedback": f"SCRUM MASTER GERI BILDIRIMI:\n{feedback}",
            })
            self._track_and_check_budget(analysis_result, "technical_design_task (SM retry)")
            raw_output = analysis_result.raw or ""
            plan = _parse_architect_output(raw_output)

        # Architect kickoff'lari bitti — repo-tool baglamini temizle (sonraki
        # adimlara sizmasin). Defensif olarak _reset_job_state'de de temizlenir.
        _cli.clear_repo_ctx()

        self.state.requirements_text = self.state.requirements_text or raw_output

        repo_name = plan["repo_name"]
        if repo_name not in self.state.known_repos:
            repo_name = _resolve_repo_name(
                repo_name, self.state.known_repos, self._client, self.state.work_item_id,
            )
            plan["repo_name"] = repo_name

        # ── Repo otoritesi: discover_repos kanit-temelli secimi architect'i ezer ──
        # Architect tool'suzken kor olabilir ve repo_name'i halusine edebilir
        # (WI #66511: architect 'orkestra' dedi, returnOffices aslinda webservice'te).
        # discover_repos symbol-grep kaniti uzerine akil yurutup dogru repoyu sectiyse
        # ve architect farkli bir repo verdiyse, discover_repos'a don.
        _disc = getattr(self, "_discovered_repo", "")
        if _disc and _disc in self.state.known_repos and repo_name != _disc:
            _log(
                f"  ⚠️ Repo otoritesi: architect '{repo_name}' dedi ama discover_repos "
                f"'{_disc}' (kanit-temelli). '{_disc}' ile devam ediliyor."
            )
            _add_wi_comment(self._client, self.state.work_item_id,
                f"## ℹ️ Repo Kararı Düzeltildi\n\n"
                f"Teknik tasarım `{repo_name}` önerdi; kanıt-temelli repo keşfi `{_disc}` "
                f"buldu (WI sembolleri bu repoda). `{_disc}` ile devam ediliyor.\n\n"
                f"---\n*Agile SDLC Crew - Repo Authority*"
            )
            repo_name = _disc
            plan["repo_name"] = repo_name

        self.state.repo_name = repo_name
        self.state.plan = plan
        # technical_design_task ciktisi JSON — cache'den parse edilebilmesi icin
        # tam veya en azindan buyuk pencereli sakla (onceden [:3000] ile kesilip
        # sonraki run'da JSON bozuk geliyordu)
        self._step_done("technical_design_task", raw_output[:50_000])
        _log(f"  Teknik tasarim tamamlandi")

    # ── Convergence: her iki planlama yolu buraya akar ──

    @listen(or_(hal_planning, crew_step4_technical_design))
    def step5_create_branch(self):
        """Adim 5: Branch Olustur + Repo'yu locale clone et."""
        from agile_sdlc_crew.pipeline import create_branch

        plan = self.state.plan
        repo_name = self.state.repo_name

        _log(f"\n  Repo: {repo_name}")
        _log(f"  Degisecek dosyalar: {len(plan.get('changes', []))}")
        for ch in plan.get("changes", []):
            _log(f"    [{ch.get('change_type', 'edit')}] {ch['file_path']}: {ch.get('description', '')[:60]}")

        # Hedef repo icin explicit fetch + checkout (en guncel main'i al)
        # + eski local feature branch'i sil — bir onceki job'dan kalan
        # stale commit'leri temizle (push'lar API'ye gidiyor ama file_exists
        # ve get_file_content local'e bakabiliyor, eski state sorun cikarabilir).
        try:
            repo_dir = self._repo_mgr.base_dir / repo_name
            branch_name_for_cleanup = f"feature/{self.state.work_item_id}"
            if repo_dir.exists() and (repo_dir / ".git").exists():
                _log(f"  Hedef repo fetch: {repo_name}")
                # once main'i guncelle
                fetch_result = self._repo_mgr._git(["fetch", "origin", "main"], cwd=repo_dir)
                if fetch_result.returncode != 0:
                    _log(f"  fetch uyarisi: {fetch_result.stderr[:150]}")
                # main'e don (feature branch'i checkoutlu olabilir)
                self._repo_mgr._git(["checkout", "main"], cwd=repo_dir)
                # local origin/main'e hard reset — kesinlikle temiz main
                self._repo_mgr._git(["reset", "--hard", "origin/main"], cwd=repo_dir)
                # KOD DUZENLEME FAZI: local feature branch'i varsa SIL ve
                # bir sonraki adimda origin/main'den taze olarak yeniden
                # olustur (create_branch API call'u). Boylece onceki yanlis
                # job'larin commit'leri agentlari yanildirmaz.
                _has_stale = self._repo_mgr._git(
                    ["rev-parse", "--verify", "--quiet", branch_name_for_cleanup],
                    cwd=repo_dir,
                ).returncode == 0
                if _has_stale:
                    self._repo_mgr._git(
                        ["branch", "-D", branch_name_for_cleanup], cwd=repo_dir,
                    )
                    _log(f"  Eski local branch silindi: {branch_name_for_cleanup}")
                else:
                    _log(f"  Local feature branch yok, taze olusturulacak")
            self._repo_mgr.checkout(repo_name, "main")
            # Deps install — vendor/ olusturur, agent'lar 3rd-party kodu okuyabilir.
            # Pipeline knob ile kontrol; default kapali (yavas ilk install).
            from agile_sdlc_crew import pipeline_config as _pc_deps
            install_ok = False
            if _pc_deps.get("CREW_INSTALL_DEPS"):
                try:
                    install_result = self._repo_mgr.install_dependencies(repo_name)
                    install_ok = bool(install_result.get("success"))
                    status_icon = "✓" if install_ok else "✗"
                    _log(f"  Deps install [{install_result.get('manager','?')}] {status_icon}: "
                         f"{install_result.get('message','')} ({install_result.get('elapsed_s',0):.0f}s)")
                except Exception as e:
                    _log(f"  Deps install hatasi: {e}")

            # Hedef odakli embed — sadece plan'daki dosyalarin parent dizinleri.
            # Tum repo embed'i (4000+ dosya) yerine ihtiyac duyulan ~20 dosya.
            # Vendor allowlist varsa o paketler de bu sinirli kapsam icine girer.
            if _pc_deps.get("CREW_VENDOR_INDEX") and self._vector_store:
                if not install_ok and _pc_deps.get("CREW_INSTALL_DEPS"):
                    _log("  Vendor index atlandi: deps install basarisiz")
                else:
                    try:
                        vendor_allow = self._repo_mgr.get_vendor_allowlist(repo_name)
                        plan_files = [
                            ch.get("file_path", "").lstrip("/")
                            for ch in (self.state.plan.get("changes") or [])
                            if ch.get("file_path")
                        ]
                        if plan_files:
                            _log(
                                f"  Hedef odakli embed: {len(plan_files)} plan dosyasi"
                                + (f", {len(vendor_allow)} vendor paketi" if vendor_allow else "")
                            )
                            self._vector_store._indexed_repos.discard(repo_name)
                            repo_dir = self._repo_mgr.base_dir / repo_name
                            self._vector_store.index_plan_files(
                                repo_name, repo_dir,
                                plan_file_paths=plan_files,
                                vendor_allowlist=vendor_allow or None,
                            )
                        else:
                            _log("  Hedef embed atlandi: plan'da degisecek dosya yok")
                    except Exception as e:
                        _log(f"  Hedef embed hatasi: {e}")

            # Kod embedding step'i bilgi amacli — gercek embed yukaridaki
            # hedef odakli akista yapildi (yapilmadiysa bile dashboard'da
            # adim "tamamlandi" gozuksun).
            self._step_start("code_embedding_task")
            self._step_done("code_embedding_task", "Hedef odakli embed (plan dosyalari)")
        except Exception as e:
            _log(f"  Local repo checkout hatasi: {e}")

        _log("\n-- ADIM 5: Branch olusturuluyor --")
        self._step_start("create_branch_task")

        if self.state.dry_run:
            # Dry-run: create branch locally (no remote API call).
            branch_name = f"feature/{self.state.work_item_id}"
            repo_dir = self._repo_mgr.base_dir / repo_name
            try:
                # Branch from current HEAD (main) — already reset above
                self._repo_mgr._git(["checkout", "-B", branch_name], cwd=repo_dir)
                self.state.branch_name = branch_name
                _log(f"  🔬 DRY-RUN Branch (local): {branch_name}")
            except Exception as e:
                raise RuntimeError(f"Dry-run branch olusturulamadi: {e}")
        else:
            branch_result = create_branch(repo_name, self.state.work_item_id)
            if not branch_result["success"]:
                raise RuntimeError(f"Branch olusturulamadi: {branch_result['error']}")
            self.state.branch_name = branch_result["branch"]
            _log(f"  Branch: {self.state.branch_name}")
            if branch_result.get("note"):
                _log(f"    ({branch_result['note']})")

        self._step_done("create_branch_task", f"Branch: {self.state.branch_name}")
        if self.state.job_id:
            self._db.update_job(self.state.job_id, repo_name=repo_name, branch_name=self.state.branch_name)

    @listen(step5_create_branch)
    def step6_implement_code(self):
        """Adim 6: Kod Gelistirme - dosya dongusu."""
        from agile_sdlc_crew.main import _extract_code_from_output, _validate_code
        from agile_sdlc_crew.pipeline import push_file
        import os.path as _osp

        _log("\n-- ADIM 6: Kod gelistirme --")
        self._step_start("implement_change_task")

        plan = self.state.plan
        repo_name = self.state.repo_name
        branch_name = self.state.branch_name
        all_pushes = []

        # Ayni dosyayi hedefleyen degisiklikleri birlestir — yoksa ikinci push
        # birincinin duzeltmesini ezer (WI #66687 Kargoist.php v2+legacy dali).
        _orig_n = len(plan.get("changes", []))
        plan["changes"] = _coalesce_plan_changes(plan.get("changes", []))
        if len(plan["changes"]) < _orig_n:
            _log(f"  Aynı dosyayı hedefleyen değişiklikler birleştirildi: "
                 f"{_orig_n} → {len(plan['changes'])} (clobber önleme)")
        self.state.plan = plan

        # Plan ozeti — developer her dosyayi implement ederken TUM plani gorsun.
        # Dosyalar arasi bagimliliklari anlamasi icin kritik (ornek: frontend API yolunu
        # backend route'tan bilmeli, service interface'ini model dosyasindan gormeli).
        plan_summary_parts = []
        for ch in plan.get("changes", []):
            plan_summary_parts.append(
                f"- [{ch.get('change_type','edit')}] {ch.get('file_path','?')}: {ch.get('description','')[:100]}"
            )
        plan_summary = "\n".join(plan_summary_parts)

        # Implement edilen dosyalarin kodlari — sonraki dosyalar bunlari referans alabilir
        implemented_codes: dict[str, str] = {}

        def _dev_context() -> str:
            """Developer'a plan ozeti + implement edilen dosyalari dondurur."""
            from agile_sdlc_crew import pipeline_config as _pc_dc
            budget = _pc_dc.get("CREW_DEV_CONTEXT_BUDGET")
            per_file = _pc_dc.get("CREW_DEV_CONTEXT_PER_FILE")
            parts = [f"# TUM PLAN ({len(plan.get('changes',[]))} dosya)\n{plan_summary}"]
            if implemented_codes:
                parts.append(f"\n# ONCEKI DOSYALAR ({len(implemented_codes)})")
                remaining = budget
                for fp, code in implemented_codes.items():
                    snippet = code[:min(per_file, remaining)]
                    parts.append(f"\n## {fp}\n```\n{snippet}\n```")
                    remaining -= len(snippet)
                    if remaining <= 0:
                        break
            return "\n".join(parts)

        for i, change in enumerate(plan.get("changes", [])):
            file_path = change["file_path"]
            change_type = change.get("change_type", "edit")
            description = change.get("description", "")
            new_code = change.get("new_code", "")
            current_code = change.get("current_code", "")

            _log(f"\n  [{i+1}/{len(plan['changes'])}] {file_path} ({change_type})")

            # Skip: branch'te bu dosya bu job'da zaten push edilmis ise atla.
            # Kritik: branch yeni olusturulduysa (hic commit yok), API
            # get_file_content branch_name icin main'in icerigini doner —
            # bu yuzden 'prefix eslesmesi' yanlis pozitif verir (edit edilmis
            # dosyanin headeri eski headerle ayni). Sadece TAM eslesme
            # gercek bir skip sinyalidir.
            try:
                branch_content = self._client.get_file_content(repo_name, file_path, branch_name)
                if (
                    branch_content and new_code
                    and branch_content.strip() == new_code.strip()
                ):
                    _log(f"    ⏩ Branch'te ayni icerik zaten push edilmis, atlanıyor")
                    all_pushes.append({"file": file_path, "success": True, "change_type": change_type, "note": "skip-exists"})
                    implemented_codes[file_path] = branch_content[:3000]
                    continue
            except Exception:
                pass  # dosya branch'te yok — normal devam

            # Mevcut dosya icerigini oku (local repo oncelikli, API fallback)
            full_content = ""
            try:
                full_content = self._repo_mgr.get_file_content(repo_name, file_path, "main")
                _log(f"    Mevcut dosya (local): {len(full_content)} karakter")
            except Exception:
                basename = _osp.basename(file_path)
                parent_dir = _osp.dirname(file_path)
                _log(f"    Dosya bulunamadi, repo'da araniyor: {basename}")
                try:
                    search_dirs = [parent_dir]
                    if parent_dir and parent_dir != "/":
                        search_dirs.append(_osp.dirname(parent_dir))
                    found_path = None
                    for search_dir in search_dirs:
                        try:
                            items = self._repo_mgr.get_items_in_path(repo_name, search_dir or "/", "main")
                            for item in items:
                                item_path = item.get("path", "")
                                item_name = _osp.basename(item_path)
                                name_no_ext = _osp.splitext(basename)[0].lower().replace("controller", "")
                                if (item_name.lower() == basename.lower() or
                                        name_no_ext in item_name.lower()):
                                    found_path = item_path
                                    break
                        except Exception:
                            continue
                        if found_path:
                            break
                    if found_path:
                        _log(f"    Benzer dosya bulundu: {found_path}")
                        file_path = found_path
                        change["file_path"] = found_path
                        full_content = self._repo_mgr.get_file_content(repo_name, found_path, "main")
                        _log(f"    Mevcut dosya (local): {len(full_content)} karakter")
                    else:
                        _log(f"    Yeni dosya olacak")
                        change_type = "add"
                except Exception as search_err:
                    _log(f"    Arama hatasi: {search_err}, yeni dosya olacak")
                    change_type = "add"

            if change_type == "add" and new_code:
                if full_content:
                    final_content = full_content.rstrip() + "\n\n" + new_code + "\n"
                    _log(f"    Mevcut dosyaya append: {len(new_code)} karakter eklendi")
                else:
                    final_content = new_code
                    _log(f"    Yeni dosya: {len(final_content)} karakter")

            elif full_content and new_code and current_code:
                # D: Direct-edit onceligi — LLM cagirmadan Python'da replace (fuzzy dahil)
                from agile_sdlc_crew.main import _try_direct_edit
                cur_lines = len(current_code.strip().splitlines())
                new_lines = len(new_code.strip().splitlines())
                # Guvenlik: current_code >> new_code ise buyuk kod kaybi riski, append'e yonlendir
                if cur_lines > 20 and new_lines < cur_lines * 0.3:
                    _log(f"    Guvenlik: current_code ({cur_lines} satir) >> new_code ({new_lines} satir), append yapiliyor")
                    final_content = full_content.rstrip() + "\n\n" + new_code + "\n"
                else:
                    replaced = _try_direct_edit(full_content, current_code, new_code)
                    if replaced is not None:
                        final_content = replaced
                        _log(f"    ✅ Direkt replace basarili (LLM cagrilmadi)")
                    else:
                        # Match edilemedi — LLM'e "SADECE YENI BLOK" sor (tam dosya degil)
                        # Kucuk local modeller (Qwen 7B) tam dosya basaramiyor,
                        # ama blok uretmek dogal. Python tarafi replace'i yapar.
                        _log(f"    Direct-edit (4 katman fuzzy) match edilemedi, LLM'den blok isteniyor")
                        code_crew = self._agile_crew.create_code_crew()
                        # Blok modu: sadece current_code → new_code degisimi
                        # previous_context MINIMAL — context length asilmasin
                        code_result = code_crew.kickoff(inputs={
                            "work_item_id": self.state.work_item_id,
                            "target_repo": repo_name,
                            "target_file": file_path,
                            "change_description": (
                                f"{description}\n\n"
                                f"⚠️ CIKTIN: SADECE YENI KOD BLOGU olmali, TAM DOSYA DEGIL.\n"
                                f"- Asagidaki current_code bloğunun YERINE gelecek yeni kodu yaz.\n"
                                f"- Dosyanin geri kalanini (import'lar, diger fonksiyonlar vb.) "
                                f"tekrar yazma — Python tarafi geri kalanini koruyacak.\n"
                                f"- Aciklama/yorum yazma, sadece degisecek blok."
                            ),
                            "current_code": current_code[:4000],
                            "new_code": new_code[:4000],
                            "previous_context": f"# PLAN\n{plan_summary}",
                        })
                        self._track_and_check_budget(code_result, f"implement:{file_path}")
                        dev_block = _extract_dev_output(code_result)
                        if not dev_block.strip():
                            _log(f"    Developer bos icerik dondurdu, atlaniyor")
                            continue
                        _log(f"    Developer blok: {len(dev_block)} karakter")
                        # Python tarafi: current_code'u dev_block ile fuzzy-replace et
                        replaced2 = _try_direct_edit(full_content, current_code, dev_block)
                        if replaced2 is not None:
                            final_content = replaced2
                            _log(f"    ✅ Developer blok + Python fuzzy-replace basarili")
                        else:
                            # Developer blok bile match edilemedi — son care: plan'in new_code'u
                            # ile append yap (dosyanin sonuna eklenir, kaybetmektense)
                            _log(f"    ⚠️ Blok match edilemedi, append stratejisine dusuluyor")
                            final_content = full_content.rstrip() + "\n\n" + dev_block + "\n"

            elif full_content and new_code:
                # current_code yok — append (add scenario)
                final_content = full_content.rstrip() + "\n\n" + new_code + "\n"
                _log(f"    Append: dosyanin sonuna eklendi (current_code yok)")

            elif full_content and not new_code:
                _log(f"    Kod belirtilmemis, agent'a birakiliyor")
                code_crew = self._agile_crew.create_code_crew()
                code_result = code_crew.kickoff(inputs={
                    "work_item_id": self.state.work_item_id,
                    "target_repo": repo_name,
                    "target_file": file_path,
                    "change_description": description,
                    "current_code": full_content[:6000],
                    "new_code": f"[Degisiklik aciklamasi: {description}]",
                    "previous_context": _dev_context(),
                })
                self._track_and_check_budget(code_result, f"implement-noNewCode:{file_path}")
                final_content = _extract_dev_output(code_result)
                if not final_content.strip():
                    _log(f"    Developer bos icerik dondurdu, atlaniyor")
                    continue
                _log(f"    Developer kodu: {len(final_content)} karakter")
            else:
                _log(f"    Ne mevcut dosya ne de yeni kod var, atlaniyor")
                continue

            # Kod dogrulama
            validated, final_content = _validate_code(
                final_content, file_path, full_content, description, repo_name=repo_name
            )
            if not validated:
                _log(f"    Kod dogrulama basarisiz, duzeltme deneniyor...")
                code_crew = self._agile_crew.create_code_crew()
                code_result = code_crew.kickoff(inputs={
                    "work_item_id": self.state.work_item_id,
                    "target_repo": repo_name,
                    "target_file": file_path,
                    "change_description": (
                        f"Asagidaki kod dogrulama hatasi var, duzelt:\n"
                        f"Dosya: {file_path}\n"
                        f"Hata: Kod derlenemiyor veya calismaz durumda.\n"
                        f"Mevcut kodu duzeltip CALISIR hale getir."
                    ),
                    "current_code": final_content[:6000],
                    "new_code": full_content[:6000] if full_content else final_content[:6000],
                    "previous_context": _dev_context(),
                })
                self._track_and_check_budget(code_result, f"fix:{file_path}")
                fixed_code = _extract_dev_output(code_result)
                if fixed_code.strip():
                    validated2, fixed_code = _validate_code(
                        fixed_code, file_path, full_content, description, repo_name=repo_name
                    )
                    if validated2:
                        final_content = fixed_code
                        _log(f"    Developer duzeltme basarili")
                    else:
                        _log(f"    Developer duzeltme de basarisiz, atlaniyor")
                        continue
                else:
                    _log(f"    Developer bos dondurdu, atlaniyor")
                    continue

            # ── Guvenlik Kontrolleri (push oncesi) ──
            orig_len = len(full_content.strip()) if full_content else 0
            new_len = len(final_content.strip())
            orig_lines = full_content.count("\n") if full_content else 0
            new_lines = final_content.count("\n")

            # 1. Append/add senaryosunda dosya kisalmamali
            if change_type == "add" and full_content and new_len < orig_len:
                _log(f"    GUVENLIK: add modunda dosya kisaldi ({orig_len} -> {new_len} char), push iptal")
                continue

            # 2. Edit senaryosunda cok buyuk kod kaybi — muhtemelen agent hatali output verdi
            # Orijinal dosya >500 char VE yeni icerik orijinalin %50'sinden kisa ise suphelen
            if full_content and orig_len > 500 and new_len < orig_len * 0.5:
                _log(
                    f"    🚨 GUVENLIK ALARMI: dosya %{100 - int(100 * new_len / orig_len)} kuculdu "
                    f"({orig_lines} → {new_lines} satir, {orig_len} → {new_len} char). "
                    f"Agent muhtemelen tam dosya yerine sadece degisen kismi dondurdu. Push IPTAL."
                )
                continue

            # 3. Cok az icerik — 3 satirdan kisa push yapma
            if new_len < 50 or new_lines < 3:
                _log(f"    GUVENLIK: cok kisa icerik ({new_lines} satir, {new_len} char), push iptal")
                continue

            commit_msg = f"#{self.state.work_item_id}: {description[:80]}"
            push_result = push_file(
                repo_name, branch_name, file_path, final_content, commit_msg,
                repo_mgr=self._repo_mgr, dry_run=self.state.dry_run,
            )
            if push_result["success"]:
                if push_result.get("dry_run"):
                    _log(f"    🔬 DRY-RUN local commit ({push_result['change_type']}): {push_result.get('local_path', file_path)}")
                else:
                    _log(f"    Push #{push_result.get('push_id','?')} ({push_result['change_type']})")
                all_pushes.append(push_result)
                # Sonraki dosyalar bu dosyanin kodunu referans alabilsin
                implemented_codes[file_path] = final_content[:3000]
            else:
                _log(f"    Push hatasi: {push_result['error']}")

        self.state.all_pushes = all_pushes
        self._step_done("implement_change_task", f"{len(all_pushes)} dosya push edildi")

    @listen(step6_implement_code)
    def step7_create_pr(self):
        """Adim 7: PR Olustur — plan-push eslesmesi kontrolu ile.
        DRY-RUN: PR olusturulmaz, placeholder URL ile gecilir."""
        from agile_sdlc_crew.main import _get_work_item_title, _add_wi_comment
        from agile_sdlc_crew.pipeline import create_pull_request

        _log("\n-- ADIM 7: PR olusturuluyor --")
        self._step_start("create_pr_task")

        if self.state.dry_run:
            repo_path = self._repo_mgr.base_dir / self.state.repo_name
            self.state.pr_id = ""
            self.state.pr_url = (
                f"DRY-RUN: no PR. Local branch '{self.state.branch_name}' at {repo_path}"
            )
            _log(f"  🔬 DRY-RUN: PR olusturma atlandi")
            _log(f"     Lokal branch: {self.state.branch_name}")
            _log(f"     Repo yolu:    {repo_path}")
            _log(f"     Inceleme:     cd {repo_path} && git log --oneline main..{self.state.branch_name}")
            self._step_done("create_pr_task", self.state.pr_url)
            return

        if not self.state.all_pushes:
            _add_wi_comment(self._client, self.state.work_item_id,
                f"## ❌ PR Oluşturulamadı — Hiçbir Dosya Push Edilemedi\n\n"
                f"Plan'daki tüm dosya değişiklikleri güvenlik kontrollerinde reddedildi "
                f"veya hata verdi. Pipeline iptal edildi.\n\n"
                f"---\n*Agile SDLC Crew*"
            )
            raise RuntimeError("Hicbir dosya push edilemedi, PR olusturulamiyor.")

        plan = self.state.plan

        # 🚨 PLAN-PUSH ESLESME KONTROLU
        expected_files = {ch.get("file_path", "") for ch in plan.get("changes", []) if ch.get("file_path")}
        pushed_files = {p.get("file", "") for p in self.state.all_pushes if p.get("file")}
        missing = expected_files - pushed_files
        coverage = len(pushed_files) / max(1, len(expected_files))

        if coverage < 0.7:
            missing_list = "\n".join(f"- `{f}`" for f in sorted(missing)[:15])
            _log(f"  🚨 PUSH EKSIK: {len(pushed_files)}/{len(expected_files)} dosya (%{int(coverage*100)})")
            _add_wi_comment(self._client, self.state.work_item_id,
                f"## ❌ PR Oluşturulmadı — Plan Eksik Uygulandı\n\n"
                f"Plan'da **{len(expected_files)} dosya** değişikliği vardı ama sadece "
                f"**{len(pushed_files)} tanesi** push edilebildi (%{int(coverage*100)}).\n\n"
                f"**Push edilemeyen dosyalar:**\n{missing_list}\n\n"
                f"Yarım PR açmak yerine pipeline iptal edildi. Lütfen işi tekrar deneyin "
                f"veya iş kalemindeki detayları gözden geçirin.\n\n"
                f"---\n*Agile SDLC Crew - Plan-Push Eşleşme Kontrolü*"
            )
            self._step_fail("create_pr_task", f"Push eksik: {len(pushed_files)}/{len(expected_files)}")
            raise RuntimeError(
                f"Plan-push uyumsuzlugu: {len(pushed_files)}/{len(expected_files)} "
                f"dosya push edildi, %70 esigin altinda. PR iptal."
            )
        elif missing:
            _log(f"  ⚠️  Bazi dosyalar push edilemedi: {sorted(missing)[:5]}")

        # ── Onceki run'dan kalan aktif PR varsa onu kullan ──
        # Branch'te zaten PR acilmissa yenisini olusturmak Azure DevOps'ta
        # 409 + retry'lar + SSL hatasi domino'su yaratir.
        try:
            existing_pr = self._client.find_active_pr_by_branch(
                self.state.repo_name, self.state.branch_name,
            )
        except Exception as _e:
            _log(f"  Mevcut PR sorgusu hatasi (devam ediliyor): {_e}")
            existing_pr = None

        if existing_pr:
            existing_pr_id = existing_pr.get("pullRequestId")
            project = (existing_pr.get("repository", {}) or {}).get("project", {}).get("name", "")
            existing_url = (
                f"{self._client.org_url}/{project}/_git/{self.state.repo_name}"
                f"/pullrequest/{existing_pr_id}"
            )
            _log(f"  ⏩ Branch'te zaten aktif PR var: #{existing_pr_id}, yeniden kullaniliyor")
            self.state.pr_id = str(existing_pr_id)
            self.state.pr_url = existing_url
            self._step_done("create_pr_task", f"PR #{self.state.pr_id} (mevcut): {self.state.pr_url}")
            if self.state.job_id:
                self._db.update_job(self.state.job_id, pr_id=self.state.pr_id, pr_url=self.state.pr_url)
            return

        wi_title = _get_work_item_title(
            self._client, self.state.work_item_id, plan.get("summary", "Gelistirme"),
        )
        pr_title = f"#{self.state.work_item_id} - {wi_title[:80]}"
        pr_desc = "## Degisiklikler\n\n"
        for ch in plan.get("changes", []):
            pr_desc += f"- [{ch.get('change_type', 'edit')}] `{ch['file_path']}`: {ch.get('description', '')[:100]}\n"
        if plan.get("acceptance_criteria"):
            pr_desc += "\n## Kabul Kriterleri\n\n"
            for ac in plan["acceptance_criteria"]:
                pr_desc += f"- [ ] {ac}\n"
        pr_desc += f"\n---\n*Agile SDLC Crew ile otomatik olusturuldu*"

        # Transient SSL/network hatalari icin retry — Azure DevOps zaman zaman
        # UNEXPECTED_EOF_WHILE_READING firlatabiliyor.
        import time as _t
        pr_result = None
        last_err = None
        for attempt in range(3):
            try:
                pr_result = create_pull_request(
                    self.state.repo_name, self.state.branch_name,
                    self.state.work_item_id, pr_title, pr_desc,
                )
                if pr_result.get("success"):
                    break
                last_err = pr_result.get("error", "unknown")
            except Exception as _e:
                last_err = str(_e)
                pr_result = {"success": False, "error": last_err}
            if attempt < 2:
                _log(f"  PR olusturma denemesi {attempt+1}/3 hatali: {str(last_err)[:120]}, {2 ** attempt}s bekleniyor")
                _t.sleep(2 ** attempt)

        if not pr_result or not pr_result.get("success"):
            # Son sans: belki PR olusturulurken SSL hatasi aldik ama PR aslinda olustu.
            # Bir kez daha mevcut PR sorgulayalim.
            try:
                created_pr = self._client.find_active_pr_by_branch(
                    self.state.repo_name, self.state.branch_name,
                )
                if created_pr:
                    pr_result = {
                        "success": True,
                        "pr_id": created_pr.get("pullRequestId"),
                        "url": (
                            f"{self._client.org_url}/"
                            f"{(created_pr.get('repository',{}) or {}).get('project',{}).get('name','')}"
                            f"/_git/{self.state.repo_name}/pullrequest/{created_pr.get('pullRequestId')}"
                        ),
                    }
                    _log(f"  ⏩ PR aslinda olusmus (SSL hatasi yanildi): #{pr_result['pr_id']}")
            except Exception:
                pass

        if not pr_result or not pr_result.get("success"):
            raise RuntimeError(f"PR olusturulamadi (3 deneme): {last_err}")

        self.state.pr_id = str(pr_result["pr_id"])
        self.state.pr_url = pr_result["url"]
        _log(f"  PR #{self.state.pr_id}: {self.state.pr_url}")
        self._step_done("create_pr_task", f"PR #{self.state.pr_id}: {self.state.pr_url}")
        if self.state.job_id:
            self._db.update_job(self.state.job_id, pr_id=self.state.pr_id, pr_url=self.state.pr_url)

    # ── Faz 3: Dogrulama ────────────────────────────

    @listen(step7_create_pr)
    def step8_code_review(self):
        """Adim 8: PR Yorumlarini Yanitla + Kod Inceleme.
        DRY-RUN: PR yok, review atlanir."""
        from agile_sdlc_crew.main import _add_wi_comment

        if self.state.dry_run:
            _log("\n-- ADIM 8: Kod inceleme — DRY-RUN: atlandi (PR yok) --")
            self._step_start("review_pr_task")
            self.state.review_text = "DRY-RUN: code review skipped (no remote PR)"
            self._step_done("review_pr_task", self.state.review_text)
            return

        # Onceki PR yorumlarina yanit ver (implement sonrasi)
        # Resume durumunda _pr_threads_to_respond bos olabilir — direkt oku
        pr_threads = getattr(self, "_pr_threads_to_respond", [])
        pr_repo = getattr(self, "_pr_repo_for_threads", "") or self.state.repo_name
        pr_id_old = getattr(self, "_pr_id_for_threads", 0)
        if not pr_threads and self.state.pr_id and pr_repo:
            try:
                _pr_id_int = int(self.state.pr_id)
                threads_raw = self._client.get_pr_threads(pr_repo, _pr_id_int)
                for thread in threads_raw:
                    if thread.get("properties", {}).get("CodeReviewThreadType"):
                        continue
                    if thread.get("status", "") in ("fixed", "closed", "wontFix", "byDesign"):
                        continue
                    tid = thread.get("id")
                    if not tid:
                        continue
                    for comment in thread.get("comments", []):
                        if comment.get("commentType") == "system":
                            continue
                        content = comment.get("content", "").strip()
                        if content and "Agile SDLC Crew" not in content:
                            fp = None
                            tc = thread.get("threadContext")
                            if tc:
                                fp = tc.get("filePath")
                            pr_threads.append({"thread_id": tid, "author": comment.get("author", {}).get("displayName", ""), "content": content, "file_path": fp})
                            break
                pr_id_old = _pr_id_int
                if pr_threads:
                    _log(f"  PR thread'leri direkt okundu: {len(pr_threads)} aktif yorum")
            except Exception as e:
                _log(f"  PR thread okuma hatasi: {e}")
        if pr_threads and pr_repo and pr_id_old:
            _log(f"\n-- PR YORUMLARINA YANIT ({len(pr_threads)} yorum) --")
            plan_files = {ch.get("file_path", ""): ch.get("description", "") for ch in self.state.plan.get("changes", [])}
            pushed_files = {p.get("file", "") for p in self.state.all_pushes}

            for t in pr_threads:
                thread_id = t["thread_id"]
                file_path = t.get("file_path")
                comment_content = t["content"]

                try:
                    if file_path and file_path in pushed_files:
                        # Dosya duzeltildi — ne yapildigini acikla
                        desc = plan_files.get(file_path, "")
                        self._client.reply_to_pr_thread(
                            pr_repo, pr_id_old, thread_id,
                            f"**Duzeltildi.**\n\n"
                            f"Plan: {desc[:200]}\n\n"
                            f"Yeni commit push edildi.\n\n"
                            f"---\n*Agile SDLC Crew*"
                        )
                        self._client.resolve_pr_thread(pr_repo, pr_id_old, thread_id)
                        _log(f"  ✅ Thread #{thread_id} ({file_path}): duzeltildi + resolve")
                    elif file_path and file_path not in plan_files:
                        # Dosya planda yok — neden yapilmadigini acikla
                        self._client.reply_to_pr_thread(
                            pr_repo, pr_id_old, thread_id,
                            f"Bu dosya mevcut gelistirme planinda yer almiyor.\n\n"
                            f"Yorum incelendi ancak is kaleminin kapsaminda degil "
                            f"veya farkli bir degisiklik gerektiriyor.\n\n"
                            f"---\n*Agile SDLC Crew*"
                        )
                        _log(f"  ℹ️ Thread #{thread_id} ({file_path}): plan disinda, yanit verildi")
                    else:
                        # Genel yorum — plan ozeti ile yanit ver
                        plan_summary = ", ".join(f"`{fp}`" for fp in list(plan_files.keys())[:5])
                        self._client.reply_to_pr_thread(
                            pr_repo, pr_id_old, thread_id,
                            f"Geri bildirim dikkate alindi.\n\n"
                            f"Guncellenen dosyalar: {plan_summary}\n\n"
                            f"---\n*Agile SDLC Crew*"
                        )
                        self._client.resolve_pr_thread(pr_repo, pr_id_old, thread_id)
                        _log(f"  ✅ Thread #{thread_id} (genel): yanit verildi + resolve")
                except Exception as e:
                    _log(f"  Thread #{thread_id} yanit hatasi: {e}")

        _log("\n-- ADIM 8: Kod inceleme --")
        self._step_start("review_pr_task")

        if self._hal:
            changed_files = ", ".join(ch["file_path"] for ch in self.state.plan.get("changes", []))
            review_detail = self._hal.followup(
                f"Yukaridaki degisiklikleri ({changed_files}) kod kalitesi acisindan incele. "
                f"SOLID uyumu, hata yonetimi, edge case eksikleri varsa belirt."
            )
            review_text = review_detail.get("response", "")
        else:
            ctx = self._build_step_context("review_pr_task")
            ctx += self._prefetch_pr_changes_context()
            review_crew = self._agile_crew.create_review_crew()
            review_result = review_crew.kickoff(inputs={
                "work_item_id": self.state.work_item_id,
                "requirements": self.state.requirements_text[:3000],
                "target_repo": self.state.repo_name,
                "target_branch": self.state.branch_name,
                "pr_id": self.state.pr_id,
                "pr_url": self.state.pr_url,
                "previous_context": ctx,
                "scrum_master_feedback": "",
            })
            self._track_and_check_budget(review_result, "review_pr_task")
            review_text = review_result.raw or ""
            # SM Review
            approved, feedback = self._scrum_review("Kod Inceleme", review_text)
            if not approved:
                _log("  SM iyilestirme istedi, tekrar calistiriliyor...")
                review_crew = self._agile_crew.create_review_crew()
                review_result = review_crew.kickoff(inputs={
                    "work_item_id": self.state.work_item_id,
                    "requirements": self.state.requirements_text[:3000],
                    "target_repo": self.state.repo_name,
                    "target_branch": self.state.branch_name,
                    "pr_id": self.state.pr_id,
                    "pr_url": self.state.pr_url,
                    "previous_context": ctx,
                    "scrum_master_feedback": f"SCRUM MASTER GERI BILDIRIMI:\n{feedback}",
                })
                review_text = review_result.raw or ""

        self.state.review_text = review_text

        # 🚨 RESPECT REVIEWER VERDICT — if CHANGES_REQUIRED / REJECTED,
        # loop back into dev (max CREW_REVIEW_MAX_RETRIES, default 2).
        # Accept both English (new) and Turkish (legacy) tokens.
        import os as _os_rev
        rejected = _review_rejected(review_text)
        from agile_sdlc_crew import pipeline_config as _pc_rev
        max_review_retries = _pc_rev.get("CREW_REVIEW_MAX_RETRIES")
        review_attempt = getattr(self, "_review_attempt", 0)
        if rejected:
            if review_attempt >= max_review_retries:
                _log(f"  🚨 REVIEWER RED (deneme {review_attempt}/{max_review_retries} — max asildi, pipeline durduruluyor)")
                _add_wi_comment(self._client, self.state.work_item_id,
                    f"## ❌ Kod İnceleme Başarısız — {max_review_retries} Deneme Sonrası\n\n"
                    f"PR: [#{self.state.pr_id}]({self.state.pr_url})\n\n"
                    f"Reviewer agent {max_review_retries} deneme sonrasında hâlâ değişiklik istiyor.\n\n"
                    f"**Son Değerlendirme:**\n{review_text[:2500]}\n\n"
                    f"Lütfen PR'ı manuel inceleyin.\n\n"
                    f"---\n*Agile SDLC Crew - Code Review Gate*"
                )
                self._step_fail("review_pr_task", f"Reviewer: {max_review_retries} deneme sonrasi RED")
                raise RuntimeError(f"Reviewer {max_review_retries} deneme sonrasi hala reddediyor")

            self._review_attempt = review_attempt + 1
            _log(f"  🔄 REVIEWER RED — tekrar gelistirme dongusune giriliyor (deneme {self._review_attempt}/{max_review_retries})")
            _add_wi_comment(self._client, self.state.work_item_id,
                f"## 🔄 Kod İnceleme — Düzeltme Gerekli (Deneme {self._review_attempt}/{max_review_retries})\n\n"
                f"PR: [#{self.state.pr_id}]({self.state.pr_url})\n\n"
                f"**Reviewer Geri Bildirimi:**\n{review_text[:1500]}\n\n"
                f"Otomatik düzeltme başlatılıyor...\n\n"
                f"---\n*Agile SDLC Crew - Review Retry*"
            )
            # Tekrar gelistirme: implement → push → review (branch + PR zaten var)
            self._review_retry_loop()
            return  # review_retry_loop icerisinde step_done cagirilir

        self._step_done("review_pr_task", review_text[:3000])
        _log(f"  Kod inceleme tamamlandi")
        _add_wi_comment(self._client, self.state.work_item_id,
            f"## Kod Inceleme\n\n"
            f"PR: [#{self.state.pr_id}]({self.state.pr_url})\n\n"
            f"{review_text[:2000]}\n\n"
            f"*Agile SDLC Crew - Kod Inceleme*"
        )

    @listen(step8_code_review)
    def pr_build_gate(self):
        """Adim 8.5: PR CI build/test gate.

        Azure DevOps her PR'da `<repo>-test` pipeline'ini `refs/pull/{id}/merge`
        ref'inde calistirir. Bu adim o build'in sonucunu poll eder; testler
        kirildiysa (failed/partiallySucceeded) developer fix dongusune girer,
        build yesil olana kadar (max CREW_PR_BUILD_MAX_RETRIES) tekrar dener.
        Env: CREW_PR_BUILD_GATE (default kapali — sure/maliyet etkisi var)."""
        from agile_sdlc_crew.main import _add_wi_comment
        from agile_sdlc_crew import pipeline_config as _pc

        step_key = "pr_build_gate"
        self._step_start(step_key)
        if self.state.dry_run:
            self._step_done(step_key, "DRY-RUN: atlandi (PR yok)")
            return
        if not _pc.get("CREW_PR_BUILD_GATE"):
            self._step_done(step_key, "Devre disi (CREW_PR_BUILD_GATE=0)")
            return
        if not self.state.pr_id:
            self._step_done(step_key, "Atlandi — PR yok")
            return

        _log("\n-- ADIM 8.5: PR build/test gate --")
        max_retries = int(_pc.get("CREW_PR_BUILD_MAX_RETRIES"))
        poll_timeout = int(_pc.get("CREW_PR_BUILD_POLL_TIMEOUT"))
        poll_interval = int(_pc.get("CREW_PR_BUILD_POLL_INTERVAL"))

        attempt = 0
        while True:
            outcome, build = self._poll_pr_build(poll_timeout, poll_interval)
            if outcome == "no_pipeline":
                _log("  PR build bulunamadi — bu repoda PR-test pipeline'i yok, gate atlaniyor")
                self._step_done(step_key, "Repoda PR-test pipeline'i yok — gate atlandi")
                return
            if outcome == "timeout":
                _log(f"  ⏱️ Build poll timeout ({poll_timeout}s) — sonuc belirsiz, gate gecildi sayildi")
                self._step_done(step_key, f"Build poll timeout — son durum: {build.get('status') if build else '?'}")
                return
            # outcome == "completed"
            result = (build or {}).get("result")
            if result == "succeeded":
                _log(f"  ✅ PR build BASARILI ({build.get('definition')}, build {build.get('build_id')})")
                _add_wi_comment(self._client, self.state.work_item_id,
                    f"## ✅ PR Test Build Geçti\n\n"
                    f"`{build.get('definition')}` build #{build.get('build_id')} başarılı — testler yeşil.\n\n"
                    f"---\n*Agile SDLC Crew - PR Build Gate*"
                )
                self._step_done(step_key, f"Build {build.get('build_id')} succeeded ({build.get('definition')})")
                return

            # failed / partiallySucceeded / canceled
            summary = ""
            try:
                summary = self._client.get_build_failure_summary(build.get("project"), build.get("build_id"))
            except Exception as e:
                _log(f"  Build failure summary alinamadi: {e}")

            if attempt >= max_retries:
                _log(f"  🚨 PR build {max_retries} deneme sonrasi hala '{result}' — pipeline durduruluyor")
                _add_wi_comment(self._client, self.state.work_item_id,
                    f"## ❌ PR Test Build Başarısız — {max_retries} Düzeltme Sonrası\n\n"
                    f"`{build.get('definition')}` build #{build.get('build_id')} sonucu: **{result}**\n\n"
                    f"**Hata özeti:**\n```\n{summary[:2000]}\n```\n\n"
                    f"Testleri manuel inceleyin.\n\n---\n*Agile SDLC Crew - PR Build Gate*"
                )
                self._step_fail(step_key, f"PR build {max_retries} deneme sonrasi {result}")
                raise RuntimeError(f"PR build {result} ({max_retries} deneme sonrasi)")

            attempt += 1
            _log(f"  🔄 PR build '{result}' — testleri düzeltme döngüsüne giriliyor (deneme {attempt}/{max_retries})")
            _add_wi_comment(self._client, self.state.work_item_id,
                f"## 🔄 PR Test Build Başarısız — Düzeltme (Deneme {attempt}/{max_retries})\n\n"
                f"`{build.get('definition')}` build #{build.get('build_id')} sonucu: **{result}**\n\n"
                f"**Hata özeti:**\n```\n{summary[:1500]}\n```\n\n"
                f"Otomatik düzeltme başlatılıyor...\n\n---\n*Agile SDLC Crew - PR Build Gate*"
            )
            self._fix_failing_build(summary)
            # döngü başına dön — build yeniden tetiklenecek, tekrar poll

    def _repo_has_tests(self, repo_name: str) -> bool:
        """Repo'da unit test altyapisi var mi? (phpunit.xml / *Test.php /
        tests dizini / *_test.go / pytest)."""
        repo_dir = self._repo_mgr.base_dir / repo_name
        if not repo_dir.exists():
            return False
        for marker in ("phpunit.xml", "phpunit.xml.dist", "pytest.ini", "tests", "test"):
            if (repo_dir / marker).exists():
                return True
        import subprocess as _sp
        try:
            res = _sp.run(
                ["grep", "-rlm", "1", "-E",
                 r"class [A-Za-z0-9_]+Test|def test_|func Test[A-Z]",
                 "--include=*.php", "--include=*.py", "--include=*.go",
                 str(repo_dir)],
                capture_output=True, text=True, timeout=8,
            )
            return bool((res.stdout or "").strip())
        except Exception:
            return False

    def _test_requirement_note(self, repo_name: str) -> str:
        """CREW_REQUIRE_TESTS acik VE repoda test varsa, plan/inceleme icin
        eklenecek test-zorunlulugu notu (yoksa bos string)."""
        from agile_sdlc_crew import pipeline_config as _pc_rt
        try:
            if not _pc_rt.get("CREW_REQUIRE_TESTS"):
                return ""
        except Exception:
            return ""
        if not self._repo_has_tests(repo_name):
            return ""
        return (
            "\n\n# TEST ZORUNLULUĞU (bu repoda unit test altyapısı VAR)\n"
            "- Değişen davranış için ilgili test dosyalarını da güncelle/ekle "
            "(aynı PR'da). Mevcut testleri kırma.\n"
            "- Plan/değişiklik listesine ilgili test dosyalarını (ör. *Test.php, "
            "tests/) DAHİL ET; yoksa yeni test ekle.\n"
            "- Test eklenm/güncellenmediyse bu eksiklik incelemede CHANGES_REQUIRED sebebidir."
        )

    def _poll_pr_build(self, timeout_s: int, interval_s: int) -> tuple[str, dict | None]:
        """PR build'ini tamamlanana kadar poll et.
        Donus: ("completed", build) | ("no_pipeline", None) | ("timeout", build|None)."""
        import time as _t
        waited = 0
        last = None
        grace = 120  # build tetiklenmesi icin taninan sure (policy gecikmesi)
        while waited < timeout_s:
            try:
                build = self._client.get_pr_build(self.state.repo_name, int(self.state.pr_id))
            except Exception as e:
                _log(f"  Build sorgu hatasi: {e}")
                build = None
            if build is None:
                if waited >= grace:
                    return ("no_pipeline", None)
            else:
                last = build
                if build.get("status") == "completed":
                    return ("completed", build)
                _log(f"  Build {build.get('build_id')} {build.get('status')}… ({waited}s/{timeout_s}s)")
            _t.sleep(interval_s)
            waited += interval_s
        return ("timeout", last)

    def _fix_failing_build(self, failure_summary: str):
        """Build'i kiran testleri/kodu duzelt: plan'daki kaynak dosyalar + hata
        ozetinden cozulen test dosyalari developer'a verilir, push edilir.
        Branch + PR zaten var; push build'i yeniden tetikler."""
        import re as _re_fb
        from agile_sdlc_crew.pipeline import push_file
        from agile_sdlc_crew.tools import claude_cli_llm as _cli
        from agile_sdlc_crew import pipeline_config as _pc_fb

        _log("\n-- BUILD FIX: Testler/kod düzeltiliyor --")
        plan = self.state.plan or {}
        repo_name = self.state.repo_name
        branch = self.state.branch_name
        repo_dir = self._repo_mgr.base_dir / repo_name

        # 1) Plan'daki kaynak dosyalar
        fix_files: list[str] = [
            c.get("file_path") for c in plan.get("changes", []) if c.get("file_path")
        ]
        # 2) Hata ozetinden test sinif adlarini cozumle (ornek: ReturnOrderTest)
        test_classes = set(_re_fb.findall(r'\b([A-Z][A-Za-z0-9_]*Test)\b', failure_summary or ""))
        for cls in list(test_classes)[:5]:
            try:
                import subprocess as _sp
                res = _sp.run(
                    ["grep", "-rl", "--include=*.php", "--include=*.py",
                     "--include=*.js", "--include=*.go", f"class {cls}", str(repo_dir)],
                    capture_output=True, text=True, timeout=8,
                )
                for f in (res.stdout or "").strip().split("\n"):
                    if f and ("vendor/" not in f and "node_modules/" not in f):
                        rel = "/" + f[len(str(repo_dir)):].lstrip("/")
                        if rel not in fix_files:
                            fix_files.append(rel)
            except Exception:
                pass

        if not fix_files:
            _log("  Düzeltilecek dosya çözümlenemedi — atlaniyor")
            return

        # Part B: developer repoyu --add-dir ile görsün (test dosyasini okuyabilsin)
        _repo_tools = False
        try:
            _repo_tools = bool(_pc_fb.get("CREW_CLI_REPO_TOOLS"))
        except Exception:
            pass
        if _repo_tools and repo_dir.exists():
            _cli.set_repo_ctx([str(repo_dir)], "Read,Grep,Glob,LS")
        try:
            for i, file_path in enumerate(fix_files):
                _log(f"  Build-fix implement [{i+1}/{len(fix_files)}]: {file_path}")
                try:
                    existing = self._client.get_file_content(repo_name, file_path, branch)
                except Exception:
                    existing = ""
                ctx = self._build_step_context("implement_change_task")
                ctx += (
                    f"\n\n# PR BUILD TEST HATASI (DÜZELT)\n"
                    f"Aşağıdaki CI build hatası testlerin kırıldığını gösteriyor. Bu dosyadaki "
                    f"kodu/testleri hatayı giderecek şekilde düzelt; mevcut davranışı koru, "
                    f"değişen davranış için test ekle/güncelle.\n```\n{failure_summary[:2500]}\n```"
                )
                code_crew = self._agile_crew.create_code_crew()
                code_result = code_crew.kickoff(inputs={
                    "work_item_id": self.state.work_item_id,
                    "target_repo": repo_name,
                    "target_file": file_path,
                    "change_description": "PR build test hatasını gider (test/kaynak düzelt)",
                    "current_code": "",
                    "new_code": "",
                    "full_content": existing,
                    "previous_context": ctx,
                })
                self._track_and_check_budget(code_result, f"build_fix_{i}")
                new_content = _extract_dev_output(code_result)
                if not new_content or len(new_content.strip()) < 30:
                    _log("    Developer boş/kısa çıktı, atlanıyor")
                    continue
                # Güvenlik: büyük kod kaybı (mevcut implement ile aynı eşik)
                if existing and len(existing.strip()) > 500 and len(new_content.strip()) < len(existing.strip()) * 0.5:
                    _log("    🚨 GÜVENLİK: dosya >%50 küçüldü, build-fix push İPTAL")
                    continue
                push_result = push_file(
                    repo_name, branch, file_path, new_content,
                    f"fix: PR build test hatasi - {file_path.rsplit('/',1)[-1]} (WI #{self.state.work_item_id})",
                    repo_mgr=self._repo_mgr, dry_run=self.state.dry_run,
                )
                _log(f"    {'Push OK' if push_result.get('success') else 'Push HATA: ' + str(push_result.get('error','?'))}: {file_path}")
        finally:
            _cli.clear_repo_ctx()
        _log("  Build-fix tamam — build yeniden tetiklenecek")

    @listen(pr_build_gate)
    def step9_test_planning(self):
        """Adim 9: Test Planlama — pr_build_gate sonrasi PARALEL calisir (UAT ile birlikte).
        DRY-RUN: PR yok, atlanir."""
        from agile_sdlc_crew.main import (
            _extract_code_from_output, _validate_code, _add_wi_comment,
        )
        from agile_sdlc_crew.pipeline import push_file

        _log("\n-- ADIM 9: Test planlama --")

        if self.state.dry_run:
            _log("  🔬 DRY-RUN: test planlama atlandi (PR yok)")
            self._step_start("test_planning_task")
            self.state.test_text = "DRY-RUN: test planning skipped"
            self._step_done("test_planning_task", self.state.test_text)
            return

        # Resume
        cached_test = self._try_resume_step("test_planning_task")
        if cached_test:
            self.state.test_text = cached_test
            self._resume_step("test_planning_task", cached_test)
            return

        self._step_start("test_planning_task")

        if self._hal:
            changed_files = ", ".join(ch["file_path"] for ch in self.state.plan.get("changes", []))
            test_detail = self._hal.followup(
                f"{changed_files} dosyalarindaki degisiklikler icin SADECE yeni test fonksiyonu yaz. "
                f"Mevcut testlere DOKUNMA. Sadece eklenecek yeni test fonksiyonunu goster. "
                f"Test dosya yolunu belirt."
            )
            test_text = test_detail.get("response", "")

            # Test kodunu parse et ve push et
            if test_text and self.state.branch_name:
                test_parsed = self._hal._llm_parse(test_text)
                for tc in test_parsed.get("changes", []):
                    test_path = tc.get("path", "")
                    test_code = tc.get("code", "")
                    if not test_path or not test_code:
                        continue
                    _log(f"  Test push: {test_path}")
                    existing = ""
                    try:
                        existing = self._repo_mgr.get_file_content(
                            self.state.repo_name, test_path, self.state.branch_name,
                        )
                        final_test = existing.rstrip() + "\n\n" + test_code + "\n"
                        _log(f"    Mevcut test dosyasina ekleniyor ({len(test_code)} karakter)")
                    except Exception:
                        final_test = test_code
                        _log(f"    Yeni test dosyasi olusturuluyor")
                    # Dogrulama
                    test_valid, final_test = _validate_code(
                        final_test, test_path, "", "unit test", repo_name=self.state.repo_name
                    )
                    if not test_valid:
                        _log(f"    Test dogrulama basarisiz, duzeltme deneniyor...")
                        code_crew = self._agile_crew.create_code_crew()
                        fix_result = code_crew.kickoff(inputs={
                            "work_item_id": self.state.work_item_id,
                            "target_repo": self.state.repo_name,
                            "target_file": test_path,
                            "change_description": "Test kodu derlenemiyor, duzelt.",
                            "current_code": final_test[:6000],
                            "new_code": final_test[:6000],
                            "start_marker": "",
                            "end_marker": "",
                        })
                        fixed_test = _extract_code_from_output(fix_result.raw or "")
                        if fixed_test.strip():
                            v2, fixed_test = _validate_code(fixed_test, test_path, "", "unit test", repo_name=self.state.repo_name)
                            if v2:
                                final_test = fixed_test
                                _log(f"    Test duzeltme basarili")
                            else:
                                _log(f"    Test duzeltme de basarisiz, atlaniyor")
                                continue
                        else:
                            _log(f"    Developer bos dondurdu, atlaniyor")
                            continue
                    # Guvenlik
                    if existing and len(final_test.strip()) < len(existing.strip()):
                        _log(f"    GUVENLIK: test dosyasi kisaldi ({len(existing)} -> {len(final_test)}), push iptal")
                        continue
                    push_result = push_file(
                        self.state.repo_name, self.state.branch_name, test_path, final_test,
                        f"#{self.state.work_item_id}: unit test eklendi",
                        repo_mgr=self._repo_mgr, dry_run=self.state.dry_run,
                    )
                    if push_result["success"]:
                        _log(f"    Test push {'(dry-run local)' if push_result.get('dry_run') else '#'+str(push_result.get('push_id','?'))}")
                    else:
                        _log(f"    Test push hatasi: {push_result['error']}")
        else:
            ctx = self._build_step_context("test_planning_task")
            test_crew = self._agile_crew.create_test_crew()
            test_result = test_crew.kickoff(inputs={
                "work_item_id": self.state.work_item_id,
                "requirements": self.state.requirements_text[:3000],
                "target_repo": self.state.repo_name,
                "target_branch": self.state.branch_name,
                "pr_id": self.state.pr_id,
                "previous_context": ctx,
                "scrum_master_feedback": "",
            })
            test_text = test_result.raw or ""
            # SM Review
            approved, feedback = self._scrum_review("Test Planlama", test_text)
            if not approved:
                _log("  SM iyilestirme istedi, tekrar calistiriliyor...")
                test_crew = self._agile_crew.create_test_crew()
                test_result = test_crew.kickoff(inputs={
                    "work_item_id": self.state.work_item_id,
                    "requirements": self.state.requirements_text[:3000],
                    "target_repo": self.state.repo_name,
                    "target_branch": self.state.branch_name,
                    "pr_id": self.state.pr_id,
                    "previous_context": ctx,
                    "scrum_master_feedback": f"SCRUM MASTER GERI BILDIRIMI:\n{feedback}",
                })
                test_text = test_result.raw or ""

        self.state.test_text = test_text
        self._step_done("test_planning_task", test_text[:3000])
        _log(f"  Test planlama tamamlandi")
        _add_wi_comment(self._client, self.state.work_item_id,
            f"## Test Planlama\n\n"
            f"{test_text[:2000]}\n\n"
            f"*Agile SDLC Crew - Test*"
        )

    @listen(pr_build_gate)
    def step10_uat(self):
        """Adim 10: UAT Dogrulama — pr_build_gate sonrasi PARALEL calisir (Test ile birlikte).
        DRY-RUN: PR yok, atlanir."""
        from agile_sdlc_crew.main import _add_wi_comment

        _log("\n-- ADIM 10: UAT dogrulama --")

        if self.state.dry_run:
            _log("  🔬 DRY-RUN: UAT atlandi (PR yok)")
            self._step_start("uat_task")
            self.state.uat_text = "DRY-RUN: UAT skipped"
            self._step_done("uat_task", self.state.uat_text)
            return

        # Resume
        cached_uat = self._try_resume_step("uat_task")
        if cached_uat:
            self.state.uat_text = cached_uat
            self._resume_step("uat_task", cached_uat)
            return

        self._step_start("uat_task")

        if self._hal:
            uat_detail = self._hal.followup(
                f"List the acceptance criteria of work item #{self.state.work_item_id} "
                f"and mark each as PASS/FAIL based on whether the changes satisfy it."
            )
            uat_text = uat_detail.get("response", "")
        else:
            ctx = self._build_step_context("uat_task")
            uat_crew = self._agile_crew.create_uat_crew()
            uat_result = uat_crew.kickoff(inputs={
                "work_item_id": self.state.work_item_id,
                "requirements": self.state.requirements_text[:3000],
                "pr_id": self.state.pr_id,
                "pr_url": self.state.pr_url,
                "previous_context": ctx,
                "scrum_master_feedback": "",
            })
            uat_text = uat_result.raw or ""
            # SM Review
            approved, feedback = self._scrum_review("UAT Dogrulama", uat_text)
            if not approved:
                _log("  SM iyilestirme istedi, tekrar calistiriliyor...")
                uat_crew = self._agile_crew.create_uat_crew()
                uat_result = uat_crew.kickoff(inputs={
                    "work_item_id": self.state.work_item_id,
                    "requirements": self.state.requirements_text[:3000],
                    "pr_id": self.state.pr_id,
                    "pr_url": self.state.pr_url,
                    "previous_context": ctx,
                    "scrum_master_feedback": f"SCRUM MASTER GERI BILDIRIMI:\n{feedback}",
                })
                uat_text = uat_result.raw or ""

        self.state.uat_text = uat_text
        self._step_done("uat_task", uat_text[:3000])
        _log(f"  UAT dogrulama tamamlandi")
        _add_wi_comment(self._client, self.state.work_item_id,
            f"## UAT Dogrulama\n\n"
            f"{uat_text[:2000]}\n\n"
            f"*Agile SDLC Crew - UAT*"
        )

    # ── Faz 4: Kapanis ──────────────────────────────

    @listen(and_(step9_test_planning, step10_uat))
    def step11_completion_report(self):
        """Adim 11: Tamamlanma Raporu — Test VE UAT bittikten sonra calisir.
        DRY-RUN: lokal rapor yazar (~/.crew_repos/<repo>/.dry_run_<job_id>.md)
        — WI'ya yorum eklemez."""
        from agile_sdlc_crew.main import _add_wi_comment

        _log("\n-- ADIM 11: Tamamlanma raporu --")
        self._step_start("completion_report_task")

        if self.state.dry_run:
            self._write_dry_run_report()
            return

        if self._hal:
            completion_detail = self._hal.followup(
                f"#{self.state.work_item_id} icin tamamlanma raporu olustur: "
                f"yapilan degisiklikler, kod inceleme sonucu, test durumu ve UAT sonucunu ozetle. "
                f"Bu raporu is kalemine yorum olarak ekle."
            )
            completion_text = completion_detail.get("response", "")
        else:
            ctx = self._build_step_context("completion_report_task")
            completion_crew = self._agile_crew.create_completion_crew()
            completion_result = completion_crew.kickoff(inputs={
                "work_item_id": self.state.work_item_id,
                "pr_url": self.state.pr_url,
                "pr_id": self.state.pr_id,
                "review_result": self.state.review_text[:2000],
                "test_result": self.state.test_text[:2000],
                "uat_result": self.state.uat_text[:2000],
                "previous_context": ctx,
            })
            completion_text = (completion_result.raw or "") if completion_result else ""

        self.state.completion_text = completion_text
        self._step_done("completion_report_task", completion_text[:3000])
        _log(f"  Tamamlanma raporu olusturuldu")
        _add_wi_comment(self._client, self.state.work_item_id,
            f"## Tamamlanma Raporu\n\n"
            f"PR: [#{self.state.pr_id}]({self.state.pr_url})\n\n"
            f"{completion_text[:3000]}\n\n"
            f"---\n*Agile SDLC Crew - Pipeline tamamlandi*"
        )

        # Geçmiş-iş repo indeksine yaz — yalnızca başarılı PR (buraya ulaşmak
        # PR oluştu + review onayladı demek; dry-run bu metodun başında döner).
        from agile_sdlc_crew import pipeline_config as _pc_rh
        if (
            _pc_rh.get("CREW_REPO_HISTORY_SUGGEST")
            and self._vector_store and self.state.repo_name and self.state.pr_id
        ):
            try:
                self._vector_store.index_repo_decision(
                    self.state.work_item_id, self.state.repo_name, self.state.pr_id,
                    self.state.plan, self.state.requirements_text[:2000],
                )
                _log("  📚 Repo kararı geçmiş indekse yazıldı")
            except Exception as e:
                _log(f"  Repo kararı indeksleme hatası: {e}")

        _log(f"\n{'='*60}")
        _log("  PIPELINE TAMAMLANDI!")
        _log(f"  PR #{self.state.pr_id}: {self.state.pr_url}")
        _log(f"{'='*60}")

    def _write_dry_run_report(self):
        """Build a local-only completion summary for dry-run jobs.
        Writes <repo_path>/.dry_run_<job_id>.md with WI summary, plan,
        changed files and `git diff main..<branch>` output."""
        from pathlib import Path
        repo_name = self.state.repo_name
        branch = self.state.branch_name
        job_id = self.state.job_id or 0

        repo_dir = self._repo_mgr.base_dir / repo_name
        report_path = repo_dir / f".dry_run_{job_id}.md"

        # Collect diff
        try:
            diff = self._repo_mgr.get_diff(repo_name, branch, base="main")
        except Exception as e:
            diff = f"(diff fetch failed: {e})"
        # Cap diff size in the report
        diff_capped = diff[:50_000] + ("\n\n...(diff truncated)" if len(diff) > 50_000 else "")

        # Plan summary
        plan = self.state.plan or {}
        changes = plan.get("changes", [])
        plan_summary = []
        for ch in changes:
            plan_summary.append(
                f"- **[{ch.get('change_type','edit')}]** `{ch.get('file_path','?')}` — "
                f"{ch.get('description','')[:200]}"
            )

        # Pushed files
        pushed = self.state.all_pushes or []
        pushed_list = "\n".join(
            f"- `{p.get('file','?')}` ({p.get('change_type','?')})"
            for p in pushed
        ) or "_(no files committed locally)_"

        # Acceptance criteria
        acs = self.state.acceptance_criteria or []
        ac_list = "\n".join(f"- {a}" for a in acs) or "_(none)_"

        report = (
            f"# Dry-Run Report — WI #{self.state.work_item_id}\n\n"
            f"**Job ID:** {job_id}\n"
            f"**Repo:** {repo_name}\n"
            f"**Local Branch:** `{branch}`\n"
            f"**Repo Path:** `{repo_dir}`\n\n"
            f"## Requirements Summary\n\n"
            f"{(self.state.requirements_text or '_(empty)_')[:3000]}\n\n"
            f"## Acceptance Criteria\n\n"
            f"{ac_list}\n\n"
            f"## Technical Plan\n\n"
            f"**Repo:** {plan.get('repo_name', repo_name)}\n"
            f"**Summary:** {plan.get('summary', '_(empty)_')}\n\n"
            f"**Planned Changes ({len(changes)}):**\n"
            f"{chr(10).join(plan_summary) if plan_summary else '_(none)_'}\n\n"
            f"## Locally Committed Files ({len(pushed)})\n\n"
            f"{pushed_list}\n\n"
            f"## How to Review Locally\n\n"
            f"```bash\n"
            f"cd {repo_dir}\n"
            f"git log --oneline main..{branch}\n"
            f"git diff main..{branch}\n"
            f"# Or open the branch in your editor:\n"
            f"git checkout {branch}\n"
            f"```\n\n"
            f"## Diff (main..{branch})\n\n"
            f"```diff\n{diff_capped}\n```\n"
        )
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(report, encoding="utf-8")
            _log(f"  🔬 Dry-run raporu yazildi: {report_path}")
        except Exception as e:
            _log(f"  Dry-run rapor yazma hatasi: {e}")
            report_path = None

        # State + step done
        completion_text = (
            f"DRY-RUN tamamlandi. {len(pushed)} dosya lokal commit edildi.\n"
            f"Branch: {branch}\n"
            f"Repo: {repo_dir}\n"
            f"Rapor: {report_path or '(yazilamadi)'}"
        )
        self.state.completion_text = completion_text
        self._step_done("completion_report_task", completion_text)
        _log(f"\n{'='*60}")
        _log("  🔬 DRY-RUN PIPELINE TAMAMLANDI")
        _log(f"  Repo:   {repo_dir}")
        _log(f"  Branch: {branch}")
        if report_path:
            _log(f"  Rapor:  {report_path}")
        _log(f"  Inceleme:")
        _log(f"    cd {repo_dir}")
        _log(f"    git log --oneline main..{branch}")
        _log(f"    git diff main..{branch}")
        _log(f"{'='*60}")
