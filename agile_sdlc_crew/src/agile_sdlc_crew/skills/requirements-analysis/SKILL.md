---
name: requirements-analysis
description: "İş analizinin karar kuralları — work item'dan bağlayıcı kabul kriteri çıkarmak, eksik bilgiyi kapsam uydurmadan sormak, tahmin üretmek. Kabul kriterleri pipeline boyunca bağlayıcıdır; burada yazılan şey tasarımı, kodu, incelemeyi ve UAT'ı bağlar."
license: proprietary
metadata:
  version: 1.0.0
---

# İş Analizi

Buradan çıkan kabul kriterleri **pipeline boyunca bağlayıcıdır**: mimar ona
göre tasarlar, geliştirici ona göre yazar, inceleyici ona göre reddeder, UAT
ona göre doğrular. Yanlış ya da uydurma bir kriter, dört adımı birden yanlış
yöne sürükler.

## 1. Kapsam: WI ne diyorsa o

Analiz, iş kaleminin **istediğini netleştirmektir**, kapsamını genişletmek
değil.

- WI'da olmayan bir gereksinimi "nasılsa lazım olur" diye ekleme. Eklediğin
  her kriter, geliştiricinin yazacağı ve inceleyicinin arayacağı koddur.
- "Bu arada şunu da düzeltelim" yok. Ayrı iş kalemidir.
- WI bir örnek/pattern gösteriyorsa (başka bir servis, başka bir ekran) o
  örnek bağlayıcıdır; kendi tasarımını uydurma.

## 2. Kabul kriteri nasıl yazılır

Her kriter **doğrulanabilir** olmalı: UAT uzmanı okuyup "oldu/olmadı"
diyebilmeli.

- ✅ "Sepette kupon kodu geçersizse 'Kupon geçersiz' mesajı gösterilir ve
  tutar değişmez."
- ❌ "Kupon akışı düzgün çalışır."

Kural:
- Her kriter tek bir davranış anlatır.
- Ölçülebilir olsun: ne girdi, ne beklenen sonuç.
- Hata/sınır durumları da kriterdir (boş girdi, yetkisiz kullanıcı, çift
  gönderim).
- WI'da açıkça yazan her şey kritere dönüşmeli — atlama.
- WI'da **yazmayan** bir şeyi kriter yapma.

## 3. Eksik bilgi: uydurma, sor

WI'ın içeriği yetersizse (açıklama yok, kriter yok, çelişki var) doğru
davranış **soru sormaktır**, boşluğu doldurmak değil. Pipeline'ın bunun için
bir kapısı var (hazırlık kapısı → `needs_info`).

Uydurulmuş bir gereksinim şu zinciri üretir: yanlış tasarım → yanlış kod →
inceleme reddi → düzeltme turu → maliyet. Bir soru sormak bunların hepsinden
ucuzdur.

Şunları sormaya değer:
- Beklenen davranış belirsizse (hata durumunda ne olacak?).
- Etkilenen sistem/servis belli değilse.
- "Mevcut yapıya benzer" deniyor ama hangi yapı olduğu yazmıyorsa.

## 4. Tahmin

Tahmin, yapılacak işin **yapısal büyüklüğüdür**: kaç kabul kriteri, kaç
servis/dosya, entegrasyon var mı, veri modeli değişiyor mu. Fibonacci'ye
yuvarlanır ve yalnızca yükselir.

Emin değilsen yüksek tahmin et; düşük tahmin sprint planını bozar.

## 5. Çıktı

- Kabul kriterleri numaralı ve bağlayıcı.
- Belirsizlikler açıkça listelenmiş (varsa).
- Metin alanları **Türkçe** (WI'a yorum olarak yazılıyor, insan okuyor).
