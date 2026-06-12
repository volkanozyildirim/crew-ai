# CrewAI Knowledge + Guardrails + Async — Tasarım Belgesi

**Tarih:** 2026-06-12
**Kapsam:** CrewAI'nin üç native özelliğini (Knowledge sources, Task guardrails, Async execution) pipeline'a, kaliteyi ve hızı artırmak amacıyla eklemek.
**Kapsam dışı:** CrewAI Memory (kasıtlı çıkarıldı — mevcut LanceDB vector store + `kickoff_grader` ile çakışıyor, özgün getirisi yok).

## Motivasyon

- **Kalite/doğruluk:** Küçük lokal modeller (Qwen3:8b / Qwen2.5-coder:7b) backstory'ye tıkıştırılan büyük knowledge bloklarında odak kaybediyor; bozuk JSON/kod çıktısı manuel parse+retry koduna yük bindiriyor.
- **Hız:** step9 (test planlama) ve step10 (UAT) birbirinden bağımsız ama seri çalışıyor.

## Ortak Tasarım İlkeleri

1. **Hiçbir mevcut kod silinmez** (Architect manuel re-kick hariç — bkz. Bölüm 2). Mevcut yollar fallback olarak kalır.
2. **Her özellik env-toggle arkasında, default KAPALI** (proje konvansiyonu: riskli/maliyetli davranış default off).
3. **Kademeli ve geri alınabilir** — toggle kapatınca eski davranışa döner.

---

## Bölüm 1 — Knowledge Sources

### Problem
`_agent_config_with_knowledge` (crew.py:230-252) her ajanın ilgili `.md` dosyasının **tüm içeriğini** backstory string'ine ekliyor → her prompt'ta token şişmesi, küçük modelde odak kaybı.

### Çözüm
İçeriği prompt'a tıkıştırmak yerine CrewAI Knowledge ile **chunk'layıp task anında sadece ilgili parçayı RAG ile çekmek.**

### Değişiklikler
- **`knowledge/__init__.py`:** Yeni `load_knowledge_source(name) -> StringKnowledgeSource` fonksiyonu (mevcut `load_knowledge` aynen korunur; ikisi de aynı `.md`'leri okur). `StringKnowledgeSource` tercih edilir çünkü dosya yollarını CrewAI'nin `knowledge/` klasör varsayımına bağımlı kılmaz.
- **`crew.py` ajan factory'leri (254-378):** `CREW_KNOWLEDGE_RAG=1` iken backstory-concat yerine `knowledge_sources=[...]` parametresi verilir. Per-ajan eşleme **birebir korunur:**

  | Ajan | Knowledge |
  |---|---|
  | scrum_master | agile_facilitation |
  | business_analyst | requirements_analysis |
  | software_architect | backend_tech_design, frontend_nextjs |
  | senior_developer | backend_feature_dev, frontend_nextjs |
  | code_reviewer | backend_code_review |
  | qa_engineer | testing_strategy |
  | uat_specialist | uat_strategy |

- Toggle KAPALI iken mevcut backstory-concat aynen çalışır (fallback).
- `{}`→`<>` placeholder dönüşümü RAG yolunda gereksiz (içerik prompt şablonunda değil); toggle açıkken atlanır.

### Load-bearing Gotcha — Embedder
CrewAI Knowledge varsayılan **OpenAI embedder** kullanır → OpenAI key olmadan patlar (aynı sebep `output_pydantic`'in kapalı olması). **Embedder Ollama'ya sabitlenmek ZORUNDA.** Mevcut `embed/` registry'sinden Ollama embedder config'i alınıp `knowledge_sources` ile birlikte ajana/crew'a `embedder=` olarak verilecek. Bu, bu bölümün zorunlu parçasıdır — embedder doğru bağlanmadan toggle açılmamalı.

### Storage
Knowledge embedding'leri bir kez hesaplanıp saklanır. Storage yolu `CREW_KNOWLEDGE_DB` env'i ile (default `~/.crew_knowledge`), mevcut `CREW_VECTOR_DB` desenine paralel. `.md` dosyaları değişince re-embed için basit içerik-hash kontrolü.

### Env
- `CREW_KNOWLEDGE_RAG` (0/1, default 0)
- `CREW_KNOWLEDGE_DB` (yol, default `~/.crew_knowledge`)

---

## Bölüm 2 — Task Guardrails

### Problem
- **Architect** (`technical_design_task`): çıktı `_parse_architect_output` (main.py:45-165, 4 kademeli onarım) ile parse ediliyor; başarısızsa flow.py:2250-2272'de **manuel re-kick** (kod tekrarı, Flow seviyesinde retry).
- **Developer** (kod task'ı): `_validate_code` (main.py:331-412) lint + Ollama-fix yapıyor.

### Çözüm
Fonksiyon-tabanlı guardrail (token harcamaz). Guardrail `(bool, Any)` döner; `False` olunca CrewAI hatayı agent'a geri besleyip `guardrail_max_retries` (default 3) kez **task'ı otomatik tekrar eder.**

### Değişiklikler

**Architect — `technical_design_task` (create_analysis_crew):**
- Guardrail closure'ı `_parse_architect_output`'u sarmalar:
  - Başarı → `(True, repaired_dict)` (onarım mantığı guardrail içinde **korunur**, kayıp yok).
  - Başarısızlık → `(False, "<spesifik hata: eksik alan / truncation / placeholder>")`.
- **flow.py:2250-2272 manuel re-kick SİLİNİR** — retry artık CrewAI guardrail'ine devredilir. (Bu, bu özellikte silinen tek mevcut koddur.)

**Developer — kod task'ı (create_code_crew):**
- Guardrail closure'ı `_validate_code`'un **lint kısmını** sarmalar. `_validate_code` `file_path / original_content / repo_name` ister; bunlar crew-oluşturma anında **closure ile yakalanır** (flow'da context mevcut).
- Lint hatası → `(False, lint_çıktısı)` → agent retry.
- **Karar (onaylandı):** Guardrail-retry **birincil** mekanizma; mevcut `_fix_with_ollama` döngüsü **fallback olarak korunur** (çift güvenlik, geri alınabilir).

### Env
- `CREW_TASK_GUARDRAILS` (0/1, default 0). Açıkken architect + developer guardrail'leri devreye girer; kapalıyken mevcut manuel parse/validate/re-kick yolları çalışır.

---

## Bölüm 3 — Async Execution (step9 ∥ step10)

### Kritik Bulgu
step9_test_planning ve step10_uat **ayrı Flow metotları**, her biri kendi `crew.kickoff()`'unu çağırıyor (tek crew içindeki task'lar DEĞİL). Dolayısıyla `Task.async_execution=True` (task-içi paralellik) işe yaramaz. Paralellik **Flow seviyesinde** yapılmalı.

### Bağımsızlık Doğrulaması
- İkisi de `@listen(step8_code_review)` ile tetikleniyor.
- Veri bağımlılığı yok: step9 `requirements_text + branch_name` okur; step10 `requirements_text + pr_id + pr_url` okur. Karşılıklı state yazımı yok.
- `@listen(and_(step9, step10)) step11_completion_report` join'i aynen kalır.

### Çözüm
- step9 ve step10 `async def` yapılır; içlerinde `await crew.kickoff_async(...)`.
- CrewAI Flow aynı event'i dinleyen coroutine listener'ları **eşzamanlı** çalıştırır → wall-clock ≈ max(step9, step10).

### Backend Uygunluğu (onaylandı: claude_cli)
Her iki crew da **claude_cli** provider'ı kullanıyor. claude_cli her çağrıda ayrı CLI subprocess başlattığı için tek-GPU lokal model gibi serileşmez → async **gerçek kazanç** sağlar. Tek dikkat: claude_cli oturum/rate limit'i; iki eşzamanlı subprocess beklenen sorun değil, çalışma anında izlenecek.

### Eşzamanlılık Güvenliği (ZORUNLU)
Concurrent çalışınca yarışan paylaşılan kaynaklar korunmalı:
- **`_track_and_check_budget`** (kümülatif USD) → `asyncio.Lock` / atomik güncelleme.
- **`StatusTracker` → `status.json` yazımı** → lock.
- **`db` `job_steps` update'leri** → step9/step10 için ayrı yazım, lock veya ayrı bağlantı.
- **`tool_cache`** → thread/async-safe erişim.

Toggle KAPALI iken step9/step10 mevcut senkron seri davranışta kalır (async güvenlik kodu sadece toggle açıkken devreye girer).

### Resume Etkileşimi
`_try_resume_step` her iki step için bağımsız çalışır; concurrent resume'da DB okuma/yazma lock'la korunur. Resume mantığı değişmez, sadece eşzamanlı erişim güvenli hale getirilir.

### Env
- `CREW_PARALLEL_TEST_UAT` (0/1, default 0)

---

## Test / Doğrulama Stratejisi

Proje konvansiyonu: pytest/linter yok. Doğrulama:
1. **Import kontrolü:** `.venv/bin/python -c "from agile_sdlc_crew.crew import AgileSDLCCrew"` — syntax/import kırılması yok.
2. **Toggle-kapalı regresyon:** Her üç toggle KAPALI iken bir WI'da uçtan uca pipeline → mevcut davranışla aynı sonuç (regresyon yok).
3. **Knowledge:** `CREW_KNOWLEDGE_RAG=1` ile çalıştır; `/tmp/crew_server.log`'da OpenAI-embedder hatası OLMAMALI (Ollama embedder doğru bağlandı); architect prompt token sayısı düşmeli.
4. **Guardrails:** `CREW_TASK_GUARDRAILS=1` ile, kasıtlı bozuk-JSON üretmeye meyilli bir WI'da architect guardrail-retry'ın tetiklendiği log'da görülmeli; manuel re-kick yolu çağrılmamalı.
5. **Async:** `CREW_PARALLEL_TEST_UAT=1` ile step9/step10 başlangıç zaman damgalarının örtüştüğü (`job_steps` / log) doğrulanmalı; toplam süre seri duruma göre kısalmalı.

## Rollout Sırası (öneri)
1. Async (en izole, en net kazanç, claude_cli ile risk düşük).
2. Guardrails (architect önce, sonra developer).
3. Knowledge (embedder gotcha'sı nedeniyle en dikkatli, en sona).

## Açık Riskler
- Knowledge embedder yanlış bağlanırsa OpenAI key hatası → toggle açılmadan embedder doğrulanmalı.
- claude_cli eşzamanlı subprocess rate-limit davranışı çalışma anında izlenecek.
- Guardrail-retry sayısı (`guardrail_max_retries`) maliyet/süreyi artırabilir → `CREW_MAX_JOB_COST` guard'ı zaten devrede, izlenecek.
