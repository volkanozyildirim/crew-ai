"""İş yaşlandırma (aging) — "bu iş şu anki durumda kaç gündür bekliyor?"

Board ve backlog'da asıl sorun çoğu zaman *yanlış* iş değil, **kımıldamayan**
iş oluyor: bir yıldır Backlog'da duran bir story, üç haftadır Code Review'da
bekleyen bir PR, "In Progress" görünüp kimsenin dokunmadığı bir task. Bunlar
tek tek bakıldığında normal görünür; ancak kartın üstünde "kaç gündür orada"
yazmadıkça hiçbir zaman göze çarpmaz.

Bu modül tek bir soruyu LLM'siz, deterministik yanıtlar: verilen durum ve
tarihlerle iş ne kadar süredir bu durumda ve bu, o durum için normal mi?

  * süre = şimdi − `Microsoft.VSTS.Common.StateChangeDate`
    (alan yoksa `System.ChangedDate`, o da yoksa `System.CreatedDate`)
  * durumlar dört **kovaya** ayrılır (backlog / todo / progress / blocked);
    Done-Closed-Removed benzeri bitmiş durumlar hiç yaşlanmaz
  * her kovanın bir uyarı ve bir kritik eşiği vardır; hepsi
    `CREW_AGING_<KOVA>_WARN_DAYS` / `_CRIT_DAYS` ile değiştirilebilir
  * `CREW_AGING=0` tüm göstergeyi kapatır (arayüz rozetleri kaybolur)

Sunucu tarafı tek doğruluk kaynağıdır: `/api/board/workitems` ve
`/api/refinement` satırlarına `aging` bloğunu buradan ekler, arayüz yalnızca
boyar. Böylece eşikler env'den değişince arayüz koduna dokunmak gerekmez.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone

# ── kovalar ────────────────────────────────────────────────────────────
# Durum adları süreçten sürece değişir (Agile / Scrum / CMMI / özel süreç),
# bu yüzden ad eşleşmesi büyük-küçük harf ve boşluk duyarsızdır. Tanınmayan
# bir durum "todo" sayılır (sessizce yaşlanmaktan kaçmasın), ancak bitmiş
# gibi duran adlar DONE_RE ile elenir.

BUCKETS: dict[str, tuple[str, ...]] = {
    "backlog": (
        "new", "backlog", "proposed", "approved", "open", "draft", "yeni",
    ),
    "todo": (
        "to do", "todo", "committed", "ready", "ready for development",
        "selected", "selected for development", "planned", "analysis",
        "refinement", "yapilacak",
    ),
    "progress": (
        "active", "in progress", "doing", "development", "in development",
        "code review", "in review", "review", "pr review", "qa to do", "qa",
        "testing", "test", "in test", "uat", "preprod check", "verify",
        "devam ediyor",
    ),
    "blocked": (
        "blocked", "on hold", "hold", "waiting", "pending", "impediment",
        "engelli", "bekliyor",
    ),
}

# Bitmiş/iptal: hiç yaşlanmaz. Bu org'da "Resolved" ve "Ready for Production"
# da TAMAMLANDI sayılır — `sprint_report.DONE_STATES` ve dashboard'daki
# `DONE_STATES` ile aynı liste; burada ayrışırsa board "bitmiş" gösterdiği işi
# aynı anda "4 gündür takılı" diye kırmızıya boyar.
DONE_STATES = (
    "done", "closed", "completed", "resolved", "ready for production",
    "removed", "canceled", "cancelled", "rejected", "duplicate", "released",
    "tamamlandi", "kapali", "iptal",
)
# Yukarıdaki listede olmayan ama açıkça bitmiş duran adlar (ör. "Closed - Won't Fix").
DONE_RE = re.compile(
    r"^(done|closed|complete|completed|removed|cancell?ed|rejected|duplicate"
    r"|released|live|production|tamamlandi|kapali|iptal)\b",
    re.IGNORECASE,
)

BUCKET_LABELS = {
    "backlog": "Backlog",
    "todo": "Yapılacak",
    "progress": "Devam eden",
    "blocked": "Engelli",
}

# (uyarı, kritik) gün — "sıkı" varsayılan: takılan iş hemen göze çarpsın.
DEFAULT_THRESHOLDS: dict[str, tuple[int, int]] = {
    "backlog": (14, 45),
    "todo": (3, 7),
    "progress": (2, 4),
    "blocked": (1, 3),
}

_NORM_RE = re.compile(r"[\s_\-]+")


def _norm(state: str) -> str:
    return _NORM_RE.sub(" ", (state or "").strip().lower())


def _cfg(name: str):
    """Ayar değeri: pipeline_config (yaml → env → default) varsa oradan, yoksa ham env.
    Eşikler dashboard'ın Pipeline ayarlarından da değiştirilebilsin diye."""
    try:
        from agile_sdlc_crew import pipeline_config
        return pipeline_config.get(name)
    except Exception:  # noqa: BLE001 — knob kayıtlı değil / yaml okunamadı
        return os.environ.get(name)


def enabled() -> bool:
    """Kapatılınca (`CREW_AGING=0`) hiçbir işe `aging` bloğu eklenmez → rozetler kaybolur."""
    val = _cfg("CREW_AGING")
    if isinstance(val, bool):
        return val
    return (val if val is not None else "1") not in ("0", "false", "False", "")


def _env_int(name: str, default: int) -> int:
    raw = _cfg(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        val = int(float(str(raw).strip()))
    except ValueError:
        return default
    return val if val > 0 else default


def thresholds() -> dict[str, dict]:
    """Kova → {warn, crit, label}. Her çağrıda env okunur (ayar canlı değişebilir)."""
    out = {}
    for bucket, (warn, crit) in DEFAULT_THRESHOLDS.items():
        w = _env_int(f"CREW_AGING_{bucket.upper()}_WARN_DAYS", warn)
        c = _env_int(f"CREW_AGING_{bucket.upper()}_CRIT_DAYS", crit)
        # Kritik eşik uyarının altına düşerse sıralama anlamsızlaşır.
        if c < w:
            c = w
        out[bucket] = {"warn": w, "crit": c, "label": BUCKET_LABELS[bucket]}
    return out


def bucket_of(state: str) -> str | None:
    """Durumun kovası. Bitmiş durumlar için None (yaşlanmaz)."""
    s = _norm(state)
    if not s:
        return None
    if s in DONE_STATES or DONE_RE.match(s):
        return None
    for bucket, names in BUCKETS.items():
        if s in names:
            return bucket
    return "todo"  # tanınmayan özel durum: orta eşikle izle


def backlog_crit_days() -> int:
    """Backlog'da "çok bayat" sayılan gün eşiği (refinement cezası bunu kullanır)."""
    return thresholds()["backlog"]["crit"]


# ── tarih ──────────────────────────────────────────────────────────────

_SOURCE_LABELS = {
    "state": "durum değişiminden beri",
    "changed": "son güncellemeden beri",
    "created": "oluşturulduğundan beri",
}


def _parse(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def classify(state: str, *, state_change=None, changed=None, created=None,
             now: datetime | None = None) -> dict | None:
    """Tek iş için yaşlandırma bloğu.

    Doner: {days, level, bucket, bucket_label, warn, crit, source, since, text}
    level: 'ok' | 'warn' | 'crit'. Durum bitmişse ya da hiçbir tarih yoksa None.
    """
    bucket = bucket_of(state)
    if bucket is None:
        return None
    dt, source = None, ""
    for value, name in ((state_change, "state"), (changed, "changed"), (created, "created")):
        dt = _parse(value)
        if dt:
            source = name
            break
    if not dt:
        return None
    now = now or datetime.now(timezone.utc)
    days = max(0, (now - dt).days)
    th = thresholds()[bucket]
    level = "crit" if days >= th["crit"] else "warn" if days >= th["warn"] else "ok"
    return {
        "days": days,
        "level": level,
        "bucket": bucket,
        "bucket_label": th["label"],
        "warn": th["warn"],
        "crit": th["crit"],
        "source": source,
        "since": dt.isoformat(),
        "text": f"{state or bucket} durumunda {days} gündür bekliyor "
                f"({_SOURCE_LABELS.get(source, '')}; uyarı {th['warn']}g, kritik {th['crit']}g)",
    }


def annotate(items: list[dict], *, state_key: str = "state",
             state_change_key: str = "stateChangeDate",
             changed_key: str = "changedDate",
             created_key: str = "createdDate",
             now: datetime | None = None) -> list[dict]:
    """Liste içindeki her sözlüğe `aging` anahtarını ekler (yerinde, listeyi döner).
    `CREW_AGING=0` ise hiçbir şey eklenmez."""
    if not enabled():
        return items
    now = now or datetime.now(timezone.utc)
    for it in items:
        if not isinstance(it, dict):
            continue
        it["aging"] = classify(
            it.get(state_key, ""),
            state_change=it.get(state_change_key),
            changed=it.get(changed_key),
            created=it.get(created_key),
            now=now,
        )
    return items


def summarize(items: list[dict]) -> dict:
    """Toplu sayım: {warn, crit, total_flagged, oldest_days}."""
    warn = crit = 0
    oldest = 0
    for it in items:
        a = (it or {}).get("aging")
        if not a:
            continue
        if a["level"] == "crit":
            crit += 1
        elif a["level"] == "warn":
            warn += 1
        if a["level"] != "ok":
            oldest = max(oldest, a["days"])
    return {"warn": warn, "crit": crit, "total_flagged": warn + crit, "oldest_days": oldest}
