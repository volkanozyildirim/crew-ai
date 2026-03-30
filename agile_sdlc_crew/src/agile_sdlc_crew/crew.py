"""Agile SDLC Crew - Azure DevOps entegrasyonlu yazilim gelistirme ekibi."""

import os

from crewai import Agent, Crew, Process, Task, LLM
from crewai.project import CrewBase, agent, crew, task

from agile_sdlc_crew.dashboard import StatusTracker, TASK_DISPLAY_NAMES
from agile_sdlc_crew.tools import (
    AzureDevOpsGetWorkItemTool,
    AzureDevOpsUpdateWorkItemTool,
    AzureDevOpsAddCommentTool,
    AzureDevOpsListWorkItemsTool,
    AzureDevOpsListReposTool,
    AzureDevOpsBrowseRepoTool,
    AzureDevOpsSearchCodeTool,
    CodeWriteTool,
    CodeReadTool,
)


def _create_llm() -> LLM:
    """Ortam degiskenlerinden LLM olusturur."""
    return LLM(
        model=os.environ.get("LITELLM_MODEL", "openai/gpt-4"),
        base_url=os.environ.get("LITELLM_BASE_URL"),
        api_key=os.environ.get("LITELLM_API_KEY"),
    )


# Task key -> method name mapping for callbacks
TASK_KEYS = list(TASK_DISPLAY_NAMES.keys())


@CrewBase
class AgileSDLCCrew:
    """Agile SDLC Crew - Hiyerarsik surecle Scrum Master yonetiminde."""

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    def __init__(self):
        self.llm = _create_llm()
        self.status_tracker: StatusTracker | None = None

    def set_status_tracker(self, tracker: StatusTracker):
        self.status_tracker = tracker

    def _make_task_callback(self, task_key: str):
        """Gorev tamamlandiginda dashboard'u guncelleyen callback olusturur."""
        tracker = self.status_tracker

        def callback(output):
            if tracker:
                tracker.task_completed(task_key)

        return callback

    def _notify_task_start(self, task_key: str):
        """Gorev basladiginda dashboard'u gunceller."""
        if self.status_tracker:
            self.status_tracker.task_started(task_key)

    # ── Agents ──────────────────────────────────────────

    @agent
    def business_analyst(self) -> Agent:
        return Agent(
            config=self.agents_config["business_analyst"],
            llm=self.llm,
            verbose=True,
            tools=[
                AzureDevOpsGetWorkItemTool(),
                AzureDevOpsAddCommentTool(),
                AzureDevOpsListWorkItemsTool(),
                AzureDevOpsUpdateWorkItemTool(),
            ],
        )

    @agent
    def software_architect(self) -> Agent:
        return Agent(
            config=self.agents_config["software_architect"],
            llm=self.llm,
            verbose=True,
            tools=[
                AzureDevOpsListReposTool(),
                AzureDevOpsBrowseRepoTool(),
                AzureDevOpsSearchCodeTool(),
                CodeReadTool(),
            ],
        )

    @agent
    def senior_developer(self) -> Agent:
        return Agent(
            config=self.agents_config["senior_developer"],
            llm=self.llm,
            verbose=True,
            tools=[
                AzureDevOpsBrowseRepoTool(),
                AzureDevOpsSearchCodeTool(),
                CodeWriteTool(),
                CodeReadTool(),
            ],
        )

    @agent
    def qa_engineer(self) -> Agent:
        return Agent(
            config=self.agents_config["qa_engineer"],
            llm=self.llm,
            verbose=True,
            tools=[
                AzureDevOpsBrowseRepoTool(),
                CodeReadTool(),
            ],
        )

    @agent
    def uat_specialist(self) -> Agent:
        return Agent(
            config=self.agents_config["uat_specialist"],
            llm=self.llm,
            verbose=True,
            tools=[
                AzureDevOpsGetWorkItemTool(),
                AzureDevOpsAddCommentTool(),
            ],
        )

    # ── Tasks ──────────────────────────────────────────

    @task
    def repo_discovery_task(self) -> Task:
        self._notify_task_start("repo_discovery_task")
        return Task(
            config=self.tasks_config["repo_discovery_task"],
            callback=self._make_task_callback("repo_discovery_task"),
        )

    @task
    def repo_dependency_analysis_task(self) -> Task:
        self._notify_task_start("repo_dependency_analysis_task")
        return Task(
            config=self.tasks_config["repo_dependency_analysis_task"],
            callback=self._make_task_callback("repo_dependency_analysis_task"),
        )

    @task
    def requirement_analysis_task(self) -> Task:
        self._notify_task_start("requirement_analysis_task")
        return Task(
            config=self.tasks_config["requirement_analysis_task"],
            callback=self._make_task_callback("requirement_analysis_task"),
        )

    @task
    def technical_design_task(self) -> Task:
        self._notify_task_start("technical_design_task")
        return Task(
            config=self.tasks_config["technical_design_task"],
            callback=self._make_task_callback("technical_design_task"),
        )

    @task
    def implementation_task(self) -> Task:
        self._notify_task_start("implementation_task")
        return Task(
            config=self.tasks_config["implementation_task"],
            callback=self._make_task_callback("implementation_task"),
        )

    @task
    def code_review_task(self) -> Task:
        self._notify_task_start("code_review_task")
        return Task(
            config=self.tasks_config["code_review_task"],
            callback=self._make_task_callback("code_review_task"),
        )

    @task
    def test_planning_task(self) -> Task:
        self._notify_task_start("test_planning_task")
        return Task(
            config=self.tasks_config["test_planning_task"],
            callback=self._make_task_callback("test_planning_task"),
        )

    @task
    def test_execution_task(self) -> Task:
        self._notify_task_start("test_execution_task")
        return Task(
            config=self.tasks_config["test_execution_task"],
            callback=self._make_task_callback("test_execution_task"),
        )

    @task
    def uat_preparation_task(self) -> Task:
        self._notify_task_start("uat_preparation_task")
        return Task(
            config=self.tasks_config["uat_preparation_task"],
            callback=self._make_task_callback("uat_preparation_task"),
        )

    @task
    def uat_execution_task(self) -> Task:
        self._notify_task_start("uat_execution_task")
        return Task(
            config=self.tasks_config["uat_execution_task"],
            callback=self._make_task_callback("uat_execution_task"),
        )

    @task
    def completion_report_task(self) -> Task:
        self._notify_task_start("completion_report_task")
        return Task(
            config=self.tasks_config["completion_report_task"],
            output_file="completion_report.md",
            callback=self._make_task_callback("completion_report_task"),
        )

    # ── Crew ──────────────────────────────────────────

    @crew
    def crew(self) -> Crew:
        """Hiyerarsik surecle Scrum Master yonetiminde crew olusturur."""

        # Scrum Master manager agent olarak ayri olusturulur
        # (agents listesinde olmamali - framework kisitlamasi)
        scrum_master_agent = Agent(
            config=self.agents_config["scrum_master"],
            llm=self.llm,
            verbose=True,
            allow_delegation=True,
        )

        return Crew(
            agents=self.agents,  # Sadece 5 worker agent (@agent decorated)
            tasks=self.tasks,
            process=Process.hierarchical,
            manager_agent=scrum_master_agent,
            verbose=True,
            memory=True,
        )
