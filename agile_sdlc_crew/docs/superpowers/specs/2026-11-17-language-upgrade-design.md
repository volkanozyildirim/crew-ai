# Dil Sürümü Yükseltme — Tasarım

**Tarih:** 2026-11-17 · **Durum:** tasarım onaylandı (kullanıcı, 2026-11-17) · **Dal:** `worktree-techdebt-upgrade`
**Dayandığı tasarım:** `2026-10-19-techdebt-design.md` (Teknik borç alanı, aşama 2a modernleştirme)

## Amaç ve neden

Teknik borç sekmesindeki Modernleştirme kartı bugün tek soruya cevap veriyor: *"repo üretimde
hangi sürümdeyse, o sürümün sunduğu yenilikleri kullanıyor mu?"* Sürümün **kendisini** yükseltmek
kimsenin işi değil: 17 servis satırı hâlâ `php-8-2`, ikisi `php-7-4` imajında; yükseltme "bir gün
bakarız" işi olarak duruyor, çünkü neyin kırılacağını bulmak elle yapılan bir keşif.

Bu tasarım o keşfi ve ardından gelen işi otomatikleştirir: her repo × dil için bir **hedef sürüm**
belirlenir, o sürüme geçişte **geri uyumluluğu bozan yerler** bulunur, bunlar iş olarak açılıp
bugünkü araç/pipeline hattından PR'a kadar gider; bulgu kalmayınca **sürüm değişikliğinin kendisi**
de (kod reposu, test hattı, üretim imajı) sistem tarafından PR olarak açılır.

Kullanıcı kararları (2026-11-17):
- Yükseltme **uçtan uca**: yalnız hazırlık değil, sürüm değişikliği PR'ları da sistemden.
- **PHP kapsamda** — imajı FLODevOps'ta dursa da.
- Tespit: **katalog + hedef sürümün kendi aracıyla yerel doğrulama**; CI son kapı.

## Hedefler

1. Repo × dil için "üretim X → hedef Y" görünür; hedefi kullanıcı kartta değiştirebilir.
2. X→Y arasında kırılan / davranışı değişen / kullanımdan kalkan her yer dosya:satır olarak listelenir.
3. Otomatik düzeltilebilenler bugünkü `modern_fix` / pipeline hattından, **şimdiki sürümde de
   çalışan** biçimde düzeltilir (kod PR'ları sürüm değişmeden önce merge edilebilsin).
4. Bulgu kalmayınca sürüm PR'ları sırayla açılır; her adım bir öncekinin kanıtına bağlıdır.
5. İsteğe bağlı "otomatik ilerlet": insan yalnız PR merge eder, gerisini sistem sürdürür.

## Hedef dışı

- Tempo hiçbir PR'ı **merge etmez**, hiçbir deploy'u tetiklemez.
- Bağımlılık (composer / npm / pip paketi) güncellemesi yapılmaz; hedef sürümü desteklemeyen
  bağımlılık **engel olarak listelenir** (aşağıda), insan işi olur.
- Yeni imaj yayınlamak (ör. `php-8-5`) DevOps'undur; yayınlanmamış imaja geçiş önerilmez.
- Ara sürümlerde durmak yok: 7.4 → 8.4 tek hamle (katalog aradaki bütün sürümleri kapsar).
- Next.js için sürüm PR'ı bu tasarımda **yok** (aşağıda "Dil kapsamı").

## Kaçınılacak sonuçlar

- Kod PR'ı şimdiki üretim sürümünde çalışmayan sözdizimi içerir (ör. 8.2'de `new Foo()->bar()`).
- Test hattı yeni sürüme alınmadan üretim imajı PR'ı açılır.
- Bir repoyu çalıştıran servislerden biri yükselir, öteki eski kalır.
- Sistem aynı anda onlarca PR açıp ekibi boğar.
- FLODevOps / yml-files'ta sürüm satırı dışında bir yer değişir.

## Bugünkü durum (ölçüldü, 2026-11-17)

| Şey | Durum |
|---|---|
| Hedef sürüm | `modern.target` = üretimde çalışan sürüm (PHP: FLODevOps imajı, çok servisliyse en düşük; Go: `go.mod`; Python: imaj → Dockerfile → `requires-python`; Next: `package.json`). "Yükseltilecek sürüm" kavramı yok. |
| Katalog | `Rule.since` + `applicable()` yalnız `since ≤ hedef` süzer. "Şu sürümde kırılır" alanı yok; en yakını 11 `deprecation` kuralı. |
| PHP sürümünün durduğu yerler | (1) FLODevOps `<bölge>/build/<servis>/Dockerfile` `FROM …/php-8-2:1.0`, dal `production`; (2) `yml-files` → `Test Pipelines/<repo>*.yml` `phpVersion: '8.2'`; (3) kod reposu: bakılan beş repoda `composer.json`'da `php` kısıtı yok. |
| Yayınlanmış imajlar (build dizini) | `php-7-4` ×2, `php-8-2` ×17, `php-8-4` ×11; `python-3-13-7` ×1; `node-14-21` ×3, `node-20-17` ×2. Russia ayrı kayıt deposu (`10.131.167.220:5000`). |
| Yerel araçlar | `php@8.2`, `php@8.4`, `php@8.5`; Go 1.27.3; `python3.13/3.14/3.15`; Node 26. |
| Deneme koşusu | Yok: test sürümü `yml-files`'ta sabit, policy build elle kuyruğa alınamıyor. |
| Hazır hat | Tara → grupla → `take` → Task + `modern_fix` (Rector / araç / pencere düzeltmesi / inceleme / build kapısı) ya da pipeline; DevOps isteği (`devops_request.open_request`). |

## Yaklaşımlar

| # | Yaklaşım | Artı | Eksi |
|---|---|---|---|
| 1 | Yalnız katalog: "şu sürümde kaldırıldı" kuralları, mevcut tarayıcı | En ucuz; bugünkü yapıya birebir oturur | Katalogda olmayan kırılma görünmez; sürüm PR'ı körlemesine açılır |
| **2** | **Katalog + hedef sürümün aracıyla yerel doğrulama; CI son kapı** | Kataloğun kör noktasını hedef derleyici kapatır; sürüm PR'ı kanıta bağlı | Yerelde hedef sürüm aracı gerekir (yoksa yalnız katalog) |
| 3 | CI'ı kâhin yap: doğrudan `yml-files` + kod PR'ı, kırmızı testlerden iş çıkar | En gerçekçi kanıt | Test kapsamı zayıf repoda yeşil build hiçbir şey kanıtlamaz; `yml-files` değişikliği reponun bütün PR'larını etkiler |

**Seçilen: 2** (kullanıcı, 2026-11-17). 3'ün kanıtı son kapı olarak zaten içinde.

## Tasarım

### 1. Hedef sürüm (`quality/upgrade.py`)

`upgrade.plan(client, repo, lang, to=None)` → `{current, target, target_source, selectable, sites, blockers}`.

- **current**: bugünkü `modern.target` (değişmez).
- **target** varsayılanı:
  - PHP: build dizininde görülen **en yüksek** `php-X-Y` imaj sürümü (bugün 8.4). Yayınlanmamış
    sürüm hedef olamaz.
  - Python: aynı kural, `python-X-Y` imajları.
  - Go, Next.js: `config/upgrade_targets.yaml` (`go: "1.27"`, `nextjs: "15.0"` gibi; elle bakılır).
- **selectable**: `current < v ≤ tavan` olan ve katalogda adı geçen sürümler. Kartta seçilen `to`
  bu listede değilse 400. Sürüm metni `^\d+\.\d+$`.
- `current ≥ target` → repo "güncel", tarama yapılmaz.

### 2. Sürüm sabitleme yerleri (`sites`)

Yükseltmenin dokunacağı yerler **varsayılmaz, bulunur**; her biri `{where, repo, path, line, old, new}`:

| Yer | Nasıl bulunur | Değişiklik |
|---|---|---|
| FLODevOps Dockerfile | `build_index` + `app_map`'in repo için verdiği her `<bölge>/build/<servis>` | `FROM` satırında yalnız imaj adı: `php-8-2:1.0` → `php-8-4:1.0`, kayıt deposu öneki aynı kalır |
| `yml-files` test hattı | `Test Pipelines/*.yml` içinde repoyu checkout eden dosyalar | yalnız `phpVersion:` (ya da dilin eşdeğeri) satırı |
| Kod reposu | `composer.json` `require.php` / `config.platform.php`; `go.mod` `go` + `toolchain`; `pyproject.toml` `requires-python`, `.python-version`; repo kökündeki `Dockerfile` `FROM` | sürüm metni |

**İmaj kanıtı:** yeni imaj metni (kayıt deposu + ad + etiket) aynı FLODevOps reposunda başka bir
serviste zaten kullanılıyor olmalı. Kullanılmıyorsa (ör. Russia deposunda `php-8-4` yoksa) o bölge
için site `blocked: "imaj yayınlanmamış"` olur ve DevOps isteği açılır; yükseltme o bölge
hazır olmadan üretim adımına geçmez — bir repoyu çalıştıran bütün servisler **birlikte** yükselir.

`yml-files` eşlemesi belirsizse (dosya repoyu adıyla anmıyor) site listelenir ama otomatik
değiştirilmez; sürüm adımı insan işi olarak açılır.

### 3. Tespit — iki katman

**a) Katalog.** `Rule`'a iki alan eklenir:

- `breaks`: `error` (hedefte çalışmaz: kaldırılmış fonksiyon, sözdizimi), `behavior` (çalışır ama
  sonucu değişir), `deprecation` (uyarı; bir sonraki majörde hata). Boşsa kural yükseltme kuralı
  değildir (bugünkü mechanical/structural kurallar).
- `fix_min`: düzeltilmiş biçimin çalıştığı en düşük sürüm (varsayılan `"7.0"`).

Yükseltme taraması `breaks` dolu ve `current < since ≤ target` olan kuralları uygular. Mevcut 11
`deprecation` kuralı `breaks: deprecation` alır ve olduğu gibi kullanılır; istisna
`removed_functions` (hedefte fonksiyon yok) → `breaks: error`.

`fix_min > current` olan bulgu **önceden düzeltilemez** (düzeltilmiş hâli bugünkü üretimde
çalışmaz). Böyle bir bulgu varken test hattı yeni sürüme alınırsa reponun bütün PR'ları kırmızıya
döner; bu yüzden otomatik sürüm adımı **açılmaz**: bulgu `needs-human` Task olur, repo `engelli`
kalır ve Task kapatılana kadar sürüm adımı yalnız insan tarafından başlatılabilir. (PHP'de pratikte
nadir: kaldırılan şeylerin karşılığı eski sürümde de çalışır.)

Katalog sürümü 4 olur; eklenecek PHP kuralları (token tarayıcıyla, `php_modern_scan.php`):

| Geçiş | Kurallar |
|---|---|
| → 8.0 | kaldırılan fonksiyonlar (`each`, `create_function`, `money_format`, `mb_ereg_replace` `e`, `get_magic_quotes_*`, `ezmlm_hash`, `restore_include_path`, `hebrevc`, `convert_cyr_string`, `fgetss`, `gzgetss`, `image2wbmp`, `png2wbmp`, `jpeg2wbmp`, `ldap_sort`, `read_exif_data`), `$s{0}` süslü parantez erişimi, `(real)` / `(unset)` cast, zorunlu parametreden önce isteğe bağlı parametre, `__autoload`, `implode` eski argüman sırası, sınıf adıyla eş kurucu, `@` ile bastırılan fatal'lar (rapor), string↔sayı gevşek karşılaştırma (**behavior**, rapor) |
| → 8.1 | iç fonksiyona `null` geçirme (**behavior**, yalnız literal `null`), `$GLOBALS` yazımı, `Serializable` tek başına, `date_sunrise/sunset`, `strftime/gmstrftime`, `mhash*`, `FILTER_SANITIZE_STRING`, `mysqli_*` kaldırılanlar |
| → 8.2 | mevcut: `dynamic_properties`, `utf8_encode`, `partial_callable`, `mb_legacy_encoding`, `${var}` |
| → 8.3 | mevcut: `get_class_noarg`, `deprecated_83`; ek: `unserialize` hata seviyesi (rapor) |
| → 8.4 | mevcut: `implicit_nullable`, `deprecated_84`; ek: `E_STRICT`, PECL'e taşınan eklentiler (`imap`, `pspell`, `oci8`, `pdo_oci`) → `requires` |

`behavior` kuralları otomatik düzeltilmez: tarayıcı niyeti bilemez. `needs-human` Task olur,
bulunan yerler listelenir.

Diğer diller:
- **Go:** dil geri uyumlu; katalog küçük (`go 1.22` döngü değişkeni semantiği — rapor, `GODEBUG`
  varsayılan değişimleri — rapor). Asıl tespit (b) katmanında.
- **Python:** kaldırılan stdlib modülleri ve API'ler (`imp`, `distutils`, `asyncore`, `asynchat`,
  `smtpd`, `cgi`, `cgitb`, `pipes`, `telnetlib`, `nntplib`, `crypt`, `audioop`, `datetime.utcnow`,
  `asyncio.get_event_loop` kullanımı …), sürümüyle. `ast` ile, depo kodu çalıştırılmadan.
- **Next.js:** mevcut regex kataloğuna majör geçiş kuralları (`next/legacy/image`, senkron
  `cookies()/headers()/params`, `next lint`, `experimental.*` adları); her kuralda resmî codemod adı.

**b) Hedef sürümün aracıyla doğrulama** (`upgrade.probe`). Katalogdaki boşluğu kapatır; çıktısı
katalog kuralına eşlenemezse `_generic_rule` ile genel bulgu olur.

| Dil | Komut | Bulgu |
|---|---|---|
| PHP | hedef sürümün binary'siyle uygulama dosyalarında `php -l` (`error_reporting=E_ALL`) | parse error → `error`; derleme zamanı `Deprecated:` → `deprecation` |
| Go | atılabilir worktree'de `go.mod` `go <hedef>` yazılıp `go build ./... && go vet ./...` (yerel toolchain ≥ hedef ise, `GOTOOLCHAIN=local`, `-mod=readonly`/vendor) | derleme / vet hatası |
| Python | hedef yorumlayıcıyla kendi dizinimizdeki sürücü betik (`-I`, cwd depo dışı) dosyaları `compile()` eder, `SyntaxWarning` hata sayılır | sözdizimi hatası / uyarısı |
| Next.js | yok (npm kurulumu gerekir) | — |

Araç yolu **yalnız env** ile gelir, panelden yazılamaz (`CREW_PHP_BIN` ile aynı karar):
`CREW_UPGRADE_PHP_BINS` = `8.4=/opt/homebrew/opt/php@8.4/bin/php,…` (ad `php…` olmalı),
`CREW_UPGRADE_PYTHON_BINS` (ad `python3…`). Hedef sürümün aracı yoksa probe atlanır ve kart
"hedef sürüm aracı yok — yalnız katalog" der; hazırlık bulguları yine üretilir. Bütün komutlar
`verify._env()` ile (Azure/Sonar anahtarı yok) çalışır.

**c) Bağımlılık engelleri** (`blockers`, iş açmaz, hazır olmayı engeller):
- PHP: `composer.lock` paketlerinin `require.php` kısıtı hedefi dışlıyorsa paket adı + kısıt.
- PHP: hedefte PECL'e taşınan / kaldırılan eklenti repoda kullanılıyorsa → mevcut `infra` yolu,
  DevOps isteği.
- Next.js: `react` / `react-dom` eş bağımlılık uyuşmazlığı.

### 4. İşler

Yeni kaynak adı **açılmaz**: bulgular dilin mevcut kaynağıyla (`php_modern`, `go_modern`, …) ve
aynı parmak iziyle (`php_modern:<repo>:<kural>:<dosya>`) gider — repo yükseldikten sonra aynı kural
modernleştirme taramasında da çıkar, board tekilleştirmesi ikisini tek iş sayar. Task'lara
`tempo-upgrade` etiketi ve başlık öneki (`Sürüm yükseltme 8.2→8.4: …`) eklenir.

Düzeltme yolu bugünküyle aynı (`php_modern.tool_fixable` → `modern_fix`; değilse pipeline;
`behavior` ve `structural` → needs-human). Tek fark: **Rector `withPhpVersion` = şimdiki üretim
sürümü**, yani çıktı bugün de çalışır. Pencere düzeltmesi talimatına "kod X sürümünde de çalışmalı"
cümlesi eklenir; `verify` hem şimdiki hem hedef binary ile `php -l` koşar (hedef aracı varsa).

Alım tavanları bugünkü gibi (`max_take`, kural başına tek iş, SP eşikleri).

### 5. Yükseltme durumu

Durum **saklanmaz, türetilir** (Azure tek kayıt, teknik borç tasarımındaki karar). `upgrade.status`:

| Aşama | Koşul |
|---|---|
| `hazırlık` | taze taramada (origin varsayılan dal) **engelleyen** bulgu var, ya da board'da açık engelleyen `tempo-upgrade` işi var |
| `engelli` | engelleyen bulgu yok ama `blockers`, `blocked` site ya da `fix_min > current` bulgusu var |
| `hazır` | engelleyen bulgu yok, engel yok, engelleyen açık iş yok |
| `test hattı` | `yml-files` PR'ı açık ya da merge edilmiş, yeni sürümle yeşil build henüz yok |
| `üretim` | FLODevOps PR'ları açık |
| `tamam` | `current ≥ target` (bir sonraki `modern.target` okumasında kendiliğinden) |

**Hangi bulgu engeller:**

| Bulgu | Engeller mi |
|---|---|
| `error` | evet — hedefte çalışmaz |
| `deprecation`, otomatik düzeltilebilir | evet — ucuz, PR'ı zaten sistem açıyor |
| `deprecation`, otomatik düzeltilemeyen | hayır — hedefte çalışır; iş açılır, yükseltmeyi bekletmez |
| `behavior` | evet, **Task'ı bir insan kapatana kadar** — kapatmak "baktım" demektir; yerler aynı kaldıkça yeniden açılmaz |
| `report`, katalogsuz probe uyarısı | hayır (katalogsuz probe **hatası** `error` sayılır) |

Aktif yükseltmenin kaydı bir **izleme Task'ı**dır: takımın alanında, **sprint'e bağlı olmadan**
(yükseltme birkaç sprint sürer), etiket `tempo-upgrade-track`, açıklamada
`tempo-upgrade-key: <dil>:<repo>:<hedef>` satırı. Hangi repoların yükseltmede olduğu buradan
okunur; aşama değişimleri ve açılan PR'lar yorum olarak düşer. Açıkken aynı repo × dil için ikinci
izleme işi açılmaz.

Sürüm PR'ları kayıt gerektirmeden bulunur: dal adı sabittir
(`tempo/upgrade-<dil>-<repo>-<hedef>`, her üç repoda), PR o daldan aranır.

### 6. Sürüm adımı (`quality/upgrade_bump.py`, `job_kind=upgrade_bump`)

Bir `upgrade_bump` işi **tek adım** atar ve biter; beklemez. (Kuyruk işçisi seri: günlerce
bekleyen bir iş bütün pipeline'ı durdurur, sunucu yeniden başlarsa öksüz kalır.) Hangi adımın
sırada olduğu saklanmaz, Azure'dan okunur: sabit dal adındaki PR var mı, merge edildi mi, ondan
sonra kuyruğa girmiş yeşil build var mı. Adımı otomatik ilerletme ya da karttaki düğme başlatır.

Kapsam (dil, repo, from, to, adım, siteler) işin kaydına **INSERT'te** yazılır; çalışan iş
düzenlenebilir WI metnini okumaz. Her adım başta yeniden doğrular: taze klonda engelleyen bulgu ve
engel yok, siteler hâlâ kayıttaki `old` değerinde. Değilse iş `failed`, neden izleme Task'ına yazılır.

| Adım | Ön koşul | Yapılan |
|---|---|---|
| 1. Test hattı | aşama `hazır` | `yml-files` PR'ı (taslak): test hattındaki sürüm satırı. Açıklama: "merge edildiğinde `<repo>` PR'ları `<hedef>` ile test edilir; üretim hâlâ `<şimdiki>`." Test hattı sitesi yoksa adım atlanır. |
| 2. Üretim | 1. adımın PR'ı merge edilmiş **ve** ondan sonra kuyruğa girmiş bir `<repo>-test` build'i yeşil | FLODevOps PR'ları (bölge reposu başına bir tane, hedef dal `production`, taslak): hedefin altındaki **bütün** servislerin `FROM` satırı. Aynı anda DevOps isteği (`devops_request.open_request`, anahtar `upgrade:<dil>:<repo>:<hedef>`): PR bağlantıları, merge sırası, geri alma (FROM'u eski imaja çevir). Merge ve deploy DevOps'undur. |
| 3. Kod reposu | üretim hedef sürümde (`modern.target ≥ hedef`) | Kod reposu PR'ı (taslak): `composer.json` / `go.mod` / `requires-python` vb. sürüm metinleri; `verify` hedef araçla. Site yoksa adım atlanır. |

Sıra bilinçli: kod reposundaki sürüm kısıtı **en son** sıkılaşır — önce merge edilirse eski
imajdaki üretim `composer install` / derleme aşamasında kırılır. İstisna: sürümü yalnız kod
reposunda duran dil (ör. imajı sabitlenmemiş bir Go servisi) — orada 3. adım tek adımdır ve
`hazır` olunca açılır.

2. adımın kanıtı: build, 1. adımın merge'inden sonra kuyruğa girmiş olmalı; reponun **herhangi**
bir PR'ının build'i sayılır (test paketi aynı, hedef sürümle koştu). Kırmızıysa
`get_build_failure_summary` izleme Task'ına ve bir `needs-human` Task'a yazılır, 2. adım açılmaz.
Reponun PR build'i hiç yoksa 2. adım yine açılır ama PR açıklamasının ilk satırı "test kanıtı
yok" olur. `CREW_TECHDEBT_UPGRADE_CI_WAIT_H` dolduğu hâlde build gelmediyse izleme Task'ına bir
kez not düşülür ("yeni sürümle koşmuş build yok — bir PR'ı yeniden tetikleyin").

Yazma kısıtları: FLODevOps ve `yml-files`'a giden değişiklik, kayıttaki sitenin o tek satırındaki
sürüm metninin yer değiştirmesinden ibarettir; başka diff üretilirse push yapılmaz. Bu repolar
başka Azure projesinde: `push_changes` / `create_pull_request` / `create_branch` çağrılarına
`project` parametresi eklenir (okuma çağrılarında var). PAT'in yazma yetkisi yoksa (403) PR yerine
aynı diff DevOps isteğinin gövdesine yazılır.

Çok servisli repoda servislerin sürümü farklı olabilir (flo-dashboard: anatolia 8.4, russia 8.3):
siteler yalnız hedefin altındaki servislerdir; herhangi birinin imajı yayınlanmamışsa (`blocked`)
2. adım **hiçbir bölge için** açılmaz.

`upgrade_bump` işleri retro / sprint planı / daily sorgularına girmez (`job_kind=pipeline` süzgeci).

### 7. Otomatik ilerletme

`CREW_TECHDEBT_UPGRADE_AUTO` (varsayılan **kapalı**). Açıkken tek bir sunucu iş parçacığı
`CREW_TECHDEBT_UPGRADE_POLL_MIN` (30) dakikada bir, açık her izleme Task'ı için:

1. Tarar; board'da olmayan otomatik grupları alır — repo başına aynı anda en çok
   `CREW_TECHDEBT_UPGRADE_MAX_OPEN` (3) açık iş/PR olacak şekilde (istek başı `max_take` tavanı
   ayrıca geçerli; küçük olan kazanır). needs-human işler bir kez açılır.
2. Sıradaki sürüm adımının ön koşulu sağlanmışsa o adımın `upgrade_bump` işini kuyruğa alır
   (aynı repo × dil için kuyrukta / çalışan bir tane yoksa).
3. Aşama `tamam` ve 3. adımın PR'ı kapanmışsa izleme Task'ını kapatır.

Otomatik alınan işler takımın **bir sonraki** sprint'indeki teknik borç story'sine açılır
(`CREW_TECHDEBT_UPGRADE_SPRINT=next|current`, varsayılan `next`): kimsenin planlamadığı iş aktif
sprint'e düşmez. Karttan elle alımda sprint bugünkü gibi kullanıcının seçtiğidir.

Kapalıyken aynı adımlar karttaki düğmelerle elle atılır. Çalışan bir işin kullandığı klona
dokunulmaz (`freshen_clone` kuralı).

### 8. API ve kart

- `GET /api/techdebt/upgrade/repos` — repo × dil: `current`, `target`, aşama, bulgu sayıları.
- `GET /api/techdebt/upgrade/scan?repo&lang&to&force` — bulgular (`auto` / `infra` / `human`),
  `sites`, `blockers`, probe durumu.
- `POST /api/techdebt/upgrade/take` — seçilen grupları iş olarak aç (bugünkü `take` kuralları).
- `POST /api/techdebt/upgrade/start` / `stop` — izleme Task'ını aç / kapat.
- `POST /api/techdebt/upgrade/bump` — sıradaki sürüm adımını kuyruğa al (ön koşulu yoksa 400).

Kart: Modernleştirme kartında **Modernleştirme | Sürüm yükseltme** anahtarı. Yükseltme görünümü:

```
core · PHP   8.2 → [8.4 ▾]     Hazırlık ● ─ Hazır ○ ─ Test hattı ○ ─ Üretim ○
  Kırılır (error) 4 · Davranış değişir 2 · Kullanımdan kalkar 61     hedef araç: php 8.4 ✓
  Engeller: laravel/framework ^8.0 (php ^7.3|^8.0) ✓ · ext-imap → DevOps
  Sürüm yerleri: anatolia/build/lojistik · russia/build/lojistik · yml-files Core.yml
  [ Otomatik işler ] [ Altyapı ] [ İnsan kararı ]      [ İşleri aç ]  [ Yükseltmeyi başlat ]
```

Arayüz eşik hesaplamaz; aşama, sayılar ve metinler sunucudan gelir.

### 9. Dil kapsamı

| Dil | Tespit | Hazırlık işleri | Sürüm PR'ları |
|---|---|---|---|
| PHP | katalog + `php -l` (hedef) | Rector / fixer / pencere / pipeline | `yml-files` → FLODevOps → kod (varsa) |
| Go | `go build` + `go vet` (hedef `go` yönergesiyle) + küçük katalog | `modernize` aracı / pipeline | `go.mod` + (varsa) test hattı ve imaj |
| Python | katalog (`ast`) + hedef yorumlayıcıyla derleme | ruff / pipeline | `requires-python` vb. + (varsa) test hattı ve imaj |
| Next.js | katalog (regex) | pipeline | **yok** — `needs-human` Task (benim kararım) |

Next.js sürüm PR'ı neden yok: `package.json` değişikliği kilit dosyasının yeniden üretilmesini
ister; bu Tempo sunucusunda `npm install` (özel paket deposu kimliği + depo betiklerinin çalışması)
demektir. Task, bulunan yerleri ve çalıştırılacak codemod komutlarını yazar.

### 10. Ayarlar

| Ayar | Varsayılan | Not |
|---|---|---|
| `CREW_TECHDEBT_UPGRADE` | kapalı | ana anahtar; `CREW_TECHDEBT` + `CREW_TECHDEBT_PHP_MODERN` de gerekir |
| `CREW_TECHDEBT_UPGRADE_AUTO` | kapalı | otomatik ilerletme |
| `CREW_TECHDEBT_UPGRADE_BUMP` | açık | kapalıysa sürüm adımı hiç PR açmaz, DevOps isteği olarak yazılır |
| `CREW_TECHDEBT_UPGRADE_POLL_MIN` | 30 | |
| `CREW_TECHDEBT_UPGRADE_MAX_OPEN` | 3 | repo başına eşzamanlı açık iş/PR |
| `CREW_TECHDEBT_UPGRADE_SPRINT` | `next` | otomatik alınan işlerin sprint'i |
| `CREW_TECHDEBT_UPGRADE_CI_WAIT_H` | 72 | yeni sürümle build bekleme; dolunca izleme Task'ına not |
| `CREW_UPGRADE_PHP_BINS`, `CREW_UPGRADE_PYTHON_BINS` | boş | yalnız `.env`; `sürüm=yol` listesi |

Panelden düzenlenebilenler `pipeline_config`'e kaydedilir; araç yolları kaydedilmez.

### 11. Güvenlik

- Depo içeriği güvenilmez: probe komutları depo kodunu **çalıştırmaz** (lint / derleme / `ast`),
  cwd deponun dışında ya da atılabilir worktree'de, ortam `verify._env()`.
- Araç yolları yalnız env; ad denetimi (`php…`, `python3…`).
- Sürüm, repo ve yol metinleri doğrulanır (`^\d+\.\d+$`, `modern_fix._REPO`, mutlak / `..` / `-` yol yok).
- Dış repolara (FLODevOps, `yml-files`) yazılan değişiklik tek satırlık sürüm değişimiyle sınırlı.
- PR / WI metinleri `render.mask_secrets`'ten geçer.

## Karar: `behavior` bulguları yükseltmeyi bekletir (kullanıcı, 2026-11-17)

Bir insan Task'ı kapatana kadar sürüm adımı açılmaz. Gerekçe: bunlar hata vermeden yanlış sonuç
üreten yerler (`0 == "a"`, iç fonksiyona `null`), testlerin yakalama ihtimali en düşük sınıf.
Bilinen bedeli: 7.4 → 8.x geçişinde yüzlerce yer çıkabilir ve yükseltme insan bakana kadar durur.

## Kullanıcıya sorulmadan verilen kararlar

- Sürüm adımı tek iş değil, **adım başına bir iş**; durum Azure'dan (dal adı, PR, build) okunur.
- Sıra: test hattı → üretim imajı → kod reposundaki kısıt (en son).
- `fix_min > current` bulgusu otomatik sürüm adımını kapatır, insan işi olur.
- İzleme Task'ı sprint'e bağlı değil; otomatik alımlar **bir sonraki** sprint'e.
- Çok bölgeli repoda bir bölge engelliyse hiçbir bölgenin üretim PR'ı açılmaz.
- Kod PR'ı yokken, reponun herhangi bir PR'ının yeni sürümle yeşil build'i kanıt sayılır.
- Next.js için sürüm PR'ı yok (needs-human Task).
- Bulgular yeni kaynak adıyla değil, dilin mevcut kaynağı + `tempo-upgrade` etiketiyle açılır.
- `yml-files` klonunda `Test Pipelines/` yanında `Test%20Pipelines` kopyası var; canlı olan
  API'den (varsayılan dal) okunarak belirlenir.

## Yapıcıya bırakılanlar

- Modül sınırları (`upgrade.py` / `upgrade_bump.py` içinde bölme), önbellek anahtarı (HEAD sha +
  katalog sürümü + hedef sürüm), test dosyalarının adı.
- Katalog kurallarının Türkçe metinleri (ayrıntı, avantaj, risk, önce/sonra) — mevcut kurallarla
  aynı biçimde.
- Token tarayıcıda her yeni kuralın tespit ayrıntısı; güvenilir tespit edilemeyen kural kataloğa
  `kind: report` olarak girer.
- Kartın görsel ayrıntısı (mevcut kart bileşenleri ve tema değişkenleriyle).
- Uygulama sırası önerisi: (A) hedef + katalog + tarama/alım, (B) probe + engeller, (C) sürüm
  adımı + izleme, (D) otomatik ilerletme. Her biri kendi başına yayınlanabilir.
