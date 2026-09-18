# PHP 8+ — güncel kullanım

Ana stack. Eski kalıpla yazılmış **yeni** kod hem okunmaz hem Sonar'da borç
yazar. Aşağıdakiler öneridir: kullanılmaması tek başına blocker değil,
`minor`'dır — blocker olması için SKILL.md'deki üç kapıdan birine ya da bir
Sonar ihlaline girmesi gerekir.

## Tip güvenliği

```php
// eski
function find($id) { ... }

// güncel
function find(int $id): ?User { ... }
```

- Parametre **ve** dönüş tipi yazılmış olmalı; dönüşü yoksa `: void`.
- Hiç dönmeyen (her zaman fırlatan/çıkan) fonksiyon: `: never`.
- `declare(strict_types=1);` dosyanın konvansiyonuysa yenisinde de olsun.
- Union/nullable açıkça: `int|string`, `?User`.

## Constructor property promotion + readonly

```php
// eski
class OrderService {
    private OrderRepository $repo;
    public function __construct(OrderRepository $repo) { $this->repo = $repo; }
}

// güncel
final class OrderService {
    public function __construct(
        private readonly OrderRepository $repo,
    ) {}
}
```

`readonly` değişmemesi gereken bağımlılıkta; `final` genişletilmesi
planlanmayan sınıfta.

## Enum — sabit sınıfı yerine

```php
// eski
class Status { const ACTIVE = 'active'; const PASSIVE = 'passive'; }

// güncel
enum Status: string {
    case Active = 'active';
    case Passive = 'passive';

    public function label(): string {
        return match ($this) {
            self::Active  => 'Aktif',
            self::Passive => 'Pasif',
        };
    }
}
```

Enum, geçersiz değeri tip seviyesinde engeller — Sonar'ın "magic string"
bulgusunu da kökten bitirir.

## match — switch yerine

```php
// güncel: katı karşılaştırma (===), dönüş değeri var, break gerekmez
$rate = match (true) {
    $amount >= 10_000 => 0.20,
    $amount >= 1_000  => 0.10,
    default           => 0.0,
};
```

`switch` gevşek karşılaştırma yapar ve `break` unutulması klasik bir bug'dır.

## Nullsafe ve null coalescing

```php
$city = $order?->customer?->address?->city ?? 'bilinmiyor';
$limit = $request->get('limit') ?? 50;
$config['ttl'] ??= 300;
```

Uzun `isset()` zincirlerinin yerini alır. Ama **sessiz null** iş kuralıysa
gizlememeli — null anlamlıysa açıkça ele al.

## Diğer güncel kalıplar

- **Named arguments** — çok parametreli çağrıda okunabilirlik:
  `createOrder(customerId: $id, isGift: true)`. Boolean parametre
  gönderiyorsan adlandır.
- **First-class callable**: `array_map($this->toDto(...), $rows)`.
- **Spread / variadic**: `function sum(int ...$n)`.
- **Sayı ayracı**: `10_000_000`.
- **`str_contains` / `str_starts_with` / `str_ends_with`** —
  `strpos($h,$n) !== false` yerine.
- **`array_is_list`**, **`array_find`** (8.4) — elle döngü yerine.
- **Exception zinciri**: `throw new DomainException('...', previous: $e);`

## Sık yapılan hatalar (incelemede ara)

- `==` kullanımı — `===` olmalı (Sonar bunu işaretler).
- Kontrolsüz `null` — `$user->name` çağrısı `$user` null olabiliyorsa.
- **N+1 sorgu** — döngü içinde repository/DB çağrısı. Eager load / tek
  sorguda topla.
- Controller içinde iş kuralı — service'e taşınmalı (SRP).
- `try/catch` ile her şeyi sarıp yutmak; `catch (\Exception)` çok geniş.
- `array_map`/`array_filter` ile büyük koleksiyon — bellek şişer, `yield`
  düşün.
- Gömülü sır: `$apiKey = 'sk_live_...'` — anında vulnerability.
- `@` ile hata bastırma, `eval`, `extract`, `global`.

## Butterfly / framework notu

Projedeki mevcut yapıyı taklit et: repository/service ayrımı, DI container
kaydı, request validation katmanı nasıl yapılıyorsa yeni kod da öyle yapsın.
Farklı bir kalıp öneriyorsan repoda o kalıbın yaşadığı yeri göster
(`precedent`) — gösteremiyorsan önerme.
