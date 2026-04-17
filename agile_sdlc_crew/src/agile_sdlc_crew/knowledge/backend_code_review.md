# Backend Code Review Checklist — PHP/Butterfly + Go/Gin

## Is Uyumu (ilk kontrol)

- [ ] PR sadece work item'da istenen dosyalari mi degistirmis?
- [ ] Gereksiz/alakasiz dosya var mi?
- [ ] TUM kabul kriterleri karsilaniyor mu?
- [ ] Ornek/pattern belirtilmisse dogru uygulanmis mi?

## Kritik Guvenlik Kontrolleri

### SQL Injection
- [ ] Raw SQL yerine prepared statement/query builder kullanilmis mi?
- [ ] Kullanici input'u query'e dogrudan concat edilmiyor mu?

### Input Validation
- [ ] Tum kullanici input'lari validate ediliyor mu?
- [ ] File upload varsa: MIME type, boyut, path traversal kontrolu?
- [ ] Email/URL/telefon vb. format validation?

### Authentication / Authorization
- [ ] Endpoint auth middleware'e sahip mi?
- [ ] Kullanicinin kendi verisine erisip erismedigi kontrol ediliyor mu? (IDOR)
- [ ] Admin endpoint'leri role check yapiyor mu?

### Data Exposure
- [ ] Response'ta hassas veri (password hash, token, iç sistem id'leri) sizmiyor mu?
- [ ] Log'larda PII maskelenmis mi?
- [ ] Error mesajlari stack trace/detay sizdirmiyor mu?

## SOLID & Clean Code

### Single Responsibility
- [ ] Her class/fonksiyon TEK bir is yapiyor mu?
- [ ] Controller'da business logic var mi? (VARSA kirmizi bayrak)

### Open/Closed
- [ ] Degisiklik mevcut kodu bozmadan genisletme yapiyor mu?
- [ ] if/else zinciri yerine polymorphism/strategy kullanilabilir mi?

### Dependency Inversion
- [ ] Somut class'lara degil interface/abstraction'lara bagimli mi?
- [ ] Testability: dependency injection kullaniliyor mu?

### DRY
- [ ] Kod tekrari var mi? (3. tekrarda refactor)

### Naming
- [ ] Method/variable isimleri NE YAPTIGINI aciklasin
- [ ] Kisaltma yerine anlasilir isim (`custAddr` → `customerAddress`)
- [ ] Boolean'lar `is/has/can` prefix: `isActive`, `hasPermission`
- [ ] Turkce/Ingilizce karismasin — projedeki conventiona uy

## PHP Ozel Kontrolleri

- [ ] Type hint: `function foo(int $id): ?User` — return type tanimli mi?
- [ ] Null check: `$user->name` yerine `$user?->name` veya explicit check
- [ ] Exception handling: `try/catch` sadece ihtiyac oldugu yerde
- [ ] N+1 query: `->with('relation')` eager load
- [ ] `array_map`, `array_filter` yerine generator/yield buyuk koleksiyonlarda

## Go Ozel Kontrolleri

- [ ] Error handling: her `err` check ediliyor mu?
- [ ] Goroutine leak: context ile cancel edilebiliyor mu?
- [ ] Defer ile cleanup (`defer db.Close()`, `defer mu.Unlock()`)
- [ ] Struct tag'leri dogru mu? (json, validate, gorm)
- [ ] Panic yerine error return
- [ ] Mutex kullaniyorsa race condition var mi? (`go test -race`)

## Performance

- [ ] Dongu icinde DB query var mi? (N+1)
- [ ] Cache mekanizmasi uygun mu? (Redis TTL dogru mu?)
- [ ] Pagination: buyuk list'ler pagination ile donuyor mu?
- [ ] Index: where/order by kolonlari index'li mi?

## Edge Cases

- [ ] Bos input (null, empty string, empty array)
- [ ] Cok buyuk input (max length/size)
- [ ] Negative sayi, sifir, cok buyuk sayi
- [ ] Unicode/emoji karakterler
- [ ] Timezone: UTC'de saklanip UI'da local'e cevriliyor mu?
- [ ] Concurrent erisim: race condition?

## Karar

- **ONAY**: Tum kriterler karsilanmis, gonderilebilir
- **DEGISIKLIK GEREKLI**: Yukaridaki sorunlardan biri var, duzeltilmeli
