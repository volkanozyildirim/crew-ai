# 00 — initialize

## Kimlik
- **step_key:** — (STEP_DEFINITIONS'ta yok; Flow `@start` metodu)
- **Flow metodu:** `AgileSDLCFlow.initialize` (`flow.py:892`)
- **Tetikleyici:** `@start()` — pipeline'ın ilk adımı
- **Sonraki:** `@router route_planning_mode` → `hal_planning` veya `crew_planning`

## Ne yapar
Pipeline'ın kurulum adımı. İstemcileri (Azure DevOps, repo yöneticisi, vector
store, DB) oluşturur, tüm repoları listeler/klonlar, REPO_SUMMARY.md'leri vector
DB'ye embed eder, bu WI'nın önceki run artıklarını temizler.

## Girdi
- `state.work_item_id`, `state.job_id`
- env: `CREW_DRY_RUN`, `CREW_REPOS_DIR` (repo klonlama kökü), `CREW_VECTOR_DB`
- DB: `jobs.dry_run` (kuyruğa eklenirken set edilmiş olabilir)

## Yaptığı işler (sıra)
1. **`_reset_job_state()`** (`flow.py:884`) — tool cache sıfırla, token sayaçlarını
   sıfırla (cross-job sızıntı önleme).
2. İstemcileri kur: `AgileSDLCCrew`, `AzureDevOpsClient`, `VectorStore`,
   `LocalRepoManager`.
3. **dry_run çözümü** — DB satırı veya `CREW_DRY_RUN` env'inden (bkz. KN-01).
4. **Repoları listele + eksikleri klonla** (`flow.py:929`) — `list_repositories()`
   ile tüm repo adları `state.known_repos`'a yazılır. Var olan repolar fetch
   EDİLMEZ (hız için); sadece local'de olmayanlar klonlanır.
5. **Workspace cleanup** (`flow.py:948`) — sadece **bu WI'ya** ait artık
   `feature/<wi>` branch'i olan repolar `origin/main`'e hard reset edilir; başka
   WI'lara ait branch'lere dokunulmaz (paralel çalışma korunur). REPO_SUMMARY.md
   `clean -fd` sırasında `-e` ile korunur.
6. **REPO_SUMMARY.md embed** (`flow.py:988`) — her repo özeti vector DB'ye embed
   edilir; eksikse `generate_repo_summary` ile yeniden üretilir. Ollama'yı
   yormamak için 0.1s aralıkla sırayla gönderilir.

## Çıktı
- `state.known_repos` (tüm repo adları), `state.dry_run`
- Vector DB: tüm REPO_SUMMARY.md'ler indexlenmiş
- Local: eksik repolar klonlanmış, bu WI'nın artıkları temizlenmiş
- `_agile_crew.local_repo_mgr` ve `_agile_crew.vector_store` set edilir (ajan
  tool'ları için)

## Karar noktaları
- **KN-01** — Dry-run modu seçimi (DB satırı VEYA env). Bkz. [decision-points.md#kn-01](../decision-points.md#kn-01)
- **KN-02** — Workspace cleanup: hangi repolar reset edilir. Bkz. [decision-points.md#kn-02](../decision-points.md#kn-02)

## Resume / dry-run
- Resume yok (kurulum her zaman çalışır).
- Dry-run burada tespit edilir; sonraki adımlar buna göre push/PR atlar.

## Kaynak
- `flow.py:884-1022` (`_reset_job_state`, `initialize`)
- `flow.py:1026-1031` (`route_planning_mode` router)
