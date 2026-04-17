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


def _create_local_llm(max_tokens: int = 4096) -> LLM:
    """Ollama uzerinden local LLM. Qwen 2.5-coder:7B varsayilan.
    CREW_LOCAL_LLM_MODEL env ile degistirilebilir."""
    model = os.environ.get("CREW_LOCAL_LLM_MODEL", "qwen2.5-coder:7b")
    return LLM(
        model=f"ollama/{model}",
        base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        max_tokens=max_tokens,
    )


def _use_local() -> bool:
    """Basit agent'lar (BA/QA/UAT/Reviewer/SM) local LLM kullansin mi?"""
    return os.environ.get("CREW_USE_LOCAL_LLM", "").lower() in ("1", "true", "yes")


# Architect: analiz ve plan - daima Claude Sonnet (karmasik, local model yetmez)
LLM_ARCHITECT = lambda: _create_llm("vertex_ai/claude-sonnet-4-6", max_tokens=8192)
# Developer: kod yazma - daima Claude Sonnet
LLM_DEVELOPER = lambda: _create_llm("vertex_ai/claude-sonnet-4-6", max_tokens=8192)
# Asagidakiler local Qwen ile calisabilir (CREW_USE_LOCAL_LLM=1 ile aktif edilir)
LLM_REVIEWER = lambda: _create_local_llm(4096) if _use_local() else _create_llm("o4-mini", max_tokens=4096)
LLM_ANALYST = lambda: _create_local_llm(4096) if _use_local() else _create_llm("o4-mini", max_tokens=4096)
LLM_QA = lambda: _create_local_llm(4096) if _use_local() else _create_llm("o4-mini", max_tokens=4096)
LLM_UAT = lambda: _create_local_llm(4096) if _use_local() else _create_llm("o4-mini", max_tokens=4096)
LLM_SCRUM = lambda: _create_local_llm(2048) if _use_local() else _create_llm("o4-mini", max_tokens=2048)


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
            config=self.agents_config["scrum_master"],
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
            config=self.agents_config["business_analyst"],
            llm=self.llm_analyst,
            verbose=True,
            max_iter=5,
            tools=[
                AzureDevOpsGetWorkItemTool(),
                AzureDevOpsAddCommentTool(),
            ],
        )

    def software_architect(self) -> Agent:
        tools = [
            AzureDevOpsGetWorkItemTool(),
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
        return Agent(
            config=self._agent_config_with_knowledge(
                "software_architect", "backend_tech_design", "frontend_nextjs"
            ),
            llm=self.llm_architect,
            verbose=True,
            max_iter=15,
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
            config=self.agents_config["uat_specialist"],
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
        """Plana gore kod yazan agent."""
        tools = [
            AzureDevOpsBrowseRepoTool(local_repo_mgr=self.local_repo_mgr),
            AzureDevOpsSearchCodeTool(local_repo_mgr=self.local_repo_mgr),
        ]
        # Step 5'te hedef repo code chunk'lari embed ediliyor — developer semantic search kullanabilir
        if self._vector_ready():
            tools.append(SemanticCodeSearchTool(vector_store=self.vector_store))
        return Agent(
            config=self._agent_config_with_knowledge(
                "senior_developer", "backend_feature_dev", "frontend_nextjs"
            ),
            llm=self.llm_developer,
            verbose=True,
            max_iter=8,
            tools=tools,
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

    def create_analysis_crew(self) -> Crew:
        """Software Architect: is kalemini oku, repo'yu incele, teknik tasarim olustur."""
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
        Parse hatasinda retry icin kullanilir. Agent tool cagrisi yapamaz."""
        arch = Agent(
            config=self._agent_config_with_knowledge(
                "software_architect", "backend_tech_design"
            ),
            llm=self.llm_architect,
            verbose=True,
            max_iter=3,  # Tool yok, 1-2 iter yeterli
            tools=[],  # Bos liste — agent hicbir tool cagiramaz
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
        """Developer: tek dosya icin degisiklik uygula."""
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
