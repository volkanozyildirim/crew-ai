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
    """Proxy uzerinden LLM olusturur."""
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


# Architect: analiz ve plan - guclü model gerekli (repo okuma, plan olusturma)
LLM_ARCHITECT = lambda: _create_llm("vertex_ai/claude-sonnet-4-6", max_tokens=8192)
# Developer: kod yazma - guclü model + yuksek token (tam dosya ciktisi)
LLM_DEVELOPER = lambda: _create_llm("vertex_ai/claude-sonnet-4-6", max_tokens=8192)
# Reviewer: hizli model yeterli
LLM_REVIEWER = lambda: _create_llm("o4-mini", max_tokens=4096)
# Analyst: is kalemi analizi
LLM_ANALYST = lambda: _create_llm("o4-mini", max_tokens=4096)
# QA: test planlama
LLM_QA = lambda: _create_llm("o4-mini", max_tokens=4096)
# UAT: kabul testi
LLM_UAT = lambda: _create_llm("o4-mini", max_tokens=4096)
# Scrum Master: is yonetimi
LLM_SCRUM = lambda: _create_llm("o4-mini", max_tokens=2048)


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

    def scrum_master(self) -> Agent:
        return Agent(
            config=self.agents_config["scrum_master"],
            llm=self.llm_scrum,
            verbose=True,
            max_iter=50,
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
            max_iter=50,
            tools=[
                AzureDevOpsGetWorkItemTool(),
                AzureDevOpsAddCommentTool(),
            ],
        )

    def software_architect(self) -> Agent:
        return Agent(
            config=self.agents_config["software_architect"],
            llm=self.llm_architect,
            verbose=True,
            max_iter=50,
            tools=[
                AzureDevOpsGetWorkItemTool(),
                AzureDevOpsAddCommentTool(),
                AzureDevOpsBrowseRepoTool(),
                AzureDevOpsSearchCodeTool(),
                AzureDevOpsListReposTool(),
            ],
        )

    def qa_engineer(self) -> Agent:
        return Agent(
            config=self.agents_config["qa_engineer"],
            llm=self.llm_qa,
            verbose=True,
            max_iter=50,
            tools=[
                AzureDevOpsBrowseRepoTool(),
                AzureDevOpsSearchCodeTool(),
                AzureDevOpsPRChangesTool(),
            ],
        )

    def uat_specialist(self) -> Agent:
        return Agent(
            config=self.agents_config["uat_specialist"],
            llm=self.llm_uat,
            verbose=True,
            max_iter=50,
            tools=[
                AzureDevOpsGetWorkItemTool(),
                AzureDevOpsAddCommentTool(),
                AzureDevOpsPRChangesTool(),
            ],
        )

    def senior_developer(self) -> Agent:
        """Plana gore kod yazan agent."""
        return Agent(
            config=self.agents_config["senior_developer"],
            llm=self.llm_developer,
            verbose=True,
            max_iter=50,
            tools=[
                AzureDevOpsBrowseRepoTool(),
                AzureDevOpsSearchCodeTool(),
            ],
        )

    def code_reviewer(self) -> Agent:
        """PR'i inceleyen agent."""
        return Agent(
            config=self.agents_config["code_reviewer"],
            llm=self.llm_reviewer,
            verbose=True,
            max_iter=50,
            tools=[
                AzureDevOpsGetWorkItemTool(),
                AzureDevOpsAddCommentTool(),
                AzureDevOpsBrowseRepoTool(),
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
