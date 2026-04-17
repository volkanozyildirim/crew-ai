# Backend Feature Development Guide — PHP/Butterfly + Go/Gin

## Degisiklik Yapma Prensipleri

### Mevcut Kodu Oku ONCE
1. Degistirecegin dosyayi TAMAMINI oku — sadece ilgili metodu degil
2. Import/namespace yapisini anla
3. Indentation stilini (tab/4-space/2-space) tespit et
4. Naming convention'i gözlemle (camelCase/snake_case)
5. Mevcut pattern'i benimse — "daha iyi" diye yeni pattern getirme

### Kod Yazimi

#### PHP
```php
<?php

namespace App\Service\Order;

use App\Model\Order;
use App\Repository\OrderRepository;

class OrderCancellationService
{
    public function __construct(
        private readonly OrderRepository $orders,
    ) {}

    public function cancel(int $orderId, string $reason): Order
    {
        $order = $this->orders->findOrFail($orderId);

        if (!$order->isCancellable()) {
            throw new \DomainException('Siparis iptal edilemez');
        }

        $order->cancel($reason);
        $this->orders->save($order);

        return $order;
    }
}
```

#### Go
```go
package order

import (
    "context"
    "fmt"
)

type CancellationService struct {
    repo OrderRepository
}

func NewCancellationService(repo OrderRepository) *CancellationService {
    return &CancellationService{repo: repo}
}

func (s *CancellationService) Cancel(ctx context.Context, orderID int64, reason string) (*Order, error) {
    order, err := s.repo.FindByID(ctx, orderID)
    if err != nil {
        return nil, fmt.Errorf("find order: %w", err)
    }

    if !order.IsCancellable() {
        return nil, ErrOrderNotCancellable
    }

    if err := order.Cancel(reason); err != nil {
        return nil, fmt.Errorf("cancel order: %w", err)
    }

    if err := s.repo.Save(ctx, order); err != nil {
        return nil, fmt.Errorf("save order: %w", err)
    }

    return order, nil
}
```

### Degisiklik Scope'u

- **Minimal degisiklik**: sadece planda belirtilen dosyalari degistir
- **SIDE EFFECT YOK**: Ilgisiz fonksiyonlari refactor etme
- **Geri uyumluluk**: Public API (method signature, response schema) BOZMA
- **Deprecate**: Eski metodu silmeden once `@deprecated` isaretle

### Import/Use Ekleme

- SADECE kullanilan import'lari ekle
- Alfabetik sirala (IDE/linter gibi)
- Mevcut import gruplarina uygun yere ekle
- Unused import BIRAKMA

### Transaction Yonetimi

```php
// PHP - Butterfly
DB::transaction(function () use ($data) {
    $order = Order::create($data);
    $this->inventory->reserve($order->items);
    event(new OrderCreated($order));
});
```

```go
// Go
tx, err := db.BeginTx(ctx, nil)
if err != nil { return err }
defer tx.Rollback()

if err := createOrder(ctx, tx, data); err != nil {
    return err
}
if err := reserveInventory(ctx, tx, items); err != nil {
    return err
}

return tx.Commit()
```

### Logging

- **Structured logging**: key-value formatinda
- **Log seviyeleri**: DEBUG (dev only), INFO (important state), WARN (unexpected), ERROR (failure)
- **PII maskele**: email, telefon, kart bilgisi log'lanmamali

### Testing Gerekli

Degisiklik ile birlikte test de guncellenmelidir:
- Unit test: tek sinif/method
- Integration test: DB veya external servisle
- Edge case'ler: null, empty, max value, concurrent access

## KESIN YAPILMAYACAKLAR

1. **Business logic Controller'da** olmaz
2. **`new` ile somut class** kullanilmaz (DI container uzerinden)
3. **`dd()`, `var_dump()`, `fmt.Println()` debug kod** birakilmaz
4. **Commented-out code** birakilmaz
5. **Magic number** (sihirli sayi) — named constant kullan
6. **Global state** degistirilmez
7. **Hassas bilgi hardcode** edilmez — env/config dosyasindan
8. **Inline SQL** yerine query builder/prepared statement

## DOGRULAMA

Dosyayi push etmeden once:
- [ ] Syntax: `php -l` veya `go build`/`gofmt`
- [ ] Unused import/variable yok
- [ ] Naming convention uyumlu
- [ ] Hata yonetimi var
- [ ] Plan ile birebir ayni
