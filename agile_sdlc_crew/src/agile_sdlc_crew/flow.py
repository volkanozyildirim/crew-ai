"""Agile SDLC Crew - CrewAI Flow ile 11 adimli pipeline orkestrasyonu.

run_pipeline() icindeki monolitik kontrol akisini event-driven Flow yapisina
donusturur. State yonetimi, HAL/CrewAI dallanmasi ve quality gate'ler
deklaratif olarak tanimlanir.
"""

import logging
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr

from crewai.flow import Flow, listen, or_, router, start

log = logging.getLogger("pipeline")


def _log(msg: str):
    log.info(msg)


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
    review_text: str = ""
    test_text: str = ""
    uat_text: str = ""
    completion_text: str = ""


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

        # Requirements (step 1 sonrasi)
        if s.requirements_text and step_key != "requirements_analysis_task":
            parts.append(f"\n# Is Analizi (Gereksinimler)\n{s.requirements_text[:3000]}")

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
        _log("  REPO_SUMMARY.md'ler vector DB'ye embed ediliyor...")
        indexed = 0
        for name in self.state.known_repos:
            try:
                repo_dir = self._repo_mgr.base_dir / name
                if (repo_dir / "REPO_SUMMARY.md").exists():
                    self._vector_store.index_repo_summary(name, repo_dir)
                    indexed += 1
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

    @listen("crew_planning")
    def crew_step1_requirements(self):
        """Adim 1: Is Analizi."""
        _log("\n-- ADIM 1: Is analizi --")
        self._step_start("requirements_analysis_task")

        ctx = self._build_step_context("requirements_analysis_task")
        req_crew = self._agile_crew.create_requirements_crew()
        req_result = req_crew.kickoff(inputs={
            "work_item_id": self.state.work_item_id,
            "previous_context": ctx,
            "scrum_master_feedback": "",
        })
        requirements_text = req_result.raw or ""

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
        self._append_context("Is Analizi", requirements_text)
        self._step_done("requirements_analysis_task", requirements_text[:3000])
        _log(f"  Is analizi tamamlandi")

    @listen(crew_step1_requirements)
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

        # Repo summary'lerini context'e ekle (agent hangi repoda ne var bilsin)
        summaries = []
        for rname in self.state.known_repos:
            s = self._repo_mgr.get_repo_summary(rname)
            if s:
                # Sadece framework ve onemli dosyalar — dizin agacini dahil etme
                short = []
                for line in s.split("\n"):
                    if line.startswith("## Dizin"):
                        break
                    short.append(line)
                summaries.append("\n".join(short).strip())
        if summaries:
            self._append_context("Repo Ozetleri", "\n---\n".join(summaries[:15]))

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
        if cached and ("{" in cached and "changes" in cached):
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
                self._step_done("technical_design_task", cached[:3000])
                _log(f"  Onceki job'dan plan kullanildi: {len(plan['changes'])} dosya, repo={repo_name}")
                return
            except (ValueError, KeyError) as e:
                _log(f"  Cache plan parse edilemedi ({e}), agent calistirilacak")

        ctx = self._build_step_context("technical_design_task")

        # Python tarafinda on-hazirlik: WI detayi + relevant repo bilgisi
        # Agent'in tool kullanmasina gerek kalmadan context'te hazir olsun
        try:
            wi = self._client.get_work_item(int(self.state.work_item_id))
            wi_title = wi.get("fields", {}).get("System.Title", "")
            wi_desc = wi.get("fields", {}).get("System.Description", "")
            wi_criteria = wi.get("fields", {}).get("Microsoft.VSTS.Common.AcceptanceCriteria", "")
            # HTML temizle
            import re as _re
            for field in (wi_desc, wi_criteria):
                pass
            wi_desc_clean = _re.sub(r'<[^>]+>', ' ', wi_desc or "").strip()[:3000]
            wi_criteria_clean = _re.sub(r'<[^>]+>', ' ', wi_criteria or "").strip()[:1500]
            ctx += (
                f"\n\n# WORK ITEM DETAYI (on-hazirlik)\n"
                f"## Baslik\n{wi_title}\n\n"
                f"## Aciklama\n{wi_desc_clean}\n\n"
                f"## Kabul Kriterleri\n{wi_criteria_clean}\n"
            )
        except Exception as e:
            _log(f"  WI on-hazirlik hatasi: {e}")

        # Vector search: hangi repoda calisilacagini on-tahmin et
        if self._vector_store:
            try:
                wi_query = f"{wi_title} {wi_desc_clean[:500]}" if 'wi_title' in dir() else self.state.requirements_text[:500]
                relevant = self._vector_store.find_relevant_repos(wi_query, limit=5)
                if relevant:
                    rel_text = "\n".join(
                        f"- {r['repo']} (score: {r['score']})" for r in relevant
                    )
                    ctx += f"\n\n# ONERILEN REPOLAR (en uygun 5)\n{rel_text}\n"
            except Exception:
                pass

        analysis_crew = self._agile_crew.create_analysis_crew()
        analysis_result = analysis_crew.kickoff(inputs={
            "work_item_id": self.state.work_item_id,
            "target_repo": "",
            "previous_context": ctx,
            "scrum_master_feedback": "",
        })
        raw_output = analysis_result.raw or ""
        # Parse hatasinda retry — TOOL'SUZ crew kullan
        try:
            plan = _parse_architect_output(raw_output)
        except ValueError as e:
            _log(f"  Parse hatasi ({e}), TOOL'SUZ crew ile retry")
            retry_ctx = ctx + (
                f"\n\n# ONCEKI DENEMEDE TOPLANAN TOOL OUTPUT\n"
                f"(Bu ciktilar tool cagrilarindan gelen bilgiler, JSON uretmek icin kullan)\n\n"
                f"{raw_output[:6000]}"
            )
            retry_crew = self._agile_crew.create_analysis_crew_toolless()
            analysis_result = retry_crew.kickoff(inputs={
                "work_item_id": self.state.work_item_id,
                "target_repo": "",
                "previous_context": retry_ctx,
                "scrum_master_feedback": (
                    "⚠️ SADECE JSON PLAN URET. Tool cagrisi yapamazsin — hicbir tool "
                    "yuklenmedi. Elindeki bilgilerle work_item_id, repo_name, summary, "
                    "changes[] (file_path, change_type, description, current_code, "
                    "new_code), acceptance_criteria alanli JSON plan yaz. "
                    "CIKTIN SADECE JSON OLMALI, baska hicbir metin yazma."
                ),
            })
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
        self._step_done("technical_design_task", raw_output[:3000])
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
        try:
            repo_dir = self._repo_mgr.base_dir / repo_name
            if repo_dir.exists() and (repo_dir / ".git").exists():
                _log(f"  Hedef repo fetch: {repo_name}")
                fetch_result = self._repo_mgr._git(["fetch", "origin", "main"], cwd=repo_dir)
                if fetch_result.returncode != 0:
                    _log(f"  fetch uyarisi: {fetch_result.stderr[:150]}")
            self._repo_mgr.checkout(repo_name, "main")
            # Repo summary'yi context'e ekle — sonraki adimlar yapiyi bilir
            repo_summary = self._repo_mgr.get_repo_summary(repo_name)
            if repo_summary:
                self._append_context("Repo Yapisi", repo_summary)

            # LAZY EMBED: hedef repo'nun kod chunk'larini vector DB'ye ekle
            # (Sadece bu pipeline'da kullanilacak repo, gereksiz 67 repo embed etmiyoruz)
            if self._vector_store:
                try:
                    _log(f"  Hedef repo kodlari vector DB'ye embed ediliyor: {repo_name}")
                    self._vector_store.index_repo(repo_name, repo_dir)
                except Exception as e:
                    _log(f"  Code embed hatasi: {e}")
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

        for i, change in enumerate(plan.get("changes", [])):
            file_path = change["file_path"]
            change_type = change.get("change_type", "edit")
            description = change.get("description", "")
            new_code = change.get("new_code", "")
            current_code = change.get("current_code", "")

            _log(f"\n  [{i+1}/{len(plan['changes'])}] {file_path} ({change_type})")

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

            elif full_content and new_code and current_code and current_code.strip() in full_content:
                cur_lines = len(current_code.strip().splitlines())
                new_lines = len(new_code.strip().splitlines())
                if cur_lines > 20 and new_lines < cur_lines * 0.3:
                    _log(f"    Guvenlik: current_code ({cur_lines} satir) >> new_code ({new_lines} satir), append yapiliyor")
                    final_content = full_content.rstrip() + "\n\n" + new_code + "\n"
                else:
                    final_content = full_content.replace(current_code.strip(), new_code, 1)
                    _log(f"    Direkt replace: current_code bulundu ve degistirildi")

            elif full_content and new_code and (not current_code or change_type == "add"):
                final_content = full_content.rstrip() + "\n\n" + new_code + "\n"
                _log(f"    Append: dosyanin sonuna eklendi")

            elif full_content and new_code:
                code_crew = self._agile_crew.create_code_crew()
                code_result = code_crew.kickoff(inputs={
                    "work_item_id": self.state.work_item_id,
                    "target_repo": repo_name,
                    "target_file": file_path,
                    "change_description": description,
                    "current_code": current_code[:6000] if current_code else full_content[:6000],
                    "new_code": new_code[:6000],
                    "start_marker": change.get("start_marker", ""),
                    "end_marker": change.get("end_marker", ""),
                })
                raw_code = code_result.raw or ""
                final_content = _extract_code_from_output(raw_code)
                if not final_content.strip():
                    _log(f"    Developer bos icerik dondurdu, fallback kullaniliyor")
                    if current_code and current_code in full_content:
                        final_content = full_content.replace(current_code, new_code, 1)
                    else:
                        final_content = new_code
                _log(f"    Developer kodu: {len(final_content)} karakter")

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
                    "start_line": "",
                    "end_line": "",
                })
                raw_code = code_result.raw or ""
                final_content = _extract_code_from_output(raw_code)
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
                    "start_marker": "",
                    "end_marker": "",
                })
                fixed_code = _extract_code_from_output(code_result.raw or "")
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
            else:
                _log(f"    Push hatasi: {push_result['error']}")

        self.state.all_pushes = all_pushes
        push_summary = ", ".join(p.get("file", "") for p in all_pushes)
        self._append_context("Kod Yazma & Push", f"{len(all_pushes)} dosya: {push_summary}")
        self._step_done("implement_change_task", f"{len(all_pushes)} dosya push edildi")

    @listen(step6_implement_code)
    def step7_create_pr(self):
        """Adim 7: PR Olustur."""
        from agile_sdlc_crew.main import _get_work_item_title
        from agile_sdlc_crew.pipeline import create_pull_request

        _log("\n-- ADIM 7: PR olusturuluyor --")
        self._step_start("create_pr_task")

        if not self.state.all_pushes:
            raise RuntimeError("Hicbir dosya push edilemedi, PR olusturulamiyor.")

        plan = self.state.plan
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
        """Adim 8: Kod Inceleme."""
        from agile_sdlc_crew.main import _add_wi_comment

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
        """Adim 9: Test Planlama."""
        from agile_sdlc_crew.main import (
            _extract_code_from_output, _validate_code, _add_wi_comment,
        )
        from agile_sdlc_crew.pipeline import push_file

        _log("\n-- ADIM 9: Test planlama --")
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

    @listen(step9_test_planning)
    def step10_uat(self):
        """Adim 10: UAT Dogrulama."""
        from agile_sdlc_crew.main import _add_wi_comment

        _log("\n-- ADIM 10: UAT dogrulama --")
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

    @listen(step10_uat)
    def step11_completion_report(self):
        """Adim 11: Tamamlanma Raporu."""
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
