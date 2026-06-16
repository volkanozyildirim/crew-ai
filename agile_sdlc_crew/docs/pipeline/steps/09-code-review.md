# 09 — review_pr_task (Kod İnceleme)

## Kimlik
- **step_key:** `review_pr_task`
- **Flow metodu:** `step8_code_review` + `_review_retry_loop` + `_prefetch_pr_changes_context` (`flow.py`)
- **Ajan:** `code_reviewer`
- **Görünen ad:** Kod İnceleme
- **Tetikleyici:** `@listen(step7_create_pr)`
- **Sonraki:** `step9_test_planning` + `step10_uat` (paralel)

## Ne yapar
İki iş yapar: (1) önceki PR yorumlarına yanıt verir + resolve eder, (2) PR'ı
kabul kriterleri / business alignment / minimality / **regression** / SOLID
açısından inceler. Reviewer RED verirse otomatik **düzeltme döngüsüne** girer.

## Girdi
- `state.pr_id`, `state.pr_url`, `state.repo_name`, `state.branch_name`
- `state.requirements_text`, `state.acceptance_criteria` (context'te bağlayıcı)
- `self._pr_threads_to_respond` (adım 01'den)
- env/knob: `CREW_REVIEW_MAX_RETRIES` (default 1), `CREW_SM_REVIEW`

## İşleyiş
1. **Dry-run** (KN-23): review atlanır.
2. **PR yorumlarına yanıt:** resolve edilmemiş insan yorumları —
   dosya düzeltildiyse "Düzeltildi" + resolve; plan dışıysa açıklama; genel
   yorumsa plan özeti.
3. **PR pre-fetch** (KN-34, `_prefetch_pr_changes_context`): değişen dosyaların
   feature-branch içerikleri "PR DEĞİŞİKLİKLERİ" bloğu olarak context'e eklenir →
   reviewer get_pr_changes/browse_repo çağırmadan inceler (claude_cli'da daha az
   subprocess adımı = daha hızlı). Retry döngüsünde de uygulanır.
4. **Kod inceleme** — HAL dalı `self._hal.followup`, CrewAI dalı `create_review_crew`.
5. **SM Review** (KN-09).
6. **Verdict kontrolü** (KN-30): çıktıda `CHANGES_REQUIRED` /
   `REJECTED` / Türkçe eşdeğerleri aranır.
7. **Retry döngüsü** (KN-31, `_review_retry_loop`): RED ise reviewer'ın bahsettiği
   dosyalar yeniden implement edilip push edilir, tekrar review yapılır. Maks.
   `CREW_REVIEW_MAX_RETRIES` (default 1) deneme; aşılırsa WI'ya hata yorumu + pipeline durur.

## Çıktı
- `state.review_text`
- WI yorumu (onay / düzeltme gerekli / başarısız)
- DB + vector: `review_pr_task`

## Karar noktaları
- **KN-09** — SM Review. Bkz. [decision-points.md#kn-09](../decision-points.md#kn-09)
- **KN-23** — Dry-run. Bkz. [decision-points.md#kn-23](../decision-points.md#kn-23)
- **KN-30** — Reviewer verdict tespiti (token'lar). Bkz. [decision-points.md#kn-30](../decision-points.md#kn-30)
- **KN-31** — Review retry döngüsü + max retry (default 1). Bkz. [decision-points.md#kn-31](../decision-points.md#kn-31)
- **KN-34** — Review PR pre-fetch (claude_cli adım azaltma). Bkz. [decision-points.md#kn-34](../decision-points.md#kn-34)
- **KN-22** — Budget guard. Bkz. [decision-points.md#kn-22](../decision-points.md#kn-22)

## Resume / dry-run
- Resume yok (review her seferinde çalışır).
- Dry-run: PR yok, review atlanır.

## Kaynak
- `flow.py` (`step8_code_review`, `_review_retry_loop`, `_prefetch_pr_changes_context`)
- `tasks.yaml` (`review_pr_task` — "PR DEĞİŞİKLİKLERİ" pre-fetch talimatı dahil)
- `agents.yaml:199-235` (`code_reviewer`)
