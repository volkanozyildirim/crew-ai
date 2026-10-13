# Kalite İşi Alımı (New Relic + SonarQube) — Tasarım

**Tarih:** 2026-10-13 · **Durum:** tasarım onayı bekliyor · **Kaynak karar:** kullanıcı,
"sonarqube ve newrelic deki maddeleri önceliklendirip detaylarıyla işini açacak …
10 SP'lik newrelic/sonarqube taskı al diyeceğim, işlerini açacak ve çözecek".

## Neden

Kalite borcu iki yerde birikiyor ve kimse sistematik olarak almıyor:

- **SonarQube** (`https://sonarqube.flo.com.tr`, v10.7): 61 proje, 99.420 açık madde.
  Kapsamdaki dilim (BUG + VULNERABILITY, BLOCKER + CRITICAL) **863 madde**: 577'si
  `css:S4657` (webservice tema CSS'leri), ~46'sı `secrets:*`.
- **New Relic** (EU region, 158 APM uygulaması): prod uygulamalarında süregelen hatalar.
  Son 7 gün: `[Prod] tr-stocks.flo.com.tr` %50 error rate (645K, `Undefined array key "host"`),
  `[Prod] channel.sneakerbox.ru` %10.9, storefront'larda aynı `content::page` hatası 6 sitede.
  Hataların çoğu `E_WARNING Undefined array key "…"` — 1–2 SP'lik mekanik işler.

Hedef: tek komutla ("New Relic'ten 10 SP al") bu havuzdan en değerli işleri deterministik
seçmek, sprintte User Story + child Task olarak açmak ve her Task'ı pipeline'a kuyruğa almak.

## Kararlar (kullanıcı onaylı)

| Konu | Karar |
|---|---|
| Otonomi | **Tam otonom** — "al" denince onay listesi yok; WI açılır ve kuyruğa girer |
| WI yeri | `story_id` verilirse onun altına (area/iteration story'den). Verilmezse seçili takımın **şu anki sprintinde** kaynağa özel User Story bulunur ya da açılır; Task'lar onun child'ı |
| Atama | PAT sahibi (`connectionData.authenticatedUser`) |
| Sonar kapsamı | Yalnızca BUG + VULNERABILITY, severity BLOCKER + CRITICAL |
| NR → repo eşlemesi | FLODevOps projesindeki `anatolia` (TR), `global`, `russia` repolarından otomatik çıkarılır |
| Mimari | Deterministik toplayıcılar → ortak bulgu → gruplama → skor → kural tabanlı SP → bütçe seçimi → WI → kuyruk. **Seçimde LLM yok** |

## Bileşenler

Yeni paket `src/agile_sdlc_crew/quality/`:

| Birim | Görevi | Bağımlılık |
|---|---|---|
| `sonar.py` | Açık BUG/VULNERABILITY (BLOCKER/CRITICAL) maddelerini sayfalayarak çeker (`api/issues/search`, sayfa 500). Proje listesi `api/components/search?qualifiers=TRK` (`api/projects/search` admin ister). `EcomBackend:webservice` → repo `webservice`. | `SONAR_TOKEN`, `CREW_SONAR_URL` |
| `newrelic.py` | NerdGraph (`https://api.eu.newrelic.com/graphql`) üzerinden NRQL: `[Prod]` uygulamalarda `TransactionError` → `error.class`, `error.message`, `transactionName`, `request.uri`, adet; uygulama başına error rate. | `NEW_RELIC_ACCOUNT_ID`, `NEW_RELIC_API_KEY`, `NEW_RELIC_REGION` (varsayılan `eu`) |
| `app_map.py` | NR uygulama adı → repo tablosu. FLODevOps `anatolia`/`global`/`russia` repolarını (default branch `production`) **Azure API ile** okur, klon gerekmez: `build/<servis>/JenkinsfileBuild` → `gitOriginApp` (kaynak repo), `build/<servis>/Dockerfile` → `newrelic.appname="…"`; websites şablonu `"[Prod] ${website}"` → `webservice`. Sonuç `config/quality_app_map.yaml`'a önbelleklenir (24 saat); elle düzeltmeler `config/quality_app_map_overrides.yaml`'da, her zaman öncelikli. | `AzureDevOpsClient.get_file_content` (proje `FLODevOps`) |
| `model.py` | `Finding` (tek bulgu) ve `Group` (iş birimi: parmak izi, repo, kanıt listesi, skor, SP, `needs_human`, kaynak linkleri). | — |
| `rank.py` | Saf fonksiyonlar: `group_findings`, `score`, `size_sp`, `pack`, `select_budget`. Ağa çıkmaz. | `model` |
| `intake.py` | Orkestratör: topla → ele (dedup/eşlenemeyen) → seç → story bul/aç → Task'ları aç → kuyruğa al → `quality_items`'a yaz. `preview()` aynı yolu yazmadan koşar. | hepsi + `db` |

**Yüzeyler**

- `POST /api/quality/take` — `{source: "newrelic"|"sonar", sp: 10, story_id?: int, team?: str}`.
  Döner: `{story: {id, url, created}, tasks: [{id, title, sp, fingerprint, job_id|null, needs_human}],
  skipped: [{fingerprint|app|project, reason}], total_sp}`.
- `GET /api/quality/preview?source=&sp=&team=` — aynı seçim, hiçbir yazma yok. Onay adımı değil;
  test ve "ne seçerdi" sorusu için.
- `GET /api/quality/unmapped` — eşlenemeyen NR uygulamaları ve kapsam dışı Sonar projeleri.
- Dashboard: Genel bakış'a **Kalite** kartı — kaynak seçimi, SP, story ID (opsiyonel), "Al" butonu;
  altında son alımlar (story linki + Task sayısı + kuyruk durumu) ve eşlenemeyenler.

**Kalıcılık:** MySQL `quality_items` tablosu (`SCHEMA`'ya eklenir, auto-create):

```sql
CREATE TABLE IF NOT EXISTS quality_items (
  id INT AUTO_INCREMENT PRIMARY KEY,
  fingerprint VARCHAR(512) NOT NULL,
  source ENUM('sonar','newrelic') NOT NULL,
  repo VARCHAR(128),
  story_id INT, wi_id INT, job_id INT NULL,
  sp DECIMAL(4,1), score DOUBLE,
  needs_human TINYINT(1) DEFAULT 0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  INDEX (fingerprint(191)), INDEX (wi_id)
)
```

## Gruplama

**Sonar**
- Temel birim: **repo + dosya + kural**. Aynı dosyada aynı kural kaç kez geçerse geçsin tek birimdir.
- Paketleme: aynı **repo + kural** birimleri tek Task'ta toplanır; Task başına en fazla
  `CREW_QUALITY_SONAR_PACK_SP` (3) SP ve `CREW_QUALITY_SONAR_PACK_FILES` (5) dosya.
  Gerekçe: `css:S4657`'nin 577 maddesi aksi halde yüzlerce 1 SP'lik Task açardı; dar plan
  geliştiricinin güvenilirliği için 5 dosya sınırı.
- Parmak izi (paket değil, **birim** düzeyinde): `sonar:<repo>:<dosya>:<kural>`.
  Bir Task birden çok parmak izi taşır.

**New Relic**
- Anahtar: **repo + action + hata sınıfı + normalize mesaj**.
- Action, `transactionName`'den (`WebTransaction/Action/api::order::integrationCancel` →
  `Api/Order::integrationCancel`).
- Mesaj normalizasyonu: sayılar, UUID/hex, URL ve e-postalar maskelenir; tırnak içindeki
  **dizi anahtarı adı korunur** (`Undefined array key "host"` ≠ `"password"`).
- Uygulamalar arası birleşir: `content::page` hatası 6 storefront'ta aynıysa tek grup, kanıtta
  6 uygulama listelenir.
- **Undefined-key ailesi:** aynı repo + action'daki `Undefined array key/index/offset/variable/property`
  grupları **tek Task'ta** birleşir (parmak izleri anahtar başına ayrı kalır — dedup anahtar
  düzeyinde çalışır). ≤3 anahtar 1 SP, >3 anahtar 2 SP. Farklı action'lardaki anahtarlar
  (ör. `Index::index "host"` ile `Api/Order::integrationCancel "password"`) ayrı Task'tır.
- Parmak izi: `nr:<repo>:<action>:<sınıf>:<normalize mesaj>` (en fazla 512 karakter; aşarsa
  sonu sha1 ile kısaltılır).

## Skor

- **Sonar:** `ağırlık × (1 + log10(adet))`. Ağırlık: Vulnerability+Blocker 20,
  Vulnerability+Critical 10, Bug+Blocker 8, Bug+Critical 4. Log10, adet baskınlığını kırar
  (300 maddelik Bug+Critical = 4 × 3.48 = 13.9 < tek Vulnerability+Blocker 20; log2 ile 36.9
  olup geçerdi). Paket skoru = paketteki en yüksek birim skoru (toplam değil).
- **New Relic:** `hata_sayısı (lookback) × etkilenen_uygulama_sayısı`; eşitlikte en yüksek
  error rate önde. `CREW_QUALITY_NR_MIN_ERRORS` (100) altı elenir.

## SP kuralları (Fibonacci; Task başına en fazla 5)

| Durum | SP | Not |
|---|---|---|
| NR `Undefined array key/index/offset/variable/property` (E_WARNING/E_NOTICE) — action başına tek Task | 1 | Aynı action'da >3 farklı anahtar → 2 |
| NR `TypeError`, `ValueError`, `ArgumentCountError`, uygulama exception'ları | 3 | |
| NR altyapı kökenli (timeout, connection refused, deadlock, cURL/PDO bağlantı, Redis/Mongo bağlantı) | 3 | **needs_human** |
| Sonar birim: ≤5 madde 1, ≤20 madde 2, >20 madde 3 | 1–3 | Paket toplamı ≤3 |
| Sonar `secrets:*` | 1 | **needs_human** — secret rotasyonu gerekir, kodu silmek yetmez |

Sınıflandırma `rank.py`'de regex tablosu olarak durur; bilinmeyen sınıf → 3 SP.

## Seçim ("10 SP al")

1. **Ele:** açık WI'a bağlı parmak izleri (aşağıda Dedup); eşlenemeyen uygulama/proje;
   prod olmayan NR uygulamaları (prod = adı `[Prod]`/`[Production]`/`[GCP-Prod]` ile başlar **ya da**
   FLODevOps `production` dalından eşlenmiştir — `Marketplace API`, `Kurumsal Flo` gibi); `AZURE_DEVOPS_REPO_PROJECTS` dışındaki Sonar projeleri
   (FloxoPLM vb.); yerelde klonu olmayan repolar (pipeline çalışamaz). Hepsi `skipped`'a sebebiyle.
2. Skora göre sırala; açgözlü doldur — sığmayan atlanır, sıradaki daha küçük denenir.
   Toplam bütçeyi asla aşmaz.
3. needs_human işler bütçeden SP yer ama en fazla `CREW_QUALITY_HUMAN_SHARE` (%30).
4. Task sayısı en fazla `CREW_QUALITY_MAX_TASKS` (8) — maliyet tavanı (her Task bir pipeline işi).
5. Seçim sonucu boşsa hiçbir WI açılmaz; `skipped` ile döner.

## Dedup

- **Kaynak board'dur:** her alımda `tempo-quality` etiketli Task'lar WIQL ile okunur, açıklamalarındaki
  `tempo-fp:` satırlarından parmak izi → (WI, durum, kapanış tarihi) dizini kurulur. `quality_items`
  geçmiş/dashboard kaydıdır, dedup kararı ona bağlı değildir (DB kaybı dedup'ı bozmaz).
- Durum kuralı:
  - **Done / Closed / Completed** → kapanıştan `CREW_QUALITY_REOPEN_GRACE_DAYS` (3) gün sonra hâlâ
    görülüyorsa (NR: `last_seen`; Sonar: madde hâlâ açık) **yeniden** seçilebilir; yeni Task
    açıklamasına "Tekrarladı — önceki: #<eski WI>" eklenir.
  - **Canceled / Removed / Rejected** → kalıcı bastırma: bir insan "yapılmayacak" dedi, asla yeniden açılmaz.
  - Diğer her durum (**Resolved, Ready for Production** dahil — düzeltme henüz canlıda değil) → açık sayılır, seçilmez.
- İşaret görünür bir satırdır (`<p><small>tempo-fp: …</small></p>`), HTML yorumu değil: Azure
  Description HTML'ini sanitize eder, yorumun korunacağı garanti değil.

## Akış

```
POST /api/quality/take
  ├─ CREW_QUALITY_INTAKE kapalı → 403
  ├─ topla: sonar.fetch() | newrelic.fetch(lookback)
  ├─ eşle: app_map.resolve() (NR) / proje anahtarı (Sonar)
  ├─ rank: group → score → size → pack → select_budget(sp)
  ├─ story:
  │    story_id verildi → onu kullan (area/iteration ondan)
  │    verilmedi → takımın timeFrame=current iterasyonu
  │               → WIQL: User Story, o iterasyon, Tags CONTAINS 'tempo-quality'
  │                       AND Tags CONTAINS 'tempo-src-<kaynak>' → varsa kullan
  │               → yoksa aç: area = get_team_area_path(team)
  ├─ her seçili grup: create_work_item("Task", fields, parent_id=story)
  │    → quality_items INSERT
  │    → needs_human değilse db.create_job(wi_id, use_hal=False) → job_id
  └─ story SP = mevcut child SP toplamı (Custom.StoryPoints, yalnızca yükselir)
```

Kuyruk mevcut tek worker'la seri boşalır; ayrı bir zamanlayıcı yok.

## WI formatı (Türkçe)

**User Story**
- Başlık: `Kalite: New Relic hataları — <Sprint adı>` / `Kalite: Sonar bulguları — <Sprint adı>`
- Etiketler: `tempo-quality; tempo-src-newrelic|tempo-src-sonar`
- Açıklama: kısa amaç + her alımda eklenen bir tablo satırı (tarih, Task sayısı, SP, kaynak sorgu linki).

**Task**
- Başlık (≤128): NR `[<repo>] <Action>: <normalize mesaj>` — örn.
  `[webservice] Index::index: Undefined array key "host"`; Sonar
  `[<repo>] <kural adı> — <dosya adı> (+N dosya)`.
- Alanlar: `System.AreaPath`/`IterationPath` story'den, `System.AssignedTo` PAT sahibi,
  `Custom.StoryPoints` = SP, `System.Tags` = `tempo-quality; tempo-src-<kaynak>` (+ `needs-human`).
- Açıklama (HTML):
  - **Repo:** `<repo>` (ilk satır — `discover_repos`'un repoyu kanıtla bulması için)
  - **Ne oluyor:** hata sınıfı + tam mesaj / kural açıklaması
  - **Kanıt:** NR → uygulama başına adet, error rate, örnek `request.uri` (ilk 3), ilk/son görülme;
    Sonar → dosya:satır listesi (en fazla 20) + madde linkleri
  - **Olası yer:** NR action → dosya ipucu (`api::order::integrationCancel` →
    `app/Controller/Api/Order.php::integrationCancelAction`; `V1` alt dizini varyantı da yazılır)
  - **Kabul kriteri:** NR → "Bu action'da bu hata sınıfı/mesajı bir daha üretilmez; davranış
    korunur"; Sonar → "Listelenen maddeler Sonar'da kapanır, yeni madde eklenmez"
  - **Kaynak:** New Relic Errors Inbox / NRQL linki ya da Sonar issues linki
  - needs_human ise: **Neden insan:** "Secret rotasyonu gerekir" / "Altyapı kökenli hata"
  - `tempo-fp: …` (her parmak izi için bir küçük satır)

**Gizlilik:** Sonar `secrets:*` için madde metnindeki değer **asla** WI'a yazılmaz — yalnızca
dosya:satır ve kural adı. NR mesajlarında token/parola benzeri değerler (`Bearer …`, `Basic …`,
`password=…`, 20+ karakterlik hex/base64) maskelenir.

## Hata durumları

| Durum | Davranış |
|---|---|
| Sonar/NR API hatası ya da zaman aşımı | Hiçbir WI açılmaz; 502 + Türkçe mesaj. Kısmi veriyle seçim yapılmaz |
| NR uygulaması eşlenemedi | `skipped` + `/api/quality/unmapped`; override dosyasından düzeltilir |
| FLODevOps okunamadı | Önbellek (süresi geçmiş olsa da) kullanılır, uyarı loglanır; önbellek de yoksa NR alımı 502 |
| Repo yerelde klonlu değil | `skipped: "repo yerelde yok"` (pipeline çalışamaz) |
| İçinde bulunulan sprint bulunamadı ve `story_id` yok | 409 "aktif sprint yok — story_id verin" |
| `story_id` User Story değil ya da kapalı | 400 |
| Task açıldı ama kuyruğa alınamadı | Task kalır, `quality_items.job_id` NULL, sonuçta `job_id: null` + sebep; tekrar "al" aynı Task'ı yeniden açmaz (dedup), dashboard'da "Kuyruğa al" ile tekrar denenir |
| Story açıldı, hiçbir Task açılamadı | Story kalır (bir sonraki alım onu kullanır), hata döner |
| Aynı anda iki "al" | Süreç içi kilit (`threading.Lock`); ikincisi 409 |

Maliyet: her Task bir pipeline işi; iş başına `CREW_MAX_JOB_COST` zaten geçerli.
`CREW_QUALITY_MAX_TASKS` alım başına üst sınır.

## Ayarlar (`pipeline_config.SCHEMA`)

| Anahtar | Tip | Varsayılan | Açıklama |
|---|---|---|---|
| `CREW_QUALITY_INTAKE` | bool | **false** | Özelliğin ana anahtarı (board'a yazar, kuyruğa iş atar) |
| `CREW_SONAR_URL` | str | `https://sonarqube.flo.com.tr` | |
| `NEW_RELIC_REGION` | str | `eu` | `eu` / `us` |
| `CREW_QUALITY_NR_LOOKBACK_DAYS` | int | 7 | |
| `CREW_QUALITY_NR_MIN_ERRORS` | int | 100 | Lookback içinde bu altı elenir |
| `CREW_QUALITY_SONAR_PACK_SP` | int | 3 | |
| `CREW_QUALITY_SONAR_PACK_FILES` | int | 5 | |
| `CREW_QUALITY_HUMAN_SHARE` | float | 0.3 | needs_human SP payı tavanı |
| `CREW_QUALITY_MAX_TASKS` | int | 8 | |
| `CREW_QUALITY_REOPEN_GRACE_DAYS` | int | 3 | Kapanmış işin hatası bu süreden sonra hâlâ görülürse yeniden açılır |

## Test

`tests/test_katman0_gates.py` desenine `test_quality_intake()` eklenir (ağ yok, sabit veri):

- NR mesaj normalizasyonu: sayı/UUID maskelenir, dizi anahtarı adı korunur.
- Uygulamalar arası birleşme: 6 storefront'taki `content::page` → tek grup, 6 uygulama kanıtta.
- Sonar paketleme: aynı kural 12 dosya → ≤5 dosya, ≤3 SP'lik paketler.
- Skor: tek Vulnerability+Blocker, 300 maddelik Bug+Critical CSS'i geçer.
- SP tablosu: `Undefined array key` → 1, >3 anahtar → 2, `TypeError` → 3, altyapı → 3+needs_human,
  `secrets:*` → 1+needs_human.
- Bütçe: toplam ≤ sp; needs_human payı ≤ %30; `MAX_TASKS` sınırı; boş seçim → WI yok.
- Dedup: açık WI'lı parmak izi elenir; Ready for Production'daki elenir; Canceled kalıcı bastırılır;
  kapanış + 3 gün sonrası tekrar eden geri gelir; `tempo-fp` satırından geri okuma.
- app_map: örnek `JenkinsfileBuild` + `Dockerfile` metinlerinden eşleme, `${website}` şablonu,
  override önceliği.
- Gizlilik: `secrets:*` değeri ve `Bearer …` açıklamaya girmez.
- Kapalı anahtar: `CREW_QUALITY_INTAKE=false` → endpoint 403.

Canlı doğrulama (uygulamadan sonra, sırayla): `preview` NR 10 SP ve Sonar 10 SP çıktısı gözle
kontrol → `take` NR 3 SP (1–3 Task) → açılan Task'larda `discover_repos`'un doğru repoyu seçtiğini
ve ilk işin PR'a ulaştığını izle.

## Kapsam dışı

- Kapanış takibi (PR merge sonrası NR'da hatanın gerçekten bittiğini doğrulayıp WI'ı kapatmak) —
  dedup'ın "kapanış sonrası tekrar" kuralı şimdilik yeterli.
- Sonar'da CODE_SMELL ve MAJOR/MINOR severity.
- Zamanlanmış otomatik alım (her sprint başında). Önce elle "al" ile kalibrasyon.
- NR stack trace (TransactionError'da yok); dosya ipucu action adından türetilir.
