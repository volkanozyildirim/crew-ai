# Context Rot Prevention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pipeline'da context-rot'a karşı deterministik (ekstra LLM çağrısı olmayan) önlemler: context boyutunu görünür kıl, ölü accumulator'ı kaldır, truncation'ları merkezile + sınırla, cross-job büyümeyi bağla.

**Architecture:** Yeni `context_budget.py` modülü context boyut politikasını (named cap'ler, env-override) ve gözlemlenebilirliği (`measure()` log+guard) tek yere toplar. `_build_step_context` bu cap'leri kullanır ve return'ünde `measure()`'dan geçer → her adımda ajana giden context boyutu loglanır. Ölü `self.state.previous_context` accumulator'ı (+onu koruyan `_state_lock`) tamamen kaldırılır. Cross-job vector büyümesi toggle'a bağlanır.

**Tech Stack:** Python 3.13, CrewAI Flow, pydantic. Test altyapısı yok (CLAUDE.md) → doğrulama: import kontrolü + grep + inline `-c` + runtime log gözlemi.

---

## File Structure

**Yeni:**
- `src/agile_sdlc_crew/context_budget.py` — context boyut cap'leri (env-override) + `measure(label, text)` ölçüm/guard helper. Tek sorumluluk: context boyut politikası + gözlemlenebilirlik.

**Değişen:**
- `src/agile_sdlc_crew/flow.py` — `_build_step_context` cap'leri kullanır + return'de `measure()`; ölü `previous_context`/`_append_context`/`_state_lock` kaldırılır; `_reset_job_state()` + kickoff boyut logu; `_step_done`'da vector kayıt toggle'ı.

---

## Önemli not — M2 (ölü accumulator) kanıtı

`self.state.previous_context` YALNIZCA yazılıyor (flow.py:227,229 `_append_context` içinde), hiçbir yerde okunmuyor (grep ile kanıtlandı: diğer tüm "previous_context" referansları kickoff *input* anahtarı = `_build_step_context()` çıktısı olan yerel `ctx`). `_state_lock` (flow.py:214,226,918) yalnızca `_append_context` tarafından kullanılıyor. Dolayısıyla ikisi de güvenle kaldırılır; davranış değişmez.

---

## Task 1: context_budget.py modülü (M1)

**Files:**
- Create: `src/agile_sdlc_crew/context_budget.py`

- [ ] **Step 1: Modülü oluştur**

```python
"""Context boyut politikasi + gozlemlenebilirlik — deterministik (LLM cagrisi yok).

Named cap'ler env ile override edilir: CREW_CTX_<NAME>. measure() her adimda
ajana giden context boyutunu pipeline log'una yazar ve CREW_CTX_TOTAL_WARN
esigini asarsa uyarir (CREW_CTX_HARD_TRUNCATE=1 ise son N char'a kirpar).
"""

import logging
import os

log = logging.getLogger("pipeline")

# char cinsinden (PLAN_CHANGES ve AC adet cinsinden). Default'lar mevcut
# magic-number'larla ayni — sikilastirma env ile opsiyonel.
_DEFAULTS = {
    "KICKOFF": 4000,        # technical_design'a tasinan kickoff
    "KICKOFF_QA": 2500,     # test/uat'a tasinan kickoff
    "KICKOFF_REVIEW": 2000, # review'a tasinan kickoff
    "REQUIREMENTS": 3000,
    "REVIEW": 2500,
    "TEST": 2500,
    "UAT": 2500,
    "PLAN_CHANGES": 10,     # adet
    "SIMILAR": 200,         # benzer is icerik char
    "TOTAL_WARN": 24000,    # assemble edilen context icin uyari esigi
}


def cap(name: str) -> int:
    """Named cap degerini dondur (env CREW_CTX_<NAME> override eder)."""
    default = _DEFAULTS[name]
    raw = os.environ.get(f"CREW_CTX_{name}")
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def measure(label: str, text: str) -> str:
    """Context boyutunu logla; esik asilirsa uyar (ve toggle ile kirp)."""
    text = text or ""
    n = len(text)
    log.info(f"  📏 context[{label}]: {n} char ≈{n // 4} tok")
    warn = cap("TOTAL_WARN")
    if n > warn:
        log.warning(f"  ⚠️ context[{label}] {n} char > {warn} esigini asti")
        if os.environ.get("CREW_CTX_HARD_TRUNCATE", "0") == "1":
            text = text[-warn:]
            log.warning(f"  ✂️ context[{label}] son {warn} char'a kirpildi")
    return text
```

- [ ] **Step 2: Import + davranış doğrula**

Run:
```bash
.venv/bin/python -c "
import logging,sys; logging.basicConfig(level=logging.INFO,format='%(message)s',stream=sys.stdout)
from agile_sdlc_crew import context_budget as cb
print('cap KICKOFF:', cb.cap('KICKOFF'))
import os; os.environ['CREW_CTX_KICKOFF']='1234'; print('cap override:', cb.cap('KICKOFF'))
out = cb.measure('test', 'x'*100); print('measure dondurdu len:', len(out))
os.environ['CREW_CTX_TOTAL_WARN']='50'; os.environ['CREW_CTX_HARD_TRUNCATE']='1'
out2 = cb.measure('big', 'y'*200); print('kirpilmis len:', len(out2))
"
```
Expected: `cap KICKOFF: 4000`, `cap override: 1234`, bir `📏 context[test]: 100 char ≈25 tok` satırı, `measure dondurdu len: 100`, `⚠️`+`✂️` satırları, `kirpilmis len: 50`.

- [ ] **Step 3: Commit**

```bash
git add src/agile_sdlc_crew/context_budget.py
git commit -m "feat: context_budget modulu — context boyut cap + measure/guard"
```

---

## Task 2: _build_step_context cap + measure (M3)

**Files:**
- Modify: `src/agile_sdlc_crew/flow.py` (`_build_step_context`, ~231-352)

- [ ] **Step 1: context_budget import'unu ekle**

`flow.py` üst import bloğunda, `from agile_sdlc_crew import pipeline_config` benzeri importların yanına ekle (yoksa modül başına):

```python
from agile_sdlc_crew import context_budget as _cb
```

- [ ] **Step 2: Magic number'ları cap() ile değiştir**

`_build_step_context` içinde şu değişiklikleri yap (mevcut metin → yeni):

- `{s.kickoff_text[:4000]}` → `{s.kickoff_text[:_cb.cap('KICKOFF')]}`
- `{s.kickoff_text[:2500]}` (test_planning_task bloğu) → `{s.kickoff_text[:_cb.cap('KICKOFF_QA')]}`
- `{s.kickoff_text[:2500]}` (uat_task bloğu) → `{s.kickoff_text[:_cb.cap('KICKOFF_QA')]}`
- `{s.kickoff_text[:2000]}` (review_pr_task bloğu) → `{s.kickoff_text[:_cb.cap('KICKOFF_REVIEW')]}`
- `{s.requirements_text[:3000]}` → `{s.requirements_text[:_cb.cap('REQUIREMENTS')]}`
- `s.plan.get("changes", [])[:10]` → `s.plan.get("changes", [])[:_cb.cap('PLAN_CHANGES')]`
- `s.review_text[:2500]` → `s.review_text[:_cb.cap('REVIEW')]`
- `s.test_text[:2500]` → `s.test_text[:_cb.cap('TEST')]`
- `s.uat_text[:2500]` → `s.uat_text[:_cb.cap('UAT')]`
- `x['content'][:200]` (benzer isler) → `x['content'][:_cb.cap('SIMILAR')]`

(Not: `acs[:10]` plan kabul kriterleri ve `acceptance_criteria` tam liste — bunlara DOKUNMA; AC truncation bu turda kapsam disi.)

- [ ] **Step 3: return'ü measure'dan geçir**

`_build_step_context`'in son satırı:
```python
        return "\n".join(parts)
```
şununla değiştir:
```python
        return _cb.measure(step_key, "\n".join(parts))
```

- [ ] **Step 4: Import + ölçüm doğrula**

Run:
```bash
.venv/bin/python -c "from agile_sdlc_crew.flow import AgileSDLCFlow; print('flow import ok')" 2>&1 | grep -vE "UserWarning|warnings.warn"
grep -n "_cb.cap\|_cb.measure" src/agile_sdlc_crew/flow.py | head
```
Expected: `flow import ok`; grep en az 10 `_cb.cap` + 1 `_cb.measure` satırı gösterir.

- [ ] **Step 5: Commit**

```bash
git add src/agile_sdlc_crew/flow.py
git commit -m "feat: _build_step_context cap'leri merkezile + her adim context boyutu loglanir"
```

---

## Task 3: Ölü previous_context + _state_lock kaldır (M2)

**Files:**
- Modify: `src/agile_sdlc_crew/flow.py` (field 175, _append_context 218-229, _state_lock 214/918, 27 çağrı yeri)

- [ ] **Step 1: Kaldırmadan önce ölü olduğunu son kez doğrula**

Run:
```bash
grep -n "state.previous_context" src/agile_sdlc_crew/flow.py
```
Expected: SADECE `_append_context` içindeki 2 yazım satırı (`self.state.previous_context += addition`). Başka satır çıkarsa (okuma) DUR ve bildir — kaldırma güvenli değil.

- [ ] **Step 2: Alan, metot ve lock'u kaldır**

- `flow.py:175` `previous_context: str = ""` satırını sil.
- `flow.py:214` `_state_lock: Any = PrivateAttr(default=None)` (ve üstündeki açıklayıcı yorumu) sil.
- `_append_context` metodunun tamamını (218-229, docstring dahil) sil.
- `flow.py:918` `self._state_lock = threading.Lock()` satırını sil.
- `flow.py` üstündeki `import threading` artık kullanılmıyorsa sil (önce `grep -n "threading" src/agile_sdlc_crew/flow.py` ile başka kullanım olmadığını doğrula; varsa bırak).

- [ ] **Step 3: 27 çağrı yerini sil**

Tüm `self._append_context(...)` ifadelerini sil. Hepsi dönüş değeri kullanılmayan bağımsız ifadelerdir. Çağrı yerleri (satır no'lar Step 2 sonrası kayar — içerikten bul):
flow.py civarı: 541, 861, 873, 1178, 1481, 1483, 1506, 1644, 1725, 1812, 1828, 1872, 2330, 2388, 2466, 2757, 2780, 2842, 2909, 3052, 3092, 3128, 3234, 3262, 3302, 3349.

**DİKKAT — boş blok tuzağı:** Bir `self._append_context(...)` bir `if:`/`else:`/`try:` bloğunun TEK ifadesiyse, sildiğinde blok boş kalır → syntax hatası. Bu durumda bloğu da kaldır veya `pass` ekle. Çoğu çağrı (resume blokları: state set + append + resume_step) çok-ifadeli, güvenli; tek-ifade olanları import kontrolü yakalar.

- [ ] **Step 4: Temizlik doğrula**

Run:
```bash
grep -n "_append_context\|_state_lock" src/agile_sdlc_crew/flow.py || echo "TEMIZ"
grep -n "state.previous_context" src/agile_sdlc_crew/flow.py || echo "previous_context TEMIZ"
.venv/bin/python -c "from agile_sdlc_crew.flow import AgileSDLCFlow; print('flow import ok')" 2>&1 | grep -vE "UserWarning|warnings.warn"
```
Expected: `TEMIZ`, `previous_context TEMIZ`, `flow import ok`. (`previous_context` kelimesi kickoff input anahtarı olarak hâlâ geçer — yalnızca `state.previous_context` ve `_append_context`/`_state_lock` sıfır olmalı.)

- [ ] **Step 5: Commit**

```bash
git add src/agile_sdlc_crew/flow.py
git commit -m "refactor: olu previous_context accumulator + gereksiz _state_lock kaldirildi (~65KB bloat)"
```

---

## Task 4: Cross-job hijyen — reset + vector toggle (M4)

**Files:**
- Modify: `src/agile_sdlc_crew/flow.py` (`initialize` ~906, `_step_done` save çağrısı)

- [ ] **Step 1: `_reset_job_state` helper ekle**

`initialize` metodunun hemen ÜSTÜNE ekle:

```python
    def _reset_job_state(self):
        """Job basinda paylasimli/birikimli durumu sifirla (cross-job sizinti onleme)."""
        from agile_sdlc_crew.tools.tool_cache import reset_tool_cache
        reset_tool_cache()
        self._job_prompt_tokens = 0
        self._job_completion_tokens = 0
        self._job_total_tokens = 0
```

`initialize` içinde mevcut `reset_tool_cache()` çağrısını (ve import'unu) şununla değiştir:
```python
        # Pipeline basi: birikimli durumu sifirla
        self._reset_job_state()
```
(Mevcut `from agile_sdlc_crew.tools.tool_cache import reset_tool_cache` satırı initialize import bloğundan kaldırılabilir — artık `_reset_job_state` içinde.)

- [ ] **Step 2: Vector step-output kaydını toggle'a bağla**

`_step_done` içindeki vector kayıt bloğunu bul:
```python
        if self._vector_store and output and len(output.strip()) > 20:
```
şununla değiştir (env toggle, default açık):
```python
        import os as _os_sd
        if (
            _os_sd.environ.get("CREW_SAVE_STEP_OUTPUT", "1") != "0"
            and self._vector_store and output and len(output.strip()) > 20
        ):
```

- [ ] **Step 3: Doğrula**

Run:
```bash
.venv/bin/python -c "from agile_sdlc_crew.flow import AgileSDLCFlow; print('reset var:', hasattr(AgileSDLCFlow,'_reset_job_state'))" 2>&1 | grep -vE "UserWarning|warnings.warn"
grep -n "CREW_SAVE_STEP_OUTPUT\|_reset_job_state" src/agile_sdlc_crew/flow.py
```
Expected: `reset var: True`; grep `_reset_job_state` tanım+çağrı ve `CREW_SAVE_STEP_OUTPUT` satırını gösterir.

- [ ] **Step 4: Commit**

```bash
git add src/agile_sdlc_crew/flow.py
git commit -m "feat: cross-job hijyen — _reset_job_state + CREW_SAVE_STEP_OUTPUT toggle"
```

**Not (kapsam dışı, follow-up):** Vector DB'de retention-tabanlı budama (eski `/jobs/...` kayıtlarını sayı sınırıyla silme) LanceDB storage delete API'si gerektirdiğinden bu plana dahil edilmedi; toggle cross-job büyümeye anında kontrol verir. Budama ayrı bir iş olarak ele alınmalı.

---

## Task 5: Kickoff boyut logu (M5)

**Files:**
- Modify: `src/agile_sdlc_crew/flow.py` (kickoff_text üretildiği yer, ~1644)

- [ ] **Step 1: kickoff_text boyutunu logla**

`step0_kickoff_meeting`'de `self.state.kickoff_text = kickoff_text` (veya kickoff_text'in set edildiği) satırının hemen ardına ekle:

```python
        _log(f"  📏 kickoff: {len(kickoff_text or '')} char")
```
(`_log` bu dosyada zaten kullanılıyor; kickoff_text değişken adı yerel bağlamda doğrulanmalı.)

- [ ] **Step 2: Doğrula**

Run: `grep -n "📏 kickoff" src/agile_sdlc_crew/flow.py`
Expected: bir satır.
Run: `.venv/bin/python -c "from agile_sdlc_crew.flow import AgileSDLCFlow; print('ok')" 2>&1 | grep -vE "UserWarning|warnings.warn"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add src/agile_sdlc_crew/flow.py
git commit -m "feat: kickoff cikti boyutu loglanir (rot gorunurlugu)"
```

---

## Task 6: Uçtan uca doğrulama

**Files:** (yok — runtime)

- [ ] **Step 1: Tüm modüller import**

Run:
```bash
.venv/bin/python -c "from agile_sdlc_crew.server import app; from agile_sdlc_crew import context_budget; print('all import ok')" 2>&1 | grep -vE "UserWarning|warnings.warn|pooling|validated_self|validate_python" | tail -2
```
Expected: `all import ok`

- [ ] **Step 2: Server'ı yeniden başlat + bir WI çalıştır**

```bash
cd /Users/volkan.ozyildirim/devel/crewai/agile_sdlc_crew && ./start.sh && sleep 5 && curl -s http://localhost:8765/api/health
```
Bir WI kuyruğa at (kullanıcıdan WI ID), sonra:
```bash
grep -nE "📏 context|📏 kickoff|⚠️ context" /tmp/crew_pipeline.log | tail -20
```
Expected: Her adımda `📏 context[<step>]: N char` satırları görünür; boyutlar makul (çoğu <24K); rot varsa `⚠️` ile işaretlenir.

- [ ] **Step 3: Regresyon — çıktı bozulmadı**

Expected: Pipeline normal tamamlanır (previous_context okunmadığı için çıktı değişmez); `/tmp/crew_server.log`'da yeni hata/traceback yok.

---

## Genel Notlar
- Tüm cap'ler `CREW_CTX_<NAME>` env ile override; default'lar mevcut davranışla aynı (regresyonsuz).
- Hard-truncate (`CREW_CTX_HARD_TRUNCATE=1`) default kapalı — önce gözlemle, gerekirse zorla.
- M2 kod sadeleştirir (ölü alan + gereksiz lock gider); davranış değişmez.
