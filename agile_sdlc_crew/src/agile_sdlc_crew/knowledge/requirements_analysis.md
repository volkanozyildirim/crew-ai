# Requirements Analysis Guide — Is Analisti Becerileri

## Gereksinim Cikarmа Süreci

### 1. Is Kalemini Okuma
- **Baslik**: Ne yapilmasi isteniyor? Tek cumlede ozetle.
- **Aciklama (Description)**: Fonksiyonel beklenti, kullanici hikayesi, is kurali.
- **Kabul Kriterleri (Acceptance Criteria)**: Test edilebilir, olculebilir kosullar.
- **Bagli is kalemleri**: Parent/child/related WI'lar baglami degistirebilir.

### 2. Gereksinim Tipleri

| Tip | Tanim | Ornek |
|---|---|---|
| Fonksiyonel | Sistemin NE yapacagi | "Kullanici siparis iptal edebilmeli" |
| Iş Kurali | Hangi sartlarda, hangi kisitla | "Kargoya verilen siparis iptal edilemez" |
| Kalite (NFR) | Performans, guvenlik, erisilebilirlik | "API 500ms altinda donmeli" |
| Arayüz | Entegrasyon noktasi | "X servisiyle REST API uzerinden haberles" |

### 3. INVEST Kriteri (User Story Kalite Kontrolu)

- **I**ndependent: Diger is kalemlerine bagimliligi minimal
- **N**egotiable: Implementasyon detaylari esnek
- **V**aluable: Kullaniciya/ise net deger katiyor
- **E**stimable: Tahmini yapilabilir buyuklukte
- **S**mall: Tek sprint'e sigar
- **T**estable: Sonucu gozlemlenebilir

### 4. Kabul Kriterleri Yazma — Given/When/Then

```
Given: [baslangic durumu / on kosul]
When:  [kullanici eylemi / tetikleyici]
Then:  [beklenen sonuc / dogrulanabilir cikti]
```

Ornek:
```
Given: Kullanici "pending" durumundaki bir siparis sayfasindayken
When:  "Iptail Et" butonuna tiklar
Then:  Siparis "cancelled" durumuna gecer ve kullaniciya onay e-postasi gider
```

### 5. Yetersiz Is Kalemi Belirtileri

Asagidaki durumlarda analisti netlik istemeli:
- Aciklama yoksa veya 2-3 cumlede bitiyor
- Kabul kriteri hic belirtilmemis
- "Bunu duzelt", "Bunu duzeltin" gibi belirsiz komutlar
- Hangi sayfa/ekran/API'nin etkilenecegi belli degil
- Mevcut davranis vs yeni davranis farki acik degil
- Hangi kullanici rollerinin etkilenecegi belirtilmemis

### 6. Gereksinim Analiz Ciktisi Formati

```markdown
## Gereksinim Analizi — #WI_ID

### Özet
[1-2 cümle: ne yapılacak, neden]

### Fonksiyonel Gereksinimler
1. [Sistem X yapacak]
2. [Kullanıcı Y yapabilecek]

### İş Kuralları
- [Kural 1: şart → sonuç]
- [Kural 2]

### Kabul Kriterleri
1. Given/When/Then formatında
2. Her kriter bağımsız test edilebilmeli

### Belirsizlikler / Açık Sorular
- [Netleştirilmesi gereken nokta]

### Bağımlılıklar
- [Başka WI, servis, ekip]
```

## KRITIK KURALLAR

1. **Yorumlama degil cikartma** — is kaleminde yazilan ne ise onu yaz, fazlasini degil
2. **Her gereksinim test edilebilir** olmali — "performansi arttirilacak" YANLIS, "API 200ms altinda donecek" DOGRU
3. **Belirsizligi not et** — analistin gorevi belirsizligi gizdirmek degil isaret etmek
4. **Scope creep'e direnc** — is kaleminde olmayan gereksinim EKLEME
5. **Teknik cozum onerilme** — "nasil" degil "ne" istendigi yazilir
