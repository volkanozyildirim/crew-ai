# Agile SDLC Crew

Azure DevOps entegrasyonlu, CrewAI tabanli agentic yazilim gelistirme ekibi.

Work Item ID girildikten sonra 6 yapay zeka ajaninin olumsuz takimi, repo kesfinden UAT'a kadar
tam bir Agile SDLC surecini otonom olarak yurutur. Canli dashboard uzerinden ilerleme izlenebilir.

---

## Gereksinimler

| Gereksinim | Detay |
|-----------|-------|
| **Python** | 3.10 - 3.13 |
| **pip / uv** | Paket yoneticisi |
| **Azure DevOps PAT** | Proje erisimine sahip Personal Access Token |
| **LiteLLM Endpoint** | OpenAI-uyumlu bir LLM API (LiteLLM proxy, OpenAI, vb.) |
| **Tarayici** | Dashboard icin (otomatik acilir) |

---

## Kurulum

### 1. Projeyi klonlayin

```bash
cd /Users/volkan.ozyildirim/devel/crewai
cd agile_sdlc_crew
```

### 2. Sanal ortam olusturun (onerilen)

```bash
python -m venv .venv
source .venv/bin/activate    # macOS / Linux
# .venv\Scripts\activate     # Windows
```

### 3. Bagimliliklari yukleyin

```bash
pip install -e .
```

Bu komut `pyproject.toml` icerisindeki su bagimliliklari yukler:

| Paket | Amac |
|-------|------|
| `crewai[tools]>=1.12.0` | CrewAI framework + 100+ hazir arac |
| `requests>=2.31.0` | Azure DevOps REST API istemcisi |

### 4. Ortam degiskenlerini ayarlayin

```bash
cp .env.example .env
```

`.env` dosyasini duzenleyin:

```env
# ── Azure DevOps ──
AZURE_DEVOPS_ORG_URL=https://dev.azure.com/sirket-adiniz
AZURE_DEVOPS_PAT=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
AZURE_DEVOPS_PROJECT=proje-adiniz

# ── LiteLLM / LLM ──
LITELLM_BASE_URL=https://litellm-proxy.example.com
LITELLM_API_KEY=sk-xxxxxxxxxxxxxxxx
LITELLM_MODEL=openai/gpt-4
```

> `.env` dosyasini yuklemek icin calistirmadan once `source .env` yapin
> veya `python-dotenv` kullanarak otomatik yukleyin (opsiyonel).

---

## Azure DevOps PAT Yapitandirmasi

Azure DevOps Personal Access Token (PAT) olusturmak icin:

1. Azure DevOps'a giris yapin
2. Sag ustten **User Settings** > **Personal Access Tokens**
3. **+ New Token** tiklayin
4. Asagidaki izinleri verin:

| Scope | Izin |
|-------|------|
| **Work Items** | Read & Write |
| **Code** | Read |
| **Project and Team** | Read |

5. Token'i kopyalayip `.env` dosyasindaki `AZURE_DEVOPS_PAT` alanina yapisitirin

---

## LLM Yapilandirmasi

Proje LiteLLM proxy uzerinden calismak uzere tasarlanmistir. Desteklenen yapilandirmalar:

### LiteLLM Proxy (Onerilen)

```env
LITELLM_BASE_URL=https://your-litellm-proxy.com
LITELLM_API_KEY=sk-your-key
LITELLM_MODEL=openai/gpt-4
```

### Dogrudan OpenAI

```env
LITELLM_BASE_URL=https://api.openai.com/v1
LITELLM_API_KEY=sk-your-openai-key
LITELLM_MODEL=openai/gpt-4o
```

### Azure OpenAI

```env
LITELLM_BASE_URL=https://your-resource.openai.azure.com
LITELLM_API_KEY=your-azure-key
LITELLM_MODEL=azure/gpt-4o
```

### Lokal (Ollama)

```env
LITELLM_BASE_URL=http://localhost:11434
LITELLM_API_KEY=not-needed
LITELLM_MODEL=ollama/llama3
```

---

## Calistirma

### Temel Kullanim

```bash
# Interaktif: Work Item ID sorar
agile_sdlc_crew

# Dogrudan Work Item ID ile
agile_sdlc_crew 12345

# veya Python modulu olarak
python -m agile_sdlc_crew.main
```

### Calistirma Adimlari

1. **Ortam degiskenlerini yukleyin:**
   ```bash
   export AZURE_DEVOPS_ORG_URL=https://dev.azure.com/sirketiniz
   export AZURE_DEVOPS_PAT=xxxxx
   export AZURE_DEVOPS_PROJECT=projeniz
   export LITELLM_BASE_URL=https://litellm.example.com
   export LITELLM_API_KEY=sk-xxxxx
   export LITELLM_MODEL=openai/gpt-4
   ```

2. **Crew'u calistirin:**
   ```bash
   agile_sdlc_crew 12345
   ```

3. **Dashboard otomatik acilir:** `http://localhost:8765`

4. **Ilerlemeyi izleyin:** Dashboard uzerinden 6 ajanin durumunu,
   11 gorevin ilerlemesini ve repo haritasini canli izleyin.

---

## Gorev Akisi (11 Adim)

Crew asagidaki siryla calisir. Her adim bir oncekinin ciktisini girdi olarak kullanir:

```
1. Repo Kesfetme            (Yazilim Mimari)
   - Tum Azure DevOps repolarini tarar
   - Dizin yapisi, teknoloji, framework tespiti
   |
2. Bagimlilik Analizi        (Yazilim Mimari)
   - Repolar arasi API cagrilari, event'ler, paylasilan kutuphaneler
   - Etki analizi: hangi repo ve servisler etkilenecek
   |
3. Gereksinim Analizi        (Is Analisti)
   - Azure DevOps work item'i okur ve analiz eder
   - Repo baglami ile zenginlestirilmis gereksinimler cikarir
   |
4. Teknik Tasarim            (Yazilim Mimari)
   - Mevcut kod tabanina uygun mimari tasarim
   - Degistirilecek dosyalar, API degisiklikleri, migration plani
   |
5. Kod Gelistirme            (Kidemli Gelistirici)
   - Teknik tasarima uygun implementasyon
   - Mevcut konvansiyonlara uygun kod
   |
6. Kod Inceleme              (Yazilim Mimari)
   - Guvenlik, performans, SOLID uyumluluk kontrolu
   |
7. Test Planlama             (QA Muhendisi)
   - Birim, entegrasyon, E2E test senaryolari
   |
8. Test Yurutme              (QA Muhendisi)
   - Test calistirma ve sonuc raporlama
   |
9. UAT Hazirlama             (UAT Uzmani)
   - Kabul testi senaryolari olusturma
   |
10. UAT Yurutme              (UAT Uzmani)
    - Kabul testlerini calistirma, onay/ret karari
    |
11. Tamamlanma Raporu        (Is Analisti)
    - Tum surecin ozeti, Azure DevOps guncelleme
    - completion_report.md dosyasi olusturulur
```

---

## Ekip Yapisi (6 Ajan)

| Ajan | Rol | Araclar |
|------|-----|---------|
| **Scrum Master** | Hiyerarsik surec yoneticisi (manager agent) | Delegasyon |
| **Is Analisti** | Gereksinim analizi, Azure DevOps entegrasyonu | DevOps Get/Update/Comment/List |
| **Yazilim Mimari** | Teknik tasarim, repo analizi, kod inceleme | RepoList/Browse/Search, CodeRead |
| **Kidemli Gelistirici** | Kod gelistirme, implementasyon | RepoBrowse/Search, CodeWrite/Read |
| **QA Muhendisi** | Test planlama ve yurutme | RepoBrowse, CodeRead |
| **UAT Uzmani** | Kullanici kabul testi | DevOps Get/Comment |

---

## Dashboard

Calisma sirasinda `http://localhost:8765` adresinde canli dashboard acilir:

- **Ofis Gorunumu:** Her ajan icin hareketli SVG karakter, durum gostergesi ve konusma balonu
- **Sprint Board:** 11 gorevin durumu (bekliyor / calisiyor / tamamlandi)
- **Repo Haritasi:** Kesfedilen repolar, kullanilan teknolojiler ve bagimliliklar
- **Aktivite Logu:** Canli log akisi
- **Ilerleme Cubugu:** Yuzdelik ilerleme
- **Karakter Secimi:** Her ajan icin 8 farkli SVG karakter secenegi (localStorage'da saklanir)

---

## Proje Yapisi

```
agile_sdlc_crew/
├── .env.example                    # Ortam degiskenleri sablonu
├── pyproject.toml                  # Proje yapilandirmasi ve bagimliliklar
├── README.md                       # Bu dokuman
└── src/agile_sdlc_crew/
    ├── __init__.py
    ├── main.py                     # Giris noktasi (run/train/replay/test)
    ├── crew.py                     # CrewBase sinifi, ajanlar, gorevler, crew
    ├── dashboard.py                # StatusTracker + HTTP sunucu
    ├── config/
    │   ├── agents.yaml             # 6 ajan tanimi (Turkce)
    │   └── tasks.yaml              # 11 gorev tanimi
    ├── tools/
    │   ├── __init__.py             # Tool export'lari
    │   ├── azure_devops_base.py    # Paylasilan REST API istemcisi
    │   ├── azure_devops_get_work_item.py
    │   ├── azure_devops_update_work_item.py
    │   ├── azure_devops_add_comment.py
    │   ├── azure_devops_list_work_items.py
    │   ├── azure_devops_list_repos.py
    │   ├── azure_devops_browse_repo.py
    │   ├── azure_devops_search_code.py
    │   ├── code_write_tool.py
    │   └── code_read_tool.py
    └── web/
        └── index.html              # Modern ofis dashboard (tek dosya)
```

---

## Sorun Giderme

### Azure DevOps Baglanti Hatalari

```
ValueError: AZURE_DEVOPS_ORG_URL, AZURE_DEVOPS_PAT ve AZURE_DEVOPS_PROJECT
ortam degiskenleri ayarlanmalidir.
```

**Cozum:** Ortam degiskenlerinin dogru ayarlandigindan emin olun:
```bash
echo $AZURE_DEVOPS_ORG_URL   # Bos olmamali
echo $AZURE_DEVOPS_PAT       # Bos olmamali
echo $AZURE_DEVOPS_PROJECT   # Bos olmamali
```

### LLM Baglanti Hatalari

**Cozum:** LiteLLM endpoint'inin erisilebildigini kontrol edin:
```bash
curl -s $LITELLM_BASE_URL/v1/models \
  -H "Authorization: Bearer $LITELLM_API_KEY" | head -20
```

### Dashboard Acilmiyor

**Cozum:** 8765 portunun baska bir uygulama tarafindan kullanilmadigini kontrol edin:
```bash
lsof -i :8765
```

### Work Item Bulunamiyor

**Cozum:** Work Item ID'nin dogru oldugundan ve PAT token'inin
o projeye erisim yetkisi oldugundun emin olun:
```bash
curl -s -u ":$AZURE_DEVOPS_PAT" \
  "$AZURE_DEVOPS_ORG_URL/$AZURE_DEVOPS_PROJECT/_apis/wit/workitems/12345?api-version=7.1"
```

---

## Diger Komutlar

```bash
# Crew'u egit (fine-tuning)
train 3 training_data.pkl

# Belirli bir gorevden tekrar calistir
replay <task_id>

# Crew'u test et
test_crew 2
```

---

## Ciktilar

Crew calismasi tamamlandiginda:

1. **`completion_report.md`** - Tum surecin detayli raporu (proje kokunde olusturulur)
2. **Azure DevOps Work Item** - Durum guncellenir ve ozet yorum eklenir
3. **Dashboard** - Final durumu gosteir (tum gorevler tamamlandi)
4. **Konsol ciktisi** - Sonuc ozeti

---

## Notlar

- Crew hiyerarsik surec ile calisir: Scrum Master tum gorev delegasyonunu yonetir
- Her gorev bir onceki gorevin ciktisini context olarak kullanir
- Dashboard 2 saniyede bir `status.json` dosyasini poll eder
- Karakter secimleri tarayici localStorage'da saklanir
- Azure DevOps API v7.1 kullanilir
- Tum ajan prompt'lari Turkce olarak yazilanmistir
