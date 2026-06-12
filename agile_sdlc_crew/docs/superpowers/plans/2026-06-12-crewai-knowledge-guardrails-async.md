# CrewAI Knowledge + Guardrails + Async Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Üç CrewAI native özelliğini (Knowledge sources, Task guardrails, Async execution) env-toggle arkasında, fallback'li ve regresyonsuz şekilde pipeline'a eklemek.

**Architecture:** Her özellik bağımsız, default-kapalı bir env knob ile devreye girer. Knowledge → backstory string enjeksiyonu yerine RAG (custom embedder mevcut `embed_text()`'i sarar). Guardrails → architect (JSON parse validate) ve developer (yapısal validate) task'larına; CrewAI'nin in-crew retry'ı birincil, mevcut Python parse/lint fallback olarak korunur. Async → step9/step10 `async def` + `asyncio.to_thread(crew.kickoff)` ile gerçek paralellik (claude_cli blocking subprocess).

**Tech Stack:** Python 3.13, CrewAI 1.14.4, pydantic, asyncio. Test altyapısı yok → doğrulama import + inline `-c` kontrolleri + toggle-kapalı regresyon + runtime log incelemesiyle yapılır (CLAUDE.md kuralı).

---

## Spec'ten Sapmalar (planlama sırasında keşfedildi, gerekçeli)

1. **Embedder:** Spec "embedder Ollama'ya sabit" diyordu. Gerçek: deploy edilmiş embedder **fastembed** (`config/embed_config.yaml`: provider=fastembed, model=intfloat/multilingual-e5-large), ve CrewAI'nin embedder provider listesinde fastembed YOK. Çözüm: CrewAI `custom` embedder ile projenin `embed_text()` registry'sini sarmak — spec'in *niyetini* (OpenAI default'a düşme, mevcut embedding altyapısını kullan) daha iyi karşılar.
2. **Developer guardrail:** Spec "guardrail = _validate_code'un lint kısmı" diyordu. Gerçek: developer task çıktısı **kısmi kod bloğu**, lint Python'da *merge edilmiş* dosyaya uygulanıyor (flow.py:2647). Bloğa lint koymak valid bloğu reddeder. Çözüm: developer guardrail **yapısal** (non-empty / kod-benzeri / tool-call yok); lint+ollama-fix Python fallback'te kalır. Kullanıcının "guardrail birincil, fix fallback kalır" kararıyla uyumlu.

---

## File Structure

**Yeni dosyalar:**
- `src/agile_sdlc_crew/guardrails.py` — architect + developer guardrail factory fonksiyonları. Tek sorumluluk: TaskOutput doğrulama, `(bool, Any)` döndürme.
- `src/agile_sdlc_crew/embed/crewai_embedder.py` — CrewAI custom EmbeddingFunction + embedder config helper. Projenin `embed_text()`'ini CrewAI Knowledge'a köprüler.

**Değişen dosyalar:**
- `src/agile_sdlc_crew/pipeline_config.py` — 4 yeni knob (SCHEMA).
- `src/agile_sdlc_crew/tools/tool_cache.py` — `CachedToolMixin`'e threading.Lock (concurrent step9/step10 güvenliği).
- `src/agile_sdlc_crew/flow.py` — `_akickoff` helper; step9/step10 `async def`.
- `src/agile_sdlc_crew/crew.py` — guardrail wiring (create_analysis_crew, create_code_crew); knowledge_sources wiring (agent factory'ler + helper'lar).
- `src/agile_sdlc_crew/knowledge/__init__.py` — `load_knowledge_source` helper.

---

# FAZ 1 — ASYNC (step9 ∥ step10)

En izole özellik, en net kazanç (claude_cli ile gerçek paralellik). Önce bu.

## Task 1: pipeline_config — 4 yeni knob ekle

**Files:**
- Modify: `src/agile_sdlc_crew/pipeline_config.py` (SCHEMA listesi)

Üç fazın da env knob'ları tek seferde eklenir (trivial, hepsi bir arada dursun).

- [ ] **Step 1: SCHEMA'ya 4 entry ekle**

`SCHEMA` listesinde `CREW_ANALYZE_WI_MEDIA` entry'sinden hemen sonra (Pipeline davranış toggle'ları bloğunun sonu) şu 4 entry'yi ekle:

```python
    {
        "key": "CREW_PARALLEL_TEST_UAT",
        "label": "Test + UAT Paralel",
        "type": "bool",
        "default": False,
        "desc": "Test planlama (step9) ve UAT (step10) ayni anda calissin. claude_cli ile sure kisalir; ayni tek lokal modelde kazanc sinirli olabilir.",
    },
    {
        "key": "CREW_TASK_GUARDRAILS",
        "label": "Task Guardrails",
        "type": "bool",
        "default": False,
        "desc": "Architect (JSON) ve Developer (kod) ciktilarini CrewAI guardrail ile dogrula; basarisizsa agent otomatik retry eder.",
    },
    {
        "key": "CREW_KNOWLEDGE_RAG",
        "label": "Knowledge RAG",
        "type": "bool",
        "default": False,
        "desc": "Domain knowledge'i backstory'ye tikistirmak yerine CrewAI Knowledge ile RAG olarak ver (token tasarrufu, kucuk modelde odak).",
    },
```

**Not:** Üçü de `bool`. Knowledge embedding storage için ayrı knob yok — CrewAI default storage dizini kullanılır (v1; gerekirse sonra eklenir).

- [ ] **Step 2: Import + get() doğrula**

Run: `.venv/bin/python -c "from agile_sdlc_crew import pipeline_config as pc; print(pc.get('CREW_PARALLEL_TEST_UAT'), pc.get('CREW_TASK_GUARDRAILS'), pc.get('CREW_KNOWLEDGE_RAG'))"`
Expected: `False False False` (üç default-kapalı bool)

- [ ] **Step 3: Commit**

```bash
git add src/agile_sdlc_crew/pipeline_config.py
git commit -m "feat: CrewAI Knowledge/Guardrails/Async icin 4 pipeline knob"
```

---

## Task 2: tool_cache — concurrent erişim için threading.Lock

step9 (QA) ve step10 (UAT) ajanları tool çağırınca `CachedToolMixin._cache`/`_call_count` (class-level, paylaşımlı) iki thread'den eş zamanlı yazılır. Race'i önle.

**Files:**
- Modify: `src/agile_sdlc_crew/tools/tool_cache.py`

- [ ] **Step 1: threading import + class-level lock ekle**

`import hashlib` satırının yanına `import threading` ekle. `CachedToolMixin` class'ında `_cache: dict = {}` / `_call_count: dict = {}` satırlarının hemen altına ekle:

```python
    _lock = threading.Lock()
```

- [ ] **Step 2: `_cached_wrap` gövdesini lock ile koru**

Mevcut `_cached_wrap` metodunda, `key = (...)` satırından itibaren tüm read-modify-write bloğunu lock altına al. `original_run` çağrısı (yavaş, I/O) lock DIŞINDA kalmalı — yoksa paralellik biter. Metodu şununla değiştir:

```python
    def _cached_wrap(self, original_run, *args, **kwargs):
        """Orijinal _run metodunu sar: cache + limit kontrolu.
        - 1. cagri: calistir
        - 2-3. cagri: cache'den, uyari yok
        - 4. cagri: uyari ile cache
        - 5+ cagri: HARD BLOCK — agent'a cevap yok, baska yaklasim dene"""
        key = (self.__class__.__name__, _hash_args(args, kwargs))
        with self._lock:
            count = self._call_count.get(key, 0) + 1
            self._call_count[key] = count
            cached_hit = self._cache.get(key, None)
            has_hit = key in self._cache

        if has_hit:
            cached = cached_hit
            if count >= 5:
                log.warning(f"  Tool BLOCKED: {self.__class__.__name__} {count}x ayni argumanla — hard block")
                return (
                    f"🛑 BLOKE: Bu tool'u bu argumanlarla {count} kez cagirdin. "
                    f"Ayni sonucu aliyorsun. DUR ve dusun: farkli bir sorgu dene "
                    f"veya elindeki bilgiyle kararini ver. BU TOOL'U AYNI ARGUMANLARLA "
                    f"TEKRAR CAGIRMA — cevap donmeyecek."
                )
            if count >= 4:
                log.warning(f"  Tool limit: {self.__class__.__name__} {count}x ayni argumanla")
                return (
                    f"[UYARI: {count}. kez ayni argumanla cagirdin. Sonuc AYNI kalacak. "
                    f"Farkli bir yaklasim dene.]\n\n{cached}"
                )
            return f"[Cache, {count}. cagri]\n{cached}"

        # Ilk cagri — calistir (lock disinda, yavas olabilir) ve cache'e yaz
        result = original_run(*args, **kwargs)
        with self._lock:
            self._cache[key] = result
        return result
```

- [ ] **Step 3: Import doğrula**

Run: `.venv/bin/python -c "from agile_sdlc_crew.tools.tool_cache import CachedToolMixin; print(type(CachedToolMixin._lock).__name__)"`
Expected: `lock` (bir threading.Lock instance'ı)

- [ ] **Step 4: Commit**

```bash
git add src/agile_sdlc_crew/tools/tool_cache.py
git commit -m "feat: tool_cache thread-safe (concurrent step9/step10 icin)"
```

---

## Task 3: flow — `_akickoff` helper + step9/step10 async

**Files:**
- Modify: `src/agile_sdlc_crew/flow.py` (helper ekle; step9_test_planning + step10_uat imzalarını async yap; kickoff çağrılarını `_akickoff`'a çevir)

- [ ] **Step 1: `asyncio` import'unu doğrula/ekle**

Run: `grep -n "^import asyncio\|^import " src/agile_sdlc_crew/flow.py | grep asyncio`
Expected: Yoksa, flow.py'nin en üstündeki import bloğuna `import asyncio` ekle.

- [ ] **Step 2: `_akickoff` helper'ı ekle**

`_track_and_check_budget` metodunun hemen üstüne (flow.py:652 civarı) ekle:

```python
    async def _akickoff(self, crew, inputs: dict):
        """crew.kickoff()'u CREW_PARALLEL_TEST_UAT acikken ayri thread'de calistirir
        (claude_cli blocking subprocess → gercek paralellik). Kapaliyken dogrudan
        cagirir (event loop'u bloklar → bugunku seri davranis korunur)."""
        from agile_sdlc_crew import pipeline_config as _pc
        if _pc.get("CREW_PARALLEL_TEST_UAT"):
            return await asyncio.to_thread(crew.kickoff, inputs=inputs)
        return crew.kickoff(inputs=inputs)
```

- [ ] **Step 3: step9_test_planning'i `async def` yap ve crew kickoff'larını çevir**

flow.py:3077 `def step9_test_planning(self):` → `async def step9_test_planning(self):`

Aynı metotta NON-HAL (else) bloğundaki iki `test_crew.kickoff(inputs={...})` çağrısını (flow.py:3177 ve 3192) `await self._akickoff(test_crew, {...})` ile değiştir. Örnek (ilk çağrı):

```python
            test_result = await self._akickoff(test_crew, {
                "work_item_id": self.state.work_item_id,
                "requirements": self.state.requirements_text[:3000],
                "target_repo": self.state.repo_name,
                "target_branch": self.state.branch_name,
                "pr_id": self.state.pr_id,
                "previous_context": ctx,
                "scrum_master_feedback": "",
            })
```

İkinci (SM-retry) çağrı da aynı şekilde `await self._akickoff(test_crew, {...})`.

**Not:** HAL bloğundaki (`if self._hal:`) `code_crew.kickoff(...)` çağrılarına DOKUNMA — bunlar test push/fix amaçlı, paralellik hedefi değil; senkron kalsın (HAL yolu sizin claude_cli akışınız değil).

- [ ] **Step 4: step10_uat'ı `async def` yap ve crew kickoff'larını çevir**

flow.py:3214 `def step10_uat(self):` → `async def step10_uat(self):`

NON-HAL bloğundaki iki `uat_crew.kickoff(inputs={...})` çağrısını (flow.py:3247 ve 3261) `await self._akickoff(uat_crew, {...})` ile değiştir:

```python
            uat_result = await self._akickoff(uat_crew, {
                "work_item_id": self.state.work_item_id,
                "requirements": self.state.requirements_text[:3000],
                "pr_id": self.state.pr_id,
                "pr_url": self.state.pr_url,
                "previous_context": ctx,
                "scrum_master_feedback": "",
            })
```

İkinci (SM-retry) çağrı da `await self._akickoff(uat_crew, {...})`.

- [ ] **Step 5: Import + flow yapısı bozulmadı doğrula**

Run: `.venv/bin/python -c "from agile_sdlc_crew.flow import AgileSDLCFlow; import inspect; print(inspect.iscoroutinefunction(AgileSDLCFlow.step9_test_planning), inspect.iscoroutinefunction(AgileSDLCFlow.step10_uat))"`
Expected: `True True`

- [ ] **Step 6: Commit**

```bash
git add src/agile_sdlc_crew/flow.py
git commit -m "feat: step9/step10 async — CREW_PARALLEL_TEST_UAT ile paralel kickoff"
```

---

## Task 4: Async — uçtan uca doğrulama

**Files:** (yok — runtime doğrulama)

- [ ] **Step 1: Toggle-KAPALI regresyon**

Server'ı yeniden başlat (kod değişti). `CREW_PARALLEL_TEST_UAT` set etmeden bir WI çalıştır (dry-run yeterli):

Run: `curl -s -X POST http://localhost:8765/api/run -H 'Content-Type: application/json' -d '{"work_item_id":"<TEST_WI>","use_hal":false}'`
Expected: Pipeline step9 ve step10'u bugünkü gibi sırayla tamamlar; `/tmp/crew_server.log`'da hata yok. (async def ama sync kickoff → seri davranış korunur.)

- [ ] **Step 2: Toggle-AÇIK paralellik kontrolü**

`.env`'e `CREW_PARALLEL_TEST_UAT=1` ekle (veya dashboard'dan aç), server'ı yeniden başlat, aynı WI'yı çalıştır.
Run: `grep -nE "ADIM 9|ADIM 10" /tmp/crew_pipeline.log | tail -8`
Expected: "ADIM 9" ve "ADIM 10" log satırları iç içe / yakın zaman damgalarıyla görünür (seri değil, üst üste binmiş). Toplam step9+step10 süresi seri duruma göre kısalır.

- [ ] **Step 3: status.json bozulmadı**

Run: `.venv/bin/python -c "import json; json.load(open('src/agile_sdlc_crew/web/status.json'))" && echo OK`
Expected: `OK` (concurrent yazımlar StatusTracker lock'u ile geçerli JSON üretti)

---

# FAZ 2 — GUARDRAILS

## Task 5: guardrails.py modülü

**Files:**
- Create: `src/agile_sdlc_crew/guardrails.py`

- [ ] **Step 1: Modülü oluştur**

```python
"""CrewAI Task guardrail'leri — yapisal-cikti task'larini dogrula.

Guardrail imzasi (CrewAI 1.14.x): Callable[[TaskOutput], tuple[bool, Any]].
  - (True, deger)  → gecti; `deger` task ciktisi olur
  - (False, hata)  → kaldi; CrewAI hatayi agent'a geri besleyip retry eder
                      (Task.guardrail_max_retries, default 3)

Tasarim: guardrail SADECE dogrular, donusturmez. Basariili durumda ham metni
aynen geri verir (passthrough) — boylece flow tarafindaki mevcut parse/merge
mantigi degismeden calismaya devam eder. Guardrail birincil retry; mevcut
Python parse (architect) / lint+ollama-fix (developer) FALLBACK olarak kalir.
"""

from __future__ import annotations


def _raw_of(output) -> str:
    """TaskOutput'tan ham metni guvenle al."""
    return (getattr(output, "raw", None) or str(output) or "").strip()


def architect_json_guardrail(output) -> tuple[bool, str]:
    """Architect technical_design ciktisinin parse edilebilir/gecerli JSON plan
    oldugunu dogrular. _parse_architect_output'u (4 kademeli onarim + placeholder
    guard) yeniden kullanir; basariili olursa ham metni passthrough eder."""
    from agile_sdlc_crew.main import _parse_architect_output

    raw = _raw_of(output)
    if not raw:
        return (False, "Cikti bos. Gecerli JSON teknik tasarim plani uret.")
    try:
        _parse_architect_output(raw)
    except ValueError as e:
        return (
            False,
            f"JSON plan validation hatasi: {e} "
            f"SADECE gecerli JSON uret — tool cagirma, aciklama yazma, "
            f"placeholder (timestamp/class/tablo adi somut olmali) kullanma.",
        )
    return (True, raw)


# Developer ciktisi TAM DOSYA DEGIL, kismi kod blogu olabilir (flow merge eder).
# Bu yuzden LINT YAPMA — lint Python tarafinda merge edilmis dosyaya uygulanir.
# Burada sadece yapisal/cheap kontrol: bos degil, kod-benzeri, tool-call yok.
_REFUSAL_MARKERS = (
    "uzgunum", "yapamam", "yardimci olamam", "i cannot", "i'm sorry", "as an ai",
)


def developer_code_guardrail(output) -> tuple[bool, str]:
    """Developer kod ciktisinin bos/cop/halusinasyon olmadigini dogrular.
    Lint DEGIL — yapisal kontrol (lint Python fallback'te kalir)."""
    raw = _raw_of(output)
    if len(raw) < 10:
        return (False, "Cikti bos veya cok kisa. Istenen kod blogunu uret.")
    if "<tool_call>" in raw:
        return (False, "Tool cagrisi tespit edildi. Tool cagirma — sadece kod blogu yaz.")
    low = raw.lower()
    if any(m in low for m in _REFUSAL_MARKERS) and "```" not in raw:
        return (False, "Aciklama/ret degil — istenen kod degisikligini kod olarak yaz.")
    return (True, raw)
```

- [ ] **Step 2: Import + architect guardrail davranışını doğrula**

Run:
```bash
.venv/bin/python -c "
from types import SimpleNamespace as NS
from agile_sdlc_crew.guardrails import architect_json_guardrail, developer_code_guardrail
# bos / bozuk → False
print(architect_json_guardrail(NS(raw=''))[0])
print(architect_json_guardrail(NS(raw='bu json degil'))[0])
# gecerli → True
ok = architect_json_guardrail(NS(raw='{\"repo_name\":\"r\",\"changes\":[{\"file_path\":\"/a.php\",\"change_type\":\"edit\",\"new_code\":\"x\"}]}'))
print(ok[0])
# developer
print(developer_code_guardrail(NS(raw=''))[0], developer_code_guardrail(NS(raw='function f(){ return 1; }'))[0])
"
```
Expected: `False` `False` `True` ardından `False True`

- [ ] **Step 3: Commit**

```bash
git add src/agile_sdlc_crew/guardrails.py
git commit -m "feat: guardrails modulu — architect JSON + developer yapisal dogrulama"
```

---

## Task 6: Architect guardrail'i create_analysis_crew'a bağla + flow fallback

**Files:**
- Modify: `src/agile_sdlc_crew/crew.py:999-1012` (create_analysis_crew)
- Modify: `src/agile_sdlc_crew/flow.py:2239-2272` (guardrail-exhaustion fallback)

- [ ] **Step 1: create_analysis_crew'a opsiyonel guardrail parametresi ekle**

`create_analysis_crew` metodunu şununla değiştir:

```python
    def create_analysis_crew(self, with_guardrail: bool | None = None) -> Crew:
        """Software Architect: is kalemini oku, repo'yu incele, teknik tasarim olustur.
        Not: output_pydantic CrewAI'da OpenAI API key istedigi icin kullanmiyoruz —
        task prompt'undaki JSON format kurali + retry mekanizmasi yeterli.
        with_guardrail=None ise CREW_TASK_GUARDRAILS knob'una bakar."""
        from agile_sdlc_crew import pipeline_config as _pc
        if with_guardrail is None:
            with_guardrail = _pc.get("CREW_TASK_GUARDRAILS")
        arch = self.software_architect()
        extra = {}
        if with_guardrail:
            from agile_sdlc_crew.guardrails import architect_json_guardrail
            extra["guardrail"] = architect_json_guardrail
        t1 = self._task("technical_design_task", arch, **extra)

        return Crew(
            agents=[arch],
            tasks=[t1],
            process=Process.sequential,
            verbose=True,
            memory=False,
        )
```

(`_task` zaten `**extra`'yı Task'a geçiriyor — crew.py:382-392.)

- [ ] **Step 2: flow'da guardrail-exhaustion fallback'i kur**

flow.py:2239-2272'deki ilk kickoff + parse bloğunu şununla değiştir. Guardrail açıkken in-crew retry birincildir; tükenirse `with_guardrail=False` ile bir kez manuel rekick (mevcut davranış = fallback):

```python
        analysis_crew = self._agile_crew.create_analysis_crew()
        try:
            analysis_result = analysis_crew.kickoff(inputs={
                "work_item_id": self.state.work_item_id,
                "target_repo": prefetch_repo or "",
                "previous_context": ctx,
                "scrum_master_feedback": ctx_hint,
            })
        except Exception as e:
            # Guardrail retry'lari tukendi (veya kickoff hatasi) — guardrail'siz
            # crew ile manuel fallback. Mevcut parse onarimi yine devrede.
            _log(f"  Guardrail/kickoff hatasi ({e}), guardrail'siz fallback kickoff")
            analysis_crew = self._agile_crew.create_analysis_crew(with_guardrail=False)
            analysis_result = analysis_crew.kickoff(inputs={
                "work_item_id": self.state.work_item_id,
                "target_repo": prefetch_repo or "",
                "previous_context": ctx,
                "scrum_master_feedback": ctx_hint,
            })
        self._track_and_check_budget(analysis_result, "technical_design_task")
        raw_output = analysis_result.raw or ""

        # Parse hatasi — onceki ciktiyi context'e ekleyip tekrar dene
        # (Guardrail KAPALIYKEN birincil retry; ACIKKEN guardrail zaten parse
        #  garantiledigi icin bu blok genelde tetiklenmez — yine de fallback kalir.)
        try:
            plan = _parse_architect_output(raw_output)
        except ValueError as e:
            _log(f"  Parse hatasi ({e}), guardrail'siz architect ile retry")
            retry_ctx = ctx + (
                f"\n\n# ONCEKI DENEME CIKTISI\n"
                f"(Asagidaki bilgilerden JSON uret — tool cagirma, direkt JSON yaz)\n\n"
                f"{raw_output[:6000]}"
            )
            analysis_crew = self._agile_crew.create_analysis_crew(with_guardrail=False)
            analysis_result = analysis_crew.kickoff(inputs={
                "work_item_id": self.state.work_item_id,
                "target_repo": prefetch_repo or "",
                "previous_context": retry_ctx,
                "scrum_master_feedback": (
                    f"⚠️ Onceki denemende plan validation hatasi:\n{e}\n\n"
                    "Hatayi gider ve SADECE gecerli JSON plan uret. "
                    "Tool cagirma, aciklama yazma — SADE JSON. "
                    "Placeholder kullanma (timestamp/class/tablo adlari somut olmali)."
                ),
            })
            self._track_and_check_budget(analysis_result, "technical_design_task (retry)")
            raw_output = analysis_result.raw or ""
            plan = _parse_architect_output(raw_output)
```

**Not:** Sonraki SM Review bloğu (flow.py:2274+) DEĞİŞMEZ.

- [ ] **Step 3: Import + crew kurulumu doğrula**

Run: `.venv/bin/python -c "from agile_sdlc_crew.crew import AgileSDLCCrew; c=AgileSDLCCrew(); cr=c.create_analysis_crew(with_guardrail=True); print(cr.tasks[0].guardrail is not None)"`
Expected: `True`

Run (guardrail'siz): `.venv/bin/python -c "from agile_sdlc_crew.crew import AgileSDLCCrew; c=AgileSDLCCrew(); cr=c.create_analysis_crew(with_guardrail=False); print(cr.tasks[0].guardrail)"`
Expected: `None`

- [ ] **Step 4: Commit**

```bash
git add src/agile_sdlc_crew/crew.py src/agile_sdlc_crew/flow.py
git commit -m "feat: architect JSON guardrail + guardrail-exhaustion fallback"
```

---

## Task 7: Developer guardrail'i create_code_crew'a bağla

Developer guardrail context'siz (sadece çıktı metnine bakar) → imza değişmeden koşullu eklenebilir. create_code_crew tüm çağrı noktalarında (step6, step9-HAL) guardrail'i otomatik alır.

**Files:**
- Modify: `src/agile_sdlc_crew/crew.py:1035-1052` (create_code_crew)

- [ ] **Step 1: create_code_crew'a koşullu guardrail ekle**

`create_code_crew` metodunu şununla değiştir:

```python
    def create_code_crew(self) -> Crew:
        """Developer: tek dosya icin degisiklik uygula.
        Not: output_pydantic kullanmiyoruz (OpenAI API key sorunu) — task
        prompt'undaki TAM DOSYA kurali + push oncesi guvenlik kontrollari yeterli.
        CREW_TASK_GUARDRAILS=1 iken yapisal guardrail (bos/cop/tool-call) eklenir;
        lint Python tarafinda (_validate_code) fallback olarak kalir."""
        from agile_sdlc_crew import pipeline_config as _pc
        dev = self.senior_developer()
        cfg = self.tasks_config["implement_change_task"]
        extra = {}
        if _pc.get("CREW_TASK_GUARDRAILS"):
            from agile_sdlc_crew.guardrails import developer_code_guardrail
            extra["guardrail"] = developer_code_guardrail
        t = Task(
            description=cfg["description"],
            expected_output=cfg["expected_output"],
            agent=dev,
            **extra,
        )
        return Crew(
            agents=[dev],
            tasks=[t],
            process=Process.sequential,
            verbose=True,
            memory=False,
        )
```

- [ ] **Step 2: Koşullu guardrail doğrula**

Run:
```bash
CREW_TASK_GUARDRAILS=1 .venv/bin/python -c "from agile_sdlc_crew.crew import AgileSDLCCrew; c=AgileSDLCCrew(); print(c.create_code_crew().tasks[0].guardrail is not None)"
.venv/bin/python -c "from agile_sdlc_crew.crew import AgileSDLCCrew; c=AgileSDLCCrew(); print(c.create_code_crew().tasks[0].guardrail)"
```
Expected: `True` ardından `None`

- [ ] **Step 3: Commit**

```bash
git add src/agile_sdlc_crew/crew.py
git commit -m "feat: developer yapisal guardrail (lint Python fallback'te kalir)"
```

---

## Task 8: Guardrails — uçtan uca doğrulama

- [ ] **Step 1: Toggle-KAPALI regresyon**

`CREW_TASK_GUARDRAILS` set etmeden bir WI çalıştır.
Expected: Architect/developer adımları bugünkü manuel parse/lint yollarıyla çalışır; `/tmp/crew_server.log`'da guardrail'le ilgili yeni davranış yok, regresyon yok.

- [ ] **Step 2: Toggle-AÇIK guardrail tetikleme**

`.env`'e `CREW_TASK_GUARDRAILS=1` ekle, server'ı yeniden başlat, küçük local model architect'i olan (bozuk JSON'a meyilli) bir profille bir WI çalıştır.
Run: `grep -niE "guardrail|validation|retry" /tmp/crew_server.log | tail -20`
Expected: En az bir architect adımında guardrail retry'ın CrewAI tarafından tetiklendiği görülür; parse başarılıysa flow'daki manuel fallback rekick ÇAĞRILMAZ. Pipeline başarıyla tamamlanır.

---

# FAZ 3 — KNOWLEDGE SOURCES

## Task 9: CrewAI custom embedder (embed_text köprüsü)

**Files:**
- Create: `src/agile_sdlc_crew/embed/crewai_embedder.py`

- [ ] **Step 1: Custom embedding function + config helper oluştur**

```python
"""CrewAI Knowledge icin custom embedder — projenin embed_text() registry'sini
CrewAI'a kopruler. Boylece Knowledge, vector store ile AYNI embedder'i kullanir
ve CrewAI'nin OpenAI embedder default'una (API key gerektirir) DUSMEZ.

CrewAI custom embedder formati:
    {"provider": "custom", "config": {"embedding_callable": <CustomEmbeddingFunction altsinifi>}}
"""

from __future__ import annotations

import logging

from crewai.rag.embeddings.providers.custom.embedding_callable import (
    CustomEmbeddingFunction,
)

log = logging.getLogger("pipeline")


class ProjectEmbeddingFunction(CustomEmbeddingFunction):
    """chromadb EmbeddingFunction arayuzu: __call__(input: list[str]) -> list[vector].
    Her cagrida embed resolver'i okur (provider/model dashboard'dan degisebilir)."""

    def __call__(self, input):
        from agile_sdlc_crew.embed import embed_text, get_model, get_provider

        if isinstance(input, str):
            input = [input]
        provider = get_provider()
        model = get_model()
        return [embed_text(provider, text, model) for text in input]


def crewai_embedder_config() -> dict:
    """CrewAI Agent/Crew/Knowledge'a verilecek embedder config dict'i."""
    return {
        "provider": "custom",
        "config": {"embedding_callable": ProjectEmbeddingFunction},
    }
```

- [ ] **Step 2: Embedder gerçekten embedding üretiyor mu doğrula (OpenAI'a düşmeden)**

Run:
```bash
.venv/bin/python -c "
from agile_sdlc_crew.embed.crewai_embedder import ProjectEmbeddingFunction, crewai_embedder_config
fn = ProjectEmbeddingFunction()
v = fn(['merhaba dunya'])
print('cfg_ok', crewai_embedder_config()['provider'] == 'custom')
print('dim', len(v), len(v[0]) if v and v[0] else 0)
"
```
Expected: `cfg_ok True` ve `dim 1` ardından embedding boyutu (örn. 1024). Hata YOK (özellikle OpenAI key hatası YOK). Eğer `embed_text` fastembed modelini indirmesi gerekiyorsa ilk çağrı yavaş olabilir — bu normal.

- [ ] **Step 3: Commit**

```bash
git add src/agile_sdlc_crew/embed/crewai_embedder.py
git commit -m "feat: CrewAI custom embedder — embed_text() koprusu (OpenAI default'u bypass)"
```

---

## Task 10: knowledge source helper + agent factory wiring

**Files:**
- Modify: `src/agile_sdlc_crew/knowledge/__init__.py` (load_knowledge_source ekle)
- Modify: `src/agile_sdlc_crew/crew.py` (_agent_config_with_knowledge koşullu skip + `_knowledge_kwargs` helper + 7 agent factory)

- [ ] **Step 1: knowledge/__init__.py'ye source helper ekle**

`load_knowledge` fonksiyonunun altına ekle:

```python
def load_knowledge_source(name: str):
    """Knowledge .md icerigini CrewAI StringKnowledgeSource olarak dondur.
    Icerik bossa None doner (cagiran filtreler)."""
    from crewai.knowledge.source.string_knowledge_source import StringKnowledgeSource

    content = load_knowledge(name)
    if not content.strip():
        return None
    return StringKnowledgeSource(content=content)
```

- [ ] **Step 2: StringKnowledgeSource import yolunu doğrula**

Run: `.venv/bin/python -c "from crewai.knowledge.source.string_knowledge_source import StringKnowledgeSource; print('ok')"`
Expected: `ok`. (Yol farklıysa düzelt: `.venv/bin/python -c "import crewai.knowledge.source as s; import pkgutil; print([m.name for m in pkgutil.iter_modules(s.__path__)])"` ile bul.)

- [ ] **Step 3: crew.py'ye `_knowledge_kwargs` helper ekle ve `_agent_config_with_knowledge`'ı koşullu yap**

`_agent_config_with_knowledge` metodunda (crew.py:230), backstory enjeksiyon döngüsünden ÖNCE knob kontrolü ekle. Toggle açıkken backstory'ye enjekte ETME (RAG kullanılacak):

`cfg.pop("llm_profile", None)` satırının hemen altına ekle:

```python
        # RAG modu acikken knowledge backstory'ye DEGIL, knowledge_sources'a gider.
        from agile_sdlc_crew import pipeline_config as _pc
        if _pc.get("CREW_KNOWLEDGE_RAG"):
            return cfg
```

Sonra `_agent_config_with_knowledge` metodunun hemen altına yeni helper ekle:

```python
    def _knowledge_kwargs(self, *knowledge_names: str) -> dict:
        """CREW_KNOWLEDGE_RAG acikken Agent'a knowledge_sources + embedder verir;
        kapaliyken bos dict (backstory enjeksiyonu devrede)."""
        from agile_sdlc_crew import pipeline_config as _pc
        if not _pc.get("CREW_KNOWLEDGE_RAG"):
            return {}
        from agile_sdlc_crew.knowledge import load_knowledge_source
        from agile_sdlc_crew.embed.crewai_embedder import crewai_embedder_config
        sources = [s for s in (load_knowledge_source(n) for n in knowledge_names) if s]
        if not sources:
            return {}
        return {"knowledge_sources": sources, "embedder": crewai_embedder_config()}
```

- [ ] **Step 4: 7 agent factory'ye `**self._knowledge_kwargs(...)` ekle**

Her agent factory'sinde, `Agent(...)` çağrısının son argümanından sonra (kapanış parantezinden önce) `_agent_config_with_knowledge`'a verilen AYNI knowledge isimleriyle `**self._knowledge_kwargs(...)` ekle:

`scrum_master` (crew.py:255) — `tools=[...]`'tan sonra:
```python
            **self._knowledge_kwargs("agile_facilitation"),
```
`business_analyst` (crew.py:269):
```python
            **self._knowledge_kwargs("requirements_analysis"),
```
`software_architect` (crew.py:303):
```python
            **self._knowledge_kwargs("backend_tech_design", "frontend_nextjs"),
```
`qa_engineer` (crew.py:321):
```python
            **self._knowledge_kwargs("testing_strategy"),
```
`uat_specialist` (crew.py:332):
```python
            **self._knowledge_kwargs("uat_strategy"),
```
`senior_developer` (crew.py:351):
```python
            **self._knowledge_kwargs("backend_feature_dev", "frontend_nextjs"),
```
`code_reviewer` (crew.py:363):
```python
            **self._knowledge_kwargs("backend_code_review"),
```

- [ ] **Step 5: Toggle-kapalı/açık config doğrula**

Run (kapalı — backstory enjeksiyonu devam):
```bash
.venv/bin/python -c "
from agile_sdlc_crew.crew import AgileSDLCCrew
c = AgileSDLCCrew()
a = c.software_architect()
print('off knowledge_sources:', bool(getattr(a, 'knowledge_sources', None)))
print('off backstory_injected:', 'DOMAIN KNOWLEDGE' in (a.backstory or ''))
"
```
Expected: `off knowledge_sources: False` ve `off backstory_injected: True`

Run (açık — RAG):
```bash
CREW_KNOWLEDGE_RAG=1 .venv/bin/python -c "
from agile_sdlc_crew.crew import AgileSDLCCrew
c = AgileSDLCCrew()
a = c.software_architect()
print('on knowledge_sources:', len(getattr(a, 'knowledge_sources', []) or []))
print('on backstory_injected:', 'DOMAIN KNOWLEDGE' in (a.backstory or ''))
"
```
Expected: `on knowledge_sources: 2` ve `on backstory_injected: False`

- [ ] **Step 6: Commit**

```bash
git add src/agile_sdlc_crew/knowledge/__init__.py src/agile_sdlc_crew/crew.py
git commit -m "feat: knowledge RAG — backstory enjeksiyonu yerine knowledge_sources + custom embedder"
```

---

## Task 11: Knowledge — uçtan uca doğrulama

- [ ] **Step 1: Toggle-KAPALI regresyon**

`CREW_KNOWLEDGE_RAG` set etmeden bir WI çalıştır.
Expected: Ajanlar bugünkü gibi backstory-enjekte knowledge ile çalışır; regresyon yok.

- [ ] **Step 2: Toggle-AÇIK — OpenAI hatası YOK + embedding üretildi**

`.env`'e `CREW_KNOWLEDGE_RAG=1` ekle, server'ı yeniden başlat, bir WI çalıştır.
Run: `grep -niE "openai|api key|embedchain|knowledge|chroma" /tmp/crew_server.log | tail -30`
Expected: **OpenAI key/embedder hatası YOK.** Knowledge kaynakları custom embedder ile embed edildi. Architect adımının prompt'u (backstory) bariz şekilde kısalmış olmalı (DOMAIN KNOWLEDGE bloğu artık prompt'ta değil).

- [ ] **Step 3: Çıktı kalitesi spot-check**

Çalışan WI'da architect/developer çıktısının domain knowledge'a uygun kaldığını (FLO stack konvansiyonları) WI yorumlarından / `/tmp/crew_pipeline.log`'dan doğrula.
Expected: Knowledge RAG ile çekilmesine rağmen çıktı kalitesi backstory-enjekte moda göre düşmemiş.

---

## Genel Doğrulama (tüm fazlar sonrası)

- [ ] **Step 1: Tüm toggle'lar kapalı — tam regresyon**

Üç toggle da kapalıyken uçtan uca bir gerçek (dry-run olmayan) WI çalıştır.
Expected: Pipeline bugünküyle birebir aynı davranır; PR oluşur, hata yok.

- [ ] **Step 2: Tüm toggle'lar açık — kombine çalışma**

`CREW_PARALLEL_TEST_UAT=1 CREW_TASK_GUARDRAILS=1 CREW_KNOWLEDGE_RAG=1` ile bir WI çalıştır.
Expected: Üçü birlikte çalışır; budget guard aşılmaz; pipeline tamamlanır.

- [ ] **Step 3: .env.example / docs güncelle (varsa)**

Run: `grep -rn "CREW_KICKOFF_MEETING\|CREW_SM_REVIEW" .env.example README* 2>/dev/null | head`
Expected: Eğer env knob'ları dokümante eden bir dosya varsa, 4 yeni knob'u aynı stille ekle. Yoksa atla.
