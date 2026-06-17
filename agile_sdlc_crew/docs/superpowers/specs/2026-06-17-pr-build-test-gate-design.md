# PR Build/Test Gate + Test Zorunluluğu — Tasarım

**Tarih:** 2026-06-17
**Tetikleyici:** WI #66687 (repo `core`, PR #38738) — değişiklik mevcut unit testleri kırdı,
pipeline yakalamadı. Azure DevOps `Core-test` build'i `refs/pull/38738/merge` üzerinde
`FAILED` (failed task: "Run tests") ama pipeline bunu hiç görmedi.

## Amaç
Unit testi olan repolarda geliştirme yaparken: (1) mevcut testleri kırmamak (kırılırsa
düzeltmek), (2) değişen davranış için test eklemek/güncellemek **zorunlu** olsun.

## Anahtar bulgu (ampirik)
Azure DevOps her PR'da `<repo>-test` build pipeline'ını `refs/pull/{pr_id}/merge` ref'i
üzerinde **zaten çalıştırıyor** (orkestra-test, marketplace-test, Core-test, ...). Yani
testleri lokalde çalıştırmaya (composer/pytest install, DB bağımlılığı, flaky) gerek yok —
**PR build sonucunu poll edip gate yapmak** hem sağlam hem doğrudan #149'u yakalar.

## Mimari

### Yeni adım: `pr_build_gate`
Akış: `create_pr_task` → **`pr_build_gate`** → `review_pr_task`
(gate review'dan ÖNCE: testler önce yeşil olsun, sonra review).

1. PR build'ini bul: `build/builds?branchName=refs/pull/{pr_id}/merge` (en yeni).
2. Build yoksa kısa süre poll et (policy tetik gecikmesi). Hâlâ yoksa → repoda PR-test
   pipeline'ı yok → gate **atla** (yalnız reviewer'ın "test var mı" kontrolü kalır).
3. Build varsa `completed` olana kadar poll et (`CREW_PR_BUILD_POLL_INTERVAL`,
   `CREW_PR_BUILD_POLL_TIMEOUT`).
4. `result == failed | partiallySucceeded` → fix döngüsü.
5. `succeeded` → gate geçti, step_done.

### Fix döngüsü (mevcut `_review_retry_loop` desenini yeniden kullanır)
- `timeline` → başarısız Task ("Run tests") + varsa test sonuç özeti/log → hata detayı.
- Detayı developer context'ine koy; developer kaynağı VE/VEYA test dosyalarını düzeltir →
  push → build yeniden tetiklenir → tekrar poll.
- `CREW_PR_BUILD_MAX_RETRIES` (default 2) bitince RED → WI yorumu + pipeline durur.

### Test yazma zorunluluğu (CI'ın yakalamadığı "yeni test ekle" kısmı)
- **step6 implement**: repo'da test altyapısı varsa (PR build koşuyor VEYA repoda
  `phpunit.xml` / `*Test.php` / `tests/` / `*_test.go` var) → developer prompt'una
  "değişen davranış için ilgili test dosyalarını da aynı PR'da güncelle/ekle" kuralı.
- **step8 review**: reviewer test eklenip eklenmediğini kontrol eder; eksikse
  `REVIEW_DECISION: CHANGES_REQUIRED`.

### Azure client (yeni metodlar)
- `get_pr_build(repo, pr_id) -> dict | None` — branchName=refs/pull/{id}/merge ile en yeni build.
- `get_build_failure_summary(project, build_id) -> str` — timeline'daki failed task'lar +
  (varsa) test outcome özeti; %2F/encoding tuzağına dikkat.
- Repo→project çözümü mevcut `_find_repo_project` ile.

## Env toggle'lar (default KAPALI — maliyet/süre/risk)
- `CREW_PR_BUILD_GATE` (bool) — build poll + gate adımı.
- `CREW_REQUIRE_TESTS` (bool) — implement/review test zorunluluğu.
- `CREW_PR_BUILD_MAX_RETRIES` (int, 2)
- `CREW_PR_BUILD_POLL_TIMEOUT` (int sn, ör. 1200)
- `CREW_PR_BUILD_POLL_INTERVAL` (int sn, ör. 30)

## Dokunulan dosyalar
- `tools/azure_devops_base.py` — build metodları.
- `flow.py` — `pr_build_gate` adımı + @listen yeniden kablolama + fix döngüsü.
- `db.py` — `STEP_DEFINITIONS`'a yeni adım.
- `dashboard.py` — `TASK_DISPLAY_NAMES`.
- `config/tasks.yaml` — implement + review prompt güncellemesi (test zorunluluğu).
- `pipeline_config.py` (+ `config/pipeline_config.yaml`) — yeni toggle'lar.

## Test/doğrulama
- WI #66687'i yeniden çalıştır: build gate `Core-test` FAILED'i yakalamalı, fix döngüsüne
  girmeli; testler yeşil olunca review'a geçmeli.
- Build pipeline'ı olmayan bir repoda gate'in temiz atlandığını doğrula.

## Kapsam dışı (YAGNI)
- Lokal test çalıştırma (Azure CI yeterli).
- Yeni build pipeline tanımı oluşturma (repolarda zaten var).
- Test coverage yüzdesi ölçümü.
