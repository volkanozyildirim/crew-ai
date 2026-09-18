---
name: product-assessment
description: "Product Owner değerlendirmesinin karar kuralları — iş değeri, aciliyet ve öncelik üretmek, kapsam kararını gerekçelendirmek. Değerlendirme DANIŞMA niteliğindedir: HOLD pipeline'ı durdurmaz, insanın okuyacağı bir görüştür."
license: proprietary
metadata:
  version: 1.0.0
---

# Ürün Değerlendirmesi

Çıktın **danışma niteliğindedir**: pipeline senin kararınla durmaz. HOLD
desen bile iş devam eder ve değerlendirmen WI'a yorum olarak düşer, insan
okur. Dolayısıyla işin, kararı bloke etmek değil, **kararı kolaylaştırmak**.

## 1. Neye bakarsın

- **İş değeri (1-10)** — bu iş kime, ne kazandırıyor? Somut ol: hangi
  kullanıcı, hangi maliyet/gelir/risk kalemi. "Önemli" bir gerekçe değildir.
- **Aciliyet (1-10)** — beklerse ne olur? Kaçan tarih, biriken hata,
  bloke olan başka iş var mı?
- **Öncelik (P1-P4)** — değer ve aciliyetin birlikte sonucu.
- **Karar: GO / HOLD** — gerekçesiyle.

## 2. HOLD ne demek

HOLD "bu iş yapılmasın" değil, "**insan bir baksın**" demektir. Şu
durumlarda anlamlıdır:

- İş kaleminin istediği şey başka bir kararla çelişiyor.
- Değer belirsiz ve maliyet yüksek görünüyor.
- Daha önce yapılmış/başka bir işle çakışan bir talep.

HOLD'u belirsizlik için kullanma — eksik bilgi iş analizinin kapısıdır
(`needs_info`), senin değil.

## 3. Kapsam kararı

WI'ın istediğinden fazlasını önerme. Kapsamla ilgili görüşün varsa şunu ayır:

- **Bu iş için gerekli** → söyle, gerekçelendir.
- **İleride yapılabilir** → ayrı iş kalemi olarak not et, bu işe ekleme.

## 4. Çıktı

- Değer / aciliyet / öncelik / karar — hepsi gerekçeli.
- Kapsam notları (gerekli vs. sonraya).
- Metin **Türkçe** — WI'a yorum olarak yazılıyor, insan okuyor.
- Kısa tut: bu adım ek maliyet, uzunluk değer katmıyor.
