# Azure DevOps Backfill — Tasarım (Design Spec)

**Tarih:** 2026-06-15
**Durum:** Onaylandı (tasarım) — implementasyon planı bekliyor
**İlgili:** [2026-06-15-repo-history-suggestion-design.md](2026-06-15-repo-history-suggestion-design.md) (bu özellik onun `/repo-decisions` indeksini Azure DevOps geçmişinden besler)

## Amaç

Board'da seçili **takımın** tüm **done + merge edilmiş PR'lı** işlerini güncelden
geriye tarayıp, her birinin (WI içeriği + PR'da değişen dosyalar → repo) kaydını
`/repo-decisions` vector indeksine yaz. Böylece geçmiş-iş repo önerisi, bu
pipeline'dan hiç geçmemiş **insan eliyle** yapılmış işlerden de öğrenir.

Mevcut MySQL backfill (`VectorStore.backfill_repo_decisions`) yalnızca bu
sistemin kendi koşumlarını kapsar; bu özellik Azure DevOps geçmişini ayrı bir
kaynak olarak ekler.

## Kararlar (brainstorming'den)
- **Tetikleme:** Dashboard board'unda buton; **async + concurrent**; progress ayrı
  modal/ekrandan takip edilir.
- **Kapsam:** Seçili takımın **tüm** done + merge'li işleri, **güncelden geriye**.

## 1. Mimari / akış

Pipeline job kuyruğundan **bağımsız** dedicated daemon thread (mevcut `/api/pr-fix`
deseni). İçinde concurrent Azure I/O. Aynı anda **tek** backfill (global guard).

```
Board "📚 Geçmiş İşleri Tara" butonu (seçili takım)
  → POST /api/backfill/start {team}
  → daemon thread: AzureBackfillRunner.run()
       1. query_done_work_items(area_path, states, limit) — WIQL, ChangedDate DESC
       2. CONCURRENT (ThreadPoolExecutor, CREW_AZ_BACKFILL_WORKERS) her WI için fetch:
            - get_work_item(wi) → relations'tan "Pull Request" linkleri (repo_id, pr_id)
            - get_pull_request(repo, pr_id) → status == "completed" (merged) filtresi
            - get_pull_request_changes(repo, pr_id) → changeEntries[].item.path
       3. SEQUENTIAL indexer (ana thread): fetch sonuçları geldikçe
            index_repo_decision(wi, repo, pr_id, synthetic_plan, wi_content)
       4. Her WI sonrası progress JSON güncelle
  → GET /api/backfill/status (frontend 2sn polling) → progress modal
  → POST /api/backfill/cancel → cancel bayrağı; runner döngüde kontrol eder
```

**Concurrent fetch / sequential index gerekçesi:** Azure API I/O darboğazdır
(paralelden faydalanır); LanceDB yazımı + idempotency taraması sequential kalır →
eşzamanlı yazım yarışı/duplicate riski sıfır, kilit gerekmez.

## 2. Birimler

### 2.1 `tools/azure_devops_base.py` — 2 yeni metot
- `get_team_area_path(team: str) -> str` — `{org}/{project}/{team}/_apis/work/teamsettings/teamfieldvalues`
  çağrısı; `defaultValue` (area path) döner. Hata/boşsa `""`.
- `query_done_work_items(area_path: str, states: list[str], limit: int) -> list[dict]` —
  WIQL:
  ```sql
  SELECT [System.Id] FROM WorkItems
  WHERE [System.TeamProject] = @project
    AND [System.State] IN ('Done','Closed','Resolved')
    [AND [System.AreaPath] UNDER '<area_path>']   -- area_path boşsa bu satır atlanır
  ORDER BY [System.ChangedDate] DESC
  ```
  `limit>0` ise `query_work_items` çağrısına `$top` benzeri sınır (WIQL `TOP` veya
  sonuç dilimleme). Mevcut `query_work_items(wiql)` yeniden kullanılır.

Mevcut metotlar yeniden kullanılır: `get_work_item`, `get_pull_request`,
`get_pull_request_changes`, `list_repositories`.

### 2.2 `azure_backfill.py` (yeni modül)
`AzureBackfillRunner` sınıfı:
- `__init__(self, team, vector_store, client, db=None)` + config (workers, limit, states).
- `run()` — enumerate → concurrent fetch → sequential index → progress; cancel kontrolü.
- `_fetch_wi(wi_id) -> dict | None` — WI detayını çek, relations'tan merge'li PR bul,
  changed files + wi_content topla. Completed PR yoksa `None`.
- `cancel()` — cancel bayrağını set eder.
- Progress: `/tmp/crew_backfill.json` thread-safe yazım (status.json deseni).
- PR link parse: relations'ta `attributes.name == "Pull Request"`, url'den
  `PullRequestId/[^%]+%2f([^%]+)%2f(\d+)` ile (repo_id, pr_id) — flow.py:1216 deseni.
- Repo adı: `get_pull_request` sonucundaki `repository.name`; yoksa repo_id→name map
  (`list_repositories` bir kez).
- changed files: `changeEntries`'ten `item.path`; klasör/silme dışı (blob, isFolder değil).
- wi_content: `System.Title` + HTML-stripped `System.Description` + `AcceptanceCriteria`.
- Sentetik plan: `{"repo_name": repo, "changes": [{"file_path": p} for p in paths]}`.
- `index_repo_decision` idempotent → tekrar çalıştırma + pipeline'ın indekslediği
  WI'lar güvenli atlanır.

### 2.3 `server.py` — 3 endpoint
- `POST /api/backfill/start` (body: `{team: str}`) — backfill çalışıyorsa **409**;
  yoksa daemon thread başlatır, `202` + döner. Tek-çalışma guard (module-level lock + flag).
- `GET /api/backfill/status` — `/tmp/crew_backfill.json` okur (yoksa `{running: false}`).
- `POST /api/backfill/cancel` — runner'a cancel bayrağı; `202`.

### 2.4 `web/index.html` — buton + progress modal
- Board alanına (team/sprint seçicilerin yanına) buton: "📚 Geçmiş İşleri Tara".
  Seçili `teamSel` değerini kullanır; takım seçili değilse uyarı.
- Modal (mevcut kickoff modal stili): progress bar = `scanned/total` (işlenen WI /
  toplam done WI), sayaçlar (scanned, indexed, with_pr, skipped, errors), current WI,
  son 20 log satırı, Cancel butonu.
- `setInterval` ile `/api/backfill/status` 2sn polling; `running=false` olunca durur.

### 2.5 `pipeline_config.py` — knob'lar
- `CREW_AZ_BACKFILL_WORKERS` (int, default 8, min 1) — concurrent fetch worker sayısı.
- `CREW_AZ_BACKFILL_LIMIT` (int, default 0) — taranacak max WI (0 = limitsiz/tümü).
- `CREW_AZ_DONE_STATES` (str, default "Done,Closed,Resolved") — done kabul edilen durumlar.

## 3. Progress dosyası şeması (`/tmp/crew_backfill.json`)
```json
{
  "running": true, "cancelled": false,
  "team": "...", "started_at": "ISO", "finished_at": "",
  "total": 0, "scanned": 0, "with_pr": 0, "indexed": 0, "skipped": 0, "errors": 0,
  "current_wi": 1234,
  "log": [{"time": "HH:MM:SS", "message": "..."}]
}
```
Thread-safe yazım (lock). `log` son 100 ile sınırlı.

## 4. Toggle ilişkisi
- Backfill **explicit** aksiyon → `CREW_REPO_HISTORY_SUGGEST`'ten **bağımsız** yazar.
- Okuma (kickoff/discover/technical-design önerisi) yine `CREW_REPO_HISTORY_SUGGEST=1` gerektirir.

## 5. Hata yönetimi & edge cases
- Her WI fetch'i try/except — bir WI hatası taramayı durdurmaz (errors++).
- Merge'li PR'ı olmayan WI → atla (skipped++).
- changed files boşsa → atla.
- `get_team_area_path` çözülemezse → proje geneli done WI'lar (area filtresi yok).
- Backfill zaten çalışıyorsa `/start` 409.
- Cancel: runner WI döngüsünde bayrak kontrol eder, temiz durur (`cancelled: true`).
- Tüm Azure çağrıları timeout'lu (mevcut client 30s default).

## 6. Etkilenen dosyalar
- `src/agile_sdlc_crew/tools/azure_devops_base.py` — `get_team_area_path`, `query_done_work_items`
- `src/agile_sdlc_crew/azure_backfill.py` — yeni: `AzureBackfillRunner`
- `src/agile_sdlc_crew/server.py` — 3 endpoint + tek-çalışma guard
- `src/agile_sdlc_crew/web/index.html` — buton + progress modal + polling
- `src/agile_sdlc_crew/pipeline_config.py` — 3 knob
- `docs/pipeline/` — opsiyonel: backfill kaynaklarını not düş (KN-33 ile ilişki)

## 7. Doğrulama (test suite yok — CLAUDE.md)
- Import smoke: `azure_backfill`, `server`, client metotları.
- Birim (mock'suz, izole): PR-link parse regex, changeEntries→path çıkarımı, WIQL string üretimi.
- `AzureBackfillRunner` progress dosyası round-trip (sahte client ile).
- Canlı: küçük takımda çalıştır → modal progress + `/repo-decisions` record artışı.
