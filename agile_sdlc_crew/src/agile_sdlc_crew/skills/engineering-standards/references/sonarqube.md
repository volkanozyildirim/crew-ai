# SonarQube — kapı ve borç üreten kurallar

Repolarda `sonar.qualitygate.wait=true`. Kapı kırılırsa **build kırılır**. Kapı
yalnızca **yeni koda** bakar (Clean as You Code) — senin kapsam disiplininle
aynı şey. Dokunulmamış satırdaki ihlal bu PR'ın meselesi değildir.

## Varsayılan kapı (Sonar Way, yeni kod)

| Ölçüt | Eşik | İncelemedeki karşılığı |
|---|---|---|
| Yeni kodda Reliability rating | A (0 bug) | bug → **blocker** |
| Yeni kodda Security rating | A (0 vulnerability) | → **blocker** |
| Security hotspot | %100 incelenmiş | → **blocker** (gerekçesiz bırakma) |
| Yeni kodda Maintainability rating | A | code smell yığını → **blocker** |
| Yeni kod coverage | ≥ %80 | testsiz yeni mantık → **minor** uyarı |
| Yeni kodda duplication | ≤ %3 | kopyala-yapıştır blok → **blocker** |

Coverage'ı neden minor tutuyoruz: test yazdırma pipeline'ın ayrı bir adımı ve
ayrı bir anahtarı (`CREW_REQUIRE_TESTS`) var; review'da blocker yapmak aynı işi
iki kez zorlar ve gereksiz düzeltme turu üretir. Yine de yaz — insan görsün.

## Sık çıkan, borç yazan kurallar (dilden bağımsız)

- **Cognitive complexity > 15** — iç içe if/loop/ternary. En sık borç kaynağı.
  Erken dönüş (guard clause) ile düzleştir.
- **Duplicated blocks** — aynı 3+ satır birden fazla yerde. Sonar'ın
  duplication yüzdesini şişirir.
- **Kullanılmayan** import / değişken / parametre / private metot.
- **Boş catch** veya sadece log'layıp yutan catch. Hata yut(ul)malıysa
  gerekçesi yorumda olmalı.
- **Sabit (magic) sayı/dize** — tekrar eden literal → named constant.
- **Derin iç içelik** (> 3 seviye), **çok uzun fonksiyon**, **çok parametre**
  (> 7).
- **TODO / FIXME** — Sonar bunları doğrudan borç sayar. Yeni kodda bırakma;
  gerekiyorsa iş kaydı aç, koda yazma.
- **Gömülü sır** — parola/anahtar/token literal'i. Anında vulnerability.
- **Kullanılmayan dönüş değeri**, **her zaman true/false olan koşul**,
  **ölü kod**.
- **Kimliksiz `catch (Exception)`** — çok geniş yakalama.

## Güvenlik: Sonar'ın vulnerability saydıkları

- SQL/komut **injection** — parametreli sorgu yoksa.
- **Path traversal** — kullanıcı girdisi dosya yoluna giriyorsa.
- **SSRF** — kullanıcıdan gelen URL'e sunucudan istek.
- **Zayıf kripto** — MD5/SHA1 parola hash'i, sabit IV/salt, `Random` yerine
  güvenli rastgelelik gerekirken.
- **Güvensiz deserialization**, **XXE** (XML dış varlık).
- **CSRF** korumasının kapatılması, **CORS `*`** ile kimlik bilgisi.
- **Sertifika/host doğrulamasının kapatılması** (`verify=false` vb.).

**Security hotspot** (kesin açık değil ama incelenmeli): dosya izinleri,
geçici dosya, regex ReDoS, HTTP (TLS yok), cookie flag'leri (`HttpOnly`,
`Secure`, `SameSite`), debug/verbose mod.

## Dile göre en sık çıkanlar

**PHP** — `==` yerine `===`; `null` dönebilen çağrının kontrolsüz kullanımı;
`global`; `eval`; `extract`; hazırlanmamış sorgu; `@` ile hata bastırma;
exception yerine `die/exit`.

**Go** — kontrol edilmeyen `err`; `_ = err`; `panic` kütüphane kodunda;
context'siz uzun işlem; goroutine sızıntısı; döngü değişkeninin goroutine'de
yakalanması (Go < 1.22); `defer` unutulmuş `Close`.

**TypeScript** — `any`; `@ts-ignore`; `==`; `console.log` prod kodunda;
`dangerouslySetInnerHTML`; kontrol edilmeyen `Promise` (floating promise);
`React.useEffect` bağımlılık dizisi eksikliği.

**Python** — çıplak `except:`; değişebilir varsayılan argüman (`def f(x=[])`);
`eval/exec`; `assert` ile üretim kontrolü; `subprocess(shell=True)`;
karşılaştırmada `type()` yerine `isinstance`.

## Not

Sonar'ı burada **çalıştıramıyoruz** (lokal kurulum yok; prodda koşuyor). Bu
liste, kapının orada kırılmaması için önden bakılacak kontrol listesidir.
Bir bulguyu blocker yapmadan önce sor: "Sonar bunu yeni kodda gerçekten
işaretler mi?" Emin değilsen `minor` yaz.
