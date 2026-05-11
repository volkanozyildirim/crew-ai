# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

CrewAI-based agentic Agile SDLC pipeline. Takes an Azure DevOps Work Item ID and autonomously runs 13 steps from requirements analysis through PR creation, review, test planning and UAT. Two run modes: long-lived FastAPI server with a MySQL job queue and a live dashboard, or one-shot CLI. Agent prompts and task definitions are in Turkish — preserve that when editing `config/agents.yaml` and `config/tasks.yaml`.

## Common commands

```bash
# Install (editable, in a venv)
python -m venv .venv && source .venv/bin/activate
pip install -e .

# CLI: one-shot run for a WI
agile_sdlc_crew 12345           # equivalent: python -m agile_sdlc_crew.main 12345
agile_sdlc_crew 12345 --hal     # use HAL planning path instead of CrewAI

# Server: queue + dashboard at http://localhost:8765
./start.sh                       # spawns server, logs to /tmp/crew_server.log
serve                            # foreground (project script)

# Queue a job via API
curl -s -X POST http://localhost:8765/api/run \
  -H 'Content-Type: application/json' \
  -d '{"work_item_id":"12345","use_hal":false}'

# CrewAI scaffolding entry points (rarely used; pinned to analysis_crew only)
train 3 training_data.pkl
replay <task_id>
test_crew 2
```

There is no test suite, linter, or formatter wired up. Don't invent a `pytest` / `ruff` invocation — verify changes by importing modules in the venv (`.venv/bin/python -c "from agile_sdlc_crew.server import app"`) or running an end-to-end pipeline against a WI.

## Logs to read when something fails

- `/tmp/crew_server.log` — full stdout/stderr of the server process (CrewAI verbose output, LLM call panels, tracebacks)
- `/tmp/crew_pipeline.log` — pipeline-only log (rotated, 10 MB × 5)
- `/tmp/crew_access.log` — uvicorn HTTP access log
- `src/agile_sdlc_crew/web/status.json` — current job's per-agent + per-step state, written by `dashboard.StatusTracker`

The dashboard polls `status.json` every 2s; `/api/status` reads the same file.

## Architecture

### Orchestration: `flow.py` (CrewAI Flow)

`AgileSDLCFlow` is the single source of truth for the 13-step pipeline. It uses CrewAI Flow's `@start`, `@listen`, `@router`, `and_`, `or_` decorators to declare an event-driven DAG. The flow holds pydantic state (`AgileSDLCFlowState`) plus private attrs for `_client`, `_repo_mgr`, `_vector_store`, `_agile_crew`, `_tracker`, `_db`. Steps:

```
initialize ─► [router] ─► hal_planning            (─► step5)
                       └► crew_step1_requirements ─► step0_kickoff_meeting
                          ─► crew_step4_technical_design ─► step5_create_branch
                          ─► step6_implement_code ─► step7_create_pr
                          ─► step8_code_review ─► (and_) step9_test_planning + step10_uat
                          ─► step11_completion_report
```

Each step calls `self._step_start/_step_done/_step_fail/_resume_step` to update both the dashboard tracker and the MySQL `job_steps` table. Resume is supported: `_try_resume_step` reads prior successful output for the same WI from MySQL and skips re-execution. `run_pipeline()` in `main.py` is a thin wrapper that constructs the flow and calls `flow.kickoff(...)`.

### LLM selection: `llm/` package + `config/llm_profiles.yaml`

LLM choice goes through a 3-layer registry/profile system instead of inline if/elif.

- **Providers** (`llm/providers/*_provider.py`) — each is a `build(model, max_tokens, **kw) -> crewai.LLM` factory registered by name in `llm/registry.py`. Built-in: `litellm`, `anthropic`, `claude_cli`, `ollama`, `lmstudio`. Adding a new backend = one module + one line in `_bootstrap_builtin_providers`.
- **Profiles** (`config/llm_profiles.yaml`) — named `{provider, model, max_tokens, ...}` bundles like `architect_premium`, `developer_cli`, `developer_local_coder`, `reasoning_local`. Profiles are the unit of reuse; agents bind to profiles, not to providers directly.
- **Resolver** (`llm/resolver.py`) — `build_for_agent(agent_key)` resolves in this order: (1) `CREW_LLM_PROFILE_<AGENT_UPPER>` env override → (2) `llm_profile:` field in `agents.yaml` → (3) backwards-compat from `CREW_USE_LOCAL_LLM` / `CREW_LOCAL_DEVELOPER` → (4) `agent_defaults` map in profiles yaml.

The `LLM_ARCHITECT / LLM_DEVELOPER / ...` lambdas in `crew.py` are now thin `build_for_agent("software_architect")` calls. `_create_llm` and `_create_local_llm` remain as backward-compat wrappers (still used inline in `crew.py` for ad-hoc LLM creation in `create_kickoff_crew` etc.) — both delegate to the registry.

Loadbearing rules baked into the resolver and profiles:
- Architect default is **always** premium remote (`architect_premium` = `vertex_ai/claude-sonnet-4-6`). `CREW_USE_LOCAL_LLM=1` does *not* downgrade architect — explicit per-agent env or `llm_profile:` is required.
- Developer downgrades to `developer_local_coder` (qwen2.5-coder:7b) only when both `CREW_USE_LOCAL_LLM=1` and `CREW_LOCAL_DEVELOPER!=0`.
- Kickoff crew (step0) uses local Ollama unconditionally — cost-sensitive multi-task focus group.

`crew.py` monkey-patches `litellm.completion` near the top to strip the trailing assistant message (Vertex Claude doesn't support prefill) and disables SSL verification globally for httpx. Leave both alone unless you understand why.

### Work item / SCM provider abstraction: `providers/`

`providers/factory.py` returns singleton `WorkItemProvider` and `SCMProvider` based on `CREW_WORK_ITEM_PROVIDER` and `CREW_SCM_PROVIDER`. Only `azure_devops` is implemented today; jira/github/gitlab/etc. raise `NotImplementedError` from the factory. Add a new provider by implementing `providers/base.py` interfaces and registering in the factory — don't add provider-specific code to `flow.py` or `crew.py`. Most existing call sites still go through `tools/azure_devops_base.AzureDevOpsClient` directly; treat the providers package as the migration target.

### Server, queue, persistence

- `server.py` — FastAPI app. Endpoints: `/api/run` (queue), `/api/pr-fix` (run `pr_fix.run_pr_fix` in a thread), `/api/jobs[/{id}]`, `/api/jobs/{id}/retry`, `/api/status`, `/api/health`. A single background worker thread (`_ensure_worker`) drains the queue serially.
- `db.py` — MySQL via pymysql. Tables `jobs` and `job_steps` (schema in `SCHEMA` constant, auto-created). `STEP_DEFINITIONS` is the canonical step list and order; keep it in sync with the flow when adding/removing steps.
- `dashboard.py` — `StatusTracker` writes `web/status.json`; `start_dashboard_server` is the legacy stdlib HTTP server used by the CLI path. The FastAPI server in `server.py` is the production path.

### Tools (CrewAI tools the agents call)

In `tools/`. The Azure DevOps tools share `AzureDevOpsClient` (`azure_devops_base.py`); `local_repo.LocalRepoManager` clones repos under `CREW_REPOS_DIR` (default `~/.crew_repos`) and is the tool layer's git interface; `vector_store.VectorStore` embeds `REPO_SUMMARY.md` into a local vector DB at `CREW_VECTOR_DB` for semantic repo lookup; `tool_cache.py` deduplicates repeated tool calls within a single pipeline run (reset in `initialize`). `claude_cli_llm.py` is the litellm custom provider implementation, not a tool agents call.

### Cost guard

`flow.py` calls `_track_and_check_budget` after each crew kickoff; if cumulative USD exceeds `CREW_MAX_JOB_COST` (default 3.0) the pipeline aborts and posts a comment to the WI. Pricing is approximated via `CREW_PRICE_INPUT_USD_PER_M` / `CREW_PRICE_OUTPUT_USD_PER_M` (default Sonnet pricing). When changing models, update the pricing envs together.

## Notes for editing

- All step keys (e.g. `kickoff_meeting_task`) are stable identifiers used in `tasks.yaml`, `STEP_DEFINITIONS`, `dashboard.TASK_DISPLAY_NAMES`, MySQL, and `status.json` — renaming one means renaming everywhere.
- New pipeline behaviors should be env-toggleable (existing pattern: `CREW_KICKOFF_MEETING`, `CREW_SM_REVIEW`, `CREW_ANALYZE_WI_MEDIA`). Default to off if there's any cost or risk impact.
- `.env` is auto-loaded by `server.py` and `main.py` via python-dotenv from the repo root.
