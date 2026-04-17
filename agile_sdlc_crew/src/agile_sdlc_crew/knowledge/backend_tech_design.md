# Backend Technical Design Guide — PHP/Butterfly + Go/Gin

## PHP / Butterfly Framework Patterns

### Module yapisi
- Her module `app/` altinda Controller, Model, Service, Widget, Cronjob dizinleri icerir
- `Module.php` module'un bootstrap'i — boot, routes, hooks tanimlari
- Controller dosyalari: `app/Controller/Api/{Name}.php` API icin, `app/Controller/{Name}.php` web icin
- Model sinif ismi dosya ismiyle ayni olmali (PSR-4)

### Service Layer
- Is mantigi Controller'da DEGIL, Service'te olmali
- Servisler `app/Service/{Domain}/{Name}Service.php` pattern'inde
- Dependency injection: constructor uzerinden, `new` kullanma
- Butterfly DI container: `app()->get(FooService::class)`

### Controller kurallari
- Method imzasi: `public function actionName(Request $request, Response $response)`
- Response JSON donerken `->json(['data' => $data])`
- Validation: `app/Rule/` altinda form request class'lari
- HTTP status kodlari: 200 (OK), 201 (created), 400 (bad request), 404, 422 (validation), 500

### Model / Database
- Butterfly ORM — `\Butterfly\Orm\Model` extend edilir
- Relation tanimlari: `protected $relations = ['foo' => [self::HAS_MANY, FooModel::class, 'parent_id']]`
- Scope methodlari: `public function scopeActive($query)` → `Model::active()->get()`
- Migration dosyalari `database/Migrations/` altinda timestamp-named

### Routing
- `routes/api.php`, `routes/web.php`
- `Route::get('/path', 'Controller@method')->name('route.name')`
- Middleware: `Route::middleware(['auth', 'throttle'])->group(fn() => ...)`

## Go / Gin Patterns

### Project Layout
```
cmd/server/main.go      — entry point
internal/handler/       — HTTP handlers (Gin)
internal/service/       — business logic
internal/repository/    — data access
internal/model/         — structs
pkg/                    — public shared libs
```

### Handler
```go
func CreateOrder(c *gin.Context) {
    var req CreateOrderRequest
    if err := c.ShouldBindJSON(&req); err != nil {
        c.JSON(400, gin.H{"error": err.Error()})
        return
    }
    // ... call service
    c.JSON(200, gin.H{"data": result})
}
```

### Service
- Interface tanimi pakette, implementasyon `_impl.go`'da
- Error wrapping: `fmt.Errorf("create order: %w", err)`
- Context propagation: her fonksiyon `ctx context.Context` alir

### Error Handling
- `errors.Is` ve `errors.As` kullan
- Sentinel errors: `var ErrNotFound = errors.New("not found")`
- Gin middleware'de recover + structured logging

## API Design Cheatsheet

- RESTful endpoints: `GET /resources`, `POST /resources`, `PUT /resources/:id`, `DELETE /resources/:id`
- Versiyonlama: `/api/v1/...`
- Pagination: `?page=1&per_page=20` veya cursor
- Filtering: `?filter[status]=active`
- Response envelope: `{"data": ..., "meta": {...}, "errors": []}`

## Kritik Kurallar

1. **Business logic** ASLA Controller/Handler'da olmaz — Service'e at
2. **Veritabani erisimi** Repository/Model'de, Service'te DEGIL
3. **Validation** Controller'a girmeden yapilir (Request/DTO)
4. **Transaction**'lar Service katmaninda baslar
5. **Logging**: yapılandırılmış (structured) log, PII maskele
6. **Hata mesajlari** kullaniciya teknik detay sizdirmaz
7. **Feature flag** ile deploy — riskli degisiklikler flag arkasinda
