---
name: test-planning
description: "Test planlamanın karar kuralları — kabul kriterini test senaryosuna çevirmek, gerçekten kırılabilecek yerlere odaklanmak, çalıştırılabilir adım yazmak. Plan, kabul kriterlerini birebir kapsamalı; kapsamadığı kriter doğrulanmamış demektir."
license: proprietary
metadata:
  version: 1.0.0
---

# Test Planlama

Test planı, kabul kriterlerinin **doğrulanabilir hâlidir**. Her kriterin
karşılığı planda olmalı; olmayan kriter kimse tarafından kontrol edilmemiş
demektir.

## 1. Kabul kriteri → senaryo

Her kabul kriteri en az bir senaryoya dönüşür. Senaryo üç şeyi söyler:

1. **Ön koşul** — hangi durumda başlıyoruz (veri, kullanıcı, yetki).
2. **Adım** — ne yapılıyor (somut: hangi ekran, hangi endpoint, hangi girdi).
3. **Beklenen** — ne olmalı (gözlenebilir: mesaj, tutar, durum, kayıt).

"Fonksiyon test edilir" senaryo değildir. "Kupon kodu boşken Uygula'ya
basılır → 'Kupon kodu giriniz' uyarısı çıkar, tutar değişmez" senaryodur.

## 2. Nerede kırılır?

Mutlu yol tek başına yetmez. Asıl değer sınırlarda:

- **Boş / eksik girdi** — null, boş string, boş liste.
- **Sınır değerler** — sıfır, negatif, maksimum, bir fazlası.
- **Yetki** — yetkisiz kullanıcı, başkasının kaydı (IDOR).
- **Tekrar** — aynı isteğin iki kez gönderilmesi (idempotency).
- **Eşzamanlılık** — iki kullanıcı aynı kayda dokunursa.
- **Dış sistem hatası** — entegrasyon cevap vermezse/hata dönerse.
- **Veri uyumsuzluğu** — beklenmeyen format, eksik alan, farklı timezone.

## 3. Kapsam

Test planı da iş kalemiyle sınırlıdır. Bu WI'ın değiştirmediği bir akışa
regresyon testi yazmak kapsamı genişletmektir — değişikliğin **etkilediği**
akışlar hariç. Etkilenen akış varsa onu açıkça gerekçelendir.

## 4. Çıktı

- Senaryolar numaralı ve kabul kriterine bağlı (hangi kriteri doğruluyor).
- Çalıştırılabilir: okuyan kişi ek bilgiye ihtiyaç duymadan uygulayabilsin.
- Metin **Türkçe** — WI'a yorum olarak yazılıyor.
