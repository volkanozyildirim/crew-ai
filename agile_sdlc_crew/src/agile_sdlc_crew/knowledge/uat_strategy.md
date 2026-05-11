# UAT Strategy Guide — Kullanici Kabul Testi Becerileri

## UAT vs Fonksiyonel Test Farki

| UAT | Fonksiyonel Test |
|---|---|
| Is perspektifinden | Teknik perspektiften |
| "Is kurali saglanmis mi?" | "Kod dogru calisuyor mu?" |
| Kabul kriterleri odakli | Test case odakli |
| Kullanici senaryosu bazli | API/metod bazli |
| QA degil, is tanimi yapan kisite | QA muhendisi |

## Kabul Kriteri Cikartma

### Is Kaleminden Cikarma Adimlari
1. `get_work_item` ile WI'yi oku
2. `Microsoft.VSTS.Common.AcceptanceCriteria` alanini kontrol et
3. Description'daki "kullanici X yapabilmeli", "sistem Y yapmali" ifadelerini bul
4. Zımni/acik belirtilmemis ama beklenen kurallari cikart (iş kuralı = kriter)

### Kriter Kalite Kontrolu
Her kabul kriteri icin:
- [ ] Test edilebilir mi? ("Daha hizli olmali" HAYIR, "500ms altinda donmeli" EVET)
- [ ] Olculebilir mi? Sayisal esik var mi?
- [ ] Kim test edecek? (kullanici mi, sistem mi?)
- [ ] Hangi on kosul gerekli?

## Degerlendirme Metodolojisi

### GECTI Kriteri
- PR'daki degisiklikler kriteri karsilayan kodu iceriyor
- Kod inceleme sonucu kriter icin ONAY alindi
- Mantiksal olarak calisacagi goruldu (test yürütülmeden de reasonlabilir)

### KALDI Kriteri
- Kod degisikliginde kriteri karsilayan kisim YOK
- Yanlış anlasilmis / yanlis uygulanmis
- Kismen karsilandi ama eksik (yari-karsilanan = KALDI)

### Ozel Durumlar
```
Kriter: "Kullanici siparisi iptal ettiginde bildirim alir"
PR: Siparis iptali kodu var AMA bildirim kodu yok
→ KALDI (kriteri tam karsilamiyor)

Kriter: "Admin kullanicilari gorebilir"
PR: Admin yetkisi olmadan da gorulur hale geldi
→ KALDI (gerileme / regression)
```

## UAT Rapor Formati

```markdown
## UAT Raporu — #WI_ID

**PR:** #PR_ID — [link]
**Degerlendirilen Branch:** [branch]

### Kabul Kriterleri Degerlendirmesi

| # | Kriter | Durum | Aciklama |
|---|--------|-------|----------|
| 1 | [kriter metni] | ✅ GECTI | [kisa justification] |
| 2 | [kriter metni] | ❌ KALDI | [neden karsilanamadi] |
| 3 | [kriter metni] | ✅ GECTI | |

### Zımni Kriterler (Is Kaleminden Cikartilanlar)

| # | Kriter | Durum |
|---|--------|-------|
| 1 | Mevcut davranis bozulmamali | ✅ GECTI |

### Genel Degerlendirme

**Karar:** KABUL ✅ / RED ❌ / SARTLI KABUL ⚠️

**Sartli kabul ise kосулlar:**
- [eksik/duzetilmesi gereken]

**Son Kullanici Etkisi:** [yüksek / orta / düşük] — [aciklama]

### Eksikler (RED veya SARTLI KABUL ise)
1. [Eksik 1 — kritik mi?]
2. [Eksik 2]
```

## Yaygin UAT Senaryolari

### E-ticaret / Siparis Yonetimi
- Siparis olusturma → onay → kargoya verme → teslim zinciri
- Iptal: iptal edilebilir durumlar vs edilemez durumlar
- Stok: yeterli stok / stok yok
- Odeme: basarili / basarisiz / iade

### Kullanici Yonetimi
- Kayit / login / sifre sifirlama akislari
- Rol bazli erisim (admin vs normal kullanici)
- Hesap donaklama / aktiflestime

### Bildirim / E-posta
- Tetikleyici kosul saglandiginda gidiyor mu?
- Doğru aliciya gidiyor mu?
- Icerik dogru mu?

### Arama / Listeleme
- Filtre kombinasyonlari
- Bos sonuc durumu
- Sayfalama

## KRITIK KURALLAR

1. **Is perspektifinden kal** — teknik implementasyonu degil is sonucunu degerlendir
2. **Her kriter icin gerekce** — "GECTI" yazarken hangi kod degisikligi karsiladi?
3. **Zımni kriterleri kapsа** — "Mevcut ozellikler bozulmamali" her zaman bir kriter
4. **Kismen karsilama = KALDI** — %80 karsilamayi GECTI sayma
5. **Regresyon tespiti** — onceki calisir ozelliklerin bozulup bozulmadığını kontrol et
6. **Is kurali ihlali = otomatik RED** — opsiyonel guzellestirme eksik olabilir, is kurali eksik olamaz
