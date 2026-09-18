---
name: engineering-standards
description: "Kod yazarken ve tasarlarken uyulacak ortak ölçüt — dilin güncel kullanımı (PHP, Go, TypeScript/Next.js, Python), SOLID ve SonarQube borç kuralları. Mimar, geliştirici ve inceleyici aynı ölçütü kullanır; repoya uyan dil referansı pipeline tarafından eklenir."
license: proprietary
compatibility: FLO stack — PHP 8+/Butterfly, Go/Gin, Next.js/React/TS, Python
metadata:
  version: 1.0.0
---

# Engineering Standards

Bu ölçüt üç adımda da aynıdır: **tasarım**, **geliştirme**, **inceleme**.
Aynı kuralı üçünün de bilmesi, hatayı en ucuz yerde — yazarken — yakalar.
İnceleme aşamasında yakalanan her ihlal bir düzeltme turu demektir; o tur
zaman ve para yazar.

## 1. Prod'daki kapı gerçek

Repolarda `sonar.qualitygate.wait=true`. SonarQube kapısı **build'i bloke
ediyor**. Yani buradan çıkan kod oradan geçmezse iş geri döner.

Kapı yalnızca **yeni koda** bakar (Clean as You Code):

> Eklediğin/değiştirdiğin satırlar temiz olsun. Dokunmadığın satırlardaki
> mevcut borç senin işin değil — onu düzeltmek de iş kaleminin dışına
> çıkmaktır.

Kapının ölçütleri ve en sık borç üreten kurallar: `references/sonarqube.md`.

En sık takılanlar, yazarken aklında tut:

- **Cognitive complexity > 15** — iç içe if/loop. Erken dönüş (guard clause)
  ile düzleştir.
- **Kopyala-yapıştır blok** — aynı 3+ satır ikinci kez yazılıyorsa çıkar.
- **Kullanılmayan** import/değişken/parametre.
- **Boş veya yutan catch** — hata yutuluyorsa gerekçesi yorumda olsun.
- **Gömülü sır** — parola/anahtar/token literal'i. Anında vulnerability.
- **TODO/FIXME** — Sonar doğrudan borç sayar. İş kaydı aç, koda yazma.
- **Magic number/string** — tekrar eden literal → adlandırılmış sabit.

## 2. Dilin güncel kullanımı

Yeni kod, o dilin **bugünkü** hâline göre yazılır. 2015 kalıbı hem okunmaz
hem Sonar'da borç üretir. Repo diline göre ilgili referans pipeline tarafından
bağlama eklenir:

| Repo işareti | Dil | Referans |
|---|---|---|
| `composer.json` | PHP | `references/php.md` |
| `go.mod` | Go | `references/go.md` |
| `package.json` | TS / Next.js / React | `references/typescript.md` |
| `pyproject.toml` | Python | `references/python.md` |

**Ama repodaki mevcut kalıp her zaman önceliklidir.** Dosyanın etrafındaki kod
nasıl yazılmışsa yeni kod da öyle yazılır. Referanstaki bir kalıbı repoda
karşılığı yokken dayatmak, iş kaleminin istemediği bir değişiklik yapmaktır.
Farklı bir kalıp öneriyorsan repoda o kalıbın yaşadığı bir yer göster.

## 3. SOLID — somut zarara bağla

- **SRP** — Bir fonksiyon/sınıf tek iş yapsın. Controller/handler'da iş kuralı
  = kırmızı bayrak; service'e taşı.
- **OCP** — Büyüyen `if/else`/`switch` zinciri yerine polymorphism/strategy.
- **LSP** — Alt sınıf üst sınıfın sözleşmesini daraltmasın.
- **ISP** — Kullanılmayan bağımlılık/metot dayatma.
- **DIP** — Somut sınıfa değil soyutlamaya bağlan; test edilebilir olsun.

SOLID bir amaç değil araçtır. İhlali ancak **somut bir zarara** işaret
ediyorsa dile getir: genişletilemiyor, test edilemiyor, mevcut davranışı
bozuyor. Saf stil tercihi değil.

## 4. Güvenlik

Bunlar Sonar'ın `vulnerability`/`hotspot` saydıkları; yani prod kapısı da
geçirmez:

- **Injection** — SQL/komut. Kullanıcı girdisi sorguya string olarak
  eklenmesin; parametreli sorgu kullan.
- **Kimlik & yetki** — Endpoint auth'lu mu? Kullanıcı başkasının kaydına
  erişebiliyor mu (IDOR)? Rol kontrolü var mı?
- **Girdi doğrulama** — Tip, uzunluk, aralık, format. Dosya yüklemede MIME,
  boyut, path traversal.
- **Veri sızıntısı** — Yanıtta/log'da parola hash'i, token, PII, iç id;
  hata mesajında stack trace.
- **Sır yönetimi** — Kodda gömülü şifre/anahtar yok.
- **Kripto** — Parola için MD5/SHA1 yok, sabit IV/salt yok.
- **Sertifika doğrulaması kapatılmaz** (`InsecureSkipVerify`, `verify=False`).

## 5. Kapsam

Bu ölçüt **nasıl yazılacağını** söyler, **ne yazılacağını** değil. Ne
yazılacağı iş kaleminden gelir. Buradaki bir kural, iş kaleminin istemediği
bir değişikliği yapmak için gerekçe değildir.
