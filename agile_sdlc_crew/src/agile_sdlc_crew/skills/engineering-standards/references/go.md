# Go 1.21+ — güncel kullanım

Ana stack. Aşağıdakiler öneridir: kullanılmaması tek başına blocker değil,
`minor`'dır — blocker olması için SKILL.md'deki üç kapıdan birine ya da bir
Sonar ihlaline girmesi gerekir. Ama **kontrol edilmeyen `err` her zaman
blocker'dır**: Sonar da bug sayar.

## Hata yönetimi

```go
// eski
if err != nil {
    return fmt.Errorf("sipariş okunamadı: %s", err.Error())
}

// güncel — sarmala (%w), zincir korunsun
if err != nil {
    return fmt.Errorf("sipariş okunamadı (id=%d): %w", id, err)
}
```

- Karşılaştırma: `errors.Is(err, ErrNotFound)`, tip için `errors.As`.
  `err == ErrX` ve `strings.Contains(err.Error(), ...)` **yanlıştır**.
- Sentinel hata: `var ErrNotFound = errors.New("not found")`.
- Birden çok hata: `errors.Join(err1, err2)` (1.20+).
- `_ = err` ya da hiç kontrol etmemek → blocker.
- Kütüphane kodunda `panic` yok; hata döndür. `panic` yalnızca programcı
  hatası (imkânsız durum) için.

## Context

```go
func (s *Service) Fetch(ctx context.Context, id int64) (*Order, error) {
    ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
    defer cancel()
    return s.repo.Get(ctx, id)
}
```

- `context.Context` **ilk parametre**, struct alanı değil.
- Dış çağrıların (DB, HTTP, queue) hepsine ctx geçir.
- `context.Background()` yalnızca en tepede (main, job başlangıcı).
- Goroutine başlatıyorsan iptal edilebilir olmalı — yoksa sızıntı.

## Standart kütüphane (elle döngü yerine)

```go
// 1.21+
slices.Contains(ids, id)
slices.SortFunc(orders, func(a, b Order) int { return cmp.Compare(a.ID, b.ID) })
idx, ok := slices.BinarySearch(sorted, x)
maps.Keys(m)          // 1.23+ iterator
min(a, b); max(a, b)  // 1.21 builtin
clear(m)              // 1.21 builtin
```

## Yapılandırılmış log — log/slog (1.21+)

```go
slog.Info("sipariş işlendi",
    "order_id", order.ID,
    "duration_ms", time.Since(start).Milliseconds(),
)
```

`fmt.Printf`/`log.Println` yerine. PII log'lama.

## Eşzamanlılık

```go
g, ctx := errgroup.WithContext(ctx)
for _, id := range ids {
    g.Go(func() error { return process(ctx, id) })  // Go 1.22+: döngü değişkeni güvenli
}
if err := g.Wait(); err != nil { return err }
```

- **Go 1.22 öncesinde** döngü değişkenini goroutine'de yakalamak klasik
  bug'dır (`id := id` gerekir). Repo `go.mod`'daki sürüme bak.
- `sync.Mutex` kullanımı varsa `go test -race` ile doğrulanabilir olmalı.
- Kanal kapatmayı **yazan** taraf yapar.

## Diğer

- `any` yerine somut tip ya da generics (`[T comparable]`).
- `defer` ile temizlik: `defer rows.Close()`, `defer mu.Unlock()`.
  `defer`'i döngü içinde kullanma — fonksiyon sonuna kadar birikir.
- Struct tag'leri: `json`, `db`, `validate` — doğru ve tutarlı.
- Interface'i **tüketen** taraf tanımlar, üreten değil; küçük tut (ISP).
- `time.Time` UTC saklansın.
- Sıfır değeri kullanışlı olsun (`var buf bytes.Buffer` kullanıma hazır).

## Sık yapılan hatalar (incelemede ara)

- Kontrol edilmeyen `err` — **blocker**.
- Goroutine sızıntısı: ctx/cancel yok, kanal okunmuyor.
- `defer resp.Body.Close()` unutulmuş HTTP çağrısı.
- Döngü içinde DB sorgusu (N+1).
- `panic` kütüphane yolunda.
- Gömülü sır, `http://` (TLS yok), `InsecureSkipVerify: true`.
- `fmt.Sprintf` ile SQL kurma → injection, **blocker**.
- Çok büyük fonksiyon / iç içe if — cognitive complexity borcu.

## Gin notu

Handler ince olsun: bind + validate + service çağrısı + yanıt. İş kuralı
handler'da olmamalı (SRP). Hata yanıtı projedeki mevcut kalıba uysun;
farklı bir kalıp öneriyorsan repoda örneğini göster (`precedent`).
