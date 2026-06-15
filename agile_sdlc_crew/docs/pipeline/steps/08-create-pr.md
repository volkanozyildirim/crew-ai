# 08 — create_pr_task (PR Oluşturma)

## Kimlik
- **step_key:** `create_pr_task`
- **Flow metodu:** `step7_create_pr` (`flow.py:2706`)
- **Ajan:** `senior_developer` (nominal)
- **Görünen ad:** PR Oluşturma
- **Tetikleyici:** `@listen(step6_implement_code)`
- **Sonraki:** `step8_code_review`

## Ne yapar
Push edilen değişiklikler için Pull Request açar. Önce **plan-push eşleşme
kontrolü** yapar (yarım PR açmaktansa iptal eder), mevcut aktif PR varsa onu
yeniden kullanır, SSL/network hataları için retry uygular.

## Girdi
- `state.all_pushes`, `state.plan`, `state.repo_name`, `state.branch_name`, `state.dry_run`

## İşleyiş
1. **Dry-run** (KN-23): PR açılmaz, placeholder URL ile geçilir.
2. **Hiç push yoksa** (KN-27): WI'ya hata yorumu, pipeline durur.
3. **Plan-push eşleşme kontrolü** (KN-27, `flow.py:2740`): coverage =
   pushed/expected. **<0.7 ise** WI'ya "Plan Eksik Uygulandı" yorumu atılıp
   pipeline durdurulur (yarım PR yok).
4. **Mevcut aktif PR kontrolü** (KN-28): branch'te zaten PR varsa yenisini açmaz
   (Azure DevOps 409 + SSL domino'sunu önler), mevcut PR'ı kullanır.
5. PR başlığı/açıklaması oluşturulur (değişiklikler + kabul kriterleri).
6. **PR oluşturma + retry** (KN-29, `flow.py:2807`): 3 deneme, exponential
   backoff. Tüm denemeler başarısızsa son şans olarak "PR aslında oluştu mu"
   sorgusu yapılır (SSL hatası yanıltabilir).

## Çıktı
- `state.pr_id`, `state.pr_url`
- DB: `jobs.pr_id`, `jobs.pr_url` güncellenir
- DB + vector: `create_pr_task`

## Karar noktaları
- **KN-23** — Dry-run (PR atla). Bkz. [decision-points.md#kn-23](../decision-points.md#kn-23)
- **KN-27** — Plan-push coverage kontrolü (<0.7 → abort). Bkz. [decision-points.md#kn-27](../decision-points.md#kn-27)
- **KN-28** — Mevcut aktif PR yeniden kullanımı. Bkz. [decision-points.md#kn-28](../decision-points.md#kn-28)
- **KN-29** — PR oluşturma retry + SSL recovery. Bkz. [decision-points.md#kn-29](../decision-points.md#kn-29)

## Resume / dry-run
- Resume yok.
- Dry-run: PR yerine local branch bilgisi, inceleme komutları loglanır.

## Kaynak
- `flow.py:2706-2855` (`step7_create_pr`)
- `pipeline.create_pull_request`, `AzureDevOpsClient.find_active_pr_by_branch`
