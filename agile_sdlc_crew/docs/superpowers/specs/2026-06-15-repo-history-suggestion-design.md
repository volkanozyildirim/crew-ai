# Geçmiş-İş Repo Önerisi — Tasarım (Design Spec)

**Tarih:** 2026-06-15
**Durum:** Onaylandı (tasarım) — implementasyon planı bekliyor

## Amaç

İşin hangi repoda yapılacağı kararını iyileştirmek. Başarıyla tamamlanmış geçmiş
işleri (WI içeriği + **gerçekten değişen dosya yolları/route'lar** → yapıldığı
repo) ayrı bir vector indekse al; yeni bir işte repo kararı verilmeden önce bu
indekste ara ve en olası repoyu **advisory** sinyal olarak besle. **Architect her
durumda son kararı verir** — bu yalnızca bir sinyaldir.

## Bağlam (mevcut durum)

Repo kararı pipeline'da 3 yerde veriliyor (bkz. `docs/pipeline/decision-points.md`):
- **KN-11** — kickoff repo tahmini (isim → kod grep → vector cascade), context için
- **KN-15** — discover_repos LLM önerisi (exclusive symbol > sahiplik > isim)
- **KN-17/KN-08** — technical-design'da architect'in **kesin** kararı (`state.repo_name`)

Vector store'da zaten `find_similar_jobs` (`/jobs/<wi>/<step>` scope, ham step
çıktısı) + `save_step_output` var ve KN-17 context'inde "Benzer Önceki İşler"
olarak kullanılıyor (`flow.py:319`). Mevcut mekanizma **repo etiketine göre
yapılandırılmamış**; ham çıktı embed ediyor. Bu özellik onun yerine repo-kararına
özel, route/path/içerik ile yapılandırılmış ayrı bir sinyal ekler.

## Kararlar (brainstorming'den)

| Soru | Karar |
|------|-------|
| İndeks anahtarı | **Hibrit** — WI metni + gerçek değişen dosya yolları/route'lar |
| Yazma kriteri | **Sadece başarılı PR** (review onaylı, tamamlanmış işler) |
| Bağlantı noktası | **Üçü birden** — kickoff (KN-11) + discover (KN-15) + technical-design (KN-17) |
| Karar tipi | **Advisory** — architect son kararı verir |
| Default | **Kapalı** (env ile açılır; CLAUDE.md konvansiyonu) |

## 1. Veri modeli — yeni scope `/repo-decisions`

Mevcut `/jobs/<wi>/<step>` scope'undan **ayrı**. İş başına **tek kayıt**:

- **content (embed edilen kompozit metin):**
  ```
  WI #<id>: <başlık>
  <açıklama + AC + requirements özeti>
  Değişen dosyalar: /app/Controller/X.php, /database/Migrations/...
  Route/endpoint: /api/v1/meta/get, ...
  ```
  Hibridi mevcut **hybrid search (vector + BM25)** sağlar: vector → içerik
  semantiği, BM25 → path/route token'larının birebir lexical eşleşmesi. İkinci bir
  embed/scope gerekmez.

- **metadata:** `{work_item_id, repo, pr_id, file_paths: [...], routes: [...]}`
- **categories:** `["repo-decision"]`, **importance:** 0.8

## 2. Yazma yolu (sadece başarılı PR)

**Tek hook:** `step11_completion_report` (non-dry-run). Buraya ulaşmak zaten
"PR oluştu (step7) + review onayladı (step8 abort etmedi)" demektir — review RED'de
retry tükenirse pipeline step9/10/11'e hiç gelmez. Dry-run işler indekslenmez.

**Yeni metod:** `VectorStore.index_repo_decision(work_item_id, repo, pr_id, plan, wi_content)`
- `plan.changes[].file_path` → dosya yolları
- Yollardan + WI metninden route/endpoint token çıkarımı (`/api/...`, `\w+.php` vb.,
  mevcut `flow.py` regex desenleriyle tutarlı)
- Kompozit metni oluştur, `_save_record(scope="/repo-decisions", ...)` ile yaz
- Idempotency: aynı `work_item_id` için varsa eski kaydı sil/üzerine yaz
- Hata yönetimi: try/except, başarısızlık pipeline'ı bozmaz

## 3. Okuma yolu

**Yeni metod:** `VectorStore.suggest_repo_from_history(query, path_hints=None, limit=3, exclude_wi=None)`
- `/repo-decisions` scope'unda hybrid arama (query = WI içerik + path ipuçları)
- Sonuçları **repo'ya göre grupla** — aynı repoda birden çok benzer geçmiş iş güveni
  artırır. Deterministik formül: `repo_score = max(tekil_skorlar) + 0.05 * (n - 1)`,
  `1.0`'da sınırlı (`n` = o repoyu destekleyen geçmiş iş sayısı)
- **Filtreler:** `repo ∉ known_repos` → ele; `work_item_id == exclude_wi` → ele
- **Dönen:** `[{repo, score, supporting_wis: [...], file_paths_evidence: [...]}]`
  (skora göre sıralı, boş liste mümkün)

## 4. Entegrasyon (3 nokta — hepsi advisory)

### KN-11 — kickoff (`step0_kickoff_meeting`, `flow.py:1493` cascade)
İsim-eşleşmesi (`_select_repo_by_name`) sonrası, kod-grep'ten **önce** yeni katman:
```
if not kickoff_repo and CREW_REPO_HISTORY_SUGGEST:
    sug = vector_store.suggest_repo_from_history(query, exclude_wi=wi)
    if sug and sug[0]["score"] >= MIN_SCORE:
        kickoff_repo = sug[0]["repo"]
```

### KN-15 — discover (`_run_discover_repos`, `flow.py:399`)
- Önerilen repo(lar) `candidate_repos`'a **zorla dahil** (symbol-grep deseni gibi)
- Prompt'a yeni kanıt bloğu:
  ```
  # BENZER GEÇMİŞ İŞLER (başarılı PR'lar şu repolarda yapıldı)
  - <repo>: WI#<...>, değişen dosyalar <...> (skor <...>)
  ```
  Öncelik notu: kod kanıtından zayıf, isim benzerliğinden güçlü.

### KN-17 — technical-design (`crew_step4_technical_design`, `flow.py:1870` cascade + context)
- Prefetch cascade'ine dahil (symbol-grep'ten sonra, isim eşleşmesinden önce/yanında)
- `_build_step_context` veya inline: "# Benzer Önceki İşler (Repo Kararı)" context bloğu
  (mevcut `find_similar_jobs` bloğunun yanında, repo-odaklı)

## 5. Env toggle (`pipeline_config.SCHEMA`)

- **`CREW_REPO_HISTORY_SUGGEST`** (bool, default `False`) — özellik aç/kapat;
  dashboard'dan yönetilebilir.
- **`CREW_REPO_HISTORY_MIN_SCORE`** (float, default `0.1`) — öneri eşiği
  (mevcut vector eşikleriyle uyumlu).

## 6. Geri-doldurma (backfill)

İndeks boş başlamasın. **Yeni fonksiyon:** `VectorStore.backfill_repo_decisions(db)`:
- `db.py` ile başarılı işleri çek (status tamamlanmış + `repo_name` dolu + `pr_id` dolu)
- Her iş için `technical_design_task` çıktısından planı parse et
  (`main._parse_architect_output`) → `index_repo_decision`
- Idempotent: zaten indekste olan WI'ları atla
- Tetikleme: indeks boşsa server başlangıcında bir kez **veya** elle çağrı.
  Ad-hoc script DEĞİL — proje kodunu kullanan fonksiyon.

## 7. Hata yönetimi & edge cases

- Tüm öneri/yazma çağrıları `try/except` — **pipeline'ı asla bozmaz** (mevcut desen).
- Boş indeks → öneri yok → cascade eski katmanlara düşer (davranış değişmez).
- Beraberlik → sıralı liste, architect seçer.
- Repo silinmiş → `known_repos` filtresiyle elenir.
- Aynı WI re-run → `exclude_wi` ile kendi geçmişini önermez.
- Dry-run → indekslenmez.

## Etkilenen dosyalar

- `src/agile_sdlc_crew/tools/vector_store.py` — `index_repo_decision`,
  `suggest_repo_from_history`, `backfill_repo_decisions`
- `src/agile_sdlc_crew/flow.py` — step11 yazma hook'u + KN-11/KN-15/KN-17 entegrasyonu
- `src/agile_sdlc_crew/pipeline_config.py` — 2 yeni knob
- `docs/pipeline/decision-points.md` + ilgili adım dokümanları — yeni KN (örn. KN-33)
  ve KN-11/KN-15/KN-17 güncellemeleri

## Doğrulama (test suite yok — CLAUDE.md)

- Import smoke: `.venv/bin/python -c "from agile_sdlc_crew.tools.vector_store import VectorStore"`
- Birim seviyesi: `index_repo_decision` + `suggest_repo_from_history` round-trip
  (geçici LanceDB ile bir kayıt yaz, sorgula, repo grupla)
- Toggle kapalıyken davranışın değişmediğini doğrula (cascade eski haliyle çalışır)
- Canlı uçtan uca: gerçek WI + backfill sonrası öneri loglarını izle
