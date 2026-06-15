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
        self._running = True
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
