---
name: uat
description: "Kullanıcı kabul testinin karar kuralları — kriterleri tek tek PASS/FAIL'e bağlamak, kanıtsız geçirmemek, kapsam dışına çıkmamak. UAT kararı Definition of Done'a girer; gevşek bir PASS hatayı prod'a taşır."
license: proprietary
metadata:
  version: 1.0.0
---

# UAT

Senin kararın Definition of Done'a giriyor. Gevşek verilmiş bir PASS hatayı
prod'a taşır; gerekçesiz bir FAIL ise işi boşuna geri döndürür.

## 1. Kriter kriter git

Her kabul kriteri için ayrı karar ver: **PASS** ya da **FAIL**. Toplu "genel
olarak çalışıyor" kararı yok.

Her karar bir gerekçeyle: neye bakarak böyle dedin? Kodda hangi davranış bunu
sağlıyor, hangi senaryoda doğruladın?

## 2. Kanıtsız PASS yok

Bir kriteri doğrulayamıyorsan PASS deme. Seçenekler:

- Kanıtı bul (kod, test, çıktı) → PASS/FAIL.
- Bulamıyorsan **açıkça yaz**: "doğrulanamadı, şu bilgi gerekli".

"Muhtemelen çalışıyor" bir UAT sonucu değildir.

## 3. Kapsam

Yalnızca bu iş kaleminin kabul kriterlerini değerlendir.

- WI'ın istemediği bir eksiklik gördüysen: not düş, FAIL sebebi yapma.
- Mevcut sistemdeki eski bir sorun bu işin FAIL'i değildir.
- "Şunu da eklemeliydi" → kapsam dışı, ayrı iş kalemi.

FAIL yalnızca şunlar için: bir kabul kriteri karşılanmamış, ya da yapılan
değişiklik var olan bir şeyi bozmuş.

## 4. Çıktı

- Her kriter için PASS/FAIL + gerekçe.
- Genel karar; varsa doğrulanamayanların listesi.
- Metin **Türkçe** — WI'a yorum olarak yazılıyor.
