# 11 — uat_task (UAT Doğrulama)

## Kimlik
- **step_key:** `uat_task`
- **Flow metodu:** `step10_uat` (`flow.py:3181`)
- **Ajan:** `uat_specialist`
- **Görünen ad:** UAT Doğrulama
- **Tetikleyici:** `@listen(step8_code_review)` — `step9_test_planning` ile **PARALEL**
- **Sonraki:** `step11_completion_report` (`and_(step9, step10)`)

## Ne yapar
Her kabul kriterini PR değişikliklerine karşı tek tek değerlendirir (PASS/FAIL),
genel sonucu ACCEPTED/REJECTED olarak verir. Kabul kriterleri BA tarafından
belirlendiğinden ve pipeline boyunca bağlayıcı olduğundan, context'te varsa
`get_work_item` çağrılmaz.

## Girdi
- `state.requirements_text`, `state.acceptance_criteria` (bağlayıcı), `state.pr_id`, `state.pr_url`
- `state.kickoff_text` (UAT perspektifi)
- env/knob: `CREW_SM_REVIEW`, `CREW_ENABLE_RESUME`

## İşleyiş
1. **Dry-run** (KN-23): atlanır.
2. **Resume** (KN-03): önceki UAT çıktısı varsa atla.
3. **HAL dalı:** followup ile AC PASS/FAIL.
4. **CrewAI dalı:** `create_uat_crew` kickoff + SM Review.

## Çıktı
- `state.uat_text`
- WI yorumu ("UAT Doğrulama")
- DB + vector: `uat_task`

## Karar noktaları
- **KN-03** — Resume. Bkz. [decision-points.md#kn-03](../decision-points.md#kn-03)
- **KN-09** — SM Review. Bkz. [decision-points.md#kn-09](../decision-points.md#kn-09)
- **KN-23** — Dry-run. Bkz. [decision-points.md#kn-23](../decision-points.md#kn-23)

## Resume / dry-run
- Resume: UAT metni cache'ten okunur.
- Dry-run: atlanır (PR yok).

## Kaynak
- `flow.py:3181-3245` (`step10_uat`)
- `tasks.yaml:767-804` (`uat_task`)
- `agents.yaml:292-321` (`uat_specialist`)
