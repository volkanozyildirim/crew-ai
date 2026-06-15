# Geçmiş-İş Repo Önerisi — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Başarılı geçmiş işleri (WI içeriği + gerçek değişen dosya yolları/route'lar → yapıldığı repo) ayrı bir vector indekse alıp, yeni işte repo kararına advisory sinyal olarak kickoff/discover/technical-design'a beslemek.

**Architecture:** Yeni `/repo-decisions` LanceDB scope'u; iş başına tek kompozit kayıt (vector→içerik, BM25→path/route lexical). Yazma yalnızca `step11_completion_report`'ta (başarılı PR). Okuma 3 karar noktasında advisory; architect son kararı verir. Tümü env toggle arkasında, default kapalı.

**Tech Stack:** Python, CrewAI Flow, LanceDB (mevcut `VectorStore`), Ollama embed, MySQL (pymysql), pymysql DictCursor.

**Spec:** `docs/superpowers/specs/2026-06-15-repo-history-suggestion-design.md`

**Test notu:** Projede pytest/test suite YOK (CLAUDE.md). Doğrulama = import smoke + `.venv/bin/python -c` inline round-trip. Vector round-trip'leri Ollama gerektirir (proje zaten Ollama kullanır). Tüm komutlar venv aktifken veya `.venv/bin/python` ile çalıştırılır.

**Konvansiyonlar (mevcut kod):**
- Repo seçim cascade'leri `flow.py`'de inline (`_select_repo_by_name` → grep → vector).
- Vector kayıt deseni: `self._save_record(content, scope, categories, metadata, importance)`.
- Tüm öneri/indeks çağrıları `try/except` ile sarılır — pipeline'ı asla bozmaz.
- Pipeline knob'ları `pipeline_config.SCHEMA` listesine eklenir, `pipeline_config.get("KEY")` ile okunur.

---

### Task 1: Pipeline config knob'ları

**Files:**
- Modify: `src/agile_sdlc_crew/pipeline_config.py` (SCHEMA listesi, "Pipeline davranis toggle'lari" bölümü, `CREW_KNOWLEDGE_RAG` entry'sinden sonra)

- [ ] **Step 1: İki knob ekle**

`pipeline_config.py` içinde `CREW_KNOWLEDGE_RAG` entry'sinin (`"desc"` satırı `}` ile biten) hemen ardına, aynı bölümde:

```python
    {
        "key": "CREW_REPO_HISTORY_SUGGEST",
        "label": "Geçmiş-İş Repo Önerisi",
        "type": "bool",
        "default": False,
        "desc": "Başarılı geçmiş işleri (içerik+dosya yolu→repo) vector indekse al; yeni işte repo kararına advisory öneri olarak kickoff/discover/technical-design'a besle. Architect son kararı verir.",
    },
    {
        "key": "CREW_REPO_HISTORY_MIN_SCORE",
        "label": "Repo Önerisi Min. Skor",
        "type": "float",
        "default": 0.1,
        "min": 0.0,
        "desc": "Geçmiş-iş repo önerisinin kabul edileceği minimum benzerlik skoru. Altındaki öneriler yok sayılır.",
    },
```

- [ ] **Step 2: Doğrula**

Run:
```bash
.venv/bin/python -c "from agile_sdlc_crew import pipeline_config as p; print(p.get('CREW_REPO_HISTORY_SUGGEST'), p.get('CREW_REPO_HISTORY_MIN_SCORE'))"
```
Expected: `False 0.1`

- [ ] **Step 3: Commit**

```bash
git add src/agile_sdlc_crew/pipeline_config.py
git commit -m "feat: repo-history-suggestion knob'ları (CREW_REPO_HISTORY_SUGGEST/MIN_SCORE)"
```

---

### Task 2: VectorStore — route çıkarımı + `index_repo_decision`

**Files:**
- Modify: `src/agile_sdlc_crew/tools/vector_store.py` (module-level helper `_content_hash` yakınına `_extract_routes`; `VectorStore` sınıfına `index_repo_decision`, `save_step_output` metodunun hemen üstüne)

- [ ] **Step 1: Module-level `_extract_routes` helper ekle**

`vector_store.py`'de `_content_hash` fonksiyonunun (satır ~99) hemen ardına. `re` zaten import'lu değilse dosyanın başındaki import'lara `import re` ekle (kontrol et; yoksa ekle):

```python
def _extract_routes(text: str) -> list[str]:
    """Metinden route/endpoint ve dosya-adı token'larini cikar (repo-decision indeksi icin).
    flow.py'deki repo-tespit regex desenleriyle tutarli."""
    import re as _re
    routes: set[str] = set()
    for m in _re.finditer(r'/api/[\w/]+', text):
        routes.add(m.group(0))
    for m in _re.finditer(r'\b(\w+\.(?:php|py|ts|tsx|js|jsx|go|cs|java|vue))\b', text):
        routes.add(m.group(1))
    return sorted(routes)
```

- [ ] **Step 2: `index_repo_decision` metodunu ekle**

`save_step_output` metodunun (satır ~606) hemen üstüne, `VectorStore` sınıfı içinde:

```python
    def index_repo_decision(self, work_item_id: str, repo: str, pr_id: str, plan: dict, wi_content: str):
        """Basarili bir isin 'icerik+dosya yollari -> repo' kaydini /repo-decisions
        scope'una yaz. Idempotent: ayni work_item_id zaten varsa atlar."""
        if not repo or not work_item_id:
            return
        scope = "/repo-decisions"
        wi = str(work_item_id)
        # Idempotency: ayni WI zaten indekste mi? (index_repo_summary deseni)
        try:
            info = self.storage.get_scope_info(scope)
            if info and info.record_count > 0:
                for r in self.storage.list_records(scope, limit=10_000):
                    if r.metadata.get("work_item_id") == wi:
                        return
        except Exception:
            pass
        changes = plan.get("changes", []) if isinstance(plan, dict) else []
        file_paths = [c.get("file_path", "") for c in changes if c.get("file_path")]
        routes = _extract_routes(f"{wi_content} " + " ".join(file_paths))
        content = (
            f"WI #{wi}\n{(wi_content or '')[:2000]}\n"
            f"Degisen dosyalar: {', '.join(file_paths)}\n"
            f"Route/endpoint: {', '.join(routes)}"
        )
        try:
            self._save_record(
                content=content[:5000],
                scope=scope,
                categories=["repo-decision"],
                metadata={
                    "work_item_id": wi,
                    "repo": repo,
                    "pr_id": str(pr_id or ""),
                    "file_paths": file_paths[:50],
                    "routes": routes[:50],
                },
                importance=0.8,
            )
        except Exception as e:
            log.warning(f"  Repo-decision indeks hatasi (WI#{wi}): {e}")
```

- [ ] **Step 3: Round-trip doğrula**

Run:
```bash
.venv/bin/python -c "
import tempfile, os
os.environ['CREW_VECTOR_DB'] = tempfile.mkdtemp()
from agile_sdlc_crew.tools.vector_store import VectorStore, _extract_routes
print('routes:', _extract_routes('see /api/v1/meta/get and Meta.php'))
vs = VectorStore()
plan = {'changes':[{'file_path':'/app/Controller/Api/V1/Meta.php'}]}
vs.index_repo_decision('111','webservice','42',plan,'stock api meta endpoint')
vs.index_repo_decision('111','webservice','42',plan,'dup')  # idempotent
info = vs.storage.get_scope_info('/repo-decisions')
print('record_count:', info.record_count if info else 0)
assert info and info.record_count == 1, 'idempotency bozuk'
print('OK')
"
```
Expected: `routes:` listesi `/api/v1/meta/get` + `Meta.php` içerir; `record_count: 1`; `OK`.

- [ ] **Step 4: Commit**

```bash
git add src/agile_sdlc_crew/tools/vector_store.py
git commit -m "feat: VectorStore.index_repo_decision + _extract_routes (/repo-decisions scope)"
```

---

### Task 3: VectorStore — `suggest_repo_from_history`

**Files:**
- Modify: `src/agile_sdlc_crew/tools/vector_store.py` (`index_repo_decision`'ın hemen ardına)

- [ ] **Step 1: Metodu ekle**

```python
    def suggest_repo_from_history(
        self, query: str, path_hints: list[str] | None = None,
        limit: int = 3, exclude_wi: str | None = None,
        known_repos: list[str] | None = None,
    ) -> list[dict]:
        """Gecmis basarili islerden repo onerisi. Sonuclari repo'ya gore gruplar:
        repo_score = max(tekil_skorlar) + 0.05*(n-1), 1.0'da sinirli.
        Donen: [{repo, score, supporting_wis, file_paths_evidence}] (skora gore sirali)."""
        try:
            q = query
            if path_hints:
                q = q + " " + " ".join(path_hints)
            results = self._search(q, "/repo-decisions", limit=max(limit * 5, 15))
        except Exception as e:
            log.warning(f"  suggest_repo_from_history arama hatasi: {e}")
            return []
        by_repo: dict[str, dict] = {}
        ex = str(exclude_wi) if exclude_wi is not None else None
        for record, score in results:
            repo = record.metadata.get("repo", "")
            wi = record.metadata.get("work_item_id", "")
            if not repo:
                continue
            if ex and wi == ex:
                continue
            if known_repos is not None and repo not in known_repos:
                continue
            e = by_repo.setdefault(
                repo, {"scores": [], "supporting_wis": [], "file_paths_evidence": []}
            )
            e["scores"].append(score)
            e["supporting_wis"].append(wi)
            e["file_paths_evidence"].extend(record.metadata.get("file_paths", [])[:3])
        out = []
        for repo, e in by_repo.items():
            n = len(e["scores"])
            repo_score = min(1.0, max(e["scores"]) + 0.05 * (n - 1))
            out.append({
                "repo": repo,
                "score": round(repo_score, 3),
                "supporting_wis": e["supporting_wis"][:5],
                "file_paths_evidence": list(dict.fromkeys(e["file_paths_evidence"]))[:8],
            })
        out.sort(key=lambda x: x["score"], reverse=True)
        return out[:limit]
```

- [ ] **Step 2: Round-trip + filtre doğrula**

Run:
```bash
.venv/bin/python -c "
import tempfile, os
os.environ['CREW_VECTOR_DB'] = tempfile.mkdtemp()
from agile_sdlc_crew.tools.vector_store import VectorStore
vs = VectorStore()
vs.index_repo_decision('111','webservice','42',{'changes':[{'file_path':'/app/Controller/Api/V1/Meta.php'}]},'stock api meta endpoint get')
vs.index_repo_decision('112','webservice','43',{'changes':[{'file_path':'/app/Controller/Api/V1/Stock.php'}]},'stock api list endpoint')
vs.index_repo_decision('113','orkestra','44',{'changes':[{'file_path':'/src/Order.php'}]},'order address scheduled delivery')
# webservice 2 destekli -> skor boost
res = vs.suggest_repo_from_history('stock api meta endpoint', known_repos=['webservice','orkestra'])
print(res)
assert res and res[0]['repo']=='webservice', res
assert len(res[0]['supporting_wis']) >= 1
# exclude_wi: kendi gecmisini onermesin
res2 = vs.suggest_repo_from_history('order address', exclude_wi='113', known_repos=['webservice','orkestra'])
assert all(r['repo']!='orkestra' or '113' not in r['supporting_wis'] for r in res2), res2
# known_repos filtresi: silinmis repo elenir
res3 = vs.suggest_repo_from_history('stock api', known_repos=['orkestra'])
assert all(r['repo']=='orkestra' for r in res3), res3
print('OK')
"
```
Expected: ilk sonuç `webservice`; `OK`.

- [ ] **Step 3: Commit**

```bash
git add src/agile_sdlc_crew/tools/vector_store.py
git commit -m "feat: VectorStore.suggest_repo_from_history (repo-grup skor + filtreler)"
```

---

### Task 4: DB helper + `backfill_repo_decisions`

**Files:**
- Modify: `src/agile_sdlc_crew/db.py` (yeni fonksiyon `list_successful_jobs_for_backfill`, `get_cached_step_output` yakınına)
- Modify: `src/agile_sdlc_crew/tools/vector_store.py` (`suggest_repo_from_history`'nin ardına `backfill_repo_decisions`)

- [ ] **Step 1: db helper ekle**

`db.py` içinde `get_cached_step_output` fonksiyonunun ardına (DB pymysql DictCursor kullanır — diğer fonksiyonlar gibi dict döner):

```python
def list_successful_jobs_for_backfill(limit: int = 1000) -> list[dict]:
    """Repo-decision indeksini geri-doldurmak icin: tamamlanmis, repo_name + pr_id
    dolu isler. En yeniden eskiye."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, work_item_id, repo_name, pr_id FROM jobs "
            "WHERE status='completed' AND repo_name <> '' AND pr_id <> '' "
            "ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        return cur.fetchall()
```

- [ ] **Step 2: `backfill_repo_decisions` metodunu ekle**

`vector_store.py`, `suggest_repo_from_history`'nin ardına:

```python
    def backfill_repo_decisions(self, db, limit: int = 1000) -> int:
        """DB'deki basarili islerden /repo-decisions indeksini geri-doldur.
        Idempotent (index_repo_decision zaten var olani atlar). Doldurulan sayi doner."""
        from agile_sdlc_crew.main import _parse_architect_output
        try:
            jobs = db.list_successful_jobs_for_backfill(limit)
        except Exception as e:
            log.warning(f"  Backfill: is listesi alinamadi: {e}")
            return 0
        done = 0
        for j in jobs:
            wi = str(j.get("work_item_id") or "")
            repo = j.get("repo_name") or ""
            pr_id = j.get("pr_id") or ""
            if not wi or not repo:
                continue
            td = db.get_cached_step_output("technical_design_task", wi)
            if not td:
                continue
            try:
                plan = _parse_architect_output(td)
            except Exception:
                continue
            wi_content = (
                db.get_cached_step_output("requirements_analysis_task", wi)
                or plan.get("summary", "")
            )
            self.index_repo_decision(wi, repo, pr_id, plan, wi_content)
            done += 1
        log.info(f"  📚 Repo-decision backfill: {done} is islendi")
        return done
```

- [ ] **Step 3: Import + syntax doğrula**

Run:
```bash
.venv/bin/python -c "
from agile_sdlc_crew import db
assert hasattr(db, 'list_successful_jobs_for_backfill')
from agile_sdlc_crew.tools.vector_store import VectorStore
assert hasattr(VectorStore, 'backfill_repo_decisions')
print('OK')
"
```
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add src/agile_sdlc_crew/db.py src/agile_sdlc_crew/tools/vector_store.py
git commit -m "feat: backfill_repo_decisions + db.list_successful_jobs_for_backfill"
```

---

### Task 5: flow.py — `step11_completion_report` yazma hook'u

**Files:**
- Modify: `src/agile_sdlc_crew/flow.py` (`step11_completion_report`, `_step_done("completion_report_task", ...)` ve WI yorumu sonrası, `flow.py:3285` civarı — dry-run dalı `_write_dry_run_report` ile zaten erken döndüğü için bu blok yalnızca non-dry-run yolda çalışır)

- [ ] **Step 1: İndeks yazma bloğu ekle**

`self._step_done("completion_report_task", completion_text[:3000])` satırının hemen ardına (WI yorumu `_add_wi_comment`'tan önce veya sonra; sonrası tercih):

```python
        # Geçmiş-iş repo indeksine yaz — yalnızca başarılı PR (buraya ulaşmak
        # PR oluştu + review onayladı demek; dry-run bu metodun başında döner).
        from agile_sdlc_crew import pipeline_config as _pc_rh
        if (
            _pc_rh.get("CREW_REPO_HISTORY_SUGGEST")
            and self._vector_store and self.state.repo_name and self.state.pr_id
        ):
            try:
                self._vector_store.index_repo_decision(
                    self.state.work_item_id, self.state.repo_name, self.state.pr_id,
                    self.state.plan, self.state.requirements_text[:2000],
                )
                _log("  📚 Repo kararı geçmiş indekse yazıldı")
            except Exception as e:
                _log(f"  Repo kararı indeksleme hatası: {e}")
```

- [ ] **Step 2: Import smoke**

Run:
```bash
.venv/bin/python -c "from agile_sdlc_crew.flow import AgileSDLCFlow; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/agile_sdlc_crew/flow.py
git commit -m "feat: step11'de başarılı PR'da repo kararını geçmiş indekse yaz"
```

---

### Task 6: flow.py — technical-design (KN-17) + discover (KN-15) entegrasyonu

**Files:**
- Modify: `src/agile_sdlc_crew/flow.py` (`crew_step4_technical_design`, symbol_evidence bloğunun ardından ~`flow.py:1779` `_run_discover_repos` çağrısından ÖNCE; ayrıca `_run_discover_repos` imzası ~`flow.py:399` ve prompt'u ~`flow.py:468`)

- [ ] **Step 1: technical-design'da öneriyi al + candidate'e ekle + context bloğu**

`crew_step4_technical_design` içinde, symbol_evidence hesaplandıktan (~`flow.py:1777`) sonra ve `self._run_discover_repos(...)` çağrısından (~`flow.py:1779`) ÖNCE:

```python
        # Geçmiş-iş repo önerisi (advisory) — candidate'e zorla dahil + discover'a kanıt
        repo_history_suggestions: list[dict] = []
        from agile_sdlc_crew import pipeline_config as _pc_rh
        if _pc_rh.get("CREW_REPO_HISTORY_SUGGEST") and self._vector_store:
            try:
                _q_hist = f"{self.state.requirements_text[:500]} {self.state.kickoff_text[:300]}"
                repo_history_suggestions = self._vector_store.suggest_repo_from_history(
                    _q_hist, exclude_wi=self.state.work_item_id,
                    known_repos=self.state.known_repos,
                )
                if repo_history_suggestions:
                    _hist_repos = [s["repo"] for s in repo_history_suggestions]
                    candidate_repos = _hist_repos + [r for r in candidate_repos if r not in _hist_repos]
                    _log(f"  Geçmiş-iş repo önerisi: " + ", ".join(
                        f"{s['repo']}({s['score']})" for s in repo_history_suggestions
                    ))
            except Exception as e:
                _log(f"  Geçmiş-iş önerisi hatası: {e}")
```

- [ ] **Step 2: `_run_discover_repos`'a öneriyi geçir**

`_run_discover_repos(candidate_repos[:25], evidence=symbol_evidence)` çağrısını şununla değiştir:

```python
        self._run_discover_repos(
            candidate_repos[:25], evidence=symbol_evidence,
            repo_history=repo_history_suggestions,
        )
```

- [ ] **Step 3: `_run_discover_repos` imzasına param ekle**

`flow.py:399` — imzayı:

```python
    def _run_discover_repos(self, candidate_repos: list[str], evidence: dict | None = None, repo_history: list[dict] | None = None) -> None:
```

- [ ] **Step 4: discover prompt'una geçmiş-iş kanıt bloğu ekle**

`_run_discover_repos` içinde, `evidence_block` prompt'a eklendikten (`prompt_user += evidence_block`, ~`flow.py:468`) sonra:

```python
        if repo_history:
            hist_lines = []
            for s in repo_history:
                hist_lines.append(
                    f"- {s['repo']} (skor {s['score']}, benzer iş: "
                    + ", ".join(f"#{w}" for w in s.get('supporting_wis', [])[:3])
                    + "; örnek dosyalar: "
                    + ", ".join(s.get('file_paths_evidence', [])[:3]) + ")"
                )
            prompt_user += (
                "\n\n# BENZER GEÇMİŞ İŞLER (başarılı PR'lar şu repolarda yapıldı — ADVISORY)\n"
                "Bu sinyal repo ADI benzerliğinden GÜÇLÜ, birebir KOD KANITINDAN zayıftır.\n"
                + "\n".join(hist_lines)
            )
```

- [ ] **Step 5: technical-design context'ine geçmiş-iş bloğu ekle**

`crew_step4_technical_design` içinde `ctx = self._build_step_context("technical_design_task")` (~`flow.py:1844`) satırından SONRA:

```python
        if repo_history_suggestions:
            _hist_txt = "\n".join(
                f"- {s['repo']} (skor {s['score']}; örnek dosyalar: "
                + ", ".join(s.get('file_paths_evidence', [])[:3]) + ")"
                for s in repo_history_suggestions
            )
            ctx += (
                "\n\n# BENZER GEÇMİŞ İŞLER (Repo Kararı — ADVISORY)\n"
                "Aşağıdaki repolarda benzer işler başarıyla tamamlandı. Repo adı "
                "benzerliğinden güçlü sinyaldir; yine de kendi kanıtınla karar ver.\n"
                f"{_hist_txt}\n"
            )
```

- [ ] **Step 6: Prefetch cascade'ine geçmiş-iş katmanı ekle**

`crew_step4_technical_design` prefetch cascade'inde, Katman 0/1 (`_select_repo_by_name`, ~`flow.py:1893-1900`) bloğundan SONRA, Katman 2 (kod grep, ~`flow.py:1905`) bloğundan ÖNCE:

```python
        # Katman 1.5: Geçmiş-iş önerisi (isim eşleşmesi yoksa, grep'ten önce)
        if not prefetch_repo and repo_history_suggestions:
            _min_score = _pc_rh.get("CREW_REPO_HISTORY_MIN_SCORE")
            if repo_history_suggestions[0]["score"] >= _min_score:
                prefetch_repo = repo_history_suggestions[0]["repo"]
                _log(f"  Adim 4 hedef repo (geçmiş-iş): {prefetch_repo} (skor {repo_history_suggestions[0]['score']})")
```

- [ ] **Step 7: Import smoke**

Run:
```bash
.venv/bin/python -c "from agile_sdlc_crew.flow import AgileSDLCFlow; print('OK')"
```
Expected: `OK`

- [ ] **Step 8: Commit**

```bash
git add src/agile_sdlc_crew/flow.py
git commit -m "feat: technical-design + discover'a geçmiş-iş repo önerisi (KN-15/KN-17)"
```

---

### Task 7: flow.py — kickoff (KN-11) entegrasyonu

**Files:**
- Modify: `src/agile_sdlc_crew/flow.py` (`step0_kickoff_meeting` repo cascade'i, `_select_repo_by_name` bloğundan ~`flow.py:1509-1512` sonra, Katman 2 grep'ten ~`flow.py:1515` önce)

- [ ] **Step 1: Kickoff cascade'ine geçmiş-iş katmanı ekle**

`step0_kickoff_meeting` içinde, `_method, _matched = _select_repo_by_name(...)` bloğunun (`if _matched: kickoff_repo = _matched ...`) hemen ardına, `# Katman 2: Kod grep` yorumundan önce:

```python
            # Katman 1.5: Geçmiş-iş önerisi (isim eşleşmesi yoksa, grep'ten önce)
            if not kickoff_repo and _pc_ko.get("CREW_REPO_HISTORY_SUGGEST") and self._vector_store:
                try:
                    _sug = self._vector_store.suggest_repo_from_history(
                        self.state.requirements_text[:600],
                        exclude_wi=self.state.work_item_id,
                        known_repos=self.state.known_repos,
                    )
                    _min = _pc_ko.get("CREW_REPO_HISTORY_MIN_SCORE")
                    if _sug and _sug[0]["score"] >= _min:
                        kickoff_repo = _sug[0]["repo"]
                        _log(f"  Kickoff hedef repo (geçmiş-iş): {kickoff_repo} (skor {_sug[0]['score']})")
                except Exception as e:
                    _log(f"  Kickoff geçmiş-iş önerisi hatası: {e}")
```

Not: `_pc_ko` zaten `step0_kickoff_meeting` başında import edilir (`from agile_sdlc_crew import pipeline_config as _pc_ko`). Ek import gerekmez.

- [ ] **Step 2: Import smoke**

Run:
```bash
.venv/bin/python -c "from agile_sdlc_crew.flow import AgileSDLCFlow; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/agile_sdlc_crew/flow.py
git commit -m "feat: kickoff repo tahminine geçmiş-iş önerisi katmanı (KN-11)"
```

---

### Task 8: flow.py — `initialize`'da backfill tetikleyici

**Files:**
- Modify: `src/agile_sdlc_crew/flow.py` (`initialize`, vector store kurulduktan ve REPO_SUMMARY embed'inden sonra, `self._agile_crew.local_repo_mgr = ...` atamasından ~`flow.py:1018` önce)

- [ ] **Step 1: Boş indekste backfill çağır**

`initialize` içinde, REPO_SUMMARY embed bloğunun (`msg = f"  {indexed}/...` log'undan) sonrasında:

```python
        # Geçmiş-iş repo önerisi açıksa ve indeks boşsa DB'den geri-doldur (bir kez)
        from agile_sdlc_crew import pipeline_config as _pc_bf
        if _pc_bf.get("CREW_REPO_HISTORY_SUGGEST"):
            try:
                _info = self._vector_store.storage.get_scope_info("/repo-decisions")
                _empty = not _info or _info.record_count == 0
            except Exception:
                _empty = True
            if _empty:
                try:
                    n = self._vector_store.backfill_repo_decisions(self._db)
                    _log(f"  Repo-decision indeksi geri-dolduruldu: {n} iş")
                except Exception as e:
                    _log(f"  Repo-decision backfill hatası: {e}")
```

- [ ] **Step 2: Import smoke**

Run:
```bash
.venv/bin/python -c "from agile_sdlc_crew.flow import AgileSDLCFlow; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/agile_sdlc_crew/flow.py
git commit -m "feat: initialize'da boş repo-decision indeksini DB'den geri-doldur"
```

---

### Task 9: Dokümanları güncelle (decision-points + step docs)

**Files:**
- Modify: `docs/pipeline/decision-points.md` (yeni KN-33 + KN-11/KN-15/KN-17 güncelleme + birleşik tablo)
- Modify: `docs/pipeline/steps/02-kickoff-meeting.md`, `03-discover-repos.md`, `05-technical-design.md` (geçmiş-iş katmanı notu)

- [ ] **Step 1: KN-33'ü decision-points.md'ye ekle**

`## KN-32` girişinin ardına, `---` ve "Repo seçim kararlarının birleşik görünümü" başlığından ÖNCE:

```markdown
## KN-33 — Geçmiş-iş repo önerisi (advisory)
- **Nerede:** `02-kickoff` (cascade), `03-discover` (prompt+candidate), `05-technical-design` (cascade+context) · `flow.py` + `VectorStore.suggest_repo_from_history`
- **Karar:** Başarılı geçmiş işlerden bu WI'ya benzer olanlar hangi repo(lar)da yapılmış?
- **Girdi:** `CREW_REPO_HISTORY_SUGGEST` açık + `/repo-decisions` scope'unda hybrid arama; repo'ya göre gruplanmış skor vs `CREW_REPO_HISTORY_MIN_SCORE`.
- **Sonuç:** Önerilen repo candidate listesine zorla dahil + context/prompt'a kanıt bloğu; kickoff/technical-design cascade'inde isim eşleşmesi yoksa seçilebilir. **Architect son kararı verir** (advisory).
- **Neden:** "Bu tür dosyalar/route'lar daha önce şu repoda değişti" sinyali repo adı benzerliğinden güçlü; geçmiş başarılı kararlardan öğrenir. Yazma yalnızca başarılı PR'da (KN-32 öncesi step11). Filtreler: repo ∉ known_repos elenir, kendi WI'sı önerilmez.
```

- [ ] **Step 2: Birleşik tabloya geçmiş-iş sinyalini ekle**

`decision-points.md` sonundaki "Repo seçim kararlarının birleşik görünümü" tablosunun altındaki paragrafa şu cümleyi ekle:

```markdown
**Geçmiş-iş önerisi (KN-33)** üç noktaya da advisory olarak eklenir: önce başarılı
PR'lar `/repo-decisions` indeksine yazılır, sonra benzer WI'larda o repolar aday
olarak öne çıkar.
```

- [ ] **Step 3: Step docs'a not ekle**

`docs/pipeline/steps/02-kickoff-meeting.md` "Karar noktaları" listesine:
```markdown
- **KN-33** — Geçmiş-iş repo önerisi katmanı (cascade'de). Bkz. [decision-points.md#kn-33](../decision-points.md#kn-33)
```
`docs/pipeline/steps/03-discover-repos.md` "Karar noktaları" listesine:
```markdown
- **KN-33** — Geçmiş-iş kanıt bloğu (candidate + prompt). Bkz. [decision-points.md#kn-33](../decision-points.md#kn-33)
```
`docs/pipeline/steps/05-technical-design.md` "Karar noktaları" listesine:
```markdown
- **KN-33** — Geçmiş-iş repo önerisi (cascade + context). Bkz. [decision-points.md#kn-33](../decision-points.md#kn-33)
```

- [ ] **Step 4: Commit**

```bash
git add docs/pipeline
git commit -m "docs: KN-33 geçmiş-iş repo önerisi + step docs güncellemesi"
```

---

### Task 10: Uçtan uca toggle doğrulaması

**Files:** (kod değişikliği yok — davranış doğrulama)

- [ ] **Step 1: Toggle KAPALIYKEN davranış değişmediğini doğrula**

Run:
```bash
.venv/bin/python -c "
from agile_sdlc_crew import pipeline_config as p
assert p.get('CREW_REPO_HISTORY_SUGGEST') is False, 'default kapalı olmalı'
from agile_sdlc_crew.flow import AgileSDLCFlow
print('OK: import + default kapalı')
"
```
Expected: `OK: import + default kapalı`

- [ ] **Step 2: Server import smoke (regresyon yok)**

Run:
```bash
.venv/bin/python -c "from agile_sdlc_crew.server import app; print('server OK')"
```
Expected: `server OK`

- [ ] **Step 3 (opsiyonel, canlı): toggle açık uçtan uca**

`.env`'e `CREW_REPO_HISTORY_SUGGEST=1` ekle, server'ı yeniden başlat, başarılı bir WI çalıştır; log'da `📚 Repo kararı geçmiş indekse yazıldı` ve sonraki benzer WI'da `Geçmiş-iş repo önerisi: ...` satırlarını izle. Backfill log'u: `Repo-decision indeksi geri-dolduruldu: N iş`.

---

## Self-Review (yazım sonrası kontrol)

**Spec coverage:**
- Veri modeli `/repo-decisions` → Task 2 ✅
- Yazma yolu (başarılı PR, step11) → Task 5 ✅
- Okuma yolu (grup + filtreler) → Task 3 ✅
- KN-11 / KN-15 / KN-17 entegrasyon → Task 7 / Task 6 ✅
- Env toggle (2 knob, default kapalı) → Task 1 ✅
- Backfill → Task 4 (mekanizma) + Task 8 (tetikleyici) ✅
- Hata yönetimi (try/except, pipeline'ı bozmaz) → her entegrasyon adımında ✅
- Edge cases (known_repos filtre, exclude_wi, dry-run yazmaz, idempotency) → Task 2/3 + Task 5 (dry-run erken döner) ✅
- Docs (KN-33) → Task 9 ✅

**Type/imza tutarlılığı:**
- `index_repo_decision(work_item_id, repo, pr_id, plan, wi_content)` — Task 2 tanım, Task 4/5 kullanım: tutarlı ✅
- `suggest_repo_from_history(query, path_hints, limit, exclude_wi, known_repos)` — Task 3 tanım, Task 6/7 kullanım (`exclude_wi=`, `known_repos=`): tutarlı ✅
- `backfill_repo_decisions(db, limit)` — Task 4 tanım, Task 8 kullanım (`backfill_repo_decisions(self._db)`): tutarlı ✅
- `_run_discover_repos(..., repo_history=...)` — Task 6 imza + çağrı: tutarlı ✅

**Placeholder taraması:** Tüm kod blokları tam; TBD/TODO yok ✅
