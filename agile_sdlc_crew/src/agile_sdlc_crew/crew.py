"""Agile SDLC Crew - Full pipeline with 7 agents."""

import os
import ssl

# Kurumsal proxy self-signed sertifika - tum SSL dogrulamasini global kapat
ssl._create_default_https_context = ssl._create_unverified_context

import httpx  # noqa: E402

_orig_client = httpx.Client.__init__
_orig_async_client = httpx.AsyncClient.__init__


def _no_ssl_client(self, *a, **kw):
    kw.setdefault("verify", False)
    _orig_client(self, *a, **kw)


def _no_ssl_async_client(self, *a, **kw):
    kw.setdefault("verify", False)
    _orig_async_client(self, *a, **kw)


httpx.Client.__init__ = _no_ssl_client
httpx.AsyncClient.__init__ = _no_ssl_async_client

import litellm  # noqa: E402

litellm.ssl_verify = False

# Vertex AI Claude assistant prefill desteklemediginden,
# litellm.completion cagrisindan once son assistant mesajini cikar
_original_completion = litellm.completion


def _patched_completion(*args, **kwargs):
    model = kwargs.get("model", "")
    # Prefill fix SADECE Vertex AI / Anthropic API claude modelleri icin.
    # claude_cli/* (subprocess provider) HARIC — kendi prompt birlestirme
    # mantigi var; bu patch tool call JSON'una "[Devam et]:" enjekte ediyordu
    # ve agent maximum iterations'a kilidiyordu.
    is_claude_cli = "claude-cli/" in model or model.startswith("claude_cli/")
    if not is_claude_cli and ("vertex_ai" in model or "anthropic/" in model):
        messages = kwargs.get("messages", [])
        if messages and messages[-1].get("role") == "assistant":
            prefill = messages.pop()
            content = prefill.get("content", "")
            if content:
                messages.append({"role": "user", "content": f"[Devam et]: {content}"})
    return _original_completion(*args, **kwargs)


litellm.completion = _patched_completion

from crewai import Agent, Crew, Process, Task, LLM  # noqa: E402

from agile_sdlc_crew.dashboard import StatusTracker, TASK_DISPLAY_NAMES  # noqa: E402
from agile_sdlc_crew.tools.semantic_search import SemanticCodeSearchTool  # noqa: E402
from agile_sdlc_crew.tools.find_relevant_repos import FindRelevantReposTool  # noqa: E402
from agile_sdlc_crew.knowledge import load_knowledge  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402


# ── Architect output schema (structured output icin) ──

class PlanChange(BaseModel):
    file_path: str = Field(..., description="Tam dosya yolu, / ile baslar")
    change_type: str = Field(default="edit", description="'edit' veya 'add'")
    description: str = Field(default="", description="Bu dosyada ne degisecek (tek cumle)")
    current_code: str = Field(default="", description="Dosyadan okunan MEVCUT ilgili kod bolumu")
    new_code: str = Field(..., description="Uygulanacak YENI kod")
    start_marker: str = Field(default="", description="Degisikligin basladigi satirdaki unique string")
    end_marker: str = Field(default="", description="Degisikligin bittigi satirdaki unique string")


class TechnicalDesignPlan(BaseModel):
    work_item_id: str = Field(..., description="Is kalemi ID")
    repo_name: str = Field(..., description="Hedef repository adi")
    summary: str = Field(..., description="Is kaleminin tek cumlede ozeti")
    changes: list[PlanChange] = Field(..., description="Yapilacak dosya degisiklikleri")
    acceptance_criteria: list[str] = Field(default_factory=list, description="Kabul kriterleri")


class ImplementedFile(BaseModel):
    """Developer agent ciktisi — dosyanin TAM icerigi."""
    full_file_content: str = Field(
        ...,
        min_length=50,
        description=(
            "Dosyanin TAM icerigi, bastan sona her satir. "
            "Sadece degisen bolumu DEGIL — orijinal dosya kac satirsa senin de "
            "cikti o kadar satir olmali. 'Thought:', aciklama, yorum EKLEME."
        ),
    )
from agile_sdlc_crew.tools import (  # noqa: E402
    AzureDevOpsGetWorkItemTool,
    AzureDevOpsAddCommentTool,
    AzureDevOpsListWorkItemsTool,
    AzureDevOpsListReposTool,
    AzureDevOpsBrowseRepoTool,
    AzureDevOpsReadFileTool,
    AzureDevOpsSearchCodeTool,
    AzureDevOpsCreateBranchTool,
    AzureDevOpsPushChangesTool,
    AzureDevOpsCreatePRTool,
    AzureDevOpsPRReviewTool,
    AzureDevOpsPRChangesTool,
)


# ── LLM seçimi: agile_sdlc_crew/llm/ paketine devredildi ────────────
# Yeni mimari: provider registry (litellm/anthropic/claude_cli/ollama/lmstudio)
# + isimli profiller (config/llm_profiles.yaml) + agent->profile resolver.
# Agent bazli override: agents.yaml `llm_profile:` alani veya
# CREW_LLM_PROFILE_<AGENT_UPPER> env degiskeni.
from agile_sdlc_crew.llm import build_for_agent  # noqa: E402
from agile_sdlc_crew.llm.resolver import build_for_profile  # noqa: E402


def _create_llm(model_name: str | None = None, max_tokens: int = 2048) -> LLM:
    """Geriye uyumlu wrapper — eskiden litellm/anthropic/claude-cli secimini
    burada yapiyorduk. Artik registry uzerinden gidiyor.

    Kullanim onerisi: yeni kod `build_for_agent(agent_key)` veya
    `build_for_profile(profile_name)` cagirsin."""
    from agile_sdlc_crew.llm.registry import build_llm

    provider = os.environ.get("CREW_LLM_PROVIDER", "litellm")

    if provider == "claude-cli":
        return build_llm("claude_cli", model="sonnet", max_tokens=max_tokens)

    if provider == "anthropic":
        return build_llm(
            "anthropic",
            model=model_name or "claude-sonnet-4-20250514",
            max_tokens=max_tokens,
        )

    return build_llm(
        "litellm",
        model=model_name or os.environ.get("LITELLM_MODEL", "openai/gpt-4"),
        max_tokens=max_tokens,
    )


def _create_local_llm(max_tokens: int = 4096, model_override: str | None = None) -> LLM:
    """Geriye uyumlu wrapper — Ollama veya LM Studio (base_url'de /v1 varsa).

    Kickoff crew gibi yerlerde dogrudan model adi gerektigi icin kalir."""
    from agile_sdlc_crew.llm.registry import build_llm

    model = model_override or os.environ.get("CREW_LOCAL_LLM_MODEL", "qwen3:8b")
    coder_model = os.environ.get("CREW_LOCAL_CODER_MODEL", "qwen2.5-coder:7b")
    is_coder = (model == coder_model or "coder" in model.lower())
    base_url = (
        os.environ.get("OLLAMA_CODER_BASE_URL") or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        if is_coder
        else os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    )
    provider = "lmstudio" if "/v1" in base_url else "ollama"
    return build_llm(provider, model=model, max_tokens=max_tokens)


def _local_reasoning_model() -> str:
    return os.environ.get("CREW_LOCAL_LLM_MODEL", "qwen3:8b")


def _local_coder_model() -> str:
    return os.environ.get("CREW_LOCAL_CODER_MODEL", "qwen2.5-coder:7b")


# Agent-bazli LLM factory'leri — resolver agent_key'i profile'a cevirir.
LLM_ARCHITECT = lambda: build_for_agent("software_architect")
LLM_DEVELOPER = lambda: build_for_agent("senior_developer")
LLM_REVIEWER = lambda: build_for_agent("code_reviewer")
LLM_ANALYST = lambda: build_for_agent("business_analyst")
LLM_QA = lambda: build_for_agent("qa_engineer")
LLM_UAT = lambda: build_for_agent("uat_specialist")
LLM_SCRUM = lambda: build_for_agent("scrum_master")


class AgileSDLCCrew:
    """Full Agile SDLC pipeline with 7 agents."""

    def __init__(self):
        import yaml
        from pathlib import Path
        config_dir = Path(__file__).parent / "config"
        with open(config_dir / "agents.yaml", encoding="utf-8") as f:
            self.agents_config = yaml.safe_load(f)
        with open(config_dir / "tasks.yaml", encoding="utf-8") as f:
            self.tasks_config = yaml.safe_load(f)
        self.llm_architect = LLM_ARCHITECT()
        self.llm_developer = LLM_DEVELOPER()
        self.llm_reviewer = LLM_REVIEWER()
        self.llm_analyst = LLM_ANALYST()
        self.llm_qa = LLM_QA()
        self.llm_uat = LLM_UAT()
        self.llm_scrum = LLM_SCRUM()
        self.status_tracker: StatusTracker | None = None
        self.local_repo_mgr = None  # LocalRepoManager, flow.py tarafindan set edilir
        self.vector_store = None  # VectorStore, flow.py tarafindan set edilir

    def set_status_tracker(self, tracker: StatusTracker):
        self.status_tracker = tracker

    def _make_task_callback(self, task_key: str):
        """Gorev tamamlandiginda dashboard'u guncelleyen callback olusturur."""
        crew_ref = self

        def callback(output):
            if crew_ref.status_tracker:
                crew_ref.status_tracker.task_completed(task_key)

        return callback

    def _notify_task_start(self, task_key: str):
        """Gorev basladiginda dashboard'u gunceller."""
        if self.status_tracker:
            self.status_tracker.task_started(task_key)

    # ── Agents ──────────────────────────────────────────

    def _vector_ready(self) -> bool:
        """Vector store hazir mi?"""
        return bool(self.vector_store and self.vector_store._memory is not None)

    def _agent_config_with_knowledge(self, agent_key: str, *knowledge_names: str) -> dict:
        """agents.yaml config'ini domain knowledge ile zenginlestir.

        CrewAI agent backstory'sini inputs ile interpolate eder. Knowledge icindeki
        {Name}, {Domain} gibi placeholder kod ornekleri hata veriyor. TUM curly
        brace'leri angle bracket'a cevir — sadece agent backstory icin."""
        import re as _re
        cfg = dict(self.agents_config[agent_key])
        # llm_profile resolver tarafindan okunur, CrewAI Agent config'ine
        # sizdirma — bilinmeyen alan hatasina neden olabilir.
        cfg.pop("llm_profile", None)
        extra = []
        for name in knowledge_names:
            content = load_knowledge(name)
            if content:
                # {X} pattern'lerini <X> yap; tek basina { veya } varsa da escape
                content = _re.sub(r'\{([^{}]*)\}', r'<\1>', content)
                # Kalan yalnız brace'leri de temizle (olur a olur)
                content = content.replace("{", "<").replace("}", ">")
                extra.append(f"\n\n=== DOMAIN KNOWLEDGE: {name} ===\n\n{content}")
        if extra:
            cfg["backstory"] = (cfg.get("backstory", "") or "") + "".join(extra)
        return cfg

    def scrum_master(self) -> Agent:
        return Agent(
            config=self._agent_config_with_knowledge(
                "scrum_master", "agile_facilitation"
            ),
            llm=self.llm_scrum,
            verbose=True,
            max_iter=5,
            tools=[
                AzureDevOpsGetWorkItemTool(),
                AzureDevOpsListWorkItemsTool(),
            ],
        )

    def business_analyst(self) -> Agent:
        return Agent(
            config=self._agent_config_with_knowledge(
                "business_analyst", "requirements_analysis"
            ),
            llm=self.llm_analyst,
            verbose=True,
            max_iter=5,
            tools=[
                AzureDevOpsGetWorkItemTool(),
                AzureDevOpsAddCommentTool(),
            ],
        )

    def software_architect(self) -> Agent:
        # get_work_item KASITLI OLARAK YOK — WI detayi Python tarafinda
        # context'e ekleniyor (flow.py crew_step4_technical_design).
        # Tool listede olursa agent yine de cagirir → 1 ekstra API call +
        # conversation history buyumesi → gereksiz token maliyeti.
        tools = [
            AzureDevOpsBrowseRepoTool(local_repo_mgr=self.local_repo_mgr),
            AzureDevOpsSearchCodeTool(local_repo_mgr=self.local_repo_mgr),
        ]
        # Vector store'da summary'ler var — FindRelevantRepos calisiyor.
        # SemanticCodeSearch code chunks gerektiriyor (henuz embed edilmedi),
        # agent'a verince bos sonuc donup iteration yakiyor — kaldirildi.
        if self.vector_store and self.vector_store._memory is not None:
            tools.append(FindRelevantReposTool(vector_store=self.vector_store))
        else:
            tools.append(AzureDevOpsListReposTool())
        # max_iter=15 → 10: Architect 67 repolu ortamda her iterasyonda
        # browse_repo ile buyuk dosyalar okuyordu; 15 iterasyon = cok fazla
        # input token birikimi. 10 yeterli, dashboard veya env ile override edilebilir.
        from agile_sdlc_crew import pipeline_config as _pc
        max_iter = _pc.get("CREW_ARCHITECT_MAX_ITER")
        return Agent(
            config=self._agent_config_with_knowledge(
                "software_architect", "backend_tech_design", "frontend_nextjs"
            ),
            llm=self.llm_architect,
            verbose=True,
            max_iter=max_iter,
            tools=tools,
        )

    def qa_engineer(self) -> Agent:
        tools = [
            AzureDevOpsBrowseRepoTool(local_repo_mgr=self.local_repo_mgr),
            AzureDevOpsSearchCodeTool(local_repo_mgr=self.local_repo_mgr),
            AzureDevOpsPRChangesTool(),
        ]
        if self._vector_ready():
            tools.append(SemanticCodeSearchTool(vector_store=self.vector_store))
        return Agent(
            config=self._agent_config_with_knowledge(
                "qa_engineer", "testing_strategy"
            ),
            llm=self.llm_qa,
            verbose=True,
            max_iter=8,
            tools=tools,
        )

    def uat_specialist(self) -> Agent:
        return Agent(
            config=self._agent_config_with_knowledge(
                "uat_specialist", "uat_strategy"
            ),
            llm=self.llm_uat,
            verbose=True,
            max_iter=8,
            tools=[
                AzureDevOpsGetWorkItemTool(),
                AzureDevOpsAddCommentTool(),
                AzureDevOpsPRChangesTool(),
            ],
        )

    def senior_developer(self) -> Agent:
        """Plana gore kod yazan agent.
        TOOL KULLANMIYOR — dosya icerigi zaten prompt'ta (current_code/full_content)
        context olarak veriliyor. browse_repo loop'u ve tekrarli tool call'lar
        iteration patlatiyordu; tool'suz model tek iterasyonda kod dondurur."""
        return Agent(
            config=self._agent_config_with_knowledge(
                "senior_developer", "backend_feature_dev", "frontend_nextjs"
            ),
            llm=self.llm_developer,
            verbose=True,
            max_iter=3,
            tools=[],
        )

    def code_reviewer(self) -> Agent:
        """PR'i inceleyen agent."""
        return Agent(
            config=self._agent_config_with_knowledge(
                "code_reviewer", "backend_code_review"
            ),
            llm=self.llm_reviewer,
            verbose=True,
            max_iter=10,
            tools=[
                AzureDevOpsGetWorkItemTool(),
                AzureDevOpsAddCommentTool(),
                AzureDevOpsBrowseRepoTool(local_repo_mgr=self.local_repo_mgr),
                AzureDevOpsReadFileTool(local_repo_mgr=self.local_repo_mgr),
                AzureDevOpsPRChangesTool(),
                AzureDevOpsPRReviewTool(),
            ],
        )

    # ── Helpers ──────────────────────────────────

    def _task(self, key: str, agent: Agent, context: list[Task] | None = None, **extra) -> Task:
        """YAML config'den Task olusturur."""
        cfg = self.tasks_config[key]
        return Task(
            description=cfg["description"],
            expected_output=cfg["expected_output"],
            agent=agent,
            context=context,
            callback=self._make_task_callback(key),
            **extra,
        )

    # ── Crew Factories ──────────────────────────────────

    def _build_kickoff_agents(self) -> tuple[Agent, Agent, Agent, Agent]:
        """Kickoff toplantisi icin 4 agent: BA, Architect, Developer, SM.

        Hem `create_kickoff_crew` (klasik tek-cagrili Crew) hem de
        `run_kickoff_meeting` (task-by-task + grading) tarafindan kullanilir.
        """
        sm = Agent(
            config=self._agent_config_with_knowledge("scrum_master", "agile_facilitation"),
            llm=build_for_agent("scrum_master"),
            verbose=True,
            max_iter=2,
            tools=[],
        )
        ba = Agent(
            config=self._agent_config_with_knowledge("business_analyst", "requirements_analysis"),
            llm=build_for_agent("business_analyst"),
            verbose=True,
            max_iter=2,
            tools=[],
        )
        arch = Agent(
            config=self._agent_config_with_knowledge(
                "software_architect", "backend_tech_design", "frontend_nextjs"
            ),
            llm=build_for_agent("software_architect"),
            verbose=True,
            max_iter=3,
            tools=[
                AzureDevOpsBrowseRepoTool(local_repo_mgr=self.local_repo_mgr),
            ],
        )
        dev = Agent(
            config=self._agent_config_with_knowledge(
                "senior_developer", "backend_feature_dev", "frontend_nextjs"
            ),
            llm=build_for_agent("senior_developer"),
            verbose=True,
            max_iter=2,
            tools=[],
        )
        return ba, arch, dev, sm

    def create_kickoff_crew(self) -> Crew:
        """Kickoff toplantisi — Sanal Odak Grup / Design Review simulasyonu.

        Her agent dashboard'da configure ettigi LLM'i kullanir (resolver uzerinden).
        Architect browse_repo + search_code ile GERCEK KOD okur — kör analiz degil.
        Context'te 'HEDEF REPO' ve dosya yapisi zaten var (flow.py tarafindan eklendi),
        agent dogrudan ilgili dosyalara gidebilir.

        NOT: Bu klasik path TEK Crew calistirir; grading + retry yok.
        Yeni varsayilan path: `run_kickoff_meeting` (Haiku grading destekli).
        """
        # 4 task: BA → Architect → Developer → SM Tutanak (~300-400s)
        ba, arch, dev, sm = self._build_kickoff_agents()

        # Her kickoff task'ina log callback ekle — hangi agent ne zaman bitti gorunsun
        import logging as _logging
        import time as _time
        from datetime import datetime as _dt
        from pathlib import Path as _KPath
        _kickoff_log = _logging.getLogger("pipeline")
        _kickoff_start = _time.time()

        _task_names = {
            "kickoff_ba_task": "BA Analiz",
            "kickoff_arch_task": "Architect",
            "kickoff_dev_task": "Developer",
            "kickoff_sm_close_task": "SM Tutanak",
        }

        # Kickoff ciktilarini diske yaz: /tmp/kickoff_<wi>_<timestamp>.md
        # WI ID'ye state.work_item_id ile erisemiyoruz (crew tarafi state bilmiyor),
        # bunun yerine dosya adina ozellik koymadan timestamp ile ayri tutarız.
        _kickoff_log_dir = _KPath("/tmp/crew_kickoff")
        _kickoff_log_dir.mkdir(parents=True, exist_ok=True)
        _kickoff_log_file = _kickoff_log_dir / f"kickoff_{_dt.now().strftime('%Y%m%d_%H%M%S')}.md"
        try:
            _kickoff_log_file.write_text(
                f"# Kickoff Toplantisi Ciktilari\n"
                f"**Tarih**: {_dt.now().isoformat()}\n\n",
                encoding="utf-8",
            )
            _kickoff_log.info(f"  Kickoff ciktilari dosyaya yaziliyor: {_kickoff_log_file}")
        except Exception as e:
            _kickoff_log.warning(f"  Kickoff log dosyasi acilamadi: {e}")

        def _make_kickoff_cb(task_key):
            orig_cb = self._make_task_callback(task_key)
            label = _task_names.get(task_key, task_key)
            def cb(output):
                elapsed = _time.time() - _kickoff_start
                full = output.raw if hasattr(output, "raw") else str(output)
                full = full or ""
                raw = full[:120]
                _kickoff_log.info(f"  ✅ Kickoff [{label}] tamamlandi ({elapsed:.0f}s) — {raw}")
                # Tam ciktiyi diske ekle
                try:
                    with open(_kickoff_log_file, "a", encoding="utf-8") as _f:
                        _f.write(f"\n---\n\n## {label} ({elapsed:.0f}s)\n\n{full}\n")
                except Exception as _e:
                    _kickoff_log.warning(f"  Kickoff log yazma hatasi ({label}): {_e}")
                orig_cb(output)
            return cb

        def _kickoff_task(key, agent, context=None):
            cfg = self.tasks_config[key]
            return Task(
                description=cfg["description"],
                expected_output=cfg["expected_output"],
                agent=agent,
                context=context,
                callback=_make_kickoff_cb(key),
            )

        # 4 task: BA → Architect → Developer → SM Tutanak
        # SM acilis ve QA/UAT ayrı task olmak zorunda degil — context'te
        # her sey var, SM tutanagi sonunda derler. 7 task yerine 4 = ~%40 hiz.
        t_ba   = _kickoff_task("kickoff_ba_task",       ba)
        t_arch = _kickoff_task("kickoff_arch_task",      arch, context=[t_ba])
        t_dev  = _kickoff_task("kickoff_dev_task",       dev,  context=[t_ba, t_arch])
        t_close= _kickoff_task("kickoff_sm_close_task",  sm,   context=[t_ba, t_arch, t_dev])

        return Crew(
            agents=[ba, arch, dev, sm],
            tasks=[t_ba, t_arch, t_dev, t_close],
            process=Process.sequential,
            verbose=True,
            memory=False,
        )

    def run_kickoff_meeting(self, inputs: dict):
        """Kickoff'u task-by-task calistirir; her ciktiya Haiku-bazli grading
        ve gerekirse iyilestirilmis prompt ile retry uygular.

        inputs: {work_item_id, previous_context, target_repo}
        Returns: synthetic obj with `.raw` (str) ve `.token_usage` (toplanmis).

        Env toggle'lari:
          CREW_KICKOFF_GRADING            (1)  — 0 ise grading kapali (klasik)
          CREW_KICKOFF_GRADE_THRESHOLD    (8)  — passing esigi (1-10)
          CREW_KICKOFF_GRADE_MAX_RETRIES  (2)  — esik altinda max retry sayisi
          CREW_KICKOFF_GRADER_PROFILE     (kickoff_grader)
        """
        import logging as _logging
        import time as _time
        from datetime import datetime as _dt
        from pathlib import Path as _KPath

        from agile_sdlc_crew.kickoff_grader import (
            grade_output,
            build_improvement_description,
            grading_enabled,
            grade_threshold,
            grade_max_retries,
        )

        log = _logging.getLogger("pipeline")
        grading_on = grading_enabled()
        threshold = grade_threshold()
        max_retries = grade_max_retries()
        log.info(
            f"  Kickoff orchestrator: grading={'ON' if grading_on else 'OFF'} "
            f"esik={threshold} max_retry={max_retries}"
        )

        ba, arch, dev, sm = self._build_kickoff_agents()

        # (key, agent, label, prior_task_keys, context_label)
        plan = [
            ("kickoff_ba_task",       ba,   "BA Analiz",  []),
            ("kickoff_arch_task",     arch, "Architect",  ["kickoff_ba_task"]),
            ("kickoff_dev_task",      dev,  "Developer",  ["kickoff_ba_task", "kickoff_arch_task"]),
            ("kickoff_sm_close_task", sm,   "SM Tutanak", ["kickoff_ba_task", "kickoff_arch_task", "kickoff_dev_task"]),
        ]

        # Log dosyasi (klasik path ile ayni klasor + format)
        _log_dir = _KPath("/tmp/crew_kickoff")
        _log_dir.mkdir(parents=True, exist_ok=True)
        _log_file = _log_dir / f"kickoff_{_dt.now().strftime('%Y%m%d_%H%M%S')}.md"
        try:
            _log_file.write_text(
                f"# Kickoff Toplantisi Ciktilari (graded)\n"
                f"**Tarih**: {_dt.now().isoformat()}\n"
                f"**Grading**: {'ON' if grading_on else 'OFF'} (esik={threshold}, max_retry={max_retries})\n\n",
                encoding="utf-8",
            )
            log.info(f"  Kickoff ciktilari dosyaya yaziliyor: {_log_file}")
        except Exception as e:
            log.warning(f"  Kickoff log dosyasi acilamadi: {e}")

        outputs: dict[str, str] = {}
        grade_log: dict[str, list[dict]] = {}
        total_pt = total_ct = total_tt = 0
        start_t = _time.time()
        wi_context = (inputs.get("previous_context") or "")[:1800]

        # Placeholder substitution: kickoff task description'larinda yalniz
        # `{work_item_id}` ve `{previous_context}` placeholder'lari kullaniliyor.
        # CrewAI'nin native input-interpolation'i ASIL/AGENT ciktisindaki butun
        # `{...}` bloklarini "template variable" sanip patliyor (orn. WI metninde
        # gecen `{orderId}` agent ciktisina dustugunde). Bunu by-pass edebilmek
        # icin substitution'i elimizle yapip mini.kickoff()'a inputs gecmiyoruz.
        _sub_map = {
            "{work_item_id}": str(inputs.get("work_item_id", "")),
            "{previous_context}": str(inputs.get("previous_context", "")),
        }
        _other_inputs = {
            k: v for k, v in (inputs or {}).items()
            if k not in ("work_item_id", "previous_context")
        }

        def _safe_sub(text: str) -> str:
            out = text or ""
            for needle, value in _sub_map.items():
                if needle in out:
                    out = out.replace(needle, value)
            # Diger inputlardaki (orn. target_repo) opsiyonel placeholder'lari da degistir.
            for k, v in _other_inputs.items():
                token = "{" + k + "}"
                if token in out:
                    out = out.replace(token, str(v))
            return out

        for key, agent, label, prior_keys in plan:
            cfg = self.tasks_config[key]
            base_desc_raw = cfg["description"]
            expected = cfg["expected_output"]

            # Onceki uzman ciktilarini description'a ek olarak gom (mini-crew
            # task'lari arasinda CrewAI context auto-link YOK)
            prior_block = ""
            for pk in prior_keys:
                if outputs.get(pk):
                    plabel = next((p[2] for p in plan if p[0] == pk), pk)
                    prior_block += (
                        f"\n\n# ONCEKI UZMAN CIKTISI — {plabel} ({pk})\n"
                        f"{outputs[pk]}\n"
                    )
            base_desc = base_desc_raw + (
                "\n\n# DIGER UZMANLARIN BU TOPLANTIDAKI KATKILARI\n" + prior_block
                if prior_block else ""
            )
            # Placeholder substitution (CrewAI input-interpolation BY-PASS):
            base_desc = _safe_sub(base_desc)
            expected_sub = _safe_sub(expected)

            current_desc = base_desc
            result_text = ""
            final_grade = None
            grade_log[key] = []

            attempts = 1 + (max_retries if grading_on else 0)
            for attempt in range(1, attempts + 1):
                t = Task(
                    description=current_desc,
                    expected_output=expected_sub,
                    agent=agent,
                    callback=self._make_task_callback(key) if attempt == attempts else None,
                )
                mini = Crew(
                    agents=[agent],
                    tasks=[t],
                    process=Process.sequential,
                    verbose=True,
                    memory=False,
                )

                t0 = _time.time()
                log.info(f"  Kickoff [{label}] deneme #{attempt}/{attempts} basliyor")
                try:
                    # inputs=None: CrewAI bunu pre-interpolate etmeyecek; biz
                    # yukarida placeholder'lari kendi elimizle doldurduk.
                    res = mini.kickoff()
                except Exception as e:
                    log.error(f"  Kickoff [{label}] deneme #{attempt} HATA: {e}")
                    if attempt >= attempts:
                        raise
                    # Hata varsa improvement description'a feedback ekleyip retry
                    current_desc = base_desc + (
                        f"\n\n# UYARI — Onceki deneme calismadi: {e}\n"
                        f"Sade, format'a uygun bir cikti uret."
                    )
                    continue

                elapsed = _time.time() - t0
                result_text = (res.raw or "") if res is not None else ""

                # Token usage topla
                usage = getattr(res, "token_usage", None)
                pt = ct = tt = 0
                if usage:
                    try:
                        pt = int(getattr(usage, "prompt_tokens", 0) or 0)
                        ct = int(getattr(usage, "completion_tokens", 0) or 0)
                        tt = int(getattr(usage, "total_tokens", 0) or 0) or (pt + ct)
                    except Exception:
                        pass
                total_pt += pt
                total_ct += ct
                total_tt += tt

                log.info(
                    f"  Kickoff [{label}] deneme #{attempt} bitti ({elapsed:.0f}s, "
                    f"+{pt}i/{ct}o token)"
                )

                if not grading_on:
                    final_grade = None
                    break

                grade = grade_output(
                    task_key=key,
                    agent_label=label,
                    description=base_desc_raw,
                    expected_output=expected,
                    actual_output=result_text,
                    work_item_context=wi_context,
                )
                final_grade = grade
                grade_log[key].append({
                    "attempt": attempt,
                    "score": grade.score,
                    "skipped": grade.skipped,
                    "weaknesses": grade.weaknesses,
                    "suggestions": grade.suggestions,
                    "reasoning": grade.reasoning,
                })
                status_emoji = "✅" if grade.passing(threshold) else "❌"
                skip_note = " (grader skipped)" if grade.skipped else ""
                log.info(
                    f"  {status_emoji} Kickoff [{label}] grade={grade.score}/10 "
                    f"(esik={threshold}){skip_note}"
                )

                if grade.passing(threshold) or grade.skipped:
                    break
                if attempt >= attempts:
                    log.warning(
                        f"  Kickoff [{label}] retry tukendi — son cikti score "
                        f"{grade.score}/10 ile kullaniliyor"
                    )
                    break

                # Iyilestirilmis prompt + retry
                current_desc = build_improvement_description(
                    base_desc, result_text, grade, attempt, threshold=threshold,
                )
                if grade.weaknesses:
                    log.info(f"  Kickoff [{label}] zayifliklar: {'; '.join(grade.weaknesses[:3])}")

            outputs[key] = result_text

            # Log dosyasina yaz
            try:
                with open(_log_file, "a", encoding="utf-8") as _f:
                    _f.write(f"\n---\n\n## {label} ({key})\n\n")
                    if final_grade is not None:
                        _f.write(
                            f"_Final grade: **{final_grade.score}/10** "
                            f"(esik={threshold}, deneme sayisi={len(grade_log[key])})_\n\n"
                        )
                        if grade_log[key]:
                            _f.write("<details><summary>Grade history</summary>\n\n")
                            for h in grade_log[key]:
                                _f.write(
                                    f"- Deneme #{h['attempt']}: {h['score']}/10"
                                    f"{' (skipped)' if h['skipped'] else ''} — "
                                    f"{h['reasoning'][:200]}\n"
                                )
                            _f.write("\n</details>\n\n")
                    _f.write(result_text + "\n")
            except Exception as e:
                log.warning(f"  Kickoff log yazma hatasi ({label}): {e}")

        # Sonucu birlestir — SM tutanagi en basta (downstream context icin
        # en degerli ozet), digerleri arkasinda referans olarak.
        sections = []
        sm_text = outputs.get("kickoff_sm_close_task", "")
        if sm_text:
            sections.append(sm_text)
        for key, _agent, label, _ in plan:
            if key == "kickoff_sm_close_task":
                continue
            if outputs.get(key):
                sections.append(f"## {label}\n\n{outputs[key]}")
        final_text = "\n\n---\n\n".join(sections)

        elapsed_total = _time.time() - start_t
        log.info(
            f"  Kickoff tamamlandi: {elapsed_total:.0f}s, toplam "
            f"{total_tt} token (+{total_pt}i/{total_ct}o)"
        )

        # Synthetic result — flow.py'nin gordugu interface ile uyumlu
        class _Usage:
            prompt_tokens = total_pt
            completion_tokens = total_ct
            total_tokens = total_tt

        class _Result:
            raw = final_text
            token_usage = _Usage()
            kickoff_grades = grade_log     # opsiyonel telemetri (debug UI)
            kickoff_outputs = outputs      # task_key -> son cikti

        return _Result()

    def create_analysis_crew(self) -> Crew:
        """Software Architect: is kalemini oku, repo'yu incele, teknik tasarim olustur.
        Not: output_pydantic CrewAI'da OpenAI API key istedigi icin kullanmiyoruz —
        task prompt'undaki JSON format kurali + retry mekanizmasi yeterli."""
        arch = self.software_architect()
        t1 = self._task("technical_design_task", arch)

        return Crew(
            agents=[arch],
            tasks=[t1],
            process=Process.sequential,
            verbose=True,
            memory=False,
        )

    def create_analysis_crew_toolless(self) -> Crew:
        """Tool'suz architect — sadece verilen context ile JSON uretir.
        max_tokens yuksek tutulur: JSON plan buyuk olabiliyor, 8192 yetmiyor."""
        arch = Agent(
            config=self._agent_config_with_knowledge(
                "software_architect", "backend_tech_design"
            ),
            llm=_create_llm("vertex_ai/claude-sonnet-4-6", max_tokens=16384),
            verbose=True,
            max_iter=3,
            tools=[],
        )
        t1 = self._task("technical_design_task", arch)
        return Crew(
            agents=[arch],
            tasks=[t1],
            process=Process.sequential,
            verbose=True,
            memory=False,
        )

    def create_code_crew(self) -> Crew:
        """Developer: tek dosya icin degisiklik uygula.
        Not: output_pydantic kullanmiyoruz (OpenAI API key sorunu) — task
        prompt'undaki TAM DOSYA kurali + push oncesi guvenlik kontrollari yeterli."""
        dev = self.senior_developer()
        cfg = self.tasks_config["implement_change_task"]
        t = Task(
            description=cfg["description"],
            expected_output=cfg["expected_output"],
            agent=dev,
        )
        return Crew(
            agents=[dev],
            tasks=[t],
            process=Process.sequential,
            verbose=True,
            memory=False,
        )

    def create_review_crew(self) -> Crew:
        """Reviewer: PR'i is kalemiyle karsilastir."""
        reviewer = self.code_reviewer()
        t1 = self._task("review_pr_task", reviewer)

        return Crew(
            agents=[reviewer],
            tasks=[t1],
            process=Process.sequential,
            verbose=True,
            memory=False,
        )

    def create_repo_discovery_crew(self) -> Crew:
        arch = self.software_architect()
        t1 = self._task("discover_repos_task", arch)
        return Crew(agents=[arch], tasks=[t1], process=Process.sequential, verbose=True, memory=False)

    def create_dependency_crew(self) -> Crew:
        arch = self.software_architect()
        t1 = self._task("dependency_analysis_task", arch)
        return Crew(agents=[arch], tasks=[t1], process=Process.sequential, verbose=True, memory=False)

    def create_requirements_crew(self) -> Crew:
        ba = self.business_analyst()
        t1 = self._task("requirements_analysis_task", ba)
        return Crew(agents=[ba], tasks=[t1], process=Process.sequential, verbose=True, memory=False)

    def create_test_crew(self) -> Crew:
        qa = self.qa_engineer()
        t1 = self._task("test_planning_task", qa)
        return Crew(agents=[qa], tasks=[t1], process=Process.sequential, verbose=True, memory=False)

    def create_uat_crew(self) -> Crew:
        uat = self.uat_specialist()
        t1 = self._task("uat_task", uat)
        return Crew(agents=[uat], tasks=[t1], process=Process.sequential, verbose=True, memory=False)

    def create_completion_crew(self) -> Crew:
        sm = self.scrum_master()
        t1 = self._task("completion_report_task", sm)
        return Crew(agents=[sm], tasks=[t1], process=Process.sequential, verbose=True, memory=False)

    def create_scrum_review_crew(self) -> Crew:
        """Scrum Master: bir adimin ciktisini incele, ONAY veya IYILESTIR."""
        sm = self.scrum_master()
        t1 = self._task("scrum_review_task", sm)
        return Crew(agents=[sm], tasks=[t1], process=Process.sequential, verbose=True, memory=False)
