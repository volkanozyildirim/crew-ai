"""Plan → alt iş kayıtları (child Task) — Scrum işlevleri, faz 2.

Takımda hiyerarşi User Story → Task (son 45 günde Task'ların %73'ü bir
parent'a bağlı). Pipeline bir **parent tipi** WI (User Story, Bug, Feature…)
üzerinde koşuyorsa teknik plan onaylandığında her plan değişikliği için bir
child Task açar; step6'da dosya push edildikçe ilgili Task `Done`a çekilir.
WI zaten Task ise (takımın olağan durumu: 73121, 73061) hiçbir şey yapılmaz —
Task altına Task açmak board'u kirletir.

Güvenlik: `CREW_WI_CHILD_TASKS` varsayılan kapalı; parent'ta daha önce
pipeline'ın açtığı (`crew-generated` etiketli) child varsa yeniden açılmaz
(retry/resume idempotent); en çok `CREW_WI_CHILD_TASKS_MAX` child, fazlası
dizine göre gruplanır. LLM yok.
"""

from __future__ import annotations

import logging
import posixpath

log = logging.getLogger("pipeline")

CHILD_PARENT_TYPES = {"User Story", "Bug", "Feature", "Product Backlog Item", "Epic", "Improvement"}
CHILD_TYPE = "Task"
CHILD_TAG = "crew-generated"
_ACTION_TR = {"add": "Yeni dosya", "edit": "Düzenle", "modify": "Düzenle", "delete": "Sil", "append": "Ekle"}


def parent_allows_children(wi_type: str) -> bool:
    return (wi_type or "").strip() in CHILD_PARENT_TYPES


def _title(repo: str, action: str, path: str, desc: str, limit: int = 128) -> str:
    base = posixpath.basename((path or "").replace("\\", "/")) or path
    head = f"[{repo}] {_ACTION_TR.get((action or '').lower(), 'Değiştir')} {base}"
    if desc:
        head = f"{head} — {desc}"
    return head if len(head) <= limit else head[: limit - 1].rstrip() + "…"


def plan_child_tasks(plan: dict, repo_name: str, max_children: int = 8) -> list[dict]:
    """Plan değişiklikleri → [{title, description, files:[...]}].
    max'ı aşarsa değişiklikler ilk dizine göre gruplanır."""
    changes = [c for c in (plan or {}).get("changes") or [] if isinstance(c, dict) and c.get("file_path")]
    repo = repo_name or (plan or {}).get("repo_name") or "repo"
    if not changes:
        return []
    if len(changes) <= max_children:
        out = []
        for c in changes:
            desc = str(c.get("description") or c.get("desc") or "").strip()
            cov = ", ".join(str(x) for x in (c.get("covers_requirements") or []))
            body = desc or "Plan değişikliği"
            if cov:
                body += f"\n\nKapsadığı gereksinimler: {cov}"
            body += f"\n\nDosya: `{c['file_path']}`"
            out.append({"title": _title(repo, c.get("change_type", ""), c["file_path"], desc),
                        "description": body, "files": [c["file_path"]]})
        return out
    groups: dict[str, list[dict]] = {}
    for c in changes:
        p = c["file_path"].replace("\\", "/").strip("/")
        parts = p.split("/")
        key = "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1 else "(kök)")
        groups.setdefault(key, []).append(c)
    out = []
    for key, cs in list(groups.items())[:max_children]:
        files = [c["file_path"] for c in cs]
        lines = [f"- `{c['file_path']}` — {str(c.get('description') or c.get('desc') or '').strip()}" for c in cs]
        out.append({"title": f"[{repo}] {key} — {len(cs)} dosya", "description": "\n".join(lines), "files": files})
    return out


def existing_generated_children(client, parent_id: int) -> list[dict]:
    """Parent'ın `crew-generated` etiketli çocukları [{id, title, state}]."""
    try:
        kids = client.get_work_item_children(int(parent_id))
    except Exception as e:  # noqa: BLE001
        log.warning(f"  Alt is kayitlari okunamadi: {e}")
        return []
    out = []
    for k in kids:
        f = k.get("fields", {}) or {}
        tags = (f.get("System.Tags") or "")
        if CHILD_TAG in tags:
            out.append({"id": k.get("id"), "title": f.get("System.Title", ""), "state": f.get("System.State", "")})
    return out


def create_children(client, parent_id: int, parent_fields: dict, items: list[dict], logger=None) -> list[dict]:
    """Child Task'ları aç; [{id, title, files}] döner. Parent'ta üretilmiş child
    varsa hiç açmaz (idempotent) ve mevcutları döndürür (files boş)."""
    _l = logger or log.info
    have = existing_generated_children(client, parent_id)
    if have:
        _l(f"  🧩 Parent #{parent_id}'de {len(have)} üretilmiş alt iş zaten var — yeniden açılmadı")
        return [{"id": h["id"], "title": h["title"], "files": []} for h in have]
    created = []
    for it in items:
        fields = {
            "System.Title": it["title"],
            "System.Description": it["description"].replace("\n", "<br>"),
            "System.Tags": CHILD_TAG,
        }
        for key in ("System.AreaPath", "System.IterationPath"):
            if parent_fields.get(key):
                fields[key] = parent_fields[key]
        try:
            wi = client.create_work_item(CHILD_TYPE, fields, parent_id=int(parent_id))
            cid = wi.get("id")
            created.append({"id": cid, "title": it["title"], "files": list(it.get("files") or [])})
            _l(f"  🧩 Alt iş #{cid}: {it['title'][:80]}")
        except Exception as e:  # noqa: BLE001
            _l(f"  🧩 Alt iş açılamadı ({it['title'][:40]}…): {e}")
    return created


def child_for_file(children: list[dict], file_path: str) -> dict | None:
    def _n(p: str) -> str:
        return (p or "").replace("\\", "/").strip("/").lower()
    target = _n(file_path)
    for ch in children or []:
        if any(_n(f) == target for f in ch.get("files") or []):
            return ch
    return None
