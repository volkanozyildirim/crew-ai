"""Agile SDLC Crew - CrewAI Flow ile 11 adimli pipeline orkestrasyonu.

run_pipeline() icindeki monolitik kontrol akisini event-driven Flow yapisina
donusturur. State yonetimi, HAL/CrewAI dallanmasi ve quality gate'ler
deklaratif olarak tanimlanir.
"""

import logging
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr

from crewai.flow import Flow, and_, listen, or_, router, start

log = logging.getLogger("pipeline")


def _log(msg: str):
    log.info(msg)


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


# ── State Model ──────────────────────────────────────

class PipelineState(BaseModel):
    """Flow boyunca tasınan state. Her adim state'i gunceller."""
    work_item_id: str = ""
    use_hal: bool = False
    job_id: int | None = None
    previous_context: str = ""
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

    def _append_context(self, step_name: str, output: str):
        """Ham previous_context string'ine step ciktisini ekler.
        Step-bazli optimize context icin _build_step_context() kullanin."""
        summary = (output or "")[:5000]  # 1500 → 5000
        self.state.previous_context += f"\n\n--- {step_name} ---\n{summary}"

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
                    f"{s.kickoff_text[:4000]}\n"
                    f"⚠️ Teknik plan 'Kritik Risk Tablosu'ndaki TUM riskler ve "
                    f"'Edge Case'ler' icin somut kod degisiklikleri icermeli."
                )
            elif step_key in ("test_planning_task",):
                parts.append(
                    f"\n# Kickoff Design Review — Test Perspektifi\n"
                    f"{s.kickoff_text[:2500]}"
                )
            elif step_key in ("uat_task",):
                parts.append(
                    f"\n# Kickoff Design Review — Backlog Adaylari ve Kabul Kriterleri\n"
                    f"{s.kickoff_text[:2500]}"
                )
            elif step_key in ("review_pr_task",):
                # Reviewer: risk tablosunu bilerek PR'i incelesin
                parts.append(
                    f"\n# Kickoff Design Review — Kritik Riskler\n"
                    f"{s.kickoff_text[:2000]}"
                )

        # Requirements (step 1 sonrasi — kickoff dahil, artik requirements once calisiyor)
        if s.requirements_text and step_key != "requirements_analysis_task":
            parts.append(f"\n# Is Analizi (Gereksinimler)\n{s.requirements_text[:3000]}")

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
                f"\n# Kabul Kriterleri (Pipeline Boyunca Bagleyici — BA Tarafindan Belirlendi)\n"
                f"{criteria_text}\n"
                f"⚠️ Her adim bu kriterlere gore yapilmalidir:\n"
                f"- Teknik Tasarim: plan her kriteri karsimali\n"
                f"- Gelistirme: kod her kriteri uygulamali\n"
                f"- Kod Inceleme: her kriter karsilanmis mi kontrol et\n"
                f"- UAT: her kriteri GECTI/KALDI olarak degerlendirr"
            )

        # Plan (step 4 sonrasi)
        if s.plan and step_key not in ("requirements_analysis_task", "technical_design_task"):
            changes_summary = []
            for ch in s.plan.get("changes", [])[:10]:
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
                parts.append(f"\n# Kod Inceleme\n{s.review_text[:2500]}")
            if s.test_text:
                parts.append(f"\n# Test Planlama\n{s.test_text[:2500]}")
            if s.uat_text:
                parts.append(f"\n# UAT Dogrulama\n{s.uat_text[:2500]}")

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
                        f"- WI#{x['work_item_id']} ({x['step']}): {x['content'][:200]}"
                        for x in rel
                    )
                    parts.append(f"\n# Benzer Onceki Isler\n{sim_text}")
            except Exception:
                pass

        return "\n".join(parts)

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
        CREW_ENABLE_RESUME=1 ile aktif edilir (default: aktif)."""
        import os as _os_resume
        if _os_resume.environ.get("CREW_ENABLE_RESUME", "1") == "0":
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
        if self._vector_store and output and len(output.strip()) > 20:
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
        price_in = float(_os.environ.get("CREW_PRICE_INPUT_USD_PER_M", "3.0"))
        price_out = float(_os.environ.get("CREW_PRICE_OUTPUT_USD_PER_M", "15.0"))
        cost = (
            self._job_prompt_tokens * price_in + self._job_completion_tokens * price_out
        ) / 1_000_000.0

        max_cost = float(_os.environ.get("CREW_MAX_JOB_COST", "5.0"))
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
        import os
        if not os.environ.get("CREW_SM_REVIEW"):
            return True, ""
        try:
            review_crew = self._agile_crew.create_scrum_review_crew()
            result = review_crew.kickoff(inputs={
                "step_name": step_name,
                "step_output": (output or "")[:4000],
                "work_item_id": self.state.work_item_id,
            })
            raw = result.raw or ""
            rejected = "IYILESTIR" in raw.upper() or "İYİLEŞTİR" in raw.upper()
            _log(f"  SM Review ({step_name}): {'IYILESTIR' if rejected else 'ONAY'}")
            return (not rejected), raw
        except Exception as e:
            _log(f"  SM Review hatasi: {e}")
            return True, ""

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

            push_result = push_file(
                repo_name, branch, file_path, new_content,
                f"fix: review feedback - {change.get('description', '')[:60]} (WI #{self.state.work_item_id})",
            )
            if push_result.get("success"):
                _log(f"    Push OK: {file_path}")
            else:
                _log(f"    Push HATA: {push_result.get('error', '?')}")

        _log("  Review retry: dosyalar guncellendi, tekrar review yapiliyor")

        # Tekrar review
        ctx = self._build_step_context("review_pr_task")
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
        self._append_context("Kod Inceleme (Retry)", review_text)

        # Tekrar kontrol — hâlâ red mi?
        review_upper = review_text.upper()
        still_rejected = any(marker in review_upper for marker in [
            "DEGISIKLIK GEREKLI", "DEĞİŞİKLİK GEREKLİ",
            "REJECTED", "REDDEDILDI", "REDDEDİLDİ",
            "KARAR: RED", "KARAR:RED",
        ])
        if still_rejected:
            # step8_code_review'daki max retry kontrolune don
            self._append_context("Reviewer Geri Bildirimi (Tekrar Red)", review_text[:2000])
            review_attempt = getattr(self, "_review_attempt", 0)
            import os as _os_rev2
            max_review_retries = int(_os_rev2.environ.get("CREW_REVIEW_MAX_RETRIES", "2"))
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

    @start()
    def initialize(self):
        """Pipeline baslangici: client'lar olustur, tracker'i baslat."""
        from agile_sdlc_crew.crew import AgileSDLCCrew
        from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient
        from agile_sdlc_crew.tools.local_repo import LocalRepoManager
        from agile_sdlc_crew.tools.vector_store import VectorStore
        from agile_sdlc_crew.tools.tool_cache import reset_tool_cache
        from agile_sdlc_crew import db as _db

        # Pipeline basi: tool cache'i sifirla
        reset_tool_cache()

        self._db = _db
        self._agile_crew = AgileSDLCCrew()
        self._agile_crew.set_status_tracker(self._tracker)
        self._client = AzureDevOpsClient()
        self._vector_store = VectorStore()
        self._repo_mgr = LocalRepoManager()
        self._repo_mgr.vector_store = self._vector_store

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

        # Tum REPO_SUMMARY.md'leri vector DB'ye embed et
        # (Agent 'hangi repo' sorusuna semantic arama ile cevap bulabilsin)
        # Sirayla embed et — Ollama'ya paralel istek gitmiyor ama model swap
        # sirasinda 500 verebilir. 0.1s araliklarla gondererek stabilize et.
        import time as _embed_time
        _log("  REPO_SUMMARY.md'ler vector DB'ye embed ediliyor...")
        indexed = 0
        for name in self.state.known_repos:
            try:
                repo_dir = self._repo_mgr.base_dir / name
                if (repo_dir / "REPO_SUMMARY.md").exists():
                    self._vector_store.index_repo_summary(name, repo_dir)
                    indexed += 1
                    _embed_time.sleep(0.1)  # Ollama throttle
            except Exception as e:
                _log(f"  Summary index hatasi ({name}): {e}")
        _log(f"  {indexed}/{len(self.state.known_repos)} repo summary embed edildi")

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
            self._append_context("Is Analizi", cached_ba[:5000])
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
                    pr_match = _re_pr.search(r'PullRequestId/[^%]+%2f([^%]+)%2f(\d+)', url)
                    if pr_match:
                        _pr_links.append({
                            "repo_id": pr_match.group(1),
                            "pr_id": int(pr_match.group(2)),
                        })
            if _pr_links:
                _log(f"  WI relations'da {len(_pr_links)} PR baglantisi bulundu")
                # En son PR'i al (en buyuk PR ID)
                _pr_links.sort(key=lambda x: x["pr_id"], reverse=True)
                latest_pr = _pr_links[0]

                # Repo ID'den repo adini bul
                _pr_repo_name = ""
                for rname in self.state.known_repos:
                    try:
                        existing_pr = self._client.get_pull_request(rname, latest_pr["pr_id"])
                        _pr_repo_name = rname
                        break
                    except Exception:
                        continue

                if _pr_repo_name:
                    pr_id = latest_pr["pr_id"]
                    _pr_id_for_threads = pr_id
                    _pr_repo_for_threads = _pr_repo_name
                    _log(f"  Mevcut PR bulundu (WI relations): #{pr_id} ({_pr_repo_name})")
                    existing_pr = {"pr_id": pr_id}  # asagidaki thread okuma blogu icin
                else:
                    existing_pr = None
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
        if _os.environ.get("CREW_ANALYZE_WI_MEDIA", "1") != "0":
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

        # 🚨 YETERSIZLIK KONTROLU — Python-first
        # Artik agent'a "YETERSIZ de" diye sormuyoruz (kucuk LLM'ler prompt'taki
        # keyword'u kopyaliyor → yanlis karar). Sadece Python icerik uzunluguna bakar.
        MIN_CONTENT_CHARS = int(_os.environ.get("CREW_MIN_WI_CONTENT_CHARS", "100"))
        if wi_content_length < MIN_CONTENT_CHARS:
            missing = (
                f"Is kaleminde yeterli bilgi yok (yalnizca {wi_content_length} karakter icerik). "
                f"Baslik + aciklama + kabul kriterleri toplamda en az {MIN_CONTENT_CHARS} karakter olmali."
            )
            _log(f"  🚨 IS KALEMI YETERSIZ: {missing}")
            _add_wi_comment(self._client, self.state.work_item_id,
                f"## ⚠️ Is Kalemi Yetersiz — Gelistirme Baslatilamadi\n\n"
                f"Is kaleminin aciklamasi cok kisa ({wi_content_length} karakter). "
                f"Otomatik gelistirme icin en az {MIN_CONTENT_CHARS} karakter iceriginiz olmali.\n\n"
                f"Lutfen is kaleminde asagidaki bilgileri netlestirin:\n"
                f"- Aciklama: ne yapilacak, neden gerekli\n"
                f"- Kabul kriterleri: basarili sayilmasi icin hangi sartlar saglanmali\n"
                f"- Teknik detay: hangi repo/modul/dosya etkilenir, ornek/referans var mi\n\n"
                f"Bilgiler eklendikten sonra is kalemini tekrar kuyruga ekleyebilirsiniz.\n\n"
                f"---\n*Agile SDLC Crew - Yetersizlik Kontrolu*"
            )
            self._step_fail("requirements_analysis_task", f"YETERSIZ: {missing}")
            raise RuntimeError(f"Is kalemi gelistirme icin yetersiz: {missing}")

        # Geriye donuk uyumluluk: eski prompt/agent "YETERSIZ: ..." cikartabilir —
        # Python zaten yeterli buldu, bu keyword'u sessizce temizle
        if _re.search(r'YETERSIZ\s*:', requirements_text[:500], _re.IGNORECASE):
            _log(f"  ℹ️  Agent ciktisinda YETERSIZ keyword'u vardi, temizleniyor (icerik yeterli: {wi_content_length} char)")
            requirements_text = _re.sub(
                r'YETERSIZ\s*:\s*[^\n]*\n?', '', requirements_text, flags=_re.IGNORECASE
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

        # BA JSON varsa yapisal context ekle, yoksa serbest metin
        if ba_json:
            ba_context = _json_ba.dumps(ba_json, ensure_ascii=False, indent=2)
            self._append_context("Is Analizi (JSON)", ba_context[:5000])
        else:
            self._append_context("Is Analizi", requirements_text)
        self._step_done("requirements_analysis_task", requirements_text[:3000])
        _log(f"  Is analizi tamamlandi")

    @listen(crew_step1_requirements)
    def step0_kickoff_meeting(self):
        """Kickoff toplantisi — requirements'tan SONRA calisir.
        Artik is analizi, kabul kriterleri ve hedef repo biliniyor.
        Agentlar bilgiye dayali teknik tartisma yapabilir.
        CREW_KICKOFF_MEETING=0 ile devre disi birakilabilir (default: aktif)."""
        import os as _os
        from agile_sdlc_crew.main import _add_wi_comment

        if _os.environ.get("CREW_KICKOFF_MEETING", "1") == "0":
            _log("  Kickoff toplantisi devre disi (CREW_KICKOFF_MEETING=0)")
            self._step_done("kickoff_meeting_task", "Devre dışı (CREW_KICKOFF_MEETING=0)")
            return

        # Resume: onceki job'dan kickoff ciktisi varsa atla
        cached_kickoff = self._try_resume_step("kickoff_meeting_task")
        if cached_kickoff:
            self.state.kickoff_text = cached_kickoff
            self._append_context("Kickoff Toplantisi", cached_kickoff[:3000])
            self._resume_step("kickoff_meeting_task", cached_kickoff)
            return

        _log("\n-- KICKOFF TOPLANTISI (is analizi sonrasi) --")
        self._step_start("kickoff_meeting_task")

        # Kickoff context'i: requirements + acceptance criteria + repo bilgisi dahil
        ctx = self._build_step_context("kickoff_meeting_task")

        # Hedef repo tahmini — 3 katman:
        # 1. Repo adi eslesmesi: WI metnindeki kelimeler repo adlarinda geciyorsa
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

            # Katman 1: Repo adi eslesmesi (en hizli, en guvenilir)
            # "HAL uzerinde..." → project-hal, "webservice'te..." → webservice
            def _repo_name_score(rname: str) -> int:
                parts = _re_ko.split(r'[-_]', rname.lower())
                return sum(1 for p in parts if len(p) > 2 and p in wi_text_ko)
            best_name = max(self.state.known_repos, key=_repo_name_score, default="")
            if best_name and _repo_name_score(best_name) > 0:
                kickoff_repo = best_name
                _log(f"  Kickoff hedef repo (isim eslesmesi): {kickoff_repo} (score={_repo_name_score(best_name)})")

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
            _log(f"  Kickoff baslatiyor: 4 task, repo={kickoff_repo}")
            _kickoff_t0 = _kt.time()
            kickoff_crew = self._agile_crew.create_kickoff_crew()
            kickoff_result = kickoff_crew.kickoff(inputs={
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
        self._append_context("Kickoff Toplantisi", kickoff_text[:3000])
        self._step_done("kickoff_meeting_task", kickoff_text[:3000])
        _log("  Kickoff toplantisi tamamlandi")

    @listen(step0_kickoff_meeting)
    def crew_step4_technical_design(self):
        """Adim 2-3 atlanir, repo ve bagimlilik bilgisi context'e eklenir.
        Teknik tasarim agent'i work item + repo summary ile calisir."""
        from agile_sdlc_crew.main import _parse_architect_output, _resolve_repo_name

        # Step 2-3: Agent'a 67 repo browse ettirmek yerine repo listesini
        # ve REPO_SUMMARY.md'leri context'e ekle
        skip_steps = ["discover_repos_task", "dependency_analysis_task"]
        for sk in skip_steps:
            self._step_done(sk, "Atlandı — repo bilgisi local'den alınıyor")

        # Repo listesini context'e ekle
        repo_list = ", ".join(self.state.known_repos)
        self._append_context("Bilinen Repolar", repo_list)

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
        if summaries:
            self._append_context("Repo Ozetleri", "\n---\n".join(summaries[:15]))
            _log(f"  Repo ozet sirasi (ilk 5): {summaries_repos[:5]}")

        # Benzer onceki isleri bul ve context'e ekle
        if self._vector_store:
            try:
                similar = self._vector_store.find_similar_jobs(
                    f"WI#{self.state.work_item_id}: {self.state.requirements_text[:500]}",
                    limit=3,
                )
                if similar:
                    sim_text = "\n".join(
                        f"- WI#{s['work_item_id']} ({s['step']}): {s['content'][:200]}"
                        for s in similar if s.get("work_item_id") != self.state.work_item_id
                    )
                    if sim_text:
                        self._append_context("Benzer Onceki Isler", sim_text)
            except Exception:
                pass

        _log("\n-- ADIM 4: Teknik tasarim --")
        self._step_start("technical_design_task")

        # Cache: ayni WI icin onceki completed job'dan plan var mi?
        cached = self._db.get_cached_step_output(
            "technical_design_task", self.state.work_item_id,
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
                self._append_context("Teknik Tasarim", cached[:1500])
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
        # 1. Repo adi eslesmesi (WI metnindeki kelimeler repo adinda geciyorsa)
        # 2. Kod grep (teknik terimler)
        # 3. Vector semantic search (fallback)
        prefetch_repo = ""
        relevant = []

        # Katman 1: Repo adi eslesmesi
        import re as _re_rn
        wi_text_for_repo = f"{wi_title} {wi_desc_clean} {self.state.requirements_text[:500]}".lower()
        def _repo_name_score_td(rname: str) -> int:
            parts = _re_rn.split(r'[-_]', rname.lower())
            return sum(1 for p in parts if len(p) > 2 and p in wi_text_for_repo)
        best_name_td = max(self.state.known_repos, key=_repo_name_score_td, default="")
        if best_name_td and _repo_name_score_td(best_name_td) > 0:
            prefetch_repo = best_name_td
            _log(f"  Repo adi eslesmesi: {prefetch_repo} (score={_repo_name_score_td(best_name_td)})")

        # grep_matched_files: repo tespitinde bulunan dosya yollari — pre-fetch'te okunur
        grep_matched_files: list[str] = []

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

        analysis_crew = self._agile_crew.create_analysis_crew()
        analysis_result = analysis_crew.kickoff(inputs={
            "work_item_id": self.state.work_item_id,
            "target_repo": prefetch_repo or "",
            "previous_context": ctx,
            "scrum_master_feedback": ctx_hint,
        })
        self._track_and_check_budget(analysis_result, "technical_design_task")
        raw_output = analysis_result.raw or ""

        # Parse hatasi — onceki ciktiyi context'e ekleyip tekrar dene
        try:
            plan = _parse_architect_output(raw_output)
        except ValueError as e:
            _log(f"  Parse hatasi ({e}), ayni architect ile retry")
            retry_ctx = ctx + (
                f"\n\n# ONCEKI DENEME CIKTISI\n"
                f"(Asagidaki bilgilerden JSON uret — tool cagirma, direkt JSON yaz)\n\n"
                f"{raw_output[:6000]}"
            )
            analysis_result = analysis_crew.kickoff(inputs={
                "work_item_id": self.state.work_item_id,
                "target_repo": prefetch_repo or "",
                "previous_context": retry_ctx,
                "scrum_master_feedback": (
                    "⚠️ Onceki denemende JSON parse edilemedi. "
                    "Simdi elindeki TUM bilgilerle SADECE JSON plan uret. "
                    "Tool cagirma, aciklama yazma — SADE JSON."
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

        self.state.requirements_text = self.state.requirements_text or raw_output

        repo_name = plan["repo_name"]
        if repo_name not in self.state.known_repos:
            repo_name = _resolve_repo_name(
                repo_name, self.state.known_repos, self._client, self.state.work_item_id,
            )
            plan["repo_name"] = repo_name

        self.state.repo_name = repo_name
        self.state.plan = plan
        self._append_context("Teknik Tasarim", raw_output[:1500])
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
                # onceki job'dan kalan local feature branch'i sil
                del_result = self._repo_mgr._git(
                    ["branch", "-D", branch_name_for_cleanup], cwd=repo_dir
                )
                if del_result.returncode == 0:
                    _log(f"  Eski local branch silindi: {branch_name_for_cleanup}")
            self._repo_mgr.checkout(repo_name, "main")
            # Repo summary'yi context'e ekle — sonraki adimlar yapiyi bilir
            repo_summary = self._repo_mgr.get_repo_summary(repo_name)
            if repo_summary:
                self._append_context("Repo Yapisi", repo_summary)

            # Kod embedding ATLANYOR — developer tool kullanmiyor (plan + pre-fetch
            # context yeterli), QA/review aşamasında gerekirse orada yapilir.
            # Onceki implementasyon Ollama 500 retry'lariyla pipeline'i dakikalarca
            # bloke ediyordu.
            self._step_start("code_embedding_task")
            self._step_done("code_embedding_task", "Atlandı — developer context ile çalışıyor")
        except Exception as e:
            _log(f"  Local repo checkout hatasi: {e}")

        _log("\n-- ADIM 5: Branch olusturuluyor --")
        self._step_start("create_branch_task")

        branch_result = create_branch(repo_name, self.state.work_item_id)
        if not branch_result["success"]:
            raise RuntimeError(f"Branch olusturulamadi: {branch_result['error']}")

        self.state.branch_name = branch_result["branch"]
        _log(f"  Branch: {self.state.branch_name}")
        if branch_result.get("note"):
            _log(f"    ({branch_result['note']})")

        self._append_context("Branch Olusturma", f"Branch: {self.state.branch_name}, Repo: {repo_name}")
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
            import os as _os_dc
            budget = int(_os_dc.environ.get("CREW_DEV_CONTEXT_BUDGET", "12000"))
            per_file = int(_os_dc.environ.get("CREW_DEV_CONTEXT_PER_FILE", "2000"))
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

            # Skip: branch'te bu dosya zaten push edilmisse ve icerik plan ile uyumluysa atla
            try:
                branch_content = self._client.get_file_content(repo_name, file_path, branch_name)
                if branch_content and new_code:
                    # Plan'daki new_code branch'teki dosyada varsa zaten push edilmis
                    new_code_stripped = new_code.strip()[:200]
                    if new_code_stripped and new_code_stripped in branch_content:
                        _log(f"    ⏩ Branch'te zaten mevcut, atlanıyor")
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
            push_result = push_file(repo_name, branch_name, file_path, final_content, commit_msg, repo_mgr=self._repo_mgr)
            if push_result["success"]:
                _log(f"    Push #{push_result['push_id']} ({push_result['change_type']})")
                all_pushes.append(push_result)
                # Sonraki dosyalar bu dosyanin kodunu referans alabilsin
                implemented_codes[file_path] = final_content[:3000]
            else:
                _log(f"    Push hatasi: {push_result['error']}")

        self.state.all_pushes = all_pushes
        push_summary = ", ".join(p.get("file", "") for p in all_pushes)
        self._append_context("Kod Yazma & Push", f"{len(all_pushes)} dosya: {push_summary}")
        self._step_done("implement_change_task", f"{len(all_pushes)} dosya push edildi")

    @listen(step6_implement_code)
    def step7_create_pr(self):
        """Adim 7: PR Olustur — plan-push eslesmesi kontrolu ile."""
        from agile_sdlc_crew.main import _get_work_item_title, _add_wi_comment
        from agile_sdlc_crew.pipeline import create_pull_request

        _log("\n-- ADIM 7: PR olusturuluyor --")
        self._step_start("create_pr_task")

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

        pr_result = create_pull_request(
            self.state.repo_name, self.state.branch_name,
            self.state.work_item_id, pr_title, pr_desc,
        )
        if not pr_result["success"]:
            raise RuntimeError(f"PR olusturulamadi: {pr_result['error']}")

        self.state.pr_id = str(pr_result["pr_id"])
        self.state.pr_url = pr_result["url"]
        _log(f"  PR #{self.state.pr_id}: {self.state.pr_url}")
        self._append_context("PR Olusturma", f"PR #{self.state.pr_id}: {self.state.pr_url}")
        self._step_done("create_pr_task", f"PR #{self.state.pr_id}: {self.state.pr_url}")
        if self.state.job_id:
            self._db.update_job(self.state.job_id, pr_id=self.state.pr_id, pr_url=self.state.pr_url)

    # ── Faz 3: Dogrulama ────────────────────────────

    @listen(step7_create_pr)
    def step8_code_review(self):
        """Adim 8: PR Yorumlarini Yanitla + Kod Inceleme."""
        from agile_sdlc_crew.main import _add_wi_comment

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
        self._append_context("Kod Inceleme", review_text)

        # 🚨 REVIEWER KARARINA SAYGI — DEGISIKLIK GEREKLI / REJECTED ise
        # tekrar gelistirme dongusune gir (max CREW_REVIEW_MAX_RETRIES, default 2)
        import os as _os_rev
        review_upper = review_text.upper()
        rejected = any(marker in review_upper for marker in [
            "DEGISIKLIK GEREKLI", "DEĞİŞİKLİK GEREKLİ",
            "REJECTED", "REDDEDILDI", "REDDEDİLDİ",
            "KARAR: RED", "KARAR:RED",
        ])
        max_review_retries = int(_os_rev.environ.get("CREW_REVIEW_MAX_RETRIES", "2"))
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
            # Reviewer feedback'ini state'e ekle — teknik tasarim + implement bunu gorsun
            self._append_context("Reviewer Geri Bildirimi (Duzeltme Talebi)", review_text[:2000])
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
    def step9_test_planning(self):
        """Adim 9: Test Planlama — code_review sonrasi PARALEL calisir (UAT ile birlikte)."""
        from agile_sdlc_crew.main import (
            _extract_code_from_output, _validate_code, _add_wi_comment,
        )
        from agile_sdlc_crew.pipeline import push_file

        _log("\n-- ADIM 9: Test planlama --")

        # Resume
        cached_test = self._try_resume_step("test_planning_task")
        if cached_test:
            self.state.test_text = cached_test
            self._append_context("Test Planlama", cached_test)
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
                        repo_mgr=self._repo_mgr,
                    )
                    if push_result["success"]:
                        _log(f"    Test push #{push_result['push_id']}")
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
        self._append_context("Test Planlama", test_text)
        self._step_done("test_planning_task", test_text[:3000])
        _log(f"  Test planlama tamamlandi")
        _add_wi_comment(self._client, self.state.work_item_id,
            f"## Test Planlama\n\n"
            f"{test_text[:2000]}\n\n"
            f"*Agile SDLC Crew - Test*"
        )

    @listen(step8_code_review)
    def step10_uat(self):
        """Adim 10: UAT Dogrulama — code_review sonrasi PARALEL calisir (Test ile birlikte)."""
        from agile_sdlc_crew.main import _add_wi_comment

        _log("\n-- ADIM 10: UAT dogrulama --")

        # Resume
        cached_uat = self._try_resume_step("uat_task")
        if cached_uat:
            self.state.uat_text = cached_uat
            self._append_context("UAT Dogrulama", cached_uat)
            self._resume_step("uat_task", cached_uat)
            return

        self._step_start("uat_task")

        if self._hal:
            uat_detail = self._hal.followup(
                f"#{self.state.work_item_id} is kaleminin kabul kriterlerini listele "
                f"ve yapilan degisikliklerin her kriteri karsilayip karsilamadigini GECTI/KALDI olarak belirt."
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
        self._append_context("UAT Dogrulama", uat_text)
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
        """Adim 11: Tamamlanma Raporu — Test VE UAT bittikten sonra calisir."""
        from agile_sdlc_crew.main import _add_wi_comment

        _log("\n-- ADIM 11: Tamamlanma raporu --")
        self._step_start("completion_report_task")

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
        self._append_context("Tamamlanma Raporu", completion_text)
        self._step_done("completion_report_task", completion_text[:3000])
        _log(f"  Tamamlanma raporu olusturuldu")
        _add_wi_comment(self._client, self.state.work_item_id,
            f"## Tamamlanma Raporu\n\n"
            f"PR: [#{self.state.pr_id}]({self.state.pr_url})\n\n"
            f"{completion_text[:3000]}\n\n"
            f"---\n*Agile SDLC Crew - Pipeline tamamlandi*"
        )

        _log(f"\n{'='*60}")
        _log("  PIPELINE TAMAMLANDI!")
        _log(f"  PR #{self.state.pr_id}: {self.state.pr_url}")
        _log(f"{'='*60}")
