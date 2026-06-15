# Azure DevOps Backfill — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dashboard board'unda bir butonla tetiklenen, async + concurrent bir taramayla seçili takımın tüm done + merge'li işlerini (WI içeriği + PR'da değişen dosyalar → repo) `/repo-decisions` indeksine yazmak; progress'i modal'dan izlemek.

**Architecture:** Pipeline kuyruğundan bağımsız dedicated daemon thread (`/api/pr-fix` deseni). Concurrent Azure I/O fetch (ThreadPoolExecutor) + sequential LanceDB index (yarış/duplicate yok). Progress `/tmp/crew_backfill.json`'a yazılır, `/api/backfill/status` ile pollanır. Tek-çalışma guard. `VectorStore.index_repo_decision` (mevcut, idempotent) yeniden kullanılır.

**Tech Stack:** Python, FastAPI, threading + concurrent.futures, LanceDB (VectorStore), Azure DevOps REST (mevcut AzureDevOpsClient), vanilla JS dashboard.

**Spec:** `docs/superpowers/specs/2026-06-15-azure-devops-backfill-design.md`

**Test notu:** Projede pytest YOK (CLAUDE.md). Doğrulama = import smoke + `.venv/bin/python -c` izole birim kontrolleri (pure helper'lar) + sahte-client ile runner round-trip. Canlı Azure çağrıları sadece son manuel adımda.

**Doğrulanmış kod gerçekleri (bu plan bunlara dayanır):**
- `AzureDevOpsClient.query_work_items(wiql)` (azure_devops_base.py:592) WIQL'i çalıştırıp ID'leri `/wit/workitems?ids=...&$expand=all` ile hydrate eder → her item `id`, `fields`, `relations` içerir.
- PR relation: `rel["attributes"]["name"] == "Pull Request"`, url'de `vstfs:///Git/PullRequestId/{proj}%2f{repo}%2f{prid}` → regex `PullRequestId/[^%]+%2f([^%]+)%2f(\d+)` (repo_id, pr_id).
- `get_pull_request(repo, pr_id)` (azure_devops_base.py:460) tam PR json döner: `status` ("completed"/"active"/"abandoned"), `repository.name`.
- `get_pull_request_changes(repo, pr_id)` (azure_devops_base.py:519) `changeEntries` listesi döner: her `{"changeType","item":{"path","isFolder"?,"gitObjectType"}}`.
- `VectorStore.index_repo_decision(work_item_id, repo, pr_id, plan, wi_content)` idempotent (work_item_id'e göre); plan `{"changes":[{"file_path":...}], "repo_name":...}` bekler.
- `pipeline_config.get("KEY")` knob okur; SCHEMA listesi var.

---

### Task 1: Pipeline config knob'ları

**Files:** Modify `src/agile_sdlc_crew/pipeline_config.py` (SCHEMA, "Repo deps (vendor/)" bölümünün hemen üstüne veya toggles bölümünün sonuna — `CREW_REPO_HISTORY_MIN_SCORE` entry'sinden sonra uygun bir yere)

- [ ] **Step 1: Üç knob ekle** — `pipeline_config.py` SCHEMA listesine, `CREW_REPO_HISTORY_MIN_SCORE` entry'sinin ardından:

```python
    {
        "key": "CREW_AZ_BACKFILL_WORKERS",
        "label": "Azure Backfill Worker Sayısı",
        "type": "int",
        "default": 8,
        "min": 1,
        "desc": "Azure DevOps backfill sırasında PR/WI çekmek için eşzamanlı thread sayısı.",
    },
    {
        "key": "CREW_AZ_BACKFILL_LIMIT",
        "label": "Azure Backfill Maks. WI",
        "type": "int",
        "default": 0,
        "min": 0,
        "desc": "Taranacak maksimum done work item sayısı (güncelden geriye). 0 = limitsiz (tümü).",
    },
    {
        "key": "CREW_AZ_DONE_STATES",
        "label": "Azure 'Done' Durumları",
        "type": "str",
        "default": "Done,Closed,Resolved",
        "desc": "Backfill'de 'tamamlanmış' sayılan WI durumları (virgülle ayrılmış).",
    },
```

NOTE: SCHEMA `"str"` tipini destekliyor mu kontrol et — `_coerce` fonksiyonunda "bool"/"int"/"float" var; "str" için son `return value` dalına düşer (coerce no-op), bu yüzden `"str"` tipi güvenli. Eğer `all_values`/UI `"str"` ile sorun çıkarırsa yine de `get()` çalışır (default string döner).

- [ ] **Step 2: Doğrula**
```
cd /Users/volkan.ozyildirim/devel/crewai/agile_sdlc_crew && .venv/bin/python -c "from agile_sdlc_crew import pipeline_config as p; print(p.get('CREW_AZ_BACKFILL_WORKERS'), p.get('CREW_AZ_BACKFILL_LIMIT'), repr(p.get('CREW_AZ_DONE_STATES')))"
```
Expected: `8 0 'Done,Closed,Resolved'`

- [ ] **Step 3: Commit**
```
git add src/agile_sdlc_crew/pipeline_config.py
git commit -m "feat: azure backfill knob'ları (workers/limit/done-states)"
```

---

### Task 2: AzureDevOpsClient — done WI sorgusu + team area path

**Files:** Modify `src/agile_sdlc_crew/tools/azure_devops_base.py`

- [ ] **Step 1: `query_work_items`'a `limit` param ekle** — mevcut imza `def query_work_items(self, wiql: str) -> list[dict]:` (satır ~592). `limit` ekle ve ID'leri hydrate'ten ÖNCE dilimle:

Değiştir:
```python
    def query_work_items(self, wiql: str) -> list[dict]:
```
→
```python
    def query_work_items(self, wiql: str, limit: int = 0) -> list[dict]:
```
Ve `ids = [item["id"] for item in result.get("workItems", [])]` satırının HEMEN ardına ekle:
```python
        if limit and limit > 0:
            ids = ids[:limit]
```

- [ ] **Step 2: `get_team_area_path` ekle** — `query_work_items`'ın ardına (dosya sonu civarı):
```python
    def get_team_area_path(self, team: str = "") -> str:
        """Takimin varsayilan area path'ini dondurur (teamfieldvalues.defaultValue).
        Bulunamazsa bos string."""
        team = (team or "").strip() or self.team
        if not team:
            return ""
        url = (
            f"{self.org_url}/{self.project}/"
            f"{requests.utils.quote(team, safe='')}/_apis/work/teamsettings/teamfieldvalues"
        )
        params = {"api-version": self.API_VERSION}
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        return (resp.json().get("defaultValue") or "").strip()
```

- [ ] **Step 3: `query_done_work_items` ekle** — `get_team_area_path`'ın ardına:
```python
    def query_done_work_items(
        self, area_path: str = "", states: list[str] | None = None, limit: int = 0,
    ) -> list[dict]:
        """Tamamlanmis (done) work item'lari ChangedDate DESC (guncelden eskiye) dondurur.
        area_path verilirse o alanin altina filtreler. Donen item'lar fields + relations icerir."""
        states = states or ["Done", "Closed", "Resolved"]
        states_sql = ", ".join("'" + s.replace("'", "''") + "'" for s in states)
        wiql = (
            "SELECT [System.Id] FROM WorkItems "
            "WHERE [System.TeamProject] = @project "
            f"AND [System.State] IN ({states_sql}) "
        )
        if area_path:
            safe_area = area_path.replace("'", "''")
            wiql += f"AND [System.AreaPath] UNDER '{safe_area}' "
        wiql += "ORDER BY [System.ChangedDate] DESC"
        return self.query_work_items(wiql, limit=limit)
```

- [ ] **Step 4: Doğrula (import + WIQL string üretimi, canlı çağrı YOK)**
```
cd /Users/volkan.ozyildirim/devel/crewai/agile_sdlc_crew && .venv/bin/python -c "
import inspect
from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient
assert 'limit' in inspect.signature(AzureDevOpsClient.query_work_items).parameters
assert hasattr(AzureDevOpsClient, 'get_team_area_path')
assert hasattr(AzureDevOpsClient, 'query_done_work_items')
print('OK')
"
```
Expected: `OK`

- [ ] **Step 5: Commit**
```
git add src/agile_sdlc_crew/tools/azure_devops_base.py
git commit -m "feat: AzureDevOpsClient.query_done_work_items + get_team_area_path + query_work_items limit"
```

---

### Task 3: VectorStore — toplu dedup (O(N²) önleme)

**Files:** Modify `src/agile_sdlc_crew/tools/vector_store.py`

`index_repo_decision` her çağrıda `/repo-decisions`'ı tarıyor (idempotency). Backfill yüzlerce WI için bunu çağırınca O(N²) olur. Runner indekste zaten olan WI'ları tek seferde öğrenip atlasın; ayrıca `index_repo_decision`'a iç taramayı atlatma seçeneği ekleyelim.

- [ ] **Step 1: `existing_repo_decision_wis` ekle** — `index_repo_decision`'ın hemen üstüne (veya altına), `VectorStore` içinde:
```python
    def existing_repo_decision_wis(self) -> set[str]:
        """/repo-decisions indeksinde halihazirda bulunan work_item_id kumesi (tek sorgu)."""
        out: set[str] = set()
        try:
            info = self.storage.get_scope_info("/repo-decisions")
            if info and info.record_count > 0:
                for r in self.storage.list_records("/repo-decisions", limit=100_000):
                    wi = r.metadata.get("work_item_id")
                    if wi:
                        out.add(str(wi))
        except Exception as e:
            log.debug(f"  existing_repo_decision_wis atlandi: {e}")
        return out
```

- [ ] **Step 2: `index_repo_decision`'a `skip_dedup_check` ekle** — mevcut imza:
```python
    def index_repo_decision(self, work_item_id: str, repo: str, pr_id: str, plan: dict, wi_content: str):
```
→
```python
    def index_repo_decision(self, work_item_id: str, repo: str, pr_id: str, plan: dict, wi_content: str, skip_dedup_check: bool = False):
```
Ve idempotency bloğunu (`# Idempotency: ...` ile başlayıp `get_scope_info`/`list_records` döngüsünü içeren try/except) bir koşula al:
```python
        if not skip_dedup_check:
            # Idempotency: ayni WI zaten indekste mi? (index_repo_summary deseni)
            try:
                info = self.storage.get_scope_info(scope)
                if info and info.record_count > 0:
                    for r in self.storage.list_records(scope, limit=10_000):
                        if r.metadata.get("work_item_id") == wi:
                            return
            except Exception as e:
                log.debug(f"  Repo-decision dedup kontrolu atlandi: {e}")
```
(yani mevcut blok aynen `if not skip_dedup_check:` altına girer; `scope`/`wi` değişkenleri bu bloktan önce zaten tanımlı olmalı — sıralamayı koru: `scope` ve `wi` tanımları if'ten önce kalsın).

- [ ] **Step 3: Doğrula**
```
cd /Users/volkan.ozyildirim/devel/crewai/agile_sdlc_crew && .venv/bin/python -c "
import inspect, tempfile, os
os.environ['CREW_VECTOR_DB'] = tempfile.mkdtemp()
from agile_sdlc_crew.tools.vector_store import VectorStore
assert 'skip_dedup_check' in inspect.signature(VectorStore.index_repo_decision).parameters
vs = VectorStore()
assert vs.existing_repo_decision_wis() == set()
vs.index_repo_decision('5','r','1',{'changes':[{'file_path':'/a.php'}]},'x', skip_dedup_check=True)
assert vs.existing_repo_decision_wis() == {'5'}
print('OK')
"
```
Expected: `OK`

- [ ] **Step 4: Commit**
```
git add src/agile_sdlc_crew/tools/vector_store.py
git commit -m "feat: existing_repo_decision_wis + index_repo_decision skip_dedup_check (O(N) backfill)"
```

---

### Task 4: azure_backfill.py — runner

**Files:** Create `src/agile_sdlc_crew/azure_backfill.py`

- [ ] **Step 1: Modülü oluştur** — tam içerik:
```python
"""Azure DevOps gecmis-is backfill.

Secili takimin done + merge'li (completed PR'li) islerini guncelden geriye tarar;
her isin (WI icerigi + PR'da degisen dosyalar -> repo) kaydini /repo-decisions
indeksine yazar. Dedicated daemon thread (pipeline kuyrugundan bagimsiz),
concurrent fetch + sequential index. Progress /tmp/crew_backfill.json'a yazilir.
"""

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

log = logging.getLogger("pipeline")

PROGRESS_FILE = Path("/tmp/crew_backfill.json")
_PR_LINK_RE = re.compile(r"PullRequestId/[^%]+%2f([^%]+)%2f(\d+)")

_run_lock = threading.Lock()
_active_runner = None  # AzureBackfillRunner | None


# ── Pure helper'lar (Azure'suz test edilebilir) ──

def _parse_pr_links(relations: list) -> list[tuple[str, int]]:
    """WI relations'tan (repo_id, pr_id) ciftleri."""
    out: list[tuple[str, int]] = []
    for rel in relations or []:
        if (rel.get("attributes", {}) or {}).get("name") != "Pull Request":
            continue
        m = _PR_LINK_RE.search(rel.get("url", "") or "")
        if m:
            out.append((m.group(1), int(m.group(2))))
    return out


def _extract_changed_paths(change_entries: list) -> list[str]:
    """PR changeEntries'ten dosya yollari (klasor + silme haric, sirali, tekil)."""
    paths: list[str] = []
    for ch in change_entries or []:
        if (ch.get("changeType") or "").lower() == "delete":
            continue
        item = ch.get("item", {}) or {}
        if item.get("isFolder"):
            continue
        p = item.get("path") or ""
        if p and p not in paths:
            paths.append(p)
    return paths


def _wi_content(fields: dict) -> str:
    """WI fields'tan icerik metni (title + desc + AC, HTML stripli)."""
    title = fields.get("System.Title", "") or ""
    desc = re.sub(r"<[^>]+>", " ", fields.get("System.Description", "") or "")
    ac = re.sub(r"<[^>]+>", " ", fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", "") or "")
    return re.sub(r"\s+", " ", f"{title}\n{desc}\n{ac}").strip()


def _build_plan(repo: str, paths: list[str]) -> dict:
    return {"repo_name": repo, "changes": [{"file_path": p} for p in paths]}


# ── Public API ──

def read_progress() -> dict:
    try:
        if PROGRESS_FILE.exists():
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"running": False}


def is_running() -> bool:
    with _run_lock:
        return _active_runner is not None and _active_runner._running


def request_cancel() -> bool:
    with _run_lock:
        if _active_runner is not None and _active_runner._running:
            _active_runner.cancel()
            return True
    return False


def start_backfill(team: str, vector_store, client, config) -> bool:
    """Backfill'i daemon thread'de baslat. Zaten calisiyorsa False."""
    global _active_runner
    with _run_lock:
        if _active_runner is not None and _active_runner._running:
            return False
        runner = AzureBackfillRunner(team, vector_store, client, config)
        _active_runner = runner
    threading.Thread(target=runner.run, daemon=True).start()
    return True


class AzureBackfillRunner:
    def __init__(self, team, vector_store, client, config):
        self.team = team
        self.vs = vector_store
        self.client = client
        self.workers = max(1, int(config.get("CREW_AZ_BACKFILL_WORKERS")))
        self.limit = int(config.get("CREW_AZ_BACKFILL_LIMIT"))
        self.states = [s.strip() for s in str(config.get("CREW_AZ_DONE_STATES")).split(",") if s.strip()]
        self._running = False
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._p = {
            "running": True, "cancelled": False, "team": team,
            "started_at": datetime.now().isoformat(timespec="seconds"), "finished_at": "",
            "total": 0, "scanned": 0, "with_pr": 0, "indexed": 0, "skipped": 0, "errors": 0,
            "current_wi": None, "log": [],
        }

    def cancel(self):
        self._cancel.set()

    def _write(self):
        try:
            PROGRESS_FILE.write_text(
                json.dumps(self._p, ensure_ascii=False, indent=2), encoding="utf-8",
            )
        except Exception:
            pass

    def _log(self, msg: str):
        with self._lock:
            self._p["log"].append({"time": datetime.now().strftime("%H:%M:%S"), "message": msg})
            self._p["log"] = self._p["log"][-100:]
            self._write()

    def _fetch(self, item: dict):
        """Worker thread'de calisir (sadece okuma I/O). Donen: tuple veya None."""
        if self._cancel.is_set():
            return None
        wi_id = item.get("id")
        fields = item.get("fields", {}) or {}
        pr_links = _parse_pr_links(item.get("relations", []))
        if not pr_links:
            return None
        pr_links.sort(key=lambda x: x[1], reverse=True)  # en yeni PR once
        for repo_id, pr_id in pr_links:
            try:
                pr = self.client.get_pull_request(repo_id, pr_id)
            except Exception:
                continue
            if (pr.get("status") or "").lower() != "completed":
                continue
            repo = (pr.get("repository", {}) or {}).get("name") or repo_id
            try:
                changes = self.client.get_pull_request_changes(repo_id, pr_id)
            except Exception:
                changes = []
            paths = _extract_changed_paths(changes)
            if not paths:
                continue
            return (wi_id, repo, pr_id, paths, _wi_content(fields))
        return None

    def run(self):
        self._running = True
        try:
            area = ""
            try:
                area = self.client.get_team_area_path(self.team)
            except Exception as e:
                self._log(f"Area path cozulemedi ({e}); proje geneli taranacak")
            self._log(f"Sorgu: states={self.states}, area={area or '(proje geneli)'}, limit={self.limit or 'tumu'}")
            try:
                items = self.client.query_done_work_items(area, self.states, self.limit)
            except Exception as e:
                self._log(f"WI sorgu hatasi: {e}")
                items = []
            with self._lock:
                self._p["total"] = len(items)
                self._write()
            self._log(f"{len(items)} done WI bulundu; taraniyor (guncelden eskiye)")

            seen = self.vs.existing_repo_decision_wis()
            if seen:
                self._log(f"{len(seen)} WI zaten indekste, atlanacak")

            with ThreadPoolExecutor(max_workers=self.workers) as ex:
                futures = {ex.submit(self._fetch, it): it for it in items}
                for fut in as_completed(futures):
                    if self._cancel.is_set():
                        break
                    with self._lock:
                        self._p["scanned"] += 1
                    try:
                        res = fut.result()
                    except Exception:
                        with self._lock:
                            self._p["errors"] += 1; self._write()
                        continue
                    if not res:
                        with self._lock:
                            self._p["skipped"] += 1; self._write()
                        continue
                    wi_id, repo, pr_id, paths, content = res
                    if str(wi_id) in seen:
                        with self._lock:
                            self._p["skipped"] += 1; self._write()
                        continue
                    with self._lock:
                        self._p["with_pr"] += 1; self._p["current_wi"] = wi_id
                    try:
                        self.vs.index_repo_decision(
                            str(wi_id), repo, str(pr_id),
                            _build_plan(repo, paths), content, skip_dedup_check=True,
                        )
                        seen.add(str(wi_id))
                        with self._lock:
                            self._p["indexed"] += 1; self._write()
                    except Exception as e:
                        with self._lock:
                            self._p["errors"] += 1; self._write()
                        log.debug(f"  Backfill index hatasi WI#{wi_id}: {e}")

            if self._cancel.is_set():
                with self._lock:
                    self._p["cancelled"] = True
                self._log("Iptal edildi")
            self._log(
                f"Bitti: {self._p['indexed']} indekslendi, "
                f"{self._p['skipped']} atlandi, {self._p['errors']} hata"
            )
        finally:
            with self._lock:
                self._p["running"] = False
                self._p["finished_at"] = datetime.now().isoformat(timespec="seconds")
                self._write()
            self._running = False
```

- [ ] **Step 2: Pure helper birim doğrulaması (Azure'suz)**
```
cd /Users/volkan.ozyildirim/devel/crewai/agile_sdlc_crew && .venv/bin/python -c "
from agile_sdlc_crew.azure_backfill import _parse_pr_links, _extract_changed_paths, _wi_content, _build_plan
rels = [{'attributes':{'name':'Pull Request'},'url':'vstfs:///Git/PullRequestId/proj%2frepoX%2f4321'},{'attributes':{'name':'Child'},'url':'x'}]
assert _parse_pr_links(rels) == [('repoX', 4321)], _parse_pr_links(rels)
ce = [{'changeType':'edit','item':{'path':'/a.php'}},{'changeType':'delete','item':{'path':'/b.php'}},{'changeType':'add','item':{'path':'/c','isFolder':True}},{'changeType':'add','item':{'path':'/d.php'}}]
assert _extract_changed_paths(ce) == ['/a.php','/d.php'], _extract_changed_paths(ce)
assert 'Baslik' in _wi_content({'System.Title':'Baslik','System.Description':'<p>aciklama</p>'})
assert _build_plan('repoX',['/a.php']) == {'repo_name':'repoX','changes':[{'file_path':'/a.php'}]}
print('OK')
"
```
Expected: `OK`

- [ ] **Step 3: Sahte-client ile runner round-trip (Azure'suz, gerçek VectorStore)**
```
cd /Users/volkan.ozyildirim/devel/crewai/agile_sdlc_crew && .venv/bin/python -c "
import tempfile, os, time
os.environ['CREW_VECTOR_DB'] = tempfile.mkdtemp()
from agile_sdlc_crew.tools.vector_store import VectorStore
from agile_sdlc_crew import azure_backfill as ab, pipeline_config

class FakeClient:
    def get_team_area_path(self, team): return 'Proj\\\\TeamA'
    def query_done_work_items(self, area, states, limit):
        return [{'id':101,'fields':{'System.Title':'stock api','System.Description':'<p>meta</p>'},
                 'relations':[{'attributes':{'name':'Pull Request'},'url':'x/PullRequestId/p%2frepoA%2f9'}]},
                {'id':102,'fields':{'System.Title':'no pr'},'relations':[]}]
    def get_pull_request(self, repo, pr):
        return {'status':'completed','repository':{'name':'repoA'}}
    def get_pull_request_changes(self, repo, pr):
        return [{'changeType':'edit','item':{'path':'/app/Meta.php'}}]

vs = VectorStore()
ok = ab.start_backfill('TeamA', vs, FakeClient(), pipeline_config)
assert ok
for _ in range(50):
    if not ab.is_running(): break
    time.sleep(0.2)
p = ab.read_progress()
print('progress:', {k:p[k] for k in ('total','scanned','with_pr','indexed','skipped','errors','running')})
assert p['running'] is False and p['indexed'] == 1 and p['skipped'] == 1, p
assert vs.existing_repo_decision_wis() == {'101'}
print('OK')
"
```
Expected: `progress: {...}` ile `indexed=1, skipped=1, running=False`; `OK`.

- [ ] **Step 4: Commit**
```
git add src/agile_sdlc_crew/azure_backfill.py
git commit -m "feat: AzureBackfillRunner (concurrent fetch + sequential index + progress)"
```

---

### Task 5: server.py — 3 endpoint

**Files:** Modify `src/agile_sdlc_crew/server.py`

- [ ] **Step 1: Request modeli + endpointler** — diğer endpointlerin (örn. `/api/teams`, server.py:1480 civarı) yanına ekle. `BaseModel` zaten import'lu (RunRequest var).

```python
class BackfillRequest(BaseModel):
    team: str = ""


@app.post("/api/backfill/start")
async def backfill_start(req: BackfillRequest):
    """Azure DevOps gecmis-is backfill'i baslat (async daemon thread)."""
    from agile_sdlc_crew import azure_backfill, pipeline_config
    from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient
    from agile_sdlc_crew.tools.vector_store import VectorStore
    if azure_backfill.is_running():
        return JSONResponse({"error": "Backfill zaten calisiyor"}, status_code=409)
    try:
        started = azure_backfill.start_backfill(
            (req.team or "").strip(), VectorStore(), AzureDevOpsClient(), pipeline_config,
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    if not started:
        return JSONResponse({"error": "Backfill zaten calisiyor"}, status_code=409)
    return JSONResponse({"status": "started", "team": (req.team or "").strip()}, status_code=202)


@app.get("/api/backfill/status")
async def backfill_status():
    """Aktif/son backfill progress'i."""
    from agile_sdlc_crew import azure_backfill
    return JSONResponse(azure_backfill.read_progress())


@app.post("/api/backfill/cancel")
async def backfill_cancel():
    """Calisan backfill'i iptal et."""
    from agile_sdlc_crew import azure_backfill
    return JSONResponse({"cancelled": azure_backfill.request_cancel()}, status_code=202)
```

- [ ] **Step 2: Doğrula (import + route kaydı)**
```
cd /Users/volkan.ozyildirim/devel/crewai/agile_sdlc_crew && .venv/bin/python -c "
from agile_sdlc_crew.server import app
paths = {r.path for r in app.routes}
assert {'/api/backfill/start','/api/backfill/status','/api/backfill/cancel'} <= paths, sorted(p for p in paths if 'backfill' in p)
print('OK')
"
```
Expected: `OK`

- [ ] **Step 3: Commit**
```
git add src/agile_sdlc_crew/server.py
git commit -m "feat: /api/backfill start/status/cancel endpointleri"
```

---

### Task 6: Dashboard — buton + progress modal

**Files:** Modify `src/agile_sdlc_crew/web/index.html`

Önce dosyayı oku: board kontrollerinin olduğu yeri (team/sprint select'ler, `id="teamSel"`, ~satır 377-385), mevcut bir modal örneğini (kickoff modal, ~545) ve polling/`setInterval` bölümünü (~815) bul.

- [ ] **Step 1: Butonu ekle** — board kontrol satırına (team/sprint select'lerin yanına, `loadBoard()` çağıran sprint select civarı). Mevcut buton stiline uy (header'daki butonlar gibi `class`'lar). Ekle:
```html
<button onclick="openBackfill()" title="Seçili takımın geçmiş done+PR'lı işlerini indeksle">📚 Geçmiş İşleri Tara</button>
```

- [ ] **Step 2: Progress modal HTML'i ekle** — mevcut modal'ların yanına (örn. kickoff modal'ın hemen ardına), gövde sonuna yakın:
```html
<div id="backfillModal" class="modal" style="display:none">
  <div class="modal-content" style="max-width:640px">
    <div class="modal-header">
      <h3>📚 Azure DevOps Geçmiş İş Taraması</h3>
      <button onclick="closeBackfill()" class="modal-close">×</button>
    </div>
    <div id="bfBody" style="padding:16px">
      <div id="bfTeamRow" style="margin-bottom:12px">Takım: <b id="bfTeam">-</b></div>
      <div style="background:#222;border-radius:6px;height:18px;overflow:hidden;margin-bottom:8px">
        <div id="bfBar" style="height:100%;width:0%;background:#6c5ce7;transition:width .3s"></div>
      </div>
      <div id="bfStats" style="font-size:13px;color:#aaa;margin-bottom:8px"></div>
      <div id="bfLog" style="font-family:monospace;font-size:12px;background:#111;border-radius:6px;
           padding:8px;height:200px;overflow:auto;white-space:pre-wrap"></div>
      <div style="margin-top:12px;display:flex;gap:8px">
        <button id="bfStartBtn" onclick="startBackfill()">Başlat</button>
        <button id="bfCancelBtn" onclick="cancelBackfill()" style="display:none">İptal</button>
      </div>
    </div>
  </div>
</div>
```
NOTE: `.modal`/`.modal-content`/`.modal-header`/`.modal-close` sınıfları mevcut kickoff modal'da kullanılıyorsa onları kullan; yoksa inline style yeterli (yukarıdaki gibi). Dosyadaki mevcut modal markup'ına bak ve sınıf adlarını ona göre eşle.

- [ ] **Step 3: JS fonksiyonlarını ekle** — `<script>` bloğunun içine (polling bölümünün yakını):
```javascript
let _bfPoll=null;
function openBackfill(){
  const team=(document.getElementById('teamSel')||{}).value||'';
  if(!team){alert('Önce bir takım seçin');return;}
  document.getElementById('bfTeam').textContent=team;
  document.getElementById('backfillModal').style.display='flex';
  document.getElementById('bfStartBtn').style.display='';
  document.getElementById('bfCancelBtn').style.display='none';
  fetchBackfill();
}
function closeBackfill(){
  document.getElementById('backfillModal').style.display='none';
  if(_bfPoll){clearInterval(_bfPoll);_bfPoll=null;}
}
async function startBackfill(){
  const team=document.getElementById('bfTeam').textContent;
  document.getElementById('bfStartBtn').style.display='none';
  document.getElementById('bfCancelBtn').style.display='';
  try{
    const r=await fetch('/api/backfill/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({team})});
    if(r.status===409){alert('Backfill zaten çalışıyor');}
  }catch(e){alert('Başlatma hatası: '+e);}
  if(!_bfPoll)_bfPoll=setInterval(fetchBackfill,2000);
  fetchBackfill();
}
async function cancelBackfill(){
  try{await fetch('/api/backfill/cancel',{method:'POST'});}catch(e){}
}
async function fetchBackfill(){
  try{
    const r=await fetch('/api/backfill/status?t='+Date.now());
    if(!r.ok)return;
    const p=await r.json();
    renderBackfill(p);
  }catch(e){}
}
function renderBackfill(p){
  const total=p.total||0, scanned=p.scanned||0;
  const pct=total?Math.round(100*scanned/total):0;
  document.getElementById('bfBar').style.width=pct+'%';
  document.getElementById('bfStats').textContent=
    `taranan ${scanned}/${total} | indekslenen ${p.indexed||0} | PR'lı ${p.with_pr||0} | atlanan ${p.skipped||0} | hata ${p.errors||0}`+
    (p.current_wi?` | şu an WI#${p.current_wi}`:'');
  const logEl=document.getElementById('bfLog');
  logEl.textContent=(p.log||[]).map(l=>`${l.time}  ${l.message}`).join('\n');
  logEl.scrollTop=logEl.scrollHeight;
  const running=!!p.running;
  document.getElementById('bfStartBtn').style.display=running?'none':'';
  document.getElementById('bfCancelBtn').style.display=running?'':'none';
  if(!running&&_bfPoll){clearInterval(_bfPoll);_bfPoll=null;}
  if(running&&!_bfPoll)_bfPoll=setInterval(fetchBackfill,2000);
}
```

- [ ] **Step 4: Doğrula (server ayağa kalkıyor + HTML butonu/modalı içeriyor)**
```
cd /Users/volkan.ozyildirim/devel/crewai/agile_sdlc_crew && .venv/bin/python -c "from agile_sdlc_crew.server import app; print('server OK')" && grep -c "openBackfill\|backfillModal\|/api/backfill/status" src/agile_sdlc_crew/web/index.html
```
Expected: `server OK` ve grep sayısı ≥ 3.

- [ ] **Step 5: Commit**
```
git add src/agile_sdlc_crew/web/index.html
git commit -m "feat: dashboard'a Azure backfill butonu + progress modal"
```

---

### Task 7: Dokümanlar

**Files:** Modify `docs/pipeline/decision-points.md` (KN-33 girişi)

- [ ] **Step 1: KN-33'ün "Neden"/"Girdi" satırına Azure kaynağını ekle** — `docs/pipeline/decision-points.md`'de KN-33 girişini bul; "Yazma yalnızca başarılı PR'da (KN-32 öncesi step11)." cümlesinin ardına ekle:
```markdown
İndeks ayrıca iki backfill ile doldurulabilir: (1) bu sistemin MySQL'deki başarılı koşumları (`backfill_repo_decisions`), (2) **Azure DevOps geçmişi** — dashboard board butonu, takımın done+merge'li işlerini tarar (`azure_backfill.AzureBackfillRunner`, `/api/backfill/start`).
```

- [ ] **Step 2: Doğrula**
```
cd /Users/volkan.ozyildirim/devel/crewai/agile_sdlc_crew && grep -c "azure_backfill\|/api/backfill/start" docs/pipeline/decision-points.md
```
Expected: ≥ 1

- [ ] **Step 3: Commit**
```
git add docs/pipeline/decision-points.md
git commit -m "docs: KN-33'e Azure DevOps backfill kaynağını ekle"
```

---

### Task 8: Uçtan uca doğrulama (kod yok)

- [ ] **Step 1: Tam import + route + default smoke**
```
cd /Users/volkan.ozyildirim/devel/crewai/agile_sdlc_crew && .venv/bin/python -c "
from agile_sdlc_crew.server import app
from agile_sdlc_crew import azure_backfill
from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient
from agile_sdlc_crew.tools.vector_store import VectorStore
from agile_sdlc_crew import pipeline_config as p
paths={r.path for r in app.routes}
assert {'/api/backfill/start','/api/backfill/status','/api/backfill/cancel'} <= paths
assert p.get('CREW_AZ_BACKFILL_WORKERS')==8
assert hasattr(AzureDevOpsClient,'query_done_work_items') and hasattr(AzureDevOpsClient,'get_team_area_path')
assert hasattr(VectorStore,'existing_repo_decision_wis')
assert azure_backfill.is_running() is False
print('E2E import OK')
"
```
Expected: `E2E import OK`

- [ ] **Step 2 (canlı, manuel — opsiyonel):** Server'ı yeniden başlat (`./start.sh`), dashboard'da bir takım seç, "📚 Geçmiş İşleri Tara" → "Başlat". Modal'da progress bar + sayaçların ilerlediğini, bitince `/repo-decisions` kayıt sayısının arttığını gözle. Gerekirse `CREW_AZ_BACKFILL_LIMIT=20` ile küçük bir testle başla.

---

## Self-Review

**Spec coverage:**
- Board butonu + modal + polling → Task 6 ✅
- Async daemon thread + concurrent fetch + sequential index → Task 4 ✅
- Tek-çalışma guard + start/status/cancel → Task 4 (guard) + Task 5 (endpoints) ✅
- WIQL done WI sorgusu (ChangedDate DESC) + team area path → Task 2 ✅
- PR link parse / completed filtresi / changed paths / wi_content / sentetik plan → Task 4 ✅
- Idempotent + O(N) dedup (seen-set + skip_dedup_check) → Task 3 + Task 4 ✅
- Progress dosyası şeması → Task 4 (`_p` dict alanları spec ile birebir) ✅
- Knob'lar (workers/limit/done-states) → Task 1 ✅
- Toggle bağımsız yazma → Task 4 (index_repo_decision toggle kontrol etmez; yazma her zaman) ✅
- Hata izolasyonu (per-WI try/except, taramayı durdurmaz) → Task 4 ✅
- Docs (KN-33 Azure kaynağı) → Task 7 ✅

**Type/imza tutarlılığı:**
- `start_backfill(team, vector_store, client, config)` — Task 4 tanım, Task 5 çağrı (`VectorStore(), AzureDevOpsClient(), pipeline_config`): tutarlı ✅
- `index_repo_decision(..., skip_dedup_check=False)` — Task 3 tanım, Task 4 çağrı (`skip_dedup_check=True`): tutarlı ✅
- `query_done_work_items(area_path, states, limit)` — Task 2 tanım, Task 4 çağrı (`(area, self.states, self.limit)`): tutarlı ✅
- `existing_repo_decision_wis() -> set[str]` — Task 3 tanım, Task 4 çağrı: tutarlı ✅
- Progress alan adları (`total/scanned/with_pr/indexed/skipped/errors/current_wi/running/cancelled`) — Task 4 `_p` ile Task 6 `renderBackfill` birebir ✅

**Placeholder taraması:** Tüm kod blokları tam; TBD/TODO yok ✅
