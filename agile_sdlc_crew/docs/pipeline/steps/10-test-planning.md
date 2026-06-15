# 10 — test_planning_task (Test Planlama)

## Kimlik
- **step_key:** `test_planning_task`
- **Flow metodu:** `step9_test_planning` (`flow.py:3046`)
- **Ajan:** `qa_engineer`
- **Görünen ad:** Test Planlama
- **Tetikleyici:** `@listen(step8_code_review)` — `step10_uat` ile **PARALEL** çalışır
- **Sonraki:** `step11_completion_report` (`and_(step9, step10)`)

> Paralellik: CrewAI Flow sync `@listen` kardeşlerini thread pool ile paralel
> çalıştırır. step9 ve step10 asla seri değildir. Bkz. memory:
> `project_crewai_flow_parallel_listeners`.

## Ne yapar
Yapılan değişiklikler için test planı üretir (happy path, hata senaryoları, edge
case, entegrasyon, mümkünse unit test kodu). Kickoff'taki QA perspektifini
başlangıç noktası olarak kullanır.

## Girdi
- `state.requirements_text`, `state.repo_name`, `state.branch_name`, `state.pr_id`
- `state.kickoff_text` (QA perspektifi context'te)
- env/knob: `CREW_SM_REVIEW`, `CREW_ENABLE_RESUME`

## İşleyiş
1. **Dry-run** (KN-23): atlanır.
2. **Resume** (KN-03): önceki test çıktısı varsa atla.
3. **HAL dalı** (`flow.py:3073`): followup ile test kodu üretip parse eder, doğrular,
   güvenlik kontrolü (test dosyası kısalmamalı) sonrası push eder.
4. **CrewAI dalı** (`flow.py:3143`): `create_test_crew` kickoff + SM Review.

## Çıktı
- `state.test_text`
- WI yorumu ("Test Planlama")
- DB + vector: `test_planning_task`

## Karar noktaları
- **KN-03** — Resume. Bkz. [decision-points.md#kn-03](../decision-points.md#kn-03)
- **KN-09** — SM Review. Bkz. [decision-points.md#kn-09](../decision-points.md#kn-09)
- **KN-23** — Dry-run. Bkz. [decision-points.md#kn-23](../decision-points.md#kn-23)

## Resume / dry-run
- Resume: test metni cache'ten okunur.
- Dry-run: atlanır (PR yok).

## Kaynak
- `flow.py:3046-3179` (`step9_test_planning`)
- `tasks.yaml:742-765` (`test_planning_task`)
- `agents.yaml:259-290` (`qa_engineer`)
