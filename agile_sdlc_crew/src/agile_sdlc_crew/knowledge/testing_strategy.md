# Testing Strategy Guide

## Test Piramidi

```
       /\
      /E2\       — az (happy path)
     /----\
    / INT \     — orta (feature-level)
   /------\
  /  UNIT  \   — cok (her fonksiyon)
 /----------\
```

- **Unit (70%)**: Tek fonksiyon/sinif — hizli, bagimsiz, mock'lu
- **Integration (20%)**: Gercek DB/API — feature level
- **E2E (10%)**: Happy path — smoke test

## Test Senaryolari Cikarma

Her degisiklik icin uret:

### Happy Path (basarili senaryolar)
- Normal input, normal davranis
- En az 1 positive test

### Error Cases
- Invalid input (null, empty, yanlis tip)
- Authorization failure (403)
- Resource not found (404)
- Conflict (409) — duplicate, concurrent modification
- Validation error (422)
- External service failure (500, timeout)

### Edge Cases
- Bos koleksiyon, null, empty string
- Maksimum/minimum deger (INT_MAX, 0, -1)
- Unicode/emoji karakterler
- Timezone farkliliklari
- Concurrent erisim (race condition)
- Cok buyuk payload (> 10MB)

### Business Rules
- Kabul kriterlerinde belirtilen kurallar
- Durum gecisleri (state machine): hangi state'ten hangisine
- Permission matrisi: kim ne yapabilir

## PHP PHPUnit Test Pattern

```php
<?php

namespace Tests\Unit\Service\Order;

use App\Service\Order\OrderCancellationService;
use App\Model\Order;
use PHPUnit\Framework\TestCase;

class OrderCancellationServiceTest extends TestCase
{
    private OrderCancellationService $service;
    private $orderRepo;

    protected function setUp(): void
    {
        $this->orderRepo = $this->createMock(OrderRepository::class);
        $this->service = new OrderCancellationService($this->orderRepo);
    }

    public function test_cancels_cancellable_order(): void
    {
        $order = Order::factory()->pending()->make();
        $this->orderRepo->method('findOrFail')->willReturn($order);
        $this->orderRepo->expects($this->once())->method('save');

        $result = $this->service->cancel($order->id, 'musteri talebi');

        $this->assertEquals('cancelled', $result->status);
    }

    public function test_throws_when_order_not_cancellable(): void
    {
        $order = Order::factory()->delivered()->make();
        $this->orderRepo->method('findOrFail')->willReturn($order);

        $this->expectException(\DomainException::class);

        $this->service->cancel($order->id, 'test');
    }
}
```

## Go Test Pattern

```go
package order_test

import (
    "context"
    "testing"

    "github.com/stretchr/testify/assert"
    "github.com/stretchr/testify/mock"
)

func TestCancellationService_Cancel(t *testing.T) {
    tests := []struct {
        name      string
        orderID   int64
        setupMock func(*MockRepo)
        wantErr   error
    }{
        {
            name:    "cancellable order",
            orderID: 1,
            setupMock: func(r *MockRepo) {
                r.On("FindByID", mock.Anything, int64(1)).Return(&Order{Status: "pending"}, nil)
                r.On("Save", mock.Anything, mock.Anything).Return(nil)
            },
            wantErr: nil,
        },
        {
            name:    "delivered order cannot cancel",
            orderID: 2,
            setupMock: func(r *MockRepo) {
                r.On("FindByID", mock.Anything, int64(2)).Return(&Order{Status: "delivered"}, nil)
            },
            wantErr: ErrOrderNotCancellable,
        },
    }

    for _, tt := range tests {
        t.Run(tt.name, func(t *testing.T) {
            repo := new(MockRepo)
            tt.setupMock(repo)
            svc := NewCancellationService(repo)

            _, err := svc.Cancel(context.Background(), tt.orderID, "reason")
            assert.ErrorIs(t, err, tt.wantErr)
            repo.AssertExpectations(t)
        })
    }
}
```

## Coverage Hedefleri

- **Unit**: %80+ (business logic katmani)
- **Integration**: Kritik flow'lar (checkout, payment, cancel)
- **E2E**: Happy path + 1-2 critical error

## Mockleme Kurallari

- Third-party API mockla (tum external service)
- DB: memory DB (SQLite) veya docker container
- Time: `freeze_time('2026-01-01')` kullan, `now()` cagirma
- Random: seed ile kontrol et

## Test Plan Formati

```markdown
## Test Plani — #WI_ID

### Happy Path
1. [scenario] -> [expected result]

### Error Cases
1. Invalid input -> 400 Bad Request
2. Unauthorized -> 401
3. Not found -> 404

### Edge Cases
1. Empty list
2. Max value
3. Concurrent request

### Onerilen Unit Testler
- test_cancels_cancellable_order()
- test_throws_when_order_not_cancellable()
- test_emits_cancellation_event()
```
