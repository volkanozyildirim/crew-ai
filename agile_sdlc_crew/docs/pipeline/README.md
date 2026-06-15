# Pipeline Adım Dokümanları

Bu klasör, Agile SDLC Crew pipeline'ının her adımını ayrı ayrı belgeler. Amaç:
ihtiyaç anında (bir adımı değiştirirken, hata ararken, yeni özellik tasarlarken)
ilgili adımın ne yaptığını, neyi girdi/çıktı aldığını ve **hangi kararları nasıl
verdiğini** tek yerden okuyabilmek.

> Tek doğru kaynak koddur: `src/agile_sdlc_crew/flow.py` (orkestrasyon),
> `config/tasks.yaml` (görev talimatları), `config/agents.yaml` (ajan personaları).
> Bu dokümanlar o koddan türetilmiştir; davranış değişince dokümanı da güncelleyin.

## Akış (CrewAI yolu)

```
initialize ─► [router: HAL mı CrewAI mı?]
   └─ crew_planning ─►
        01 requirements_analysis_task   (İş Analizi — İLK çalışır)
        02 kickoff_meeting_task         (Kickoff — requirements'tan SONRA)
        03 discover_repos_task          (Repo keşfi — öneri üretir)
        04 dependency_analysis_task     (atlanır — repo özeti yeterli)
        05 technical_design_task        (Teknik tasarım — JSON plan)
        06 create_branch_task           (+ code_embedding_task, deps install)
        07 implement_change_task        (Kod yazma & push)
        08 create_pr_task               (PR oluşturma)
        09 review_pr_task               (Kod inceleme + retry döngüsü)
        ├─ 10 test_planning_task        (paralel)
        └─ 11 uat_task                  (paralel)
        12 completion_report_task       (Test VE UAT bitince)
```

HAL yolu (`--hal`): `requirements`, `discover_repos`, `dependency_analysis`
atlanır; `hal_planning` tek adımda analiz+tasarım yapıp `technical_design`'ı
doldurur, sonra `step5_create_branch`'e yakınsar. Bkz. [steps/01a-hal-planning.md](steps/01a-hal-planning.md).

## Adım dosyaları

| # | Dosya | step_key | Flow metodu | Ajan |
|---|-------|----------|-------------|------|
| 00 | [00-initialize.md](steps/00-initialize.md) | — | `initialize` | — |
| 01 | [01-requirements-analysis.md](steps/01-requirements-analysis.md) | `requirements_analysis_task` | `crew_step1_requirements` | business_analyst |
| 01a | [01a-hal-planning.md](steps/01a-hal-planning.md) | (çoklu) | `hal_planning` | HAL |
| 02 | [02-kickoff-meeting.md](steps/02-kickoff-meeting.md) | `kickoff_meeting_task` | `step0_kickoff_meeting` | scrum_master (+4 persona) |
| 03 | [03-discover-repos.md](steps/03-discover-repos.md) | `discover_repos_task` | `_run_discover_repos` | software_architect |
| 04 | [04-dependency-analysis.md](steps/04-dependency-analysis.md) | `dependency_analysis_task` | (atlanır) | software_architect |
| 05 | [05-technical-design.md](steps/05-technical-design.md) | `technical_design_task` | `crew_step4_technical_design` | software_architect |
| 06 | [06-create-branch.md](steps/06-create-branch.md) | `create_branch_task` | `step5_create_branch` | senior_developer |
| 07 | [07-implement-code.md](steps/07-implement-code.md) | `implement_change_task` | `step6_implement_code` | senior_developer |
| 08 | [08-create-pr.md](steps/08-create-pr.md) | `create_pr_task` | `step7_create_pr` | senior_developer |
| 09 | [09-code-review.md](steps/09-code-review.md) | `review_pr_task` | `step8_code_review` | code_reviewer |
| 10 | [10-test-planning.md](steps/10-test-planning.md) | `test_planning_task` | `step9_test_planning` | qa_engineer |
| 11 | [11-uat.md](steps/11-uat.md) | `uat_task` | `step10_uat` | uat_specialist |
| 12 | [12-completion-report.md](steps/12-completion-report.md) | `completion_report_task` | `step11_completion_report` | scrum_master |

## Karar noktaları

Tüm adımlardaki dallanma/karar mantığı tek dokümanda toplanmıştır:
[**decision-points.md**](decision-points.md). Her karar noktası `KN-NN` koduyla
etiketlidir; adım dosyalarındaki "Karar noktaları" bölümlerinden bu koda referans
verilir.

## Adım dosyası şablonu

Her adım dosyası şu bölümleri içerir:
- **Kimlik** — step_key, flow metodu, ajan, görünen ad, tetikleyici (`@listen`)
- **Ne yapar** — adımın amacı
- **Girdi** — okuduğu state alanları, context blokları, env knob'ları
- **Çıktı** — güncellediği state, WI yorumu, DB kaydı, vector store
- **Karar noktaları** — bu adımdaki dallanmalar (KN kodlarıyla)
- **Resume / dry-run davranışı**
- **Kaynak** — `flow.py` / `tasks.yaml` / `agents.yaml` satır referansları
