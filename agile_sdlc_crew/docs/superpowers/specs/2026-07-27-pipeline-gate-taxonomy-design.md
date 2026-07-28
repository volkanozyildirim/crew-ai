# Pipeline Kapı Taksonomisi — Tasarım

**Tarih:** 2026-07-27
**Durum:** onay bekliyor
**İlişki:** V2 Tasarımı'nın (2026-04-24) ölçüm temelli revizyonu

## Amaç

Pipeline'ın tam otonom çalışması: bir Work Item girer, testleri/build'i yeşil ve
kendi review kapısını geçmiş bir PR çıkar. Bugünkü engel, kapının kendisinin
güvenilir olmaması: pipeline kullanılabilir iş üretip kendi reviewer'ının
itirazı üzerine job'ı öldürüyor.

**Terminal sözleşmesi:** job "tamamlandı" sayılır ⟺ build/test yeşil **ve**
bloklayıcı review maddesi kalmadı.

## Ölçüm temeli

2026-07-27'de job #178 ve #179 üzerinde yapılan ölçümler. İkisi de
`review_pr_task` adımında öldü; toplam $16.21 harcandı, sıfır WI teslim edildi.

| Bulgu | Kanıt |
|---|---|
| Reviewer false positive üretiyor | #179/R2 `cms_setting_group_id` eksik dedi; #67539'un kendi `Upgrade.php` insert'i de o kolonu kullanmıyor — uydurma standart |
| Reviewer ürün kararını hata gibi sunuyor | #179/R1 merchant elemesinin sipariş-bazlı olmasına itiraz etti; WI FR4 "merchant **siparişleri** dışında tutulmalıdır" diyor, sipariş-bazlı okuma sadık |
| Reviewer gerçek kusuru kaçırıyor | `Allocator.php:441`, 3 parametreli `luggageSuffix`'e 4. argüman geçiyor → PHP yutuyor, düzenleme no-op |
| Tek-dosya linter dosyalar-arası ihlali göremez | `_validate_code` `php -l`'i geçici dosyada izole çalıştırıyor; arity kayması yapısal olarak görünmez |
| İtiraz yönlendirmesi yol biçimine duyarlı | plan `/app/X.php`, itiraz `app/X.php` → kesişim boş → reviewer'ın şikâyet ettiği dosyalar retry'da hiç düzeltilmedi |
| Idle watchdog verimli işi öldürüyor | `⏱️ Hard timeout (idle 90s (event yok)) — SIGKILL`, 13.5 dakikalık keşfin ortasında; Opus 5'te `thinking.display=omitted` olduğu için düşünürken stream'e event gitmiyor |
| Watchdog kaybı kaliteye yayılıyor | keşif ölünce plan bulgusuz üretildi → completeness gate AC5/AC6/FR3 boşluğu buldu → $1.27/280s amend → hâlâ TR3 eksik |
| Maliyet çağrı sayısı ve cache durumundan geliyor | aynı prompt, soğuk cache $0.278 · sıcak cache **$0.014** (20×). "ok" yazdıran tek Opus çağrısı $0.17 taban |
| Config sessizce etkisiz kalabiliyor | litellm custom provider'a model'i prefix'siz geçiyor; `if "/" in model else ""` boş dönüyordu → `--model` hiç eklenmiyor → tüm agent'lar CLI default'unda koşuyordu, `agent_llm_overrides.yaml`'daki sonnet ayarları aylarca dekoratif |

Fiyat referansı (Anthropic liste): Opus 5 $5/$25 per 1M · Sonnet 5 $3/$15 ·
Haiku 4.5 $1/$5. Cache yazma = input×1.25 (5dk TTL) / ×2 (1sa TTL); okuma =
input×0.1. Opus 5'te 1M context standart fiyatta, uzun-context primi yok.

## V2'den korunanlar ve revize edilenler

**Korunan prensipler:** JSON girdi/çıktı sözleşmesi (serbest metin yok);
her FR/TR/AC benzersiz id ve pipeline boyunca izlenir; developer plan hatasında
`status="blocked"` döner, sessiz düzeltme yapmaz; reviewer somut kanıt vermek
zorunda.

**Revize edilen:** "her adımdan sonra SM gate". Ölçülen çağrı-başı $0.17 taban
maliyetiyle 14 adımda ~$2.4 ek yük demek. Yerine: mekanik olarak karar
verilebilen her kapı deterministik doğrulayıcıya döner (maliyet ~0); LLM gate'i
yalnızca gerçek yargı gereken iki yerde kalır (teknik tasarım sonrası, review
sonrası).

**Eklenen:** V2 "kanıt zorunluluğu" diyordu; ölçüm eksik yarıyı gösterdi —
*kanıtı pipeline doğrular*. Kanıt formatına uymak yetmiyor (R2 uydu), kanıtın
içeriği sınanmalı.

## Mimari: kapı taksonomisi

Düzenleyici ilke: **her kapı, kendisinden pahalı olan her şeyden önce çalışır.**

### Katman 0 — Deterministik (LLM yok, ~$0)

| Kapı | Durum | Kapattığı hata |
|---|---|---|
| Plan yol geçerliliği | mevcut (`_validate_plan_paths`) | uydurma dizin |
| Entegrasyon varlığı | mevcut | çağrılmayan yeni kod |
| Dosyalar-arası sözleşme (arity, erişilebilirlik) | **yeni** | ölü 4. argüman |
| İtiraz kanıt doğrulama | **yeni** | uydurma kanıt |
| İtiraz gereksinim bağı | **yeni** | R1/R2 sınıfı yanlış blokör |
| Emsal doğrulama | **yeni** | uydurma convention (R2) |
| Plan completeness (küme farkı) | **yeni** — haiku çağrısının yerine | $0.22 + tetiklediği $1.27 amend |
| Regresyon koruması | mevcut | kod kaybı |
| Test varlığı | mevcut (`CREW_REQUIRE_TESTS`) | test'siz PR |

**Dosyalar-arası sözleşme kapısının kapsamı.** Genel bir statik analizör değil;
bu pipeline'ın tekrar ürettiği iki hata sınıfına hedefli iki kontrol:

1. **Arity** — push edilecek diff'te eklenen her metot çağrısı için, çağrılan
   metot repoda tanımlıysa bildirilen parametre sayısıyla karşılaştırılır.
   Fazla/eksik argüman → bloklar. (`luggageSuffix($a,$b,$c,$d)` vs 3 parametreli
   imza.)
2. **Erişilebilirlik** — planın eklediği her yeni public metot için repoda en az
   bir çağrı noktası aranır. Yok → **uyarı** (blok değil). Bu, mevcut entegrasyon
   kontrolünün dosya seviyesinden sembol seviyesine indirilmiş hâli.

   **Neden bloklamaz:** arity deterministiktir (imza ya uyar ya uymaz), ama
   erişilebilirlik sezgiseldir — metot dinamik/framework tarafından çağrılabilir,
   bir interface implementasyonu olabilir, ya da aynı PR'ın henüz yazılmamış bir
   dosyasından çağrılacak olabilir. Sezgisel bir kontrolü bloklayıcı yapmanın
   maliyeti ölçüldü: job #180'de asıl implementasyon dosyası engellendi ve iş
   boşa gitti.

İkisi de klon üzerinde grep/regex seviyesinde uygulanabilir; dil-başına tam
parser gerekmiyor. PHP ile başlanır (tek aktif repo dili), diğer diller
eklenene kadar kapı o dillerde sessizce atlanır.

### Katman 1 — Deterministik dış sistem (LLM yok)

PR build/test gate. Mevcut, yerinde.

### Katman 2 — LLM yargısı

Requirements analizi, teknik tasarım, implement, review semantiği, UAT.
**Katman 2'nin çıktısı da Katman 0'dan geçer** — reviewer'ın verdiği veri
olarak ele alınır, hüküm olarak değil.

### Sıralama

İlke: pahalı olan hiçbir kapı, kendisinden ucuz bir kapıdan önce çalışmaz.

Completeness kontrolü bu tasarımda deterministikleştiği için (küme farkı, LLM
yok) yol gate'i ile aynı katmana iner ve aralarındaki maliyet sıralaması
sorunu ortadan kalkar. Geçiş döneminde completeness hâlâ haiku çağrısıyla
çalışıyorsa, yol/entegrasyon gate'i **ondan önce** koşar — 2026-07-27'de ters
kondu (yol gate'i completeness'ten sonra), düzeltilmeli.

Her iki kapı da bir architect amend tetikleyebildiği için, ikisi de tek turda
değerlendirilip **tek bir birleşik geri bildirimle** amend edilir; iki ayrı
sıralı amend (bugünkü davranış, #179'da $1.27 + ek çağrı) yerine.

## Sözleşmeler

### Plan değişikliği

```json
{"file_path": "app/Model/StockSource.php",
 "requirement_ids": ["FR1", "FR2", "AC1", "AC2"],
 "description": "...", "current_code": "...", "new_code": "..."}
```

`requirement_ids`, completeness kontrolünü küme farkına indirir: gereksinim
id'lerinin tamamı en az bir değişiklikte görünüyorsa plan kapsayıcıdır. LLM
denetçisi gerekmez.

Model kendi bağını beyan ettiği için yanlış beyan mümkündür; review aşamasında
çapraz kontrol edilir (itirazlar da id taşıyor). Bugünkü durumdan — düzyazıdan
hüküm veren bir LLM denetçisi — kesin olarak daha iyi.

### Review itirazı

```json
{"requirement_ids": ["AC1"],
 "evidence":    {"file": "app/Model/StockSource.php", "line": 920, "quote": "..."},
 "precedent":   {"file": "app/Migration/Upgrade.php", "line": 218, "quote": "..."},
 "fix_targets": ["app/Library/Helper/Allocator.php"],
 "severity": "blocker", "problem": "...", "required_fix": "..."}
```

**Bloklama kuralı (tam deterministik):** bir itiraz ancak `severity` ∈
{blocker, major} **ve** aşağıdakilerden en az biri sağlanıyorsa bloklar:

1. `requirement_ids` en az bir id içeriyor **ve** o id'lerin tamamı
   requirements JSON'unda gerçekten mevcut, **veya**
2. `precedent` verilmiş **ve** doğrulanmış (gösterilen dosya:satır okunabiliyor
   ve `quote` orada bulunuyor).

Aksi halde itiraz **yargı sınıfına** düşer: PR yorumuna gider, bloklamaz.

Bu kural üç ayrı hatayı tek geçişte eler:
- **Gereksinim bağı yok** → R2 ve R1 burada düşer (ikisi de bir AC id'sine
  bağlanamıyor).
- **Var olmayan id'ye atıf** → reviewer bloklamak için uydurma bir AC id'si
  gösterirse elenir (küme kontrolü).
- **Uydurma convention** → R2 alternatif yol olarak `precedent` denese de
  gösterecek emsal yok: #67539'un kendi insert'i o kolonu kullanmıyor.

`evidence` her itiraz için zorunludur ve ayrıca doğrulanır: pipeline dosyayı
okur, `quote`'un belirtilen satır civarında bulunduğunu kontrol eder.
Doğrulanamayan itiraz — severity'si ne olursa olsun — elenir ve loglanır.

`fix_targets` → düzeltilecek dosyalar açıkça. Bugünkü yönlendirme hatası ve
slash uyuşmazlığı, ikisi de hedefi `issue.file`'dan **çıkarsamaya**
çalışmaktan doğuyor; alan açık olunca çıkarsama kalkar.

### Developer çıktısı

```json
{"status": "done|blocked", "file_path": "...", "content": "...",
 "blocked_reason": "luggageSuffix imzası 3 parametreli, satır-bazlı filtre 4. parametre gerektiriyor",
 "blocked_needs": ["app/Model/StockSource.php: luggageSuffix imzası"]}
```

`blocked` → architect amend, plan genişletilir, tekrar denenir. Yarım
uygulamayı push etmek yerine.

### Yol normalizasyonu

LLM'den gelen her yol **parse anında** normalize edilir (baştaki `/`, ters
slash), her karşılaştırma noktasında değil. Büyük/küçük harf dönüştürülmez —
repo yolları case-sensitive.

## Veri akışı: yakınsama döngüsü

```
Reviewer → itirazlar (JSON)
   ├─ Katman 0 filtresi → bloklayıcı küme
   ├─ küme boş → APPROVE → build gate → test planlama + UAT → rapor
   └─ küme dolu:
        fix_targets birleşimi = düzeltilecek dosyalar
        ├─ plan içermiyorsa → architect amend (yalnızca eksikleri EKLE)
        ├─ developer her hedef için; status=blocked → amend → tekrar
        ├─ verify: madde-madde kapanma, DIFF kanıtıyla
        └─ aynı bloklayıcı id kümesi 2 tur üst üste → İLERLEME YOK → eskalasyon
```

Verify adımı tam dosya değil **unified diff** alır. Tam dosya `per_file` ile
baştan kesiliyor ve büyük dosyalarda düzeltmenin olduğu satırlar context'e hiç
girmiyor; #179'da verifier bu yüzden "kanıt yok" deyip maddeleri kalıcı `open`
bıraktı. Diff hem küçük hem tam isabetli (ölçüm: 24.583 → 13.905 karakter,
kesme yok).

### Eskalasyon

`needs_human` — `failed` değil. PR açık kalır; açık maddeler, denenen
düzeltmeler ve kapatılamama gerekçesi PR ve WI yorumuna yazılır.

Şema etkisi: `jobs.status` yeni bir değer alır. `db.py` içindeki durum
filtreleri (`/api/health` sayaçları, `fail_orphan_running_jobs`, dashboard
listeleri) `needs_human`'ı tanımalı — aksi halde bu işler sayımlarda kaybolur.
`needs_human` **terminal** bir durumdur: worker onu yeniden almaz, ama retry
endpoint'i ile elle yeniden kuyruğa alınabilir.

Durma koşulu "aynı açık id kümesi tekrarlandı" (ilerleme yokluğu), V2'nin
"3 üst üste İYİLEŞTİR" sayacı değil: sayaç ilerleme olup olmadığını görmez.

## Değişken zarf

İki aşamada: analiz sonrası kaba, plan sonrası kesin.

| Sinyal | Kaynak |
|---|---|
| FR+TR+AC sayısı | requirements JSON |
| plan dosya sayısı, yeni/mevcut oranı | plan JSON |
| `NEED_EXPLORE` tetiklendi mi | step4 |
| dokunulacak dosyaların toplam boyutu | klon |

| Sınıf | Koşul | Bütçe | Süre | Review retry |
|---|---|---|---|---|
| S | ≤3 AC, ≤2 dosya, keşif yok | $5 | 15 dk | 1 |
| M | varsayılan | $10 | 30 dk | 2 |
| L | ≥6 AC **veya** ≥5 dosya **veya** keşif gerekti | $18 | 60 dk | 3 |

WI 69378 L sınıfına giriyor; 2026-07-27'de M zarfında boğuldu.

**Zarf yalnızca yükselir.** İki aşamanın sonucu farklıysa büyük olan geçerlidir;
plan analizden daha karmaşık çıkarsa zarf büyür, tersi durumda küçülmez. Aşağı
düzeltme, halihazırda harcanmış bütçenin altında bir tavan üretip işi anında
öldürebilir.

## Faz-farkındalı timeout

Kök neden: Opus 5'te `thinking.display` varsayılanı `omitted`, model düşünürken
stream'e event gitmiyor; sabit 90s idle bunu hang sanıyor.

| Faz | idle | hard |
|---|---|---|
| Repo-araçlı (keşif, implement) | 240s | 900s |
| Tool'suz emit | 90s | 300s (mevcut değer) |
| Denetçi (haiku) | 90s | 300s (mevcut değer) |

**Kural: yalnızca gevşet, asla sık.** Faz değerleri yapılandırılmış tabanla
`max()` alınır. Sıkmak, düzeltilen hatanın (verimli işi öldürmek) aynısını geri
getirir; hızlı olması *beklenen* bir fazın yavaş çalışması bir uyarı sinyalidir,
öldürme gerekçesi değil. Bu yüzden emit ve denetçi fazları pratikte mevcut
90s/300s tabanında kalır ve tabloda yalnızca hedef olarak listelenir.

Watchdog kaldırılmıyor — gerçek hang'ler için gerekli — faza göre kalibre
ediliyor.

## Hata yönetimi ve gözlemlenebilirlik

**Açılış doğrulaması.** Her farklı modele bir minik çağrı yapılır; CLI'ın
bildirdiği model beklenenle karşılaştırılır. Uyuşmazlık deploy'u durdurur.
Deploy başına ~$0.45, job başına değil. Gözlemlenemeyen config kayan config'tir
— litellm prefix hatası aylarca sessiz kaldı.

**Adım bütçesi.** Her adımın zarftaki payı loglanır; zarfın %40'ını aşan adım
uyarı üretir. #179'da $1.27'lik tek çağrı sessizce geçti.

**Tek bütçe kaynağı.** Zarf hem adım-sınırı guard'ında hem ara-adım kısa-devresinde
okunmalı. İkisi ayrışırsa iş her zaman DÜŞÜK tavanda ölür — job #181 tam böyle
öldü: zarf $18 dedi, sink sabit $10 okudu, $10.62'de bütün çağrılar boş döndü.

**Kanıt eleme logu.** Katman 0'ın eledigi her itiraz gerekçesiyle loglanır.
Reviewer kalitesinin zaman içindeki ölçümü bu logdan çıkar; sessiz eleme
reviewer'ın bozulduğunu gizler.

## Test stratejisi

Projede test altyapısı yok. Katman 0 doğrulayıcıları veri üzerinde saf
fonksiyonlar — LLM çağrısı olmadan test edilebilirler.

**Replay korpusu:** `jobs` / `job_steps` / `llm_calls` tabloları 125 job'lık
gerçek üretim çıktısı barındırıyor. Her Katman 0 doğrulayıcısı bu korpusa karşı
doğrulanır: mock değil, kaydedilmiş gerçek LLM çıktısı.

Uygulama: `tests/test_katman0_gates.py` — 59 test, bağımsız çalışır
(`.venv/bin/python tests/test_katman0_gates.py`), pytest gerekmez.

Bilinen fixture'lar:
- job #178 planı → yol gate 4 sorun bulmalı (3 uydurma yol + entegrasyon yok)
- job #179 planı → yol gate temiz dönmeli (yanlış alarm yok)
- job #179 itirazları → R1 ve R2 yargı sınıfına düşmeli (`requirement_ids` boş)
- `Allocator.php` #179 diff'i → dosyalar-arası sözleşme kapısı arity
  uyuşmazlığını yakalamalı
- feature branch'te dosya diskte varken → yol gate base ref üzerinden
  doğrulamalı (git fixture ile test edildi)

## Kapsam dışı

- Reviewer/architect modelini düşürmek. Ölçüm gösteriyor ki maliyet çağrı
  sayısı ve cache durumundan geliyor, model kademesinden değil; Sonnet Opus'un
  yalnızca 1.67 katı ucuzu.
- Adversaryal çoklu doğrulama (itiraz başına N çürütücü). Çağrı-başı taban
  maliyetiyle zarf disiplinine aykırı ve false negative'i çözmüyor. Yalnızca
  developer `blocked` ile itiraz ettiğinde eskalasyon yolu olarak saklanır.
- Prompt prefix'ini cache dostu yeniden yapılandırmak. Ölçülen kazanç yüksek
  (20×) ama bu tasarımın kapsamı dışında; ayrı bir iş olarak değerlendirilmeli.
- `flow.py`'ın 14 adımlı yapısını değiştirmek. Adım grafiği yerinde kalıyor;
  değişen şey kapıların türü ve sırası.

## 2026-07-27'de zaten uygulanmış parçalar

Bu tasarımın bir kısmı bugün ölçümler sırasında uygulandı ve gerçek veriyle
doğrulandı:

1. `_validate_plan_paths` — yol + entegrasyon gate'i (git base-ref üzerinden)
2. Review retry yönlendirmesi — `fix_targets` alanının sezgisel karşılığı
   (`_paths_in_text`) + re-plan'ın eklediği dosyaların implement listesine
   alınması
3. Yol normalizasyonu — karşılaştırma noktalarında (`_norm_path`)
4. Verify'a diff kanıtı — `_prefetch_pr_changes_context(diff_mode=True)`

Tasarım bunları sözleşme seviyesine taşıyor: (2) ve (3) sezgisel yamalar,
şema alanları onların yerini alır.

## Uygulama sonrası: gerçek koşuda doğrulananlar

Job #181 (2026-07-27) her mekanizmayı gerçek koşuda çalıştırdı:
zarf `L/$18/3` · keşif ~2 dk'da bitti (`Hard timeout` yok; önce 13.5 dk + SIGKILL)
· `yollar + entegrasyon doğrulandı` → `completeness 8/8, LLM yok` · sözleşme
kapısı gerçek arity ihlali yakaladı (`OrderTest.php`'de `luggageSuffix` 2
argüman, imza 3 zorunlu) ve #180'in yanlış alarmlarını üretmedi · itiraz kapısı
`4 bloklayıcı, 3 düşürüldü` (reviewer yeni şemayı üretti) · `fix_targets`
yönlendirmesi 5 dosyayı implement listesine aldı (#178'de hiç çalışmadı,
#179'da 1 dosya).

**Uçtan uca başarılı bir koşu hâlâ yok.** #181 bütçe tavanı hatasıyla düştü
(düzeltildi). WI 69378 artık doğrulama için uygun değil — PR #40663 ile main'e
merge edildi, dolayısıyla mimar yalnızca çevresel işler planlıyor. Tam
doğrulama için başka bir WI gerekiyor.
