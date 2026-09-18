---
name: technical-design
description: "Teknik tasarımın karar kuralları — repo kararına uymak, planı gerçekten var olan dosyalar üzerine kurmak, minimum değişiklikle tüm kabul kriterlerini kapsamak. Plandaki her dosya geliştiricinin dokunacağı dosyadır; uydurulmuş yol boş dosya üretir."
license: proprietary
metadata:
  version: 1.0.0
---

# Teknik Tasarım

Senin ürettiğin plan, geliştiricinin **birebir uygulayacağı** listedir.
Plandaki bir yol yanlışsa geliştirici o yolda yeni bir dosya açar ve iş
sessizce yanlış yere yazılır. Bu, bu pipeline'da gerçekten yaşanmış bir
hatadır.

## 1. Repo kararı senin değil

Hangi repoda çalışılacağına `discover_repos` karar verir ve o karar
**bağlayıcıdır**. Senin önerdiğin repo farklıysa pipeline seninkini ezer.
Dolayısıyla planı verilen repoya göre kur; başka repoyu işaret etme.

## 2. Her yol gerçek olmalı

Plandaki her dosya yolu, repoda **var olduğunu gördüğün** bir yol olmalı.

- Dosyayı gerçekten oku/gör; "muhtemelen buradadır" diye yazma.
- Yeni dosya gerekiyorsa, onu **yeni** olarak işaretle ve nereye, hangi
  mevcut kalıba göre açılacağını söyle.
- Var olan bir dosyayı değiştiriyorsan, hangi fonksiyon/blok değişecek —
  bunu yaz. "Dosyayı güncelle" yetersiz bir talimattır.

Yolundan emin değilsen aramak, uydurmaktan ucuzdur.

## 3. Minimum değişiklik, tam kapsam

İki kural aynı anda geçerli:

- **Tam**: Kabul kriterlerinin **hepsi** planda karşılığını bulmalı. Eksik
  bırakılan kriter, inceleme aşamasında ret olarak geri döner.
- **Minimum**: Kriterlerin gerektirmediği hiçbir değişiklik planda olmamalı.
  Refactor, yeniden adlandırma, "bu arada düzeltelim" — hepsi kapsam dışıdır
  ve inceleme bunları reddeder.

Plan, bu ikisinin kesişimidir: gerekli olan her şey, gereksiz hiçbir şey.

## 4. Mevcut kalıbı taklit et

Yeni kod, etrafındaki koda benzemeli. Tasarımda bunu açıkça söyle:

- Bu repoda servis/repository ayrımı nasıl yapılıyorsa öyle.
- Hata yönetimi, doğrulama, log kalıbı nasılsa öyle.
- İsimlendirme konvansiyonu neyse o.

Repoda olmayan bir kalıbı getirmek istiyorsan gerekçesi iş kaleminden
gelmeli, senin tercihinden değil.

## 5. Aynı dosyaya tek geçiş

Bir dosyada birden fazla değişiklik gerekiyorsa bunları **tek bir bütünsel
değişiklik** olarak tarif et. Aynı dosya için ayrı ayrı adımlar yazarsan
ikinci yazım birincisini ezer (pipeline bunları birleştirmek zorunda kalıyor).

## 6. Test dosyaları

`CREW_REQUIRE_TESTS` açıkken plan test dosyalarını da içermeli. Hangi
davranışın test edileceğini yaz — "test ekle" değil, "kupon geçersizken
tutarın değişmediğini doğrulayan test".

## 7. Nasıl yazılacağı

Kodun nasıl yazılacağı (dilin güncel kullanımı, SOLID, Sonar borcu) ayrı bir
ölçütte: `engineering-standards`. Pipeline repoya uyan dil referansını
bağlama ekler. Tasarımın o ölçütle çelişmesin — özellikle: iş kuralını
controller/handler'a koyma, büyüyen if/else zinciri önerme, sırrı koda gömme.
