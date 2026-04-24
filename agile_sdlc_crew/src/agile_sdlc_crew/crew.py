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
    # Prefill fix sadece vertex_ai/claude modelleri icin
    if "claude" in model or "vertex_ai" in model:
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
    AzureDevOpsSearchCodeTool,
    AzureDevOpsCreateBranchTool,
    AzureDevOpsPushChangesTool,
    AzureDevOpsCreatePRTool,
    AzureDevOpsPRReviewTool,
    AzureDevOpsPRChangesTool,
)


def _create_llm(model_name: str | None = None, max_tokens: int = 2048) -> LLM:
    """LLM olusturur. CREW_LLM_PROVIDER env ile provider secilir:
    - litellm (default): LITELLM_BASE_URL + LITELLM_API_KEY uzerinden
    - anthropic: ANTHROPIC_API_KEY ile direkt Anthropic API
    - claude-cli: Claude CLI OAuth session uzerinden (API key gerektirmez)
    """
    provider = os.environ.get("CREW_LLM_PROVIDER", "litellm")

    if provider == "claude-cli":
        # Claude CLI — subprocess uzerinden, litellm custom callback ile
        _register_claude_cli_provider()
        model = "claude-cli/sonnet"
        return LLM(
            model=model,
            max_tokens=max_tokens,
        )

    if provider == "anthropic":
        model = model_name or "claude-sonnet-4-20250514"
        if not model.startswith("anthropic/"):
            if "claude" in model:
                clean = model.split("/")[-1]
                model = f"anthropic/{clean}"
            else:
                model = f"anthropic/{model}"
        return LLM(
            model=model,
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            max_tokens=max_tokens,
        )

    # Default: litellm proxy
    model = model_name or os.environ.get("LITELLM_MODEL", "openai/gpt-4")
    base_url = os.environ.get("LITELLM_BASE_URL")
    if base_url and not model.startswith("openai/"):
        model = f"openai/{model}"
    return LLM(
        model=model,
        base_url=base_url,
        api_key=os.environ.get("LITELLM_API_KEY"),
        max_tokens=max_tokens,
    )


_claude_cli_registered = False

def _register_claude_cli_provider():
    """Claude CLI'i litellm custom provider olarak kaydet."""
    global _claude_cli_registered
    if _claude_cli_registered:
        return
    import litellm
    from agile_sdlc_crew.tools.claude_cli_llm import claude_cli_completion

    class ClaudeCLIHandler(litellm.CustomLLM):
        def completion(self, model, messages, **kwargs):
            # Messages'i tek prompt'a cevir
            prompt_parts = []
            for msg in messages:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                if role == "system":
                    prompt_parts.append(f"[System]: {content}")
                elif role == "assistant":
                    prompt_parts.append(f"[Assistant]: {content}")
                else:
                    prompt_parts.append(content)
            prompt = "\n\n".join(prompt_parts)

            max_tokens = kwargs.get("max_tokens", 4096)
            result = claude_cli_completion(prompt, max_tokens=max_tokens)

            from litellm import ModelResponse, Choices, Message, Usage
            return ModelResponse(
                choices=[Choices(
                    message=Message(role="assistant", content=result),
                    index=0,
                    finish_reason="stop",
                )],
                model="claude-cli/sonnet",
                usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            )

    handler = ClaudeCLIHandler()
    litellm.custom_provider_map = [
        {"provider": "claude-cli", "custom_handler": handler},
    ]
    _claude_cli_registered = True


def _create_local_llm(max_tokens: int = 4096, model_override: str | None = None) -> LLM:
    """Local LLM olusturur. Ollama veya LM Studio (OpenAI-compatible) destekler.

    OLLAMA_CODER_BASE_URL: coder modeli farkli makinede (LM Studio vb.) calisiyorsa
    ayri URL. URL '/v1' iceriyorsa LM Studio (openai/) prefix kullanilir,
    degilse Ollama (ollama/) prefix kullanilir."""
    model = model_override or os.environ.get("CREW_LOCAL_LLM_MODEL", "qwen3:8b")
    # Coder modeli farkli makinede olabilir
    coder_model = os.environ.get("CREW_LOCAL_CODER_MODEL", "qwen2.5-coder:7b")
    is_coder = (model == coder_model or "coder" in model.lower())

    if is_coder:
        base_url = os.environ.get("OLLAMA_CODER_BASE_URL") or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    else:
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

    # LM Studio: OpenAI-compatible API (/v1 endpoint)
    # Ollama: kendi API formatı
    if "/v1" in base_url:
        # LM Studio — openai/ prefix, model adi: "qwen/qwen2.5-coder-14b" formatinda
        # Ollama model adi (qwen2.5-coder:14b) → LM Studio formatina cevir
        lms_model = model.replace(":", "-")  # qwen2.5-coder:14b → qwen2.5-coder-14b
        # LM Studio genelde "vendor/model" formatinda — /v1/models'tan kontrol ederiz
        # ama basit heuristic yeterli
        if "/" not in lms_model:
            lms_model = f"qwen/{lms_model}"  # default vendor
        return LLM(
            model=f"openai/{lms_model}",
            base_url=base_url,
            api_key="lm-studio",  # LM Studio API key gerektirmez ama litellm ister
            max_tokens=max_tokens,
        )
    else:
        # Ollama
        return LLM(
            model=f"ollama/{model}",
            base_url=base_url,
            max_tokens=max_tokens,
        )


def _use_local() -> bool:
    """Basit agent'lar (BA/QA/UAT/Reviewer/SM) local LLM kullansin mi?"""
    return os.environ.get("CREW_USE_LOCAL_LLM", "").lower() in ("1", "true", "yes")


def _local_reasoning_model() -> str:
    """BA/SM/Reviewer/QA/UAT icin muhakeme modeli — Turkce instruct.
    CREW_LOCAL_LLM_MODEL env'i (default qwen3:8b)."""
    return os.environ.get("CREW_LOCAL_LLM_MODEL", "qwen3:8b")


def _local_coder_model() -> str:
    """Developer icin kod modeli — kod generation icin optimize edilmis.
    CREW_LOCAL_CODER_MODEL env'i (default qwen2.5-coder:7b)."""
    return os.environ.get("CREW_LOCAL_CODER_MODEL", "qwen2.5-coder:7b")


# Architect: geliştirme plani DAIMA Claude Sonnet — yuksek kaliteli tasarim
# karari gerekli, local model yetmez (kullanici onayi)
LLM_ARCHITECT = lambda: _create_llm("vertex_ai/claude-sonnet-4-6", max_tokens=32768)
# Developer: kod yazma — CODER modeli kullanir (qwen2.5-coder:7b default).
# CREW_USE_LOCAL_LLM=1 ile local'e duser.
# CREW_LOCAL_DEVELOPER=0 ile devre disi birakilabilir (Sonnet'e geri dusurulur).
def _use_local_developer() -> bool:
    if os.environ.get("CREW_LOCAL_DEVELOPER", "1").lower() in ("0", "false", "no"):
        return False
    return _use_local()

LLM_DEVELOPER = lambda: (
    _create_local_llm(8192, model_override=_local_coder_model())
    if _use_local_developer()
    else _create_llm("vertex_ai/claude-sonnet-4-6", max_tokens=8192)
)
# BA/SM/Reviewer/QA/UAT — reasoning modeli (qwen3:8b default, Turkce instruct).
# Coder modeli yerine instruct model kullanilir — YETERSIZ keyword kopyalama vb.
# sorunlari engellemek icin.
LLM_REVIEWER = lambda: _create_local_llm(4096, _local_reasoning_model()) if _use_local() else _create_llm("o4-mini", max_tokens=4096)
LLM_ANALYST = lambda: _create_local_llm(4096, _local_reasoning_model()) if _use_local() else _create_llm("o4-mini", max_tokens=4096)
LLM_QA = lambda: _create_local_llm(4096, _local_reasoning_model()) if _use_local() else _create_llm("o4-mini", max_tokens=4096)
LLM_UAT = lambda: _create_local_llm(4096, _local_reasoning_model()) if _use_local() else _create_llm("o4-mini", max_tokens=4096)
LLM_SCRUM = lambda: _create_local_llm(2048, _local_reasoning_model()) if _use_local() else _create_llm("o4-mini", max_tokens=2048)


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
        # input token birikimi. 10 yeterli, gerekirse env ile override edilebilir.
        import os as _os
        max_iter = int(_os.environ.get("CREW_ARCHITECT_MAX_ITER", "10"))
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

    def create_kickoff_crew(self) -> Crew:
        """Kickoff toplantisi — Sanal Odak Grup / Design Review simulasyonu.

        Tum agentlar LOCAL Ollama modeli kullanir (bulut maliyeti yok).
        Architect browse_repo + search_code ile GERCEK KOD okur — kör analiz degil.
        Context'te 'HEDEF REPO' ve dosya yapisi zaten var (flow.py tarafindan eklendi),
        agent dogrudan ilgili dosyalara gidebilir.
        """
        local_llm = _create_local_llm(4096, _local_reasoning_model())

        # 4 agent: BA, Architect, Developer, SM (tutanak)
        # SM acilis, QA, UAT ayrı task olarak gereksiz — her biri ~100s suruyordu,
        # toplam 700s > timeout. 4 task ile ~300-400s hedefi.
        sm = Agent(
            config=self._agent_config_with_knowledge("scrum_master", "agile_facilitation"),
            llm=local_llm,
            verbose=True,
            max_iter=2,
            tools=[],
        )
        ba = Agent(
            config=self._agent_config_with_knowledge("business_analyst", "requirements_analysis"),
            llm=local_llm,
            verbose=True,
            max_iter=2,
            tools=[],
        )

        # Architect — yerel model + browse_repo (context'te repo yapisi var ama
        # spesifik dosya okumasi gerekebilir). max_iter=3: 1-2 tool call + final answer.
        arch = Agent(
            config=self._agent_config_with_knowledge(
                "software_architect", "backend_tech_design", "frontend_nextjs"
            ),
            llm=local_llm,
            verbose=True,
            max_iter=3,
            tools=[
                AzureDevOpsBrowseRepoTool(local_repo_mgr=self.local_repo_mgr),
            ],
        )

        # Developer — kod kalitesi/karmasiklik degerlendirmesi, max_iter=2
        dev = Agent(
            config=self._agent_config_with_knowledge(
                "senior_developer", "backend_feature_dev", "frontend_nextjs"
            ),
            llm=_create_local_llm(4096, _local_coder_model()),
            verbose=True,
            max_iter=2,
            tools=[],
        )

        # Her kickoff task'ina log callback ekle — hangi agent ne zaman bitti gorunsun
        import logging as _logging
        import time as _time
        _kickoff_log = _logging.getLogger("pipeline")
        _kickoff_start = _time.time()

        _task_names = {
            "kickoff_ba_task": "BA Analiz",
            "kickoff_arch_task": "Architect",
            "kickoff_dev_task": "Developer",
            "kickoff_sm_close_task": "SM Tutanak",
        }

        def _make_kickoff_cb(task_key):
            orig_cb = self._make_task_callback(task_key)
            label = _task_names.get(task_key, task_key)
            def cb(output):
                elapsed = _time.time() - _kickoff_start
                raw = (output.raw or "")[:120] if hasattr(output, "raw") else str(output)[:120]
                _kickoff_log.info(f"  ✅ Kickoff [{label}] tamamlandi ({elapsed:.0f}s) — {raw}")
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
