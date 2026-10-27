"""Kalite işi alımı — aynı bulgunun iki kez açılmaması.

Kaynak board'dur: `tempo-quality` etiketli Task'ların açıklamasındaki
`tempo-fp:` satırları okunur. quality_items tablosu yalnızca geçmiştir —
DB kaybı dedup'ı bozmaz.

Durum kuralı:
  Done/Closed/Completed          → kapanış + grace sonrası hâlâ görülüyorsa yeniden açılabilir
  Canceled/Removed/Rejected      → kalıcı bastırma (insan "yapılmayacak" dedi)
  diğer her şey (Resolved, Ready for Production dahil — düzeltme canlıda değil) → açık
"""

from __future__ import annotations

from datetime import datetime

from agile_sdlc_crew.quality.render import TAG, parse_fingerprints

CLOSED_FOR_GOOD = {"canceled", "cancelled", "removed", "rejected"}
CLOSED_DONE = {"done", "closed", "completed"}

WIQL = ("SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
        f"AND [System.WorkItemType] = 'Task' AND [System.Tags] CONTAINS '{TAG}'")


def fetch_board(client) -> list[dict]:
    return client.query_work_items(WIQL)


def _epoch(iso: str) -> float:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def index_board(work_items: list[dict]) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for wi in work_items or []:
        f = wi.get("fields") or {}
        entry = {
            "wi": int(wi.get("id") or 0),
            "state": (f.get("System.State") or "").strip(),
            "closed_at": _epoch(f.get("Microsoft.VSTS.Common.ClosedDate")
                                or f.get("Microsoft.VSTS.Common.StateChangeDate") or ""),
        }
        for fp in parse_fingerprints(f.get("System.Description") or ""):
            prev = idx.get(fp)
            if prev is None or entry["wi"] > prev["wi"]:
                idx[fp] = entry
    return idx


def verdict(entry: dict | None, *, last_seen: float, now: float,
            grace_days: int = 3) -> tuple[str, int | None]:
    if not entry:
        return "new", None
    state = (entry.get("state") or "").lower()
    if state in CLOSED_FOR_GOOD:
        return "suppressed", entry["wi"]
    if state not in CLOSED_DONE:
        return "blocked", entry["wi"]
    closed = entry.get("closed_at") or 0.0
    seen = last_seen or now
    if closed and seen > closed + grace_days * 86400:
        return "recurring", entry["wi"]
    return "blocked", entry["wi"]
