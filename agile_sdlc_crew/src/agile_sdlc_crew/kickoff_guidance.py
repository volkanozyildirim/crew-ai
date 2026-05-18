"""Kickoff ogrenim deposu — gecmis WI'larda kullanicinin verdigi
duzeltmelerden olusan global yonergeler.

Her kickoff calistirilirken `format_for_context()` ile context'in basina
enjekte edilir; uzmanlar bu kurallari hesaba katar (orn: "FLO-X repo'su
icin sepet endpoint'i her zaman .../v2/cart altinda yer alir").

Storage: JSON dosyasi (CREW_KICKOFF_GUIDANCE_FILE env ile override edilebilir).
Default: <repo_root>/.crew_kickoff_guidance.json
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger("pipeline")
_LOCK = threading.Lock()


def _default_path() -> Path:
    env = os.environ.get("CREW_KICKOFF_GUIDANCE_FILE")
    if env:
        return Path(env)
    # repo root: src/agile_sdlc_crew/.. -> repo
    return Path(__file__).resolve().parents[2] / ".crew_kickoff_guidance.json"


def _path() -> Path:
    p = _default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_raw() -> dict:
    p = _path()
    if not p.exists():
        return {"rules": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"  Kickoff guidance dosyasi okunamadi ({p}): {e}")
        return {"rules": []}


def _save_raw(data: dict) -> None:
    p = _path()
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def list_rules() -> list[dict]:
    """Tum guidance kurallarini dondurur (en yenisi en ustte)."""
    with _LOCK:
        data = _load_raw()
    rules = data.get("rules") or []
    return sorted(rules, key=lambda r: r.get("created_at", 0), reverse=True)


def add_rule(text: str, *, source_wi: str = "", source_job_id: int | None = None) -> dict:
    """Yeni kural ekle. Bos/duplicate text ise hata dondurmez, sadece atlar."""
    t = (text or "").strip()
    if not t:
        return {}
    with _LOCK:
        data = _load_raw()
        rules = data.get("rules") or []
        # Tam ayni text varsa dokunma
        for r in rules:
            if (r.get("text") or "").strip() == t:
                return r
        new = {
            "id": uuid.uuid4().hex[:12],
            "text": t,
            "source_wi": str(source_wi or ""),
            "source_job_id": int(source_job_id) if source_job_id else None,
            "created_at": int(time.time()),
        }
        rules.append(new)
        data["rules"] = rules
        _save_raw(data)
    log.info(f"  Kickoff guidance eklendi: id={new['id']} ({t[:80]})")
    return new


def remove_rule(rule_id: str) -> bool:
    with _LOCK:
        data = _load_raw()
        rules = data.get("rules") or []
        before = len(rules)
        rules = [r for r in rules if r.get("id") != rule_id]
        if len(rules) == before:
            return False
        data["rules"] = rules
        _save_raw(data)
    log.info(f"  Kickoff guidance silindi: id={rule_id}")
    return True


def format_for_context(max_rules: int = 30) -> str:
    """Aktif kurallari kickoff context'ine enjekte edilecek metne cevirir.

    Kural yoksa bos string. Kurallar varsa baska bir formatta:
        # OGRENILMIS YONERGELER (gecmis WI'lardan)
        1. ...
        2. ...
    """
    rules = list_rules()[:max_rules]
    if not rules:
        return ""
    lines = ["# OGRENILMIS YONERGELER (gecmis kickoff'lardan)", ""]
    for i, r in enumerate(rules, 1):
        wi = r.get("source_wi") or "?"
        lines.append(f"{i}. (WI #{wi}) {r.get('text','').strip()}")
    lines.append("")
    lines.append(
        "Bu yonergeler ekibin gecmiste verdigi duzeltmelerden ogrenildi. "
        "Mevcut is kalemine uygun olanlari dikkate al, uygun degilse goz ardi et."
    )
    return "\n".join(lines)
