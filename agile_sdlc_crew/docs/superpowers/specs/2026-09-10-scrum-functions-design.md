# Scrum İşlevleri — Tasarım (program + Faz 1 ayrıntısı)

**Tarih:** 2026-09-10 · **Durum:** Faz 1 uygulanıyor · **Kaynak karar:** kullanıcı,
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
| 2 | Tahminleme + alt iş kaydı — S/M/L zarfı → Story Points/Effort; kickoff "Open Tasks" → child Task | WI'da SP dolu; plan dosyaları child Task olarak board'da |
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
