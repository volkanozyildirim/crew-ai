# 06 — create_branch_task (Branch Oluşturma) + code_embedding_task

## Kimlik
- **step_key:** `create_branch_task` (+ `code_embedding_task`)
- **Flow metodu:** `step5_create_branch` (`flow.py:2293`)
- **Ajan:** `senior_developer` (nominal)
- **Görünen ad:** Branch Oluşturma / Kod Tabanı Analizi
- **Tetikleyici:** `@listen(or_(hal_planning, crew_step4_technical_design))` — iki planlama yolunun yakınsama noktası
- **Sonraki:** `step6_implement_code`

## Ne yapar
Hedef repoyu en güncel `main`'e çeker, eski feature branch artıklarını siler,
(opsiyonel) bağımlılıkları kurar, (opsiyonel) plan dosyalarını vector'e embed
eder ve `feature/<wi>` branch'ini oluşturur.

## Girdi
- `state.repo_name`, `state.plan`, `state.work_item_id`, `state.dry_run`
- env/knob: `CREW_INSTALL_DEPS` (default kapalı), `CREW_VENDOR_INDEX` (default kapalı)

## İşleyiş
1. **Repo hazırlığı** (`flow.py:2310`): `fetch origin main` → `checkout main` →
   `reset --hard origin/main`. Eski local `feature/<wi>` branch'i varsa SİLİNİR
   (önceki yanlış job commit'leri ajanları yanıltmasın).
2. **Deps install** (KN-20, `CREW_INSTALL_DEPS`): `install_dependencies` —
   composer/npm/go. vendor/ oluşur, ajanlar 3rd-party kodu okuyabilir. İlk
   install yavaş. (composer hang fix: `curl_multi_*` disable — bkz. memory.)
3. **Hedef odaklı embed** (KN-21, `CREW_VENDOR_INDEX`): tüm repo yerine sadece
   plan dosyalarının parent dizinleri + vendor allowlist embed edilir.
4. `code_embedding_task` bilgi amaçlı done işaretlenir.
5. **Branch oluştur** (KN-23):
   - Dry-run: local `checkout -B feature/<wi>` (remote API yok).
   - Normal: `create_branch` API çağrısı.

## Çıktı
- `state.branch_name` (`feature/<wi>`)
- DB: `jobs.repo_name`, `jobs.branch_name` güncellenir
- Local repo: main'e reset, yeni feature branch checkout edilmiş

## Karar noktaları
- **KN-20** — Deps install (env-toggle). Bkz. [decision-points.md#kn-20](../decision-points.md#kn-20)
- **KN-21** — Vendor/plan embed (env-toggle). Bkz. [decision-points.md#kn-21](../decision-points.md#kn-21)
- **KN-23** — Dry-run vs normal branch oluşturma. Bkz. [decision-points.md#kn-23](../decision-points.md#kn-23)

## Resume / dry-run
- Resume yok (branch hazırlığı her seferinde çalışır).
- Dry-run: branch local oluşturulur, remote API çağrısı yapılmaz.

## Kaynak
- `flow.py:2293-2417` (`step5_create_branch`)
- `tools/local_repo.py` (`install_dependencies`, `index_plan_files`)
