# 01a — hal_planning (HAL alternatif planlama yolu)

## Kimlik
- **step_key:** birden çok adımı kapatır (`requirements_analysis_task`,
  `discover_repos_task`, `dependency_analysis_task`, `technical_design_task`)
- **Flow metodu:** `hal_planning` (`flow.py:1035`)
- **Tetikleyici:** `@listen("hal_planning")` — router `state.use_hal` true ise buraya yönlendirir
- **Sonraki:** `step5_create_branch` (convergence)

## Ne yapar
CrewAI yerine HAL servisini kullanan alternatif planlama yolu (`--hal` veya
`use_hal=true`). Tek adımda analiz + teknik tasarımı yapar: HAL work item'ı
analiz eder, repo adını ve değişiklik listesini döndürür.

## İşleyiş
1. `HALClient.login()` + `analyze_work_item()`.
2. **Repo çözümü** (`_resolve_repo_name`, `flow.py:1054`) — HAL'in döndürdüğü repo
   adı known_repos'a göre çözülür.
3. Plan oluşturulur (`changes[]`). Değişiklik yoksa **aynı sohbette followup**
   ile detay istenir (KN-07, `flow.py:1081`).
4. İlk 3 adım (`requirements`, `discover_repos`, `dependency_analysis`) **atlanmış**
   olarak işaretlenir (`flow.py:1101`).
5. `_enrich_plan_with_agent` ile eksikler tamamlanır → `technical_design_task` done.
6. WI'ya "Analiz & Teknik Tasarım" yorumu eklenir.

## Çıktı
- `state.repo_name`, `state.plan`, `state.requirements_text`
- `self._hal` set edilir (sonraki adımlar — review/test/uat — HAL followup kullanır)

## Karar noktaları
- **KN-07** — HAL: değişiklik bulunamazsa followup ile tekrar sor. Bkz. [decision-points.md#kn-07](../decision-points.md#kn-07)
- **KN-08** — Repo adı çözümü (`_resolve_repo_name`). Bkz. [decision-points.md#kn-08](../decision-points.md#kn-08)

## Not
HAL yolu seçildiğinde sonraki adımların (review/test/uat) bir kısmı CrewAI crew
yerine `self._hal.followup()` çağrısı kullanır — ilgili adım dosyalarında "HAL
dalı" olarak belirtilmiştir.

## Kaynak
- `flow.py:1035-1126` (`hal_planning`)
- `main._resolve_repo_name`, `main._enrich_plan_with_agent`
