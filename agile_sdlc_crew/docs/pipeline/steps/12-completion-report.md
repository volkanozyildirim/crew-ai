# 12 — completion_report_task (Tamamlanma Raporu)

## Kimlik
- **step_key:** `completion_report_task`
- **Flow metodu:** `step11_completion_report` (`flow.py:3249`)
- **Ajan:** `scrum_master`
- **Görünen ad:** Tamamlanma Raporu
- **Tetikleyici:** `@listen(and_(step9_test_planning, step10_uat))` — Test VE UAT bitince
- **Sonraki:** — (pipeline sonu)

## Ne yapar
Tüm süreci özetleyen tamamlanma raporu üretir (gereksinimler, yapılan
değişiklikler, kod inceleme, test, UAT, PR linki) ve WI'ya yorum olarak ekler.
Dry-run'da WI yerine local rapor dosyası yazar.

## Girdi
- `state.review_text`, `state.test_text`, `state.uat_text`, `state.pr_id`, `state.pr_url`
- `state.plan`, `state.acceptance_criteria` (dry-run raporu için)

## İşleyiş
1. **Dry-run** (KN-32): `_write_dry_run_report` — `<repo>/.dry_run_<job_id>.md`
   dosyası (WI özeti, plan, commit edilen dosyalar, `git diff main..branch`).
   WI'ya yorum EKLENMEZ.
2. **HAL dalı:** followup ile rapor.
3. **CrewAI dalı:** `create_completion_crew` kickoff.
4. **Definition of Done** (KN-36, `CREW_DOD_CHECKLIST`): `wi_lifecycle.evaluate_dod`
   — review onayı, açık madde, `state.build_status`, UAT PASS/FAIL, test dosyası,
   PR — deterministik ✅/❌/⚪ tablosu; `state.dod`'a yazılır.
5. WI'ya "Tamamlanma Raporu" yorumu eklenir (DoD tablosu dahil).
6. DoD ❌ ve `CREW_DOD_ENFORCE` (KN-37) → `needs_human` + `NeedsHumanReview`
   (PR açık kalır). Aksi hâlde `_wi_transition("handoff")` → WI QA'ya devredilir
   (KN-35, `CREW_WI_LIFECYCLE` açıksa).

## Çıktı
- `state.completion_text`
- WI yorumu (normal) veya local rapor dosyası (dry-run)
- DB + vector: `completion_report_task`

## Karar noktaları
- **KN-32** — Dry-run rapor dosyası vs WI yorumu. Bkz. [decision-points.md#kn-32](../decision-points.md#kn-32)
- **KN-35** — WI yaşam döngüsü `handoff` geçişi (Code Review → QA To Do / QA). Bkz. [decision-points.md#kn-35](../decision-points.md#kn-35)
- **KN-36** — Definition of Done değerlendirmesi. Bkz. [decision-points.md#kn-36](../decision-points.md#kn-36)
- **KN-37** — DoD geçilemezse `needs_human` (zorlama). Bkz. [decision-points.md#kn-37](../decision-points.md#kn-37)
- **KN-38** — Yorumun başında "Tahmin N SP (sınıf, kaynak) · Gerçekleşen X dk · $Y" satırı (`estimation.render_estimate_line`; `jobs.started_at` + gerçek maliyet). Bkz. [decision-points.md#kn-38](../decision-points.md#kn-38)

## Resume / dry-run
- Resume yok (rapor her seferinde son state'ten üretilir).
- Dry-run: local `.dry_run_<job_id>.md` dosyası.

## Kaynak
- `flow.py:3249-3395` (`step11_completion_report`, `_write_dry_run_report`)
- `tasks.yaml:810-822` (`completion_report_task`)
- `agents.yaml:237-257` (`scrum_master`)
