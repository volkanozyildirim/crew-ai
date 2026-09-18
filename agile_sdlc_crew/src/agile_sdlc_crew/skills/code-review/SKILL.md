---
name: code-review
description: PR incelemesi için kapsam disiplini, dilin güncel kullanımı, SOLID, güvenlik ve SonarQube borç kontrolü. Work item'ın istediğinin dışına çıkmadan, PR'ın DOKUNDUĞU satırlarda kalite kapısını uygular. Repo dili PHP, Go, TypeScript/Next.js veya Python olabilir — dile özgü kurallar references/ altındadır.
license: proprietary
compatibility: FLO stack — PHP 8+/Butterfly, Go/Gin, Next.js/React/TS, Python
metadata:
  version: 1.0.0
---

# Code Review

Bu iş için tek ölçüt var: **work item ne istediyse o yapılmış mı, ve yapılan
değişiklik prod'daki kalite kapısından geçer mi.** İkisi dışındaki her şey
yorumdur, ret sebebi değildir.

## 1. Kapsam disiplini — ret sebeplerinin sınırı

Work item'ın istediği iş, incelemenin de sınırıdır. Bir bulgunun PR'ı
reddedebilmesi için şu üçünden birine girmesi gerekir:

1. **Eksik**: WI'ın istediği bir şey yapılmamış / bir kabul kriteri karşılanmamış.
2. **Bozuk**: İstenen değişiklik hatalı — bug, regresyon, silinmiş mevcut kural.
3. **Fazla**: WI'ın istemediği değişiklik var (refactor, rename, reformat,
   alakasız "iyileştirme", ilgisiz dosya).

Bunların dışındaki her şey `minor`'dır: yoruma yazılır, PR'ı bloke etmez.

**Mevcut borca dokunma.** Dosyanın PR'dan önce de var olan sorunları bu PR'ın
meselesi değildir. 200 satırlık eski spagettinin yanına 3 satır eklenmişse,
incelenen o 3 satırdır. Eski kodu düzeltmek WI'ın işi değilse istemek
"fazla" talebidir — sen scope creep'i engellemekle görevliyken kendin
yaratma.

**Konvansiyon iddiası kanıt ister.** "Burada X kullanılmalı" diyorsan repoda o
konvansiyonun yaşadığı bir yer göster (`precedent`). Gösteremiyorsan o
konvansiyon yoktur; bulguyu düş.

## 2. SonarQube — Clean as You Code

Repolarda `sonar.qualitygate.wait=true`: Sonar kapısı prod'da **build'i
bloke ediyor**. Yani buradan geçen bir ihlal orada patlar. Ama kapı yalnızca
**yeni koda** bakar, senin kapsam disiplinin de bununla birebir örtüşür:

> PR'ın eklediği/değiştirdiği satırlardaki Sonar ihlali → **blocker**.
> Dokunulmamış satırlardaki ihlal → **görmezden gel**.

Kapının ayrıntısı ve dile göre en sık borç üreten kurallar:
`references/sonarqube.md`.

Yeni kodun test kapsamı da kapının parçasıdır; testi olmayan yeni mantık için
`minor` uyarı yaz (blocker değil — test zorunluluğu ayrı bir ayar,
`CREW_REQUIRE_TESTS`, ve pipeline'ın kendi adımı var).

## 3. Dilin güncel kullanımı

Kod, o dilin **bugünkü** hâline göre yazılmış olmalı; 2015 kalıbıyla yazılmış
yeni kod Sonar'da da borç üretir. Repo diline göre ilgili referansı aç:

| Repo | Referans |
|---|---|
| PHP (`composer.json`) | `references/php.md` |
| Go (`go.mod`) | `references/go.md` |
| Next.js / React / TS (`package.json`) | `references/typescript.md` |
| Python (`pyproject.toml`) | `references/python.md` |

Dil referansı **öneri kaynağıdır**: oradaki bir kalıbın kullanılmaması tek
başına blocker değildir. Blocker olması için 1. maddedeki üç kapıdan birine
ya da 2. maddedeki Sonar ihlaline girmesi gerekir. "Daha modern yazılabilirdi"
→ `minor`.

## 4. SOLID

Yeni eklenen/değiştirilen birimlere bak, tüm dosyaya değil:

- **SRP** — Bu fonksiyon/sınıf tek bir şey mi yapıyor? Controller'da iş
  kuralı = kırmızı bayrak.
- **OCP** — Değişiklik mevcut davranışı bozmadan genişletiyor mu? Büyüyen
  `if/else`/`switch` zinciri yerine polymorphism/strategy mümkün mü?
- **LSP** — Alt sınıf üst sınıfın sözleşmesini bozuyor mu (daha dar girdi,
  beklenmeyen exception)?
- **ISP** — Kullanılmayan bağımlılık/metot dayatılıyor mu?
- **DIP** — Somut sınıfa mı, soyutlamaya mı bağımlı? Test edilebilir mi?

SOLID ihlali ancak **somut bir zarara** işaret ediyorsa blocker olur
(genişletilemez, test edilemez, mevcut davranışı bozuyor). Saf stil tercihi
`minor`'dır.

## 5. Güvenlik

Sonar'ın `vulnerability` ve `security hotspot` saydığı şeyler burada blocker'dır
— çünkü prod kapısı da onları geçirmez. PR'ın dokunduğu satırlarda ara:

- **Injection** — SQL/NoSQL/komut/LDAP. Kullanıcı girdisi sorguya string
  olarak ekleniyor mu? Prepared statement / parametreli sorgu var mı?
- **Kimlik & yetki** — Endpoint auth'lu mu? Kullanıcı başkasının kaydına
  erişebiliyor mu (IDOR)? Rol kontrolü var mı?
- **Girdi doğrulama** — Tip, uzunluk, aralık, format. Dosya yüklemede MIME,
  boyut, path traversal.
- **Veri sızıntısı** — Yanıtta/log'da parola hash'i, token, PII, iç id.
  Hata mesajı stack trace döküyor mu?
- **Sır yönetimi** — Kodda gömülü şifre/anahtar/token. (Sonar bunu anında
  yakalar.)
- **Kriptografi** — MD5/SHA1 parola için, sabit IV, zayıf rastgelelik.
- **SSRF / deserialization** — Kullanıcıdan gelen URL'e istek, güvenilmeyen
  veriden nesne kurma.

Ayrıntı ve dile özgü karşılıkları: `references/sonarqube.md`.

## 6. Bulgu yazarken

Her bulgu için: dosya, satır, severity, sorun, gerekli düzeltme. Ek olarak

- `requirement_ids` — ihlal edilen FR/TR/AC. Yoksa bu bir tercih, kusur değil.
- `evidence` — itiraz ettiğin satırın **birebir** kopyası. Pipeline dosyayı
  açıp alıntıyı doğruluyor; uydurma bulgu sessizce düşer.
- `precedent` — konvansiyon iddiası varsa zorunlu (bkz. 1. madde).
- `fix_targets` — düzeltmenin yapılacağı dosya(lar). Çoğu zaman sorunu
  gördüğün dosya değildir; örneğin yeni sınıf hiç çağrılmıyorsa düzeltme
  mevcut çağıranda yapılır.

Blocker/major yoksa karar ONAY'dır. Emin olmadığın bir şeyi blocker yapma:
yanlış ret, pipeline'ı gereksiz bir düzeltme turuna sokar ve maliyet yazar.
