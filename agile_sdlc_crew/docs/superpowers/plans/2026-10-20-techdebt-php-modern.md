# Teknik Borç — Aşama 2a: PHP Modernleştirme (`php_modern`) Uygulama Planı

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (Native, bu oturum). Her task TDD; testler `tests/test_katman0_gates.py` (`check()` / `skip()`).

**Spec:** `docs/superpowers/specs/2026-10-19-techdebt-design.md` → "Aşama 2a — php_modern ayrıntı" · **Dal:** `feature/techdebt`
**Tek test:** `.venv/bin/python -c "import sys; sys.path.insert(0,'tests'); import test_katman0_gates as t; t.<TEST>(); sys.exit(1 if t.FAIL else 0)"` · **Tüm dosya:** `.venv/bin/python tests/test_katman0_gates.py`

**Hedef:** Teknik borç sekmesinden bir PHP reposu seçilir, hedef PHP sürümü FLODevOps imajından bulunur, repo token tabanlı taranır, hedef sürümün sunduğu ama kodun kullanmadığı yapılar kural kataloğuna göre **ayrıntı + avantaj + risk + önce/sonra** ile listelenir, seçilenler "Teknik borç — <sprint>" story'si altında Task olur; mekanik olanlar pipeline kuyruğuna girer, mimari olanlar `needs-human` açılır.

**Mimari:** Kalite alımının `techdebt` hattına üçüncü kaynak (`php_modern`). Tarama = paketle gelen `php_modern_scan.php` (token_get_all → yapısal JSON) + Python kural uygulayıcı. LLM yok. Katalog YAML. Mevcut `intake.resolve_story` / `_open_tasks` / `dedup` / `pack_sonar` yeniden kullanılır.

## Global kısıtlar
- Kullanıcıya görünen metin Türkçe. Yeni ayarlar kapalı/güvenli başlar. Yeni Python bağımlılığı yok; `php` binary'si zorunlu (yoksa 502).
- Azure tek kayıt kaynağı; tarama önbelleği silinebilir (`config/quality_php_modern_cache/`, gitignore).
- Repo kodu Task metnine `render._e()` ile girer (mask + defang + escape). Yol kaçışı yok: tarama yalnız `~/.crew_repos/<repo>` altında, `repo` adı `intake.repo_is_local` ile doğrulanır.
- Kod değişikliğinden sonra sunucu `running:0` **ve** backfill `running:false` kontrolüyle yeniden başlatılır.
- Kullanıcının commit'lenmemiş `config/{agent_llm_overrides,pipeline_config,vision_config}.yaml` dosyalarına dokunulmaz.

## Dosyalar
- Create: `src/agile_sdlc_crew/config/php_modern_rules.yaml` (katalog)
- Create: `src/agile_sdlc_crew/quality/php_modern_scan.php` (token tarayıcı)
- Create: `src/agile_sdlc_crew/quality/php_modern.py` (hedef sürüm, tarama, kurallar, gruplama, take)
- Modify: `quality/render.py` (`SOURCE_LABEL`, `_modern_body`, `expected_behavior`/`acceptance_criteria` php_modern dalı), `quality/rank.py` (`pack_sonar` → `pack_units(source=)`), `pipeline_config.py` (5 knob), `server.py` (3 uç), `web/index.html` (kart), `.gitignore`, `CLAUDE.md`
- Test: `tests/test_katman0_gates.py` (`test_php_modern_*`), fikstür `tests/fixtures/php_modern/` (küçük PHP dosyaları)

## Review Focus (testlerin örtmediği, kullanıcıyı ısıracak girdiler)
1. Heredoc/nowdoc ve string içinde `strpos(` geçen satır → token tarayıcı string token'larını atlar (Task 2 testi: string içindeki çağrı bulgu değil).
2. Aynı repo iki imajla (8.4 + 7.4) → en düşük alınır; aksi 7.4'e `match` önerilir ve kırılır (Task 1 testi).
3. Tarama önbelleği eski sha'ya aitken `take` → yeniden tarama, grup id'si yoksa 400 "tarama değişti" (Task 5 testi).
4. `needs-human` yapısal grup seçilip "iş olarak aç" denince → Task açılır ama kuyruğa alınmaz; yanıt bunu söyler (Task 5 testi).
5. `php` binary'si yok / çıkış kodu ≠ 0 / JSON bozuk → `QualitySourceError` → 502, kısmi veriyle liste gösterilmez (Task 2 testi).

---

## Task 1 — Hedef sürüm ve katalog

**Files:** Create `config/php_modern_rules.yaml`, Create `quality/php_modern.py` (ilk bölüm), Modify `.gitignore`.
**Produces:** `php_modern.load_rules() -> list[Rule]` (dataclass: `key, since: tuple[int,int], kind, title, summary, advantages, risks, before, after, ref, auto, unit, sp_unit, weight, rector`), `php_modern.target_version(client, repo, *, app_map_cache=None, repo_root=None) -> dict` (`{"version": "8.4", "images": [...], "source": "dockerfile"|"composer"|None}`), `php_modern.applicable(rules, version) -> list[Rule]`, `php_modern.parse_php_image(from_line) -> tuple|None`.

- [ ] **Test `test_php_modern_catalog`**: YAML yüklenir; her kuralda zorunlu alanlar dolu, `kind ∈ {deprecation, mechanical, structural}`, `unit ∈ {file, module}`, `since` `X.Y`; `structural` kuralların hepsi `auto: false`; `applicable(rules, "8.2")` property hooks'u içermez, `"8.4"` içerir; `parse_php_image("europe-west1-docker.pkg.dev/flo-global/prod-ar/php-8-4:1.0") == (8,4)`; `parse_php_image("alpine:3.19") is None`; iki imaj `(8,4)` ve `(7,4)` → `target_version` `"7.4"`; imaj yoksa `composer.json` `"php": "^8.1"` → `"8.1"`; hiçbiri yoksa `version None`.
- [ ] Kırmızı koş → **Katalog YAML** (aşağıdaki içerik aynen; metin Task'a ve arayüze gider):

```yaml
version: 1
rules:
  # ── deprecation: hedef sürümde uyarı / hata üretir ──
  - key: implicit_nullable
    since: "8.4"
    kind: deprecation
    unit: file
    auto: true
    sp_unit: 1
    title: "Örtük nullable parametre (Type $x = null)"
    summary: "Tipli bir parametreye varsayılan null verilmiş ama tip ?Type yazılmamış. PHP 8.4 bunu deprecation olarak loglar, PHP 9'da hata olur."
    advantages:
      - "Üretim logundaki deprecation gürültüsü kesilir; New Relic'te gerçek hatalar görünür kalır."
      - "PHP 9 geçişinin en yaygın kırılma noktası şimdiden kapanır."
      - "İmza niyeti açık: parametre gerçekten null olabilir."
    risks:
      - "Yok — davranış birebir aynı; yalnız imza değişir."
    before: "function find(string $sku = null, array $opts = null)"
    after: "function find(?string $sku = null, ?array $opts = null)"
    ref: "https://wiki.php.net/rfc/deprecate-implicitly-nullable-types"
    rector: "Rector\\Php84\\Rector\\Param\\ExplicitNullableParamTypeRector"
  - key: utf8_encode
    since: "8.2"
    kind: deprecation
    unit: file
    auto: true
    sp_unit: 1
    title: "utf8_encode / utf8_decode"
    summary: "Yalnız ISO-8859-1 ↔ UTF-8 çeviren ve adı yanıltıcı olan bu fonksiyonlar 8.2'de deprecated; 9'da kaldırılacak."
    advantages:
      - "Deprecation logu kesilir."
      - "mb_convert_encoding ile kaynak kodlama açıkça yazılır; Türkçe karakter bozulmalarının bir sınıfı ortadan kalkar."
    risks:
      - "Kaynak kodlama gerçekten ISO-8859-1 değilse eski kod zaten yanlış çeviriyordu; değişiklik bunu görünür kılabilir."
    before: "$s = utf8_encode($raw);"
    after: "$s = mb_convert_encoding($raw, 'UTF-8', 'ISO-8859-1');"
    ref: "https://wiki.php.net/rfc/remove_utf8_decode_and_utf8_encode"
    rector: "Rector\\Php82\\Rector\\FuncCall\\Utf8DecodeEncodeToMbConvertEncodingRector"
  - key: string_interp_brace
    since: "8.2"
    kind: deprecation
    unit: file
    auto: true
    sp_unit: 1
    title: "\"${var}\" string içi değişken"
    summary: "Çift tırnak içinde ${var} ve ${expr} biçimi 8.2'de deprecated."
    advantages:
      - "Deprecation logu kesilir."
      - "{$var} biçimi tek ve okunur standart olur."
    risks:
      - "Yok — çıktı aynı."
    before: "\"Sipariş ${orderNo} hazır\""
    after: "\"Sipariş {$orderNo} hazır\""
    ref: "https://wiki.php.net/rfc/deprecate_dollar_brace_string_interpolation"
    rector: "Rector\\Php82\\Rector\\Encapsed\\VariableInStringInterpolationFixerRector"
  - key: filter_sanitize_string
    since: "8.1"
    kind: deprecation
    unit: file
    auto: true
    sp_unit: 1
    title: "FILTER_SANITIZE_STRING"
    summary: "Etiket soyup tırnakları HTML entity yapan bu filtre 8.1'de deprecated; ne yaptığı belirsiz olduğu için kaldırılıyor."
    advantages:
      - "Deprecation logu kesilir."
      - "Niyet açık hale gelir: htmlspecialchars (çıktıda kaçış) ya da strip_tags (girdiyi temizleme)."
    risks:
      - "İki fonksiyon aynı şeyi yapmaz; çağrı yerine göre doğru olan seçilmeli (yapısal karar olan yerler insana bırakılır)."
    before: "filter_var($q, FILTER_SANITIZE_STRING)"
    after: "htmlspecialchars($q, ENT_QUOTES)"
    ref: "https://wiki.php.net/rfc/deprecations_php_8_1#filter_sanitize_string"
  - key: removed_functions
    since: "8.0"
    kind: deprecation
    unit: file
    auto: true
    sp_unit: 1
    title: "8.0'da kaldırılan fonksiyonlar (each, create_function, money_format)"
    summary: "Bu çağrılar 8.0'dan beri yok: ulaşıldığında Fatal error üretir. Üretimde kalmışsa o yol ya hiç çalışmıyor ya da ölü kod."
    advantages:
      - "Gizli bir Fatal error kaynağı kapanır ya da ölü kod silinir."
    risks:
      - "Yol gerçekten kullanılıyorsa bugüne kadar patlıyordu; düzeltme davranışı görünür kılar."
    before: "while (list($k, $v) = each($arr)) { … }"
    after: "foreach ($arr as $k => $v) { … }"
    ref: "https://www.php.net/manual/en/migration80.incompatible.php"

  # ── mechanical: davranış aynı, dönüşüm yerel ──
  - key: str_contains
    since: "8.0"
    kind: mechanical
    unit: file
    auto: true
    sp_unit: 1
    title: "strpos(...) !== false → str_contains()"
    summary: "Alt dize var mı kontrolü strpos'un dönüşünü false ile karşılaştırarak yapılıyor."
    advantages:
      - "Niyet okunur: 'içeriyor mu' sorusu tek fonksiyon."
      - "Klasik 0 / false tuzağı (== false, !strpos) biter — bu tuzak gerçek bug üretir."
      - "Aynı aile: str_starts_with / str_ends_with."
    risks:
      - "Yok — strict karşılaştırmalı yerler birebir eşdeğer. (== false ya da !strpos yazılmış yerler zaten hatalı olabilir; dönüşüm bunu düzeltir, PR açıklamasında belirtilir.)"
    before: "if (strpos($uri, '/api/') !== false)"
    after: "if (str_contains($uri, '/api/'))"
    ref: "https://wiki.php.net/rfc/str_contains"
    rector: "Rector\\Php80\\Rector\\NotIdentical\\StrContainsRector"
  - key: str_starts_with
    since: "8.0"
    kind: mechanical
    unit: file
    auto: true
    sp_unit: 1
    title: "substr($s, 0, n) === … / strpos() === 0 → str_starts_with()"
    summary: "Önek kontrolü substr uzunluğu elle sayılarak ya da strpos === 0 ile yapılıyor."
    advantages:
      - "Elle sayılan uzunluk (yaygın off-by-one kaynağı) kalkar."
      - "Okunabilirlik: str_starts_with($sku, 'FLO-')."
    risks:
      - "Yok."
    before: "if (substr($sku, 0, 4) === 'FLO-')"
    after: "if (str_starts_with($sku, 'FLO-'))"
    ref: "https://wiki.php.net/rfc/add_str_starts_with_and_ends_with_functions"
    rector: "Rector\\Php80\\Rector\\Identical\\StrStartsWithRector"
  - key: null_coalesce
    since: "7.0"
    kind: mechanical
    unit: file
    auto: true
    sp_unit: 1
    title: "isset($a) ? $a : $b → $a ?? $b"
    summary: "Null birleştirme operatörü varken isset'li üçlü operatör kullanılıyor."
    advantages:
      - "İfade bir kez yazılır; kopyala-yapıştır farkı (isset($a['x']) ? $a['y'] : …) biter."
      - "Undefined array key uyarısı üretmeden güvenli okuma — New Relic'teki 'Undefined array key' hatalarının önleyici biçimi."
    risks:
      - "Yok — ?? tam olarak isset semantiğidir."
    before: "$city = isset($addr['city']) ? $addr['city'] : null;"
    after: "$city = $addr['city'] ?? null;"
    ref: "https://wiki.php.net/rfc/isset_ternary"
    rector: "Rector\\Php70\\Rector\\Ternary\\TernaryToNullCoalescingRector"
  - key: nullsafe
    since: "8.0"
    kind: mechanical
    unit: file
    auto: true
    sp_unit: 1
    title: "$a ? $a->b() : null → $a?->b()"
    summary: "Nesne null olabildiğinde üye erişimi üçlü operatörle korunuyor."
    advantages:
      - "Zincir kısalır, koşul ve erişim tek yerde; 'null üzerinde metot çağrısı' hatasının önleyici biçimi."
    risks:
      - "$a falsy ama null değilse (0, '') eski kod null dönerdi, ?-> çağırır — tarayıcı yalnız null/is_null/=== null karşılaştırmalı yerleri önerir."
    before: "$name = $order->getCustomer() ? $order->getCustomer()->getName() : null;"
    after: "$name = $order->getCustomer()?->getName();"
    ref: "https://wiki.php.net/rfc/nullsafe_operator"
    rector: "Rector\\Php80\\Rector\\Ternary\\TernaryToNullsafeOperatorRector"
  - key: array_destructuring
    since: "7.1"
    kind: mechanical
    unit: file
    auto: true
    sp_unit: 1
    title: "list($a, $b) = … → [$a, $b] = …"
    summary: "Eski list() sözdizimi."
    advantages:
      - "Kısa biçim; anahtarlı yıkım (['id' => $id] = $row) ve foreach içinde kullanım aynı sözdizimiyle."
    risks:
      - "Yok."
    before: "list($code, $message) = explode(':', $line, 2);"
    after: "[$code, $message] = explode(':', $line, 2);"
    ref: "https://wiki.php.net/rfc/short_list_syntax"
    rector: "Rector\\Php71\\Rector\\List_\\ListToArrayDestructRector"
  - key: match
    since: "8.0"
    kind: mechanical
    unit: file
    auto: true
    sp_unit: 1
    title: "Her dalı tek değer döndüren switch → match"
    summary: "switch'in her case'i aynı değişkene atama ya da return yapıyor; break unutma riski taşıyor."
    advantages:
      - "Strict (===) karşılaştırma: '1' == 1 tuzağı biter."
      - "Eşleşmeyen değer sessizce geçmez, UnhandledMatchError fırlatır — eksik dal testte görünür."
      - "İfade olduğu için doğrudan return/atama; break yok, düşme (fall-through) hatası yok."
    risks:
      - "Eski switch'te eşleşmeyen değer sessizce geçiyorsa match'e default eklenmeli — tarayıcı default'suz switch'leri işaretler."
    before: |
      switch ($status) {
          case 'paid': $label = 'Ödendi'; break;
          case 'refund': $label = 'İade'; break;
          default: $label = 'Bilinmiyor';
      }
    after: |
      $label = match ($status) {
          'paid' => 'Ödendi',
          'refund' => 'İade',
          default => 'Bilinmiyor',
      };
    ref: "https://wiki.php.net/rfc/match_expression_v2"
    rector: "Rector\\Php80\\Rector\\Switch_\\ChangeSwitchToMatchRector"
  - key: first_class_callable
    since: "8.1"
    kind: mechanical
    unit: file
    auto: true
    sp_unit: 1
    title: "[$this, 'metot'] / 'fonksiyon' → metot(...)"
    summary: "array_map, usort, array_filter gibi çağrılara callable dizi ya da string olarak veriliyor."
    advantages:
      - "Statik analiz ve IDE metodu görür; yeniden adlandırma kırmaz (string içindeki ad bulunamazdı)."
      - "Yanlış yazılmış metot adı çalışma zamanında değil, analizde yakalanır."
    risks:
      - "Yok."
    before: "array_map([$this, 'normalize'], $rows)"
    after: "array_map($this->normalize(...), $rows)"
    ref: "https://wiki.php.net/rfc/first_class_callable_syntax"
    rector: "Rector\\Php81\\Rector\\Array_\\FirstClassCallableRector"
  - key: ctor_promotion
    since: "8.0"
    kind: mechanical
    unit: file
    auto: true
    sp_unit: 1
    title: "Yalnız $this->x = $x yapan constructor → constructor promotion"
    summary: "Property tanımı, constructor parametresi ve atama üç ayrı yerde tekrar ediyor."
    advantages:
      - "Her bağımlılık tek satır; sınıf gövdesi kısalır, yeni bağımlılık eklemek tek yer."
      - "Tip property'ye de yazılır (örtük typed property kazancı)."
    risks:
      - "Property'nin docblock'unda sınıf dışından okunan bir ek açıklama varsa taşınmalı."
    before: |
      private $client;
      public function __construct(HttpClient $client) { $this->client = $client; }
    after: |
      public function __construct(private HttpClient $client) {}
    ref: "https://wiki.php.net/rfc/constructor_promotion"
    rector: "Rector\\Php80\\Rector\\Class_\\ClassPropertyAssignToConstructorPromotionRector"

  # ── structural: tasarım kararı, insan onayı ──
  - key: typed_properties
    since: "7.4"
    kind: structural
    unit: module
    auto: false
    sp_unit: 5
    title: "Tipsiz property'ler → tipli property"
    summary: "Sınıf alanları tip olmadan tanımlı; tip yalnız docblock'ta ya da hiç yok. Yanlış tipte atama sessizce kabul ediliyor."
    advantages:
      - "Yanlış tip atandığı anda TypeError: hata üretildiği yerde yakalanır, 3 çağrı sonra 'Undefined array key' olarak değil."
      - "IDE ve PHPStan/Sonar sınıfı anlar; otomatik tamamlama ve ölü kod analizi çalışır."
      - "Opcache/JIT tipli property'de daha az tür denetimi yapar."
      - "Sonraki adımların (readonly, promotion, property hooks) ön koşulu."
    risks:
      - "null atanan yerler ?Type ister; başlangıç değeri verilmeyen tipli property okunursa 'must not be accessed before initialization' hatası — her sınıfta atama yolları incelenmeli. Bu yüzden insan kararı."
    before: |
      class Order {
          public $id;
          protected $lines = [];
          private $total;
      }
    after: |
      class Order {
          public int $id;
          /** @var OrderLine[] */
          protected array $lines = [];
          private ?Money $total = null;
      }
    ref: "https://wiki.php.net/rfc/typed_properties_v2"
    rector: "Rector\\TypeDeclaration\\Rector\\Property\\TypedPropertyFromAssignsRector"
  - key: enum
    since: "8.1"
    kind: structural
    unit: module
    auto: false
    sp_unit: 5
    title: "Sabit grubu sınıfı → backed enum"
    summary: "Durum/tip değerleri bir sınıftaki const'larla tutuluyor; metot imzaları string/int alıyor, geçersiz değer derleme zamanında yakalanmıyor."
    advantages:
      - "Parametre tipi Status olur: geçersiz değer fonksiyona giremez, string typo biter."
      - "match ile eksiksizlik: yeni bir durum eklendiğinde ele alınmayan yer UnhandledMatchError ile görünür."
      - "Status::from('paid') / tryFrom() ile DB/API değerleri tek yerden doğrulanır."
      - "Davranış enum'a taşınır (label(), isFinal()) — dağınık if/switch toplanır."
    risks:
      - "Dış API/DB string bekler: ->value ile geçiş, tüm çağrı noktaları güncellenmeli. Sabit sınıfı başka repolardan kullanılıyorsa geçiş adım adım (önce enum + eski sabitler korunur)."
    before: |
      class OrderStatus {
          const PAID = 'paid';
          const REFUND = 'refund';
      }
      function label(string $status): string { … }
    after: |
      enum OrderStatus: string {
          case Paid = 'paid';
          case Refund = 'refund';
          public function label(): string { return match($this) { self::Paid => 'Ödendi', self::Refund => 'İade' }; }
      }
      function label(OrderStatus $status): string { return $status->label(); }
    ref: "https://wiki.php.net/rfc/enumerations"
  - key: dto_from_array
    since: "8.1"
    kind: structural
    unit: module
    auto: false
    sp_unit: 5
    title: "Dizi şekli döndüren metotlar → readonly DTO / value object"
    summary: "Metotlar ['id' => …, 'status' => …] gibi anahtarlı dizi döndürüyor; sözleşme docblock'ta bile yok. Tüketen taraf anahtar adını tahmin ediyor."
    advantages:
      - "'Undefined array key' sınıfı hataların kök nedeni kalkar: alan yoksa derleme/analiz hatası, çalışma zamanı uyarısı değil. (New Relic'ten açılan son kalite işlerinin tamamı bu sınıftandı.)"
      - "IDE alanları gösterir; yeniden adlandırma güvenli."
      - "readonly ile değiştirilemezlik: dizi yolda sessizce mutasyona uğramaz."
      - "Serileştirme tek yerde (toArray/JsonSerializable)."
    risks:
      - "Tüketen tüm yerler değişir; JSON çıktısı korunmalı (JsonSerializable). Büyük iş — modül başına kademeli, insan kapsam belirler."
    before: |
      /** @return array */
      public function toRow(): array { return ['id' => $this->id, 'status' => $this->status, 'total' => $this->total]; }
    after: |
      final readonly class OrderRow implements \JsonSerializable {
          public function __construct(public int $id, public OrderStatus $status, public Money $total) {}
          public function jsonSerialize(): array { return ['id' => $this->id, 'status' => $this->status->value, 'total' => $this->total->amount]; }
      }
      public function toRow(): OrderRow { … }
    ref: "https://wiki.php.net/rfc/readonly_properties_v2"
  - key: property_hooks
    since: "8.4"
    kind: structural
    unit: module
    auto: false
    sp_unit: 5
    title: "Getter/setter çiftleri → property hooks"
    summary: "Private alan + getX()/setX() çiftleri gövdesi basit (döndür / ata / küçük doğrulama)."
    advantages:
      - "Sınıf başına onlarca satır boilerplate kalkar; alan doğrudan $obj->x olarak okunur, doğrulama hook'ta kalır."
      - "Arayüzde property tanımlanabilir (interface'te public string $x { get; })."
    risks:
      - "Public API değişir ($o->getX() → $o->x); çağrı noktaları ve alt sınıflar güncellenmeli. Framework (ORM/serializer) getter bekliyorsa uygulanmaz — insan kararı."
    before: |
      private string $sku;
      public function getSku(): string { return $this->sku; }
      public function setSku(string $v): void { $this->sku = strtoupper($v); }
    after: |
      public string $sku { set => strtoupper($value); }
    ref: "https://wiki.php.net/rfc/property-hooks"
  - key: asymmetric_visibility
    since: "8.4"
    kind: structural
    unit: module
    auto: false
    sp_unit: 3
    title: "private alan + public getter (setter yok) → public private(set)"
    summary: "Alan dışarıdan yalnız okunuyor; bunun için getter yazılmış."
    advantages:
      - "Getter silinir, alan doğrudan okunur, yazma hâlâ sınıf içinde kalır."
      - "readonly'den esnek: sınıf içi yeniden atama serbest."
    risks:
      - "Public API değişir; property_hooks ile aynı kapsam kararı."
    before: |
      private int $count;
      public function getCount(): int { return $this->count; }
    after: |
      public private(set) int $count;
    ref: "https://wiki.php.net/rfc/asymmetric-visibility-v2"
  - key: attributes
    since: "8.0"
    kind: structural
    unit: module
    auto: false
    sp_unit: 3
    title: "Docblock anotasyonları → attribute"
    summary: "Yönlendirme, doğrulama, test sağlayıcı gibi meta veriler yorum satırında (@Route, @Assert, @dataProvider)."
    advantages:
      - "Derleyici doğrular: yanlış yazılmış anotasyon sessizce yok sayılmaz."
      - "IDE 'kullanımları bul' ve yeniden adlandırma çalışır."
      - "PHPUnit 10+ docblock anotasyonlarını kaldırıyor (#[DataProvider], #[Test])."
    risks:
      - "Kullanılan framework/kütüphane sürümünün attribute desteği doğrulanmalı; aksi halde anotasyon işlevini kaybeder."
    before: |
      /** @dataProvider skuProvider */
      public function testNormalize(string $in): void
    after: |
      #[DataProvider('skuProvider')]
      public function testNormalize(string $in): void
    ref: "https://wiki.php.net/rfc/attributes_v2"

  # ── report-only: iş açılmaz, repo kartında gösterilir ──
  - key: strict_types
    since: "7.0"
    kind: report
    unit: file
    auto: false
    sp_unit: 0
    title: "declare(strict_types=1) kapsamı"
    summary: "Dosya başında strict_types yoksa skaler tipler sessizce dönüştürülür ('12abc' → 12)."
    advantages:
      - "Yanlış tip çağrı anında TypeError; tipli property/parametre kazanımı ancak bununla tamamlanır."
    risks:
      - "Repo genelinde geriye dönük açmak kırıcıdır; politika: yeni dosyalarda zorunlu, eski dosyalarda modül bazlı. İş olarak değil politika kararı olarak sunulur."
    before: "<?php\nnamespace App;"
    after: "<?php\ndeclare(strict_types=1);\nnamespace App;"
    ref: "https://wiki.php.net/rfc/scalar_type_hints_v5"
```

- [ ] `php_modern.py`: `Rule` dataclass, `load_rules(path=RULES_PATH)`, `_ver(s) -> tuple[int,int]`, `applicable(rules, version)`, `WEIGHT = {"deprecation": 20, "structural": 10, "mechanical": 8, "report": 0}`.
  `_IMAGE = re.compile(r"/php-(\d+)-(\d+)(?::|$)")`; `parse_php_image`. `target_version(client, repo, *, app_map_cache=None, repo_root=None)`: `app_map.load_map(client)` değerlerinden `repo` eşleşen `src` dizinleri → her biri için `app_map._read(client, devrepo, "/" + path + "/Dockerfile")` `FROM` satırları → en düşük; yoksa `composer.json` `require.php` içindeki ilk `\d+\.\d+`; yoksa `{"version": None, "source": None}`. Sonuç `config/quality_php_targets.yaml` içinde 24 saat önbellek (`CREW_TECHDEBT_SCAN_TTL_H`). `.gitignore`: `quality_php_targets.yaml`, `quality_php_modern_cache/`.
- [ ] Yeşil koş; commit `feat(techdebt): php_modern katalog + hedef PHP sürümü`.

## Task 2 — Token tarayıcı (`php_modern_scan.php`) ve Python sarmalayıcı

**Files:** Create `quality/php_modern_scan.php`, `php_modern.py` (`scan_repo`), fikstürler `tests/fixtures/php_modern/{legacy.php,modern.php,strings.php,ConstGroup.php,GetterSetter.php}`.
**Produces:** `php_modern.scan_repo(repo_root: Path, *, exclude: list[str], php_bin="php", timeout=120) -> dict` → `{"files": N, "sha": "<HEAD>", "hits": [{"rule": key, "file": "app/X.php", "line": 12, "text": "<satır>", "module": "app/Model"}], "adoption": {"enum": n, "readonly": n, "match": n, "nullsafe": n, "attribute": n, "typed_props": n, "untyped_props": n, "strict_types_files": n}}`. `php` yoksa / çıkış ≠ 0 / JSON bozuk → `QualitySourceError`.

- [ ] **Test `test_php_modern_scan`** (`shutil.which(php_bin)` yoksa `skip`): fikstür dizini taranır; `legacy.php` için beklenen kural seti `{implicit_nullable, utf8_encode, string_interp_brace, removed_functions, str_contains, str_starts_with, null_coalesce, nullsafe, array_destructuring, match, first_class_callable, ctor_promotion}` tam eşleşir (her biri satır numarasıyla); `modern.php` hiç bulgu üretmez; `strings.php` (heredoc/`'strpos($a) !== false'` string'i/yorum satırı içinde kalıp) **hiç** bulgu üretmez; `ConstGroup.php` → `enum` bulgusu (3+ aynı tipte sabit, sınıfta metot yok); `GetterSetter.php` → `property_hooks` (get+set çifti) ve `asymmetric_visibility` (yalnız get) ve `typed_properties` (tipsiz alan); `exclude=["vendor"]` ile `vendor/x.php` taranmaz; `adoption["untyped_props"]` doğru; bozuk `php_bin` → `QualitySourceError`.
- [ ] Kırmızı koş → **`php_modern_scan.php`** (argv: kök dizin, virgüllü hariç liste; stdout JSON). `token_get_all` ile: string/heredoc/yorum token'ları atlanır; kural tespitleri:
  - `implicit_nullable`: fonksiyon parametre listesinde tip token(lar)ı + `$x` + `=` + `null`, tip `?` ile başlamıyor ve union içinde `null` yok.
  - `utf8_encode`, `filter_sanitize_string` (`FILTER_SANITIZE_STRING` sabiti), `removed_functions` (`each`, `create_function`, `money_format` fonksiyon çağrısı — önünde `->`/`::`/`function` yok).
  - `string_interp_brace`: `T_DOLLAR_OPEN_CURLY_BRACES`.
  - `str_contains`: `strpos|stripos(` … `)` ardından `!==|===|!=|==` `false`; `str_starts_with`: `substr(` `, 0,` … `)` `===|!==` string, ya da `strpos(...) === 0`.
  - `null_coalesce`: `isset(X) ? X :` (X token dizisi birebir aynı).
  - `nullsafe`: `X ? X->` ya da `X !== null ? X->` ya da `!is_null(X) ? X->` … `: null`.
  - `array_destructuring`: `list(` ardından `=`.
  - `match`: `switch` gövdesinde her `case`/`default` bloğu tam olarak `return expr;` ya da `$aynıDeğişken = expr; break;`; ≥2 case.
  - `first_class_callable`: `[$this, 'ad']` / `[self::class, 'ad']` / `'fonksiyon_adı'` doğrudan `array_map|array_filter|usort|uasort|array_walk|call_user_func(_array)?` argümanı.
  - `ctor_promotion`: `__construct` gövdesi yalnız `$this->p = $p;` atamalarından oluşuyor (her p parametre).
  - `typed_properties`: `T_PUBLIC|T_PROTECTED|T_PRIVATE` (+`static`) hemen ardından `T_VARIABLE` (tip yok).
  - `enum`: sınıfta ≥3 `const` ve hepsi aynı skaler türde (hepsi string ya da hepsi int) ve metot yok; ya da sınıf adı `Status|Type|Kind|State|Enum|Constants?` ile bitiyor ve ≥3 sabit.
  - `dto_from_array`: `return [` ile başlayan ve ≥4 `'anahtar' =>` içeren dizi literal'i döndüren metot.
  - `property_hooks`: aynı sınıfta `getX`+`setX` çifti, gövdeleri ≤3 ifade; `asymmetric_visibility`: `private|protected $x` + `getX` var, `setX` yok ve property başka public yerden yazılmıyor (yalnız `__construct` ve private metotlarda atama).
  - `attributes`: docblock'ta `@(Route|Assert|ORM|dataProvider|test|depends|covers|Serializer|Groups)`.
  - `strict_types`: dosya başında `declare(strict_types` var/yok sayımı (adoption).
  - adoption sayaçları: `T_ENUM`, `T_READONLY`, `T_MATCH`, `T_NULLSAFE_OBJECT_OPERATOR`, `T_ATTRIBUTE`, tipli/tipsiz property.
  Modül: dosya yolunun ilk 2 dizini (`app/Model`), kökte ise dosya dizini. Python: `subprocess.run([php_bin, scan_php, str(root), ",".join(exclude)], timeout=…)`, `git -C root rev-parse HEAD` → `sha`.
- [ ] Yeşil koş; commit `feat(techdebt): php_modern token tarayıcı`.

## Task 3 — Gruplama, skor, dedup ve önbellek

**Files:** `php_modern.py` (`build_groups`, `scan_cached`), `rank.py` (`pack_sonar` → `pack_units(units, pack_sp, pack_files, source="sonar")`; `pack_sonar` ince sarmalayıcı kalır).
**Produces:** `php_modern.build_groups(scan: dict, rules: list[Rule], version: str, *, board_index: dict, now: float, cfg: dict) -> tuple[list[Group], list[dict], dict]` → (gruplar, atlananlar, kural özeti `{key: {"count", "files", "modules", "sp", "auto", "kind"}}`). `Finding(source="php_modern", repo, file, rule=<key>, rule_name=<title>, lines=[…], message=<satır metni>)`; `Group.title = "<kural title> — <modül|dosya>"`, `needs_human = kind == "structural"`, `human_reason = "Tasarım kararı: " + risks[0]`; `kind == "report"` grup üretmez, özete girer. Parmak izi `php_modern:<repo>:<kural>:<dosya|modül>`. `scan_cached(repo_root, …)` → `config/quality_php_modern_cache/<repo>.json` (`sha`, `rules_version`, `built_at`); HEAD sha ya da katalog sürümü değişmişse ya da TTL dolmuşsa ya da `force` ise yeniden tarar.

- [ ] **Test `test_php_modern_groups`**: sahte scan (hits listesi) + kurallar, `version="8.2"` → `property_hooks` grubu yok, `"8.4"` → var; mekanik birimler dosya başına, aynı kural ≤`pack_files` dosyada paketlenir, SP = `sp_unit` toplamı ≤ `pack_sp`; yapısal birim modül başına, SP 5, `needs_human True`; skor `WEIGHT × (1+log10(adet))`, deprecation > structural > mechanical aynı adette; board'da açık fp → `skipped` "açık WI #"; `report` kuralı grup üretmez ama özette; `pack_sonar` eski davranışı korur (`test_quality_rank_sonar` geçer); önbellek: aynı sha ikinci çağrı tarayıcıyı çağırmaz, sha değişince çağırır, `force=True` çağırır.
- [ ] Kırmızı → uygula → yeşil; commit `feat(techdebt): php_modern gruplama/skor/önbellek`.

## Task 4 — Task metni (render)

**Files:** `render.py`: `SOURCE_LABEL["php_modern"] = "PHP modernleştirme"`; `_modern_body(g, rule, target)`; `task_description(g, *, sonar_url, nr_days, rule=None, target="")` php_modern dalı; `expected_behavior` / `acceptance_criteria` php_modern dalı.

- [ ] **Test `test_php_modern_render`**: açıklama ilk satır `Repo:` (`repo_from_description` çözer); `Hedef PHP: 8.4` satırı; kural başlığı + "PHP 8.0'dan beri"; **Avantajlar** ve **Riskler** listeleri katalogdan; Önce/Sonra `<pre>` içinde HTML-escape'li; Bulunan yerler `dosya:satır` + satır metni `_e()` ile (içinde `tempo-fp` geçen satır defang edilir, `password=…` maskelenir), en çok 20 + "+N daha"; `tempo-fp:` satırları; `parse_fingerprints` geri okur; mekanik için beklenen davranış "dış davranış değişmez… yalnız listelenen yerler… dar birim testi… ortama bağlı kontrol yazılmaz"; yapısal için "tasarım kararını insan verir… etiket kaldırılınca kuyruğa alınabilir"; `acceptance_criteria` mekanik: "listelenen yerlerde eski yapı kalmaz; testler geçer; davranış değişmez"; `acceptance_block` AC alanı dolu; `task_tags(g, lane="techdebt")` = `tempo-quality; tempo-techdebt; tech-debt; tempo-src-php_modern` (+`needs-human`).
- [ ] Kırmızı → uygula → yeşil; commit `feat(techdebt): php_modern Task metni (avantaj/risk/önce-sonra)`.

## Task 5 — Repo listesi, tarama ve iş açma (`php_modern.repos/scan/take`)

**Files:** `php_modern.py`.
**Produces:** `repos(client, *, deps) -> dict` (`items: [{repo, version, source, images, adoption?}]`, yerel PHP repoları = `knowledge.detect_repo_type == "php"` ya da `composer.json` var); `scan(client, repo, *, team="", force=False, deps) -> dict` (`{"repo", "version", "sha", "files", "adoption", "rules": özet, "auto": [grup satırı], "human": [grup satırı], "skipped_counts", "scanned_at"}`; grup satırı `_row(g)` + `{"gid": fp'lerin sha1[:12], "rule": key, "kind", "since", "title", "summary", "advantages", "risks", "before", "after", "ref", "count", "files", "module", "locations": [{file,line,text}][:20]}`); `take(client, repo, group_ids, *, team, deps, max_take) -> dict`.

- [ ] **Test `test_php_modern_take`** (sahte client + `Deps`): `scan` yazma yapmaz; `take` `_LOCK` altında (kilitliyken `Busy`), gid'leri taze taramaya (aynı sha) karşı doğrular — bilinmeyen gid → `ValueError("tarama değişti, yeniden tarayın")`; `max_take` aşımı → atlanan; story `resolve_story(lane="techdebt")` ile "Teknik borç — <sprint>", `tempo-techdebt`; mekanik grup Task + `deps.queue` çağrılır, yapısal grup Task açılır **kuyruğa alınmaz** (`job_id None`, yanıtta `human_tasks`); `quality_items` `source="php_modern"`; story SP güncellenir; board'da açık fp olan gid 'açık WI #' ile atlanır; `version None` repo → `ValueError("hedef PHP sürümü bilinmiyor")`; `repo_is_local` false → `ValueError`.
- [ ] Kırmızı → uygula → yeşil; commit `feat(techdebt): php_modern tara/aç akışı`.

## Task 6 — Ayarlar ve uçlar

**Files:** `pipeline_config.py` (5 knob: `CREW_TECHDEBT_PHP_MODERN` bool false; `CREW_TECHDEBT_PATH_EXCLUDE` str `vendor,node_modules,app/Themes,tmp,webroot,public,storage,cache,Test,tests`; `CREW_TECHDEBT_SCAN_TTL_H` int 24 min 1; `CREW_TECHDEBT_MODERN_MAX_TAKE` int 8 min 1; `CREW_PHP_BIN` str `php`), `server.py` (`GET /api/techdebt/modern/repos`, `GET /api/techdebt/modern/scan?repo=&team=&force=`, `POST /api/techdebt/modern/take` + `TechdebtModernTakeRequest(repo: str, group_ids: list[str], team: str = "")`).

- [ ] **Test `test_php_modern_endpoints`** (TestClient, `pipeline_config.get` monkeypatch): `CREW_TECHDEBT` ya da `CREW_TECHDEBT_PHP_MODERN` kapalı → 403; `repo` boş → 400; `group_ids` boş → 400; `QualitySourceError` → 502; `Busy` → 409; `ValueError` → 400; başarılı take ve kuyruk → 202 + `_ensure_worker` çağrıldı; knob'lar `pipeline_config` şemasında ve varsayılanları doğru; `CREW_PHP_BIN` yalnız binary adı/yol (boşluk ve `;` reddedilir — subprocess liste argümanı, shell yok).
- [ ] Kırmızı → uygula → yeşil; commit `feat(techdebt): php_modern ayarları ve uçlar`.

## Task 7 — Arayüz

**Files:** `web/index.html` (`paneTechdebt` içinde `tdModernCard`).

- [ ] **Test `test_php_modern_ui`** (html grep): `tdModernCard`, `tdmRepo` (select), `tdmScan` (Tara), `tdmTake` (Seçilenleri iş olarak aç), `loadModernRepos`, `modernScan`, `modernTake`, `renderModern`; iki grup başlığı "Otomatik işler" ve "İnsan kararı gerektiren yapısal işler"; satır ayrıntısında "Avantajlar", "Riskler", "Önce", "Sonra", "Bulunan yerler"; hedef sürüm chip'i `tdmTarget`; benimseme özeti `tdmAdoption`; `humanChip` kullanımı; `_tdTeam()` ile takım; kart yalnız `/api/techdebt/modern/repos` 200 dönünce görünür (403'te gizli); tema renkleri (`var(--text)`, `var(--text-dim)`) — sabit renk yok.
- [ ] Uygula: kart başlığı "Modernleştirme (PHP)"; repo seçici (`"<repo> · PHP 8.4"`, sürümü bilinmeyen repo `disabled` + "hedef sürüm bilinmiyor"); Tara (ve "Yeniden tara" = force); özet satırı: `N dosya · hedef 8.4 · benimseme: enum 1 · readonly 11 · match 10 · ?-> 3 · attribute 0 · tipli property %24`; kural özeti chip'leri (kural · adet · kind rengi: deprecation=warning, mechanical=accent, structural=human); iki tablo: checkbox · Kural · Yer (modül/dosya +N) · Adet · SP · Durum (`açık WI #`, `İnsan işi`, `Kuyruğa alınır`) · ▸ ayrıntı (`<details>`: Ne, Avantajlar, Riskler, Önce/Sonra `<pre>`, Bulunan yerler `dosya:satır` listesi, RFC bağlantısı); "Seçilenleri iş olarak aç" → `POST take` → sonuç satırı (story bağlantısı, açılan/atlanan) → yeniden tarama. İstek sürerken butonlar kilitli.
- [ ] Yeşil koş; commit `feat(techdebt): Modernleştirme (PHP) kartı`.

## Task 8 — Doküman, sunucu, canlı doğrulama

- [ ] `CLAUDE.md` Tech debt notuna php_modern paragrafı (hedef sürüm kaynağı, token tarayıcı, katalog, mekanik/yapısal ayrımı, knob'lar, Rector 2b). Spec durum satırı "2a uygulandı".
- [ ] Tüm test dosyası yeşil; sunucu `running:0` + backfill `running:false` kontrolüyle yeniden başlat; `.env`'e `CREW_TECHDEBT_PHP_MODERN=1` (kullanıcı kararı; kapalıysa kart görünmez).
- [ ] Canlı **yalnız okuma**: `GET /api/techdebt/modern/repos` (webservice 8.4, alfred 8.2, sorting 7.4 bekleniyor), `GET /api/techdebt/modern/scan?repo=webservice` süresi ve grup sayıları raporlanır. Canlı `take` kullanıcı onayıyla.
- [ ] Commit `docs(techdebt): php_modern notları`; PR #31 dalına push.
