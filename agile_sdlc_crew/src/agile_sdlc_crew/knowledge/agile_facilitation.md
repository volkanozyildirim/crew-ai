# Agile Facilitation Guide — Scrum Master Becerileri

## Sprint Süreci Rolleri

### Scrum Master'in Temel Sorumlulukları
- Her ajanin ciktisinin kalitesini, eksiksizligini ve is uyumunu denetlemek
- Blocker'lari erken tespit etmek ve escalate etmek
- Pipeline sonunda kapsamli tamamlanma raporu olusturmak
- "ONAY" veya "IYILESTIR" kararini net, gerekceleriyle vermek

## Cikti Degerlendirme — Definition of Done

### Is Analizi (BA Ciktisi) — ONAY icin
- [ ] Is kalemindeki TUM gereksinimler listelenmis mi?
- [ ] Kabul kriterleri test edilebilir formda mi?
- [ ] Acik sorular / belirsizlikler not edilmis mi?
- [ ] Bağımlılıklar belirlenmiş mi?

### Teknik Tasarim (Architect Ciktisi) — ONAY icin
- [ ] Etkilenecek repo ve dosyalar somut olarak belirtilmis mi?
- [ ] Mevcut kod okunmus, tahminde bulunulmamis mi?
- [ ] Her degisiklik icin `current_code` ve `new_code` var mi?
- [ ] Kabul kriterleri tasarima yansimis mi?

### Kod (Developer Ciktisi) — ONAY icin
- [ ] Push edilen dosya sayisi plan ile uyusmu mu?
- [ ] Her dosya tam icerikliyken push edilmis mi (truncate yok mu)?
- [ ] Planda olmayan degisiklik yapilmamis mi?

### Kod Inceleme (Reviewer Ciktisi) — ONAY icin
- [ ] Is kalemi gereksinimleriyle birebir uyum degerlendirmesi var mi?
- [ ] SOLID + clean code analizi somut orneklerle yapilmis mi?
- [ ] Net ONAY veya DEGISIKLIK GEREKLI karari belirtilmis mi?

### Test Plani (QA Ciktisi) — ONAY icin
- [ ] Happy path, error case ve edge case senaryolari var mi?
- [ ] Onerilen unit test kodu (en azindan taslak) uretilmis mi?
- [ ] Regresyon riski degerlendirmesi yapilmis mi?

### UAT (UAT Uzmani Ciktisi) — ONAY icin
- [ ] Her kabul kriteri GECTI veya KALDI olarak degerlendirmis mi?
- [ ] KALDI olanlar icin somut eksik aciklamasi var mi?
- [ ] Genel KABUL veya RED karari verilmis mi?

## Geri Bildirim Yazma Kurallari

### Etkili Geri Bildirim
```
IYILESTIR

Eksikler:
1. [Somut eksik madde 1]
2. [Somut eksik madde 2]

Iyilestirme onerileri:
- [Yapilacak acikca belirtilmeli]
- [Kopyalanabilir format tercih edilmeli]
```

### Yanlis Geri Bildirim (YAPILMAZ)
- "Daha iyi yaz" — somut degil
- "Eksik var" — ne eksik belirtilmemis
- "Kalite dusuk" — olculsuz, eyleme donusturulemez

## Kickoff Toplantisi Yonetimi

### Acilistaki Gorevler
1. Is kalemini takim adina oku ve ozetle
2. Her rolu hatirla: BA (gereksinim), Architect (tasarim), Developer (kod), QA (test), UAT (kabul)
3. Acik sorulari not et

### Kapanis Karar Listesi
- Takim is kalemi hakkinda ayni gorustu mi? (EVET/HAYIR)
- Kritik riskler tespit edildi mi? (liste)
- Hangi adim en fazla dikkat gerektiriyor? (isim)
- Pipeline baslamadan once netlestirilmesi gereken var mi? (liste)

## Tamamlanma Raporu Formati

```markdown
## Tamamlanma Raporu — #WI_ID

**Durum:** TAMAMLANDI ✅ / KISMI ⚠️ / BASARISIZ ❌

### Ozet
[Ne yapildi, 2-3 cumle]

### PR
- URL: [link]
- Degistirilen dosyalar: [liste]

### Kalite Sonuclari
- Kod Inceleme: ONAY / DEGISIKLIK GEREKLI
- Test Plani: Hazir / Eksik
- UAT: KABUL / RED — [sonuc ozeti]

### Kabul Kriterleri
| Kriter | Durum |
|--------|-------|
| [kriter] | GECTI / KALDI |

### Notlar
[Sonraki sprint icin dikkat edilecekler, teknik borc vb.]
```

## KRITIK KURALLAR

1. **Her karari gerekcele** — "IYILESTIR" yazarken NE eksik oldugunu, NASIL duzeltilecegini yaz
2. **Is kalemi otoritesi** — tartismali durumlarda is kalemi aciklamasini esas al
3. **Pipeline engelleme karar esigi** — kucuk eksikler uyari, buyuk uyumsuzluk "DEGISIKLIK GEREKLI"
4. **Hiz vs kalite dengesi** — %80 kapsam yeterli sayilabilir, %50 alti reddedilir
5. **Takimi koruma** — blocker'i erken yakala, ilerleyen adimlara tasima
