# Sprint Report Generator — Design

- **Date:** 2026-07-03
- **Status:** Implemented (2026-07-03). First real deck generated for `E-commerce Logistic Operations / 2026_13_Sudo` (17 groups, 40 items, AI summaries).
- **Author:** Volkan Özyıldırım (with Claude Code)

## 1. Goal

Add a **"📊 Sprint Raporu"** button to the dashboard board that, for the
currently-selected team + sprint, produces a downloadable PowerPoint (`.pptx`)
matching the FLO GROUP sprint-review template.

The auto-generated content is the **"Sprint Özeti — Neler Yaptık"** slides:
work items grouped by their **parent** (Epic/Feature = "project"), each with a
rolled-up status suffix and a bulleted summary of the work done. Everything else
(branding, team slide, thanks slide) comes from the template; the two metric
slides are left as titled-but-empty placeholders for the user to paste Azure
Analytics screenshots into, exactly as they do today.

### Reference template

`~/Downloads/2026_06- E-Commerce Logistic Operations.pptx` — 8 slides, 16:9,
white background, FLO orange (`#E97132`), Aptos font:

1. **Title** — orange concentric rings + FLO GROUP logo; sprint name + date range.
2. **"Story Points - Burndown Chart"** — pasted Azure Analytics screenshots.
3–5. **"Sprint Özeti - Neler Yaptık"** — numbered projects, each a bold title +
   status suffix (`– Devam Ediyor` / `– Tamamlandı` / `– Ek Gereksinimler …`)
   plus indented bullets describing work done. **This is the auto-generated part.**
6. **"Son 3 aylık değerlendirme"** — Azure Analytics trend screenshot.
7. **"Ekip"** — SUDO AGILE TEAM org chart with photos.
8. **"Teşekkürler."**

## 2. Decisions (locked)

| # | Decision | Choice |
|---|----------|--------|
| D1 | Output format | Editable `.pptx` cloned from the FLO template (not HTML). New dep: `python-pptx`. |
| D2 | Metric slides | Left as titled-but-empty placeholders; user pastes Azure screenshots manually. |
| D3 | Summary layer | Light AI pass (claude_cli) that summarizes **only already-stored context** into 2–4 Turkish bullets per group. On by default; env-toggle off falls back to child WI titles. |
| D4 | Template location | Local file at a configurable path (`CREW_SPRINT_REPORT_TEMPLATE`), **not committed** to the repo. Generated output `.pptx` files are gitignored. |

## 3. Feature toggles (env)

New behaviors are env-gated (repo convention). Defaults keep the feature dark
until configured.

- `CREW_SPRINT_REPORT` (default `0`) — master toggle; endpoint + button no-op / hidden when off.
- `CREW_SPRINT_REPORT_AI_SUMMARY` (default `1`) — AI bullet summarization on/off.
- `CREW_SPRINT_REPORT_VISUALS` (default `1`) — per-parent slides + domain badges.
- `CREW_SPRINT_REPORT_AZURE_CHARTS` (default `1`) — auto burndown + velocity charts from Azure Analytics.
- `CREW_SPRINT_REPORT_MIN_PCT` (default `5`) — parents above this % of sprint SP get their own slide; the rest go to "Diğer Çalışmalar".
- `CREW_SPRINT_REPORT_DIGEST_PER_SLIDE` (default `6`) — rows per "Diğer Çalışmalar" slide.
- `CREW_SPRINT_REPORT_VELOCITY_SPRINTS` (default `6`) — completed sprints in the velocity chart.
- `CREW_SPRINT_REPORT_GROUPS_PER_SLIDE` (default `3`) — density when visuals are OFF (legacy layout).

**Material paths (all outside the repo, never committed):**
- `CREW_SPRINT_REPORT_TEMPLATE` — template `.pptx` (default `~/.crew_repos/sprint_report_template.pptx`).
- `CREW_SPRINT_REPORT_TEAM_IMAGE` — team org-chart image (default `~/.crew_repos/sprint_report_team.png`).
- `CREW_SPRINT_REPORT_ICON_DIR` — generated domain-icon cache (default `~/.crew_repos/sprint_report_icons`).
- `CREW_SPRINT_REPORT_OUT_DIR` — generated decks (default `~/.crew_repos/sprint_reports`).

## 4. Template (local, not committed) — cleaned at runtime

**As built:** the template is just a **copy of the reference deck** placed at
`CREW_SPRINT_REPORT_TEMPLATE` (default `~/.crew_repos/sprint_report_template.pptx`).
No separate cleaning script — `build_pptx` cleans at generation time each run so
the template can even be the user's real deck and never drifts:

- **Keeps:** title-slide rings + FLO logo, orange decorative element, theme
  (Aptos, `#E97132`), slide layouts, the **Ekip** slide, the **Teşekkürler** slide.
- **Resets at runtime:** title text (sprint name + date range); the metric slides'
  pasted screenshots/tables are stripped (`_strip_metric_slides`, removes PICTURE
  ≥3in wide + TABLE, keeps logo + orange chrome) → titled-but-empty placeholders;
  the "Neler Yaptık" content is rebuilt from the sprint data (`_fill_content_slide`),
  extra content slides cloned (`_clone_slide`, remaps image rels) or surplus dropped,
  then slides reordered to the canonical sequence (`_reorder_slides`) and renumbered.

The file lives outside git. `.gitignore` has `sprint_report_template.pptx`,
`sprint_reports/`, and `*.pptx`.

## 5. Architecture

### 5.1 New module: `src/agile_sdlc_crew/sprint_report.py`

Single-purpose module, self-contained, testable via import. Public entry point:

```python
def generate_sprint_report(team: str, iteration_path: str,
                           client=None, db=None, vector_store=None) -> dict:
    """Build the sprint .pptx. Returns {report_id, file_path, group_count, item_count}."""
```

Internal units (each independently understandable/testable):

- `collect_sprint_items(client, iteration_path) -> list[SprintItem]`
  — sprint work items with resolved parent.
- `group_by_parent(items) -> list[ParentGroup]`
  — groups + rolled-up status; parentless items → `"Diğer / Bağımsız İşler"`.
- `gather_context(group, db, vector_store, client) -> GroupContext`
  — reuses already-stored data (see §5.3).
- `summarize_group(group_context) -> GroupSummary`
  — AI bullets (or WI-title fallback).
- `build_pptx(sprint_meta, summaries, template_path, out_dir) -> str`
  — clone template + inject slides; returns file path.

### 5.2 Data model (dataclasses)

```
SprintItem(id, title, type, state, assignee, parent_id, parent_title, parent_type)
ParentGroup(parent_id, parent_title, parent_type, status, items: list[SprintItem])
GroupContext(group, per_item_context: dict[wi_id, str])   # stored text per WI
GroupSummary(title, status, bullets: list[str])
```

### 5.3 Data pipeline & reuse (D3 steer: "already scanned → reuse it")

1. **Sprint WIs** — `client.get_iteration_work_items(iteration_path)`
   → id/title/state/type/assignee (excludes User Stories by design).
2. **Resolve parents** — WIQL selecting `[System.Parent]` for those WI ids, then a
   batch fetch of parent titles/types. Requires a small new client helper (§5.4).
   WIs with no parent → `"Diğer / Bağımsız İşler"` group.
3. **Group + status** — group by `parent_id`; status = `Tamamlandı` if every child
   is Done/Closed/Resolved, else `Devam Ediyor`.
4. **Gather "work done" context per WI — stored sources first, no re-discovery:**
   1. MySQL `db.get_cached_step_output("technical_design_task"/"requirements_analysis_task", wi)`
      — richest, for pipeline-run WIs.
   2. Vector store `/repo-decisions` (via `list_records`) — WI content, changed
      `file_paths`, `pr_id` for backfilled done WIs.
   3. Fallback: the Azure WI's own description / acceptance criteria.
5. **Summarize** — if `CREW_SPRINT_REPORT_AI_SUMMARY`, one claude_cli call per group
   turns the gathered context into 2–4 concise **Turkish** bullets + confirms status.
   Otherwise bullets = child WI titles. Cost recorded/guarded like other claude
   calls (`_track_and_check_budget` semantics; ~1 call per group).

### 5.4 Azure client helper (`tools/azure_devops_base.py`)

Add a parent-resolution helper (keeps provider logic out of the report module):

```python
def get_work_item_parents(self, work_item_ids: list[int]) -> dict[int, dict]:
    """{child_id: {"parent_id", "parent_title", "parent_type"}} via WIQL + batch get.
       Missing parent → child_id absent from the map."""
```

Reuses existing `query_work_items` / `get_work_item` patterns. `list_iterations`
already returns `startDate`/`finishDate` for the title-slide date range.

### 5.5 PPTX assembly (`python-pptx`)

- Open cleaned template: `Presentation(template_path)`.
- **Title slide:** set sprint name + `startDate – finishDate`.
- **"Neler Yaptık" slides:** clone the template's project-slide (XML-level part +
  rels copy — python-pptx has no native slide-dup) `ceil(n_groups / GROUPS_PER_SLIDE)`
  times; inject numbered `"{i}. {parent_title} – {status}"` headers + bullets.
- **Metric slides / Ekip / Teşekkürler:** untouched (already in template).
- Save to `CREW_SPRINT_REPORT_OUT_DIR/sprint_report_{team}_{iter}_{id}.pptx`.

### 5.6 Server endpoints (`server.py`)

Follow existing async + worker-thread pattern (cf. `/api/pr-fix`, `/api/backfill/*`):

- `POST /api/sprint-report` `{team, iteration_path}` → runs generation in a thread,
  returns `202 {report_id, status:"running"}`. No-op 404/409 if `CREW_SPRINT_REPORT` off.
- `GET /api/sprint-report/status?report_id=…` → `{status, download_url?, error?}`.
- `GET /api/sprint-report/download/{report_id}` → streams the `.pptx`
  (`FileResponse`, content-type `application/vnd.openxmlformats-officedocument.presentationml.presentation`).

Errors: `try/except → JSONResponse({"error": …}, status_code=500)`, consistent with
the rest of the file.

### 5.7 UI (`web/index.html`)

- New button next to "📚 Geçmiş İşleri Tara" (~line 382), shown only when the feature
  is enabled (a flag surfaced via an existing state endpoint or a cheap probe).
- Click → reads `teamSel` + selected sprint `iteration_path` → opens a small modal
  (reuse backfill-modal pattern, `#sprintReportModal`) → `POST /api/sprint-report`
  → polls `…/status` every 2s → shows an **"İndir"** link on success; `showToast()` on error.

## 6. Language convention

- Code, dataclasses, log messages, the AI **prompt/instructions**: English.
- **User-facing strings** — slide titles/headers ("Sprint Özeti — Neler Yaptık",
  status suffixes), AI-produced bullet text, modal/button labels, WI comments:
  **Turkish** (matches repo convention; AI is instructed to write bullets in Turkish).

## 7. Files touched

**New**
- `src/agile_sdlc_crew/sprint_report.py` — generation module.
- (local, not committed) cleaned template at `CREW_SPRINT_REPORT_TEMPLATE`.

**Modified**
- `src/agile_sdlc_crew/server.py` — 3 endpoints + worker wiring.
- `src/agile_sdlc_crew/tools/azure_devops_base.py` — `get_work_item_parents`.
- `src/agile_sdlc_crew/web/index.html` — button + modal + JS.
- `pyproject.toml` — add `python-pptx`.
- `.gitignore` — template + output dir patterns.

## 8. Verification (no test suite in repo)

- Import check: `.venv/bin/python -c "import agile_sdlc_crew.sprint_report"` and
  `"from agile_sdlc_crew.server import app"`.
- Unit-ish: run `group_by_parent` / status rollup against a small hand-built item list.
- End-to-end: with the feature enabled and a template in place, POST for a real
  sprint; open the resulting `.pptx` and confirm title, grouped "Neler Yaptık"
  slides, and untouched team/thanks/placeholder slides.
- Restart the server only when `/api/health` shows `running:0` (repo rule).

## 9. Out of scope (this iteration)

- Auto-computed burndown/velocity/planning charts (metric slides stay manual).
- Dynamic/auto-built team slide.
- Scheduling or emailing the deck.
- Non-Azure work-item providers.

## 10. Open items

- Exact XML slide-clone approach for python-pptx (fill during implementation; the
  template's project slide is text-only, so a part+rels deep-copy is sufficient).
- How the UI learns the feature is enabled (add a flag to an existing `…/state`
  response vs. a dedicated tiny endpoint) — decide during implementation.
