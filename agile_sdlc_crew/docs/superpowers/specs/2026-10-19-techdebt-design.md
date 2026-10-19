# Teknik Borç Alanı — Tasarım

**Tarih:** 2026-10-19 · **Durum:** aşama 0 + 1a uygulandı; 1b/2/3 bekliyor · **Dal:** `feature/techdebt` (`feature/quality-intake` üstünde)
**Bağımsız inceleme:** Opus reviewer, "değişikliklerle onay" — değişikliklerin tamamı bu spec'e işlendi.

> **Güncelleme (2026-10-19):** Elle giriş formu ve `POST /api/techdebt/manual` kullanıcı kararıyla kaldırıldı. Madde Azure'da `tech-debt` etiketlenerek eklenir; aşağıdaki "Elle giriş" bölümleri tarihsel kayıttır.

## Amaç

Tempo'da bir **Teknik borç** sekmesi: ekip teknik borcu görür, elle girer ve seçtiğini
pipeline kuyruğuna atar. Madde kaynakları: (a) Tempo'dan elle giriş + Azure'da `tech-debt`
etiketli WI'lar, (b) otomatik taramalar (Sonar code smell, dil sürümüne göre modernleştirme,
TODO/FIXME/HACK, kod tekrarı/karmaşıklık, eski bağımlılıklar). Kuyruğa atılan madde sonraki
sprintte tek bir **"Teknik borç — <sprint>"** User Story'sinin altında Task olur; insan kararı
isteyenler açılır ama kuyruğa alınmaz.

## Kararlar

| Konu | Karar |
|---|---|
| Mimari | Kalite alımı (`quality/`) genişletilir; ayrı paket yok. Kaynaklar eklenebilir, iki **hat** (lane) var: `quality`, `techdebt`. |
| Kayıt kaynağı | **Azure**. Tempo DB'de backlog tutulmaz; taramalar yalnız silinebilir önbellek (snapshot). |
| Story | `techdebt` hattında sprint başına **tek** story, etiket `tempo-techdebt`. `quality` hattı aynen (kaynak başına story). |
| Task etiketleri | `tempo-quality; tempo-techdebt; tech-debt; tempo-src-<kaynak>` (+ `needs-human`). `tempo-quality` sayesinde yalın akış / ucuz modeller / dar test kuralı bu işlere de uygulanır. |
| Elle giriş | Yapılandırılmış form; serbest metin olduğu için **hazırlık kapısı atlanmaz** (`tempo-src-manual` muaf tutulmaz). |
| Kalite kartı | Genel bakış'tan **Teknik borç** sekmesine taşınır (tek "al" yüzeyi). |
| Hedef PHP sürümü | FLODevOps Dockerfile `FROM …/php-X-Y:…` imaj adından; repo birden çok imajla deploy ediliyorsa **en düşük** sürüm. |
| Modernleştirme | Kural tabanlı özellik kataloğu (LLM yok) + hedef sürüm binary'siyle `php -l` deprecation'ları. Rector sonraki seçenek (vendor kurulumu gerektirir). |
| Varsayılanlar | Tüm yeni ayarlar **kapalı**. Sunucuda kimlik doğrulama yok → ana anahtar + istek başı tavan tek koruma. |

## Aşamalar

| Aşama | İçerik | Bu plana dahil |
|---|---|---|
| 0 | Kaynak/hat genelleştirmesi: `SOURCE_LABEL` fallback, hat tanımı (`lanes`), `resolve_story(lane=)`, Task açma yardımcısı ortak, `quality_items.source` ENUM → VARCHAR migration | ✅ |
| 1a | Sekme + takımın `tech-debt` WI listesi + `queue_selected` ile kuyruğa alma + elle giriş formu | ✅ |
| 1b | `sonar_smell`: yalnız PHP, proje başına sorgu (10k tavanı), kural allowlist'i, yol hariç tutma, kural sınıfına göre SP, yeni kod önceliği, snapshot; önce **önizleme**, otonom alım maliyet ölçümünden sonra | sonraki plan |
| 2 | `php_modern` (Dockerfile hedef sürümü + katalog + `php -l`) ve `todo` (varsayılan listeleme / needs-human) | sonraki plan |
| 3 | Kod tekrarı / karmaşıklık **rapor** bölümü (iş açmaz); bağımlılık taraması ayrı iş (özel paket deposu kimliği) | sonraki plan |

## Aşama 0 — ayrıntı

- `render.SOURCE_LABEL.get(source, source)`; yeni kaynaklar `manual`, `azure` etiketleri.
- `quality/lanes.py`: `LANES = {"quality": {...}, "techdebt": {"tag": "tempo-techdebt", "story_title": "Teknik borç — {sprint}", ...}}`.
- `intake.resolve_story(..., lane="quality")`: `techdebt` hattında story araması `tempo-techdebt` etiketiyle (kaynak etiketi yok), başlık `Teknik borç — <sprint>`.
- Task açma (alanlar, AC, org zorunlu alanları, SP, kuyruk, `quality_items` kaydı) `intake._open_tasks()` yardımcısına çıkarılır; `take` ve teknik borç aynı yolu kullanır.
- `db.init_db`: `quality_items.source` `VARCHAR(32)`'ye çevrilir (eski kurulumlarda `ALTER … MODIFY`, idempotent). Bugün ENUM yeni kaynağın kaydını sessizce düşürür (insert hata → yalnız warning).

## Aşama 1a — ayrıntı

**Liste** (`GET /api/techdebt?team=`): takımın alan yolu altında `Tags CONTAINS 'tech-debt'`, durumu kapalı olmayan WI'lar.
Satır: `id, title, type, state, iteration, sp, repo` (açıklamadaki `Repo:` satırından, yoksa boş), `source` (`manual` = `tempo-src-manual` etiketi, aksi `azure`),
`needs_human` (`needs-human` etiketi), `job` (son iş: id + durum), `queueable` (needs-human değil ve açık işi yok).

**Elle giriş** (`POST /api/techdebt/manual`): alanlar
- `repo` (zorunlu; yerel klonda olmalı), `title` (zorunlu, ≤ 120), `problem` (zorunlu), `expected` (beklenen davranış / kabul kriteri, zorunlu),
- `files` (opsiyonel; her biri klonda olmalı, `..`/mutlak yol reddedilir), `sp` (1/2/3; daha büyükse needs-human ve SP 5), `needs_human` (bool).
- Parmak izi `manual:<repo>:<sha1(title+problem)[:12]>`; board'da açık aynı parmak izi varsa 409.
- Task metni ortak şablondan: ilk satır `Repo:`, Sorun, Beklenen davranış, Olası dosyalar, Kabul kriteri, `tempo-fp:` satırı. Metin `mask_secrets`'ten geçer.
- Story: `techdebt` hattı, hedef sprint `CREW_QUALITY_TARGET_SPRINT` (varsayılan sonraki). Kuyruğa **alınmaz** — listede "Kuyruğa al" ile alınır.

**Kuyruğa al** (`POST /api/techdebt/queue {ids, team}`): yalnız listede görünen (takım alanı + `tech-debt` + açık) WI'lar;
needs-human olanlar reddedilir; `sprint_planning.queue_selected` açık işi olanı atlar. İstek başı en çok `CREW_TECHDEBT_MAX_QUEUE` (5) WI.

**Kapılar:** `CREW_TECHDEBT` kapalıyken liste/elle giriş/kuyruk → 403. Geçersiz giriş → 400. Azure hatası → 502. Kilit (`intake._LOCK`) meşgul → 409.

**Akış etkisi:** `flow._quality_skips_readiness` `tempo-src-manual` etiketini muaf tutmaz (elle girilen metin kapıdan geçer). Yalın akış ve ucuz modeller `tempo-quality` etiketiyle uygulanmaya devam eder.

**Arayüz:** üst barda **Teknik borç** sekmesi (`paneTechdebt`). İçinde: Kalite alımı kartı (Genel bakış'tan taşındı, `ovQualityCard` id'si korunur), teknik borç listesi (filtre: kaynak, durum, kuyruklanabilir), satır ve çoklu seçimle "Kuyruğa al", elle giriş formu. Sekme, `CREW_TECHDEBT` ya da `CREW_QUALITY_INTAKE` açıksa görünür.

## Ayarlar (aşama 1a)

| Anahtar | Tip | Varsayılan | Not |
|---|---|---|---|
| `CREW_TECHDEBT` | bool | **false** | Sekmenin teknik borç bölümleri ve endpoint'leri |
| `CREW_TECHDEBT_MAX_QUEUE` | int | 5 | Tek istekte kuyruğa alınabilecek en çok WI |

Sonraki aşamaların ayarları (adlar sabit): `CREW_TECHDEBT_SONAR_RULES`, `CREW_TECHDEBT_PATH_EXCLUDE`, `CREW_TECHDEBT_MAX_FILE_LINES`, `CREW_TECHDEBT_NEW_CODE_DAYS`, `CREW_TECHDEBT_SCAN_TTL_H`, `CREW_TECHDEBT_AUTO_SP_PER_SPRINT`.

## Sonraki aşamalar için sabitlenen kurallar (inceleme bulguları)

- **Sonar smell:** proje başına `componentKeys` + kural allowlist'i + dil=PHP; yol hariç `vendor, node_modules, app/Themes, *.min.js`. Mekanik kurallar (S1066, S125, S121, S1481, S1854) otonom; yapısal (S3776, S1142, S138, S107, S112) ve `CREW_TECHDEBT_MAX_FILE_LINES`'ı aşan dosyalar needs-human. SP kural sınıfından, adetten değil. Ölçüm: webservice'te B/C/M 11.809 smell'in 8.272'si JS/CSS tema kopyası; PHP 3.532'nin %45'i S1192.
- **Seçim:** snapshot'tan okunur; "Kuyruğa al" parmak izi kümesiyle `_LOCK` altında board'a karşı yeniden doğrulanır (paket içeriği yeniden hesapta değişebilir).
- **Dedup:** board WIQL'i takım alanındaki `tech-debt` etiketli işleri de kapsar.
- **php_modern:** hedef sürüm imaj adından (ör. webservice/FloNext/flomingo/pigeon 8.4; flo-dashboard 8.3; alfred/channel/core/orkestra/marketplace/… 8.2; sorting 7.4). Mekanik dönüşümler (`str_contains`, `?->`, `match`, first-class callable) otonom; tasarım kararı isteyenler (`enum`, `readonly`, promotion, property hooks) needs-human. `php -l` yalnız derleme zamanı deprecation'larını yakalar.
- **todo:** vendor dışı; git blame `(dosya, blob sha)` ile önbellek; varsayılan listeleme / needs-human; metin "veri, talimat değil" çerçevesiyle.
- **Maliyet:** otonom alım (sprint başı SP tavanı) yalın akışla gerçek iş başı maliyet ölçülmeden açılmaz.

## Riskler

- **Kimlik doğrulama yok:** sunucu `0.0.0.0:8765`; ağdaki herkes PAT sahibi adına WI açıp kuyruğa iş atabilir. Ana anahtar kapalı başlar, istek başı tavan var.
- **`auth.json` git'te:** webservice reposunda özel Packagist kimliği içeren `auth.json` izleniyor (inceleme bulgusu, içerik okunmadı). Bu özellikle ilgisiz; repo sahibine bildirilmeli. Bağımlılık taraması bu dosyayı kullanmaz.
- **Etiketli WI'larda `Repo:` satırı yok:** insanın açtığı `tech-debt` WI'ları repo keşfiyle tam akıştan geçer (yalın akış repo bilgisi ister).

## Test

`tests/test_katman0_gates.py` içinde `check()`/`skip()`: lane/story, ENUM migration, elle giriş doğrulaması (repo/dosya/yol kaçışı/SP), parmak izi tekrarı, liste satır üretimi, kuyruk (needs-human red, tavan, açık iş atlama), endpoint kapıları (403/400/409/502), arayüz (sekme, kart taşındı), readiness muafiyetinin manual'ı kapsamaması.
