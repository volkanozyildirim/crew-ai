---
name: implementation
description: "Kod yazmanın karar kuralları — planın dışına çıkmamak, mevcut mantığı silmemek, dosyayı bütün olarak yazmak. Burada yapılan iş incelemeden geçecek; incelemenin reddettiği her şey bir düzeltme turu, yani zaman ve para demektir."
license: proprietary
metadata:
  version: 1.0.0
---

# Geliştirme

Yazdığın kod incelemeye gidiyor. İnceleyicinin reddettiği her şey geri döner
ve pipeline bir tur daha çalışır. **Baştan doğru yazmak, düzeltmekten
ucuzdur** — bu yüzden inceleyicinin bakacağı ölçütü sen de biliyorsun
(`engineering-standards`).

## 1. Plan bağlayıcı

Plandaki dosyalar ve değişiklikler senin iş listendir.

- Planda **olmayan** dosyaya dokunma.
- Planda olan her dosyayı yap — eksik bırakılan, incelemede "eksik" olarak
  geri döner.
- Plandaki yol repoda yoksa **yeni dosya açma**: yol yanlış olabilir. Doğru
  yolu ara, bulamıyorsan bunu açıkça belirt. (Uydurma yola yazılan kod, iş
  yapılmış gibi görünüp hiçbir şey yapmaz — bu hata daha önce yaşandı.)

## 2. Mevcut mantığı silme

En pahalı hata bu. Yeni bir kontrol eklerken var olanı sessizce kaldırmak,
inceleme tarafından **regresyon** sayılır ve reddedilir.

Değiştirdiğin her `if`, guard, koşul, erken dönüş, hata mesajı için sor:

> Bu satır zaten vardı ve bir iş kuralını taşıyordu mu?

- `if (X && yeniKosul)` → `if (yeniKosul)` yapmak **X kuralını siler**.
- Var olan bir hata mesajını/dönüşünü değiştirmek, WI istemediyse kapsam
  dışıdır.
- Bir dalı, null kontrolünü, yetki kontrolünü kaldırmak — WI açıkça
  istemediyse yapma.

Ekleme yapmak, var olanı silmenin bedeliyle olmaz.

## 3. Dosyayı bütün olarak yaz

Bir dosyada birden fazla değişiklik varsa hepsini **tek seferde** yap. Parçalı
yazım ikinci geçişte birincisini eziyor.

Dosyanın tamamını üretiyorsan: dokunmadığın kısımlar **birebir** korunmalı.
Yeniden biçimlendirme, import sıralaması, boşluk düzeltmesi — hiçbiri
yapılmamalı. İnceleme bunları "fazla değişiklik" sayar.

## 4. Kapsam dışına çıkma

- Refactor yok, yeniden adlandırma yok, "bu arada iyileştirme" yok.
- İş kalemine atıfta bulunan yorum satırı yazma (`// WI-1234 için eklendi`).
  Kod ne yaptığını anlatır; hangi talep yüzünden yazıldığını değil.
- Dokunmadığın satırlardaki mevcut borcu düzeltme — o senin işin değil ve
  incelemede kapsam dışı sayılır.

## 5. Nasıl yazılacağı

`engineering-standards` ölçütü geçerli: dilin güncel kullanımı, SOLID,
güvenlik ve Sonar borcu. Pipeline repoya uyan dil referansını bağlama ekler.

Yazarken en sık takılanlar:
- İç içe if yerine erken dönüş (cognitive complexity).
- Kopyala-yapıştır blok — ikinci kez yazıyorsan çıkar.
- Kullanılmayan import/değişken bırakma.
- Hata yutma; `err`/exception kontrolsüz geçmesin.
- Sırrı koda gömme; TODO/FIXME bırakma.
- Kullanıcı girdisini sorguya string olarak ekleme.

## 6. Test

`CREW_REQUIRE_TESTS` açıkken plandaki test dosyalarını da yaz. Test, kabul
kriterindeki davranışı doğrulasın — kapsama sayısı için boş test değil.
