"""Günlük özet (Daily Scrum) — Scrum işlevleri, faz 4.

Son 24 saatin pipeline hareketi tek bakışta: bitenler (PR linkiyle), koşan iş
ve adımı, kuyruk, **insan bekleyenler** (needs_info / needs_human — engel
listesi), maliyet. Deterministik, LLM yok. Dashboard'dan okunur
(`GET /api/daily`); isteğe bağlı zamanlayıcı her gün `CREW_DAILY_TIME`'da
üretip Telegram Bot API'ye gönderir (`CREW_DAILY_TELEGRAM_TOKEN` +
`CREW_DAILY_TELEGRAM_CHAT_ID`; yoksa yalnızca log + dosya).

Neden ayrı modül: worker'dan bağımsız, salt okunur; sunucu olmadan da
(`python -m agile_sdlc_crew.daily`) çalıştırılabilir.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("pipeline")

_MONTHS_TR = ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"]


def _fmt_date(d: datetime) -> str:
    return f"{d.day} {_MONTHS_TR[d.month - 1]} {d.year}"


def _minutes(a, b) -> float | None:
    if not a or not b:
        return None
    try:
        return max(0.0, (b - a).total_seconds() / 60.0)
    except Exception:
        return None


def _f(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def collect(now: datetime | None = None, hours: int = 24) -> dict:
    """DB'den son `hours` saat: finished, running, queued, waiting (açık needs_*)."""
    from agile_sdlc_crew import db
    now = now or datetime.now()
    since = now - timedelta(hours=int(hours))
    out = {"now": now, "since": since, "finished": [], "running": [], "queued": [], "waiting": []}
    with db.get_conn() as conn:
        cur = conn.cursor()
        base = "SELECT id, work_item_id, wi_title, status, current_step, pr_url, error_message, " \
               "total_cost_usd, estimate_sp, started_at, finished_at, created_at FROM jobs " \
               "WHERE COALESCE(kickoff_only,0)=0 AND COALESCE(dry_run,0)=0 "
        cur.execute(base + "AND finished_at >= %s AND status IN ('completed','failed','needs_human','needs_info') "
                    "ORDER BY finished_at DESC", (since,))
        out["finished"] = list(cur.fetchall() or [])
        cur.execute(base + "AND status='running' ORDER BY started_at")
        out["running"] = list(cur.fetchall() or [])
        cur.execute(base + "AND status='queued' ORDER BY id")
        out["queued"] = list(cur.fetchall() or [])
        # Açık bekleyenler: WI'nin EN SON isi needs_info/needs_human ise (sonradan retry edilmemis)
        cur.execute(
            "SELECT j.id, j.work_item_id, j.wi_title, j.status, j.error_message, j.finished_at FROM jobs j "
            "JOIN (SELECT work_item_id, MAX(id) AS mid FROM jobs "
            "      WHERE COALESCE(kickoff_only,0)=0 AND COALESCE(dry_run,0)=0 GROUP BY work_item_id) m "
            "ON m.mid = j.id WHERE j.status IN ('needs_info','needs_human') ORDER BY j.finished_at DESC LIMIT 20"
        )
        out["waiting"] = list(cur.fetchall() or [])
    return out


_STATUS_TR = {"completed": "✅ tamamlandı", "failed": "❌ başarısız",
              "needs_human": "🧑 insan kararı", "needs_info": "ℹ️ detay bekliyor"}


def render_markdown(d: dict, *, wi_base_url: str = "") -> str:
    now = d["now"]
    L = [f"## ☀️ Günlük özet — {_fmt_date(now)} {now:%H:%M}", ""]

    def wi(j):
        wid = j.get("work_item_id")
        title = (j.get("wi_title") or "").strip()
        link = f"[#{wid}]({wi_base_url}/{wid})" if wi_base_url else f"#{wid}"
        return f"{link}" + (f" {title[:70]}" if title else "")

    fin = d.get("finished") or []
    cost = sum(_f(j.get("total_cost_usd")) for j in fin)
    L.append(f"**Son {int((now - d['since']).total_seconds() // 3600)} saat:** {len(fin)} iş bitti"
             + (f" · ${cost:.2f}" if fin else "") + f" · koşan {len(d.get('running') or [])} · kuyrukta {len(d.get('queued') or [])}")
    L.append("")
    if fin:
        L += ["### Bitenler", ""]
        for j in fin:
            mins = _minutes(j.get("started_at"), j.get("finished_at"))
            extra = []
            if j.get("pr_url"):
                extra.append(f"[PR]({j['pr_url']})")
            if j.get("estimate_sp"):
                extra.append(f"{j['estimate_sp']} SP")
            if mins is not None:
                extra.append(f"{mins:.0f} dk")
            if _f(j.get("total_cost_usd")):
                extra.append(f"${_f(j.get('total_cost_usd')):.2f}")
            line = f"- {_STATUS_TR.get(j.get('status'), j.get('status'))} — {wi(j)}" + (f" · {' · '.join(extra)}" if extra else "")
            if j.get("status") in ("failed", "needs_human", "needs_info") and j.get("error_message"):
                line += f"\n  _{(j['error_message'] or '')[:140]}_"
            L.append(line)
        L.append("")
    run = d.get("running") or []
    if run:
        L += ["### Şu an koşan", ""]
        for j in run:
            el = _minutes(j.get("started_at"), now)
            L.append(f"- 🔄 {wi(j)} — adım `{j.get('current_step') or '?'}`" + (f", {el:.0f} dk" if el is not None else ""))
        L.append("")
    q = d.get("queued") or []
    if q:
        L += ["### Kuyruk", "", "- " + ", ".join(f"#{j.get('work_item_id')}" for j in q[:15]) + (" …" if len(q) > 15 else ""), ""]
    wait = d.get("waiting") or []
    if wait:
        L += ["### İnsan bekleyenler (engeller)", ""]
        for j in wait:
            days = (now - j["finished_at"]).days if j.get("finished_at") else None
            L.append(f"- {_STATUS_TR.get(j.get('status'), j.get('status'))} — {wi(j)}"
                     + (f" · {days} gün" if days else "") + f"\n  _{(j.get('error_message') or '')[:140]}_")
        L.append("")
    if not fin and not run and not q and not wait:
        L.append("Hareket yok: biten, koşan, kuyrukta ya da bekleyen iş bulunmadı.")
    return "\n".join(L).rstrip() + "\n"


def to_plain(md: str) -> str:
    """Telegram için düz metin: başlık/kalın/kod/link işaretleri sadeleştirilir."""
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 \2", md)
    s = re.sub(r"^#{1,6}\s*", "", s, flags=re.M)
    s = s.replace("**", "").replace("`", "").replace("_", "")
    return s.strip()


def build_daily(hours: int = 24, wi_base_url: str = "") -> dict:
    d = collect(hours=hours)
    md = render_markdown(d, wi_base_url=wi_base_url)
    return {
        "markdown": md, "plain": to_plain(md),
        "counts": {"finished": len(d["finished"]), "running": len(d["running"]),
                   "queued": len(d["queued"]), "waiting": len(d["waiting"])},
        "generated_at": d["now"].isoformat(timespec="seconds"),
    }


# ── Gönderim + zamanlayıcı ───────────────────────────────────────────────

def telegram_configured() -> bool:
    return bool(os.environ.get("CREW_DAILY_TELEGRAM_TOKEN") and os.environ.get("CREW_DAILY_TELEGRAM_CHAT_ID"))


def send_telegram(text: str) -> bool:
    """Telegram Bot API sendMessage (düz metin). Yapılandırma yoksa False."""
    token = os.environ.get("CREW_DAILY_TELEGRAM_TOKEN", "")
    chat = os.environ.get("CREW_DAILY_TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        return False
    import requests
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}, timeout=20)
        ok = r.ok and (r.json().get("ok") is True)
        if not ok:
            log.warning(f"  Günlük özet Telegram'a gidemedi: {r.status_code} {r.text[:200]}")
        return ok
    except Exception as e:  # noqa: BLE001
        log.warning(f"  Günlük özet Telegram hatasi: {e}")
        return False


def save_to_dir(md: str, when: datetime | None = None) -> str:
    d = Path(os.environ.get("CREW_DAILY_DIR", "/tmp/crew_daily"))
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{(when or datetime.now()):%Y-%m-%d}.md"
    p.write_text(md)
    return str(p)


def publish_daily(hours: int = 24) -> dict:
    """Üret + kaydet + (varsa) Telegram. Zamanlayıcı ve /api/daily/send çağırır."""
    rep = build_daily(hours=hours)
    path = save_to_dir(rep["markdown"])
    sent = send_telegram(rep["plain"]) if telegram_configured() else False
    log.info(f"  ☀️ Günlük özet üretildi → {path}" + (" · Telegram gönderildi" if sent else ""))
    rep.update({"file": path, "telegram_sent": sent, "telegram_configured": telegram_configured()})
    return rep


def _parse_hhmm(s: str) -> tuple[int, int]:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", s or "")
    if not m:
        return 9, 0
    return max(0, min(23, int(m.group(1)))), max(0, min(59, int(m.group(2))))


def seconds_until(now: datetime, hh: int, mm: int) -> float:
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


_sched_lock = threading.Lock()
_sched_thread: threading.Thread | None = None


def start_scheduler(enabled_fn) -> bool:
    """Günde bir kez (CREW_DAILY_TIME, varsayılan 09:00) publish_daily. enabled_fn her
    turda okunur → dashboard'dan kapatılırsa bir sonraki turda susar. İdempotent."""
    global _sched_thread
    with _sched_lock:
        if _sched_thread and _sched_thread.is_alive():
            return False

        def _loop():
            while True:
                hh, mm = _parse_hhmm(os.environ.get("CREW_DAILY_TIME", "09:00"))
                time.sleep(max(30.0, seconds_until(datetime.now(), hh, mm)))
                try:
                    if enabled_fn():
                        publish_daily()
                except Exception as e:  # noqa: BLE001
                    log.warning(f"  Günlük özet zamanlayıcı hatasi: {e}")

        _sched_thread = threading.Thread(target=_loop, daemon=True, name="daily-scheduler")
        _sched_thread.start()
        return True


if __name__ == "__main__":  # pragma: no cover
    from dotenv import load_dotenv
    load_dotenv()
    print(build_daily()["markdown"])
