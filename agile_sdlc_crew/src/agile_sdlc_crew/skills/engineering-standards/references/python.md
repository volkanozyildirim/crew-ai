# Python — güncel kullanım

İkincil stack (AI/ML servisleri). Aşağıdakiler öneridir; blocker olması için
SKILL.md'deki üç kapıdan birine ya da bir Sonar ihlaline girmesi gerekir.

## Tipler

```python
# güncel (3.10+)
def find(order_id: int) -> Order | None: ...
def process(items: list[str], opts: dict[str, Any] | None = None) -> None: ...
```

- `Optional[X]` yerine `X | None`; `List/Dict` yerine `list/dict`.
- `from __future__ import annotations` dosyanın konvansiyonuysa koru.
- Veri taşıyan sınıf: `@dataclass(frozen=True, slots=True)` ya da pydantic
  modeli. Elle `__init__` yazma.

## Sık yapılan hatalar (incelemede ara)

- **Çıplak `except:`** veya `except Exception: pass` — hata yutulur, Sonar
  işaretler. En dar exception'ı yakala, gerekçesiz yutma.
- **Değişebilir varsayılan argüman**: `def f(x: list = [])` → `None` + içeride
  kur. Klasik bug, Sonar bug sayar.
- `eval` / `exec` / `pickle.loads` güvenilmeyen veriyle → **blocker**.
- `subprocess(..., shell=True)` kullanıcı girdisiyle → komut injection,
  **blocker**.
- `assert` ile üretim kontrolü — `-O` ile derlenince kaybolur.
- `open()` yerine `with open()`; yol işlemleri `pathlib.Path` ile.
- `%` / `.format()` yerine f-string; log'da ise `logger.info("x=%s", x)`
  (tembel biçimlendirme).
- `type(x) == Foo` yerine `isinstance(x, Foo)`.
- Döngü içinde DB/HTTP çağrısı (N+1); `asyncio.gather` uygunsa kullan.
- Async fonksiyonda bloklayan çağrı (`requests`, `time.sleep`) — event loop'u
  kilitler.
- Gömülü sır; `verify=False` ile TLS doğrulamasını kapatma → **blocker**.
