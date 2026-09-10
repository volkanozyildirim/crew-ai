# Scrum İşlevleri — Tasarım (program + Faz 1–2 ayrıntısı)

**Tarih:** 2026-09-10 · **Durum:** Faz 1 PR #8 · Faz 2 uygulanıyor · **Kaynak karar:** kullanıcı,
"agile metodolojinin tüm fonksiyonlarını sisteme eklemeliyiz" → boşluk analizi →
"önerine göre ilerle".

## Neden

Pipeline bir WI için uçtan uca teslimat yapıyor ama Scrum'ın çerçevesi eksik:
sprint planlama, günlük özet, retrospektif, tahminleme, WI yaşam döngüsü,
Definition of Done, Product Owner rolü, iş tipine göre akış. Boşluk analizi
(2026-09-09) mevcut/eksik haritasını çıkardı; bu doküman programı beş faza
böler ve Faz 1'i uygulanabilir ayrıntıda tasarlar.

## Program (öncelik sırası)

| Faz | Kapsam | Ölçülebilir çıktı |
|---|---|---|
| **1** | **WI yaşam döngüsü + DoD** — durum geçişleri, isteğe bağlı atama, deterministik DoD listesi | Board pipeline'ı yansıtır; DoD tablosu tamamlanma yorumunda; UAT REJECTED artık görünür |
| **2** | **Tahminleme + alt iş kaydı** — BA `estimate` + yapısal sinyaller → Fibonacci SP (yalnızca yükselir); parent tipi WI için plan → child Task | `jobs.estimate_sp`, tamamlanma yorumunda tahmin/gerçekleşen; WI'da SP (boşsa); child Task'lar board'da |
| 3 | Retrospektif — sprint sonu öğrenme raporu (review red nedenleri, build kırılmaları, needs_info, maliyet, süre) → kickoff kılavuz kurallarına besleme | Sprint kapanışında `.md`/`.pptx` + önerilen kurallar |
| 4 | Sprint Planning + Daily — velocity tabanlı seçim, bağımlılık sırası, günlük Telegram/WI özeti | Board'dan "sprint'i kuyrukla"; 09:00 günlük özet |
| 5 | İş tipine göre akış + PO ajanı — Bug (reproduce → fix → regresyon testi), Spike (kodsuz araştırma), PO değer/öncelik kararı | Tip bazlı router; PO çıktısı kickoff'a girdi |

Her faz kendi PR'ı; her davranış env/dashboard ile açılıp kapanır (proje kuralı:
risk/maliyet etkisi varsa **varsayılan kapalı**).

## Faz 1 — WI yaşam döngüsü + Definition of Done

### Gözlem (gerçek veri)

- Pipeline `System.State`'e hiç yazmıyor (`grep update_work_item flow.py` → 0).
  WI 73121: job #189 PR açtı, review + build yeşil; durumu insanlar taşıdı
  ("Ready for Production", reason "Moved out of state UAT"). WI 73061: "QA To Do",
  yine elle.
- Job #189 `completed` bitti; UAT raporu **REJECTED** (AC2 FAIL — Rusça çeviri
  yok). Terminal sözleşme (review onayı + build yeşil) UAT'ı hesaba katmıyor;
  tamamlanma yorumunda görünmüyor.
- Takımın süreci özel: Task/Bug için `Backlog, To Do, In Progress, Code Review,
  QA To Do, QA, UAT, Preprod Check, Blocked, Ready for Production (Resolved),
  Done, Canceled`; User Story'de `QA To Do` yok, `Prod Check` var; Issue yalnızca
  `Active/Closed`. Durum adları **sabit kodlanamaz**.

### Tasarım

**Modül:** `src/agile_sdlc_crew/wi_lifecycle.py` — saf fonksiyonlar + ince Azure
sarmalayıcı. LLM yok.

**Durum seçimi (süreçten bağımsız):** olay → tercih listesi; takımın
`workitemtypes/{type}/states` listesinde ilk eşleşen ad seçilir.

| Olay | Nerede | Tercih sırası |
|---|---|---|
| `start` | step1 WI okunduktan sonra (HAL yolunda `hal_planning` başı) | In Progress, Active, Doing, … |
| `review` | step7 PR oluştu / mevcut PR yeniden kullanıldı | Code Review, In Review, Review, In Progress |
| `wait` | `NeedsMoreInfo` / `NeedsHumanReview` (main.run_pipeline except) | Blocked, On Hold, Waiting |
| `handoff` | step11 DoD geçti | QA To Do, Ready for QA, Ready for Test, QA, Testing, Resolved |
| revert | genel hata, PR **yoksa** | başlangıç durumu |

**Sahiplik aralığı (güvenlik kuralı):** geçiş yalnızca WI şu an *Proposed*
kategorisinde ya da `In Progress / Code Review / Blocked`'ta ise yapılır. QA,
UAT, Preprod, Ready for Production, Done'daki bir WI'a **dokunulmaz** (insan
ilerletmiş). Hedef == mevcut ise no-op. Süreç geçişe izin vermezse Azure 400
döner → loglanır, pipeline devam eder.

**Atama (isteğe bağlı):** `CREW_WI_ASSIGN_IF_EMPTY` açıksa ve WI atanmamışsa
`start`'ta PAT sahibine (`connectionData.authenticatedUser`) atanır. Dolu
atama asla değiştirilmez.

**Definition of Done (deterministik):**

| Madde | Kaynak | Zorunlu |
|---|---|---|
| Kod incelemesi onaylandı | `_review_approved(review_text)` | evet |
| Açık review maddesi yok | `state.review_issues` status≠closed sayısı | evet (yapısal takip açıksa) |
| PR test build'i yeşil | `state.build_status` (yeni alan; gate yazar) | evet; `no_pipeline/disabled/skipped` → ⚪ doğrulanamadı |
| UAT kabul etti | `parse_uat(uat_text)`: Overall ACCEPTED ve 0 FAIL | evet |
| Test dosyası dahil | `all_pushes` yollarında test deseni | `CREW_REQUIRE_TESTS` ise |
| PR iş kaydına bağlı | `state.pr_id` | evet |

Sonuç: zorunlu maddelerden biri **açıkça ❌** ise DoD geçilemedi (⚪ bloklamaz,
raporlanır). Tablo tamamlanma yorumuna eklenir (`_md_to_html` tabloyu bilir).
`CREW_DOD_ENFORCE` açıksa geçilemeyen DoD → `needs_human` + `NeedsHumanReview`
(PR açık kalır); kapalıysa iş `completed`, tablo uyarır. Varsayılan kapalı: UAT
ajanı yalnızca PR diff'ini görüyor, #189'daki AC2 FAIL'i reviewer R1 gibi yanlış
pozitif olabilir; zorlamadan önce birkaç koşuda tablo izlenmeli.

### Yapılandırma

| Knob | Tip | Varsayılan | Not |
|---|---|---|---|
| `CREW_WI_LIFECYCLE` | bool | **kapalı** | Takımın board'una yazar → açık rıza |
| `CREW_WI_ASSIGN_IF_EMPTY` | bool | kapalı | Yalnızca boş atamayı doldurur |
| `CREW_DOD_CHECKLIST` | bool | **açık** | Yalnızca yorum; risk yok |
| `CREW_DOD_ENFORCE` | bool | kapalı | DoD ❌ → needs_human |

### Dokunulan yerler

- `PipelineState`: `wi_type, wi_state_initial, wi_state_current, wi_states,
  wi_assigned_to, wi_assigned_by_pipeline, build_status, dod`.
- `flow.py`: `_wi_begin(fields)`, `_wi_transition(event)`,
  `wi_lifecycle_on_exception(exc)`; step1/hal_planning/step7/pr_build_gate/step11.
- `main.run_pipeline` except: önce yaşam döngüsü kancası, sonra `fail_job`.
- `AzureDevOpsClient`: `get_work_item_type_states`, `set_work_item_state`,
  `get_authenticated_user`, `assign_work_item`.
- Testler: `tests/test_katman0_gates.py` 27–29 (FLO süreç listesiyle geçiş
  planı, sahiplik/revert, DoD #189 fixture'ı, stub client kaydı).

### Yapılmayanlar (bilinçli)

- WI'ı `Done`'a taşımak — Done, prod doğrulamasından sonra insanın kararı.
- Dry-run'da hiçbir Azure yazımı (mevcut kural).
- `System.Reason` / özel geçiş alanları — süreç isterse 400 loglanır; ihtiyaç
  çıkarsa faz 1.1.

## Faz 2 — Tahminleme + alt iş kaydı (2026-09-10)

### Gözlem (Azure, son 45 gün, E-commerce Logistic Operations)

| Tip | n | StoryPoints dolu | Effort/OriginalEstimate | Parent'lı |
|---|---|---|---|---|
| Task | 153 | 89 | 0 | 111 |
| Bug | 20 | 9 | 0 | 1 |
| User Story | 27 | 0 | 0 | 25 |

Takım SP'yi **Task** seviyesinde tutuyor; hiyerarşi User Story → Task. Pipeline'ın koştuğu WI'lar Task (73121, 73061; SP boş).

### Tasarım

**Tahmin (`estimation.py`):** BA JSON'una `estimate {story_points ∈ Fibonacci, confidence, rationale(TR)}` eklendi (kural İngilizce, metin Türkçe). Python: `parse_ba_estimate` · `structural_estimate(n_req, n_files, explored, stage)` (req: ≤3→2, ≤5→3, ≤8→5, >8→8; plan: ≥3 dosya +1, ≥6 dosya +2, keşif +1 basamak; tavan 13) · `reconcile` = max(BA, yapısal, önceki) → Fibonacci, **yalnızca yükselir**. İki aşama: step1 kaba, step4 kesin (normal/resume/HAL). `jobs.estimate_sp` kolonu; tamamlanma yorumunda "Tahmin 5 SP (M, BA) · Gerçekleşen 17 dk · $3.96" (Faz 3 retrospektif verisi). Yazma: `CREW_WI_WRITE_ESTIMATE` açık ve WI'da SP boşsa `StoryPoints` (tipte yoksa `Effort`).

**Alt iş (`wi_children.py`):** yalnızca parent tipi WI (User Story, Bug, Feature, Epic, Improvement). Plan değişikliği başına child Task (`[repo] Düzenle Dosya.php — açıklama`, `crew-generated` etiketi, alan/iterasyon parent'tan, Hierarchy-Reverse ilişkisi); `CREW_WI_CHILD_TASKS_MAX` aşılırsa dizine göre grup. Parent'ta üretilmiş child varsa yeniden açılmaz (idempotent). step6'da dosya push edildikçe ilgili child `complete` tercihine göre kapanır (Done/Closed/…).

### Yapılandırma

| Knob | Tip | Varsayılan |
|---|---|---|
| `CREW_ESTIMATE` | bool | **açık** (yalnızca log/DB/yorum) |
| `CREW_WI_WRITE_ESTIMATE` | bool | kapalı |
| `CREW_WI_CHILD_TASKS` | bool | kapalı |
| `CREW_WI_CHILD_TASKS_MAX` | int | 8 |

### Dokunulan yerler
`PipelineState.{wi_story_points, wi_area_path, wi_iteration_path, estimate, child_tasks}` · `flow._estimate / _after_plan_finalized / _create_child_tasks / _complete_child_task` · `AzureDevOpsClient.create_work_item / get_work_item_children` · `db.jobs.estimate_sp` · `tasks.yaml` BA estimate bloğu · KN-38, KN-39 · testler 30–32.

### Yapılmayanlar (bilinçli)
- Task tipi WI'a child açmak; child'lara SP dağıtmak (takım child SP'yi elle giriyor).
- Kickoff "Open Tasks / Stories" serbest metninden WI üretmek (LLM metninden board kaydı → gürültü riski).
- Tahmini WI yorumu olarak yazmak (tamamlanma yorumundaki satır yeter; ayrı yorum gürültü).
