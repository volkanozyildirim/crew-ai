"""Sürüm yükseltme taramasının önbelleği (Sürüm & güvenlik sekmesi; kullanıcı isteği 2026-11-26).

Bir repo taraması (katalog + hedef araç + sürüm yerleri + CI + aşama) soğukken 30–45 sn sürer ve her repo seçiminde
baştan yapılıyordu. Sonuç artık diskte tutulur (`config/quality_upgrade_cache/`, süreç yeniden başlasa da kalır):

- taze kayıt → hemen döner (`cache.state = "taze"`);
- süresi dolmuş kayıt → ESKİ sonuç hemen döner (`"yenileniyor"`) ve arka planda yeniden kurulur (kart 8 sn'de bir
  yeniden sorar); ısıtıcı (`start_warmer`) süresi dolan kayıtları kimse açmadan da yeniler;
- kayıt yok / *Yeniden tara* (`force`) → eşzamanlı kurulur (eskisi gibi bekler).

Yazan uçlar (başlat / durdur / adım / şablon / iş aç) o reponun kayıtlarını SİLER — eylemden sonra kart bayat
durumu göstermesin; otomatik ilerletme turu ise süresini doldurur (sonraki açılış eskiyi gösterip yeniler).
Ayar: `CREW_TECHDEBT_UPGRADE_CACHE` (varsayılan açık), süre `CREW_TECHDEBT_UPGRADE_CACHE_H` (6 sa).
Dosyalar güvenilmezdir: biçim tutmazsa kayıt yok sayılır."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("pipeline")

CACHE_DIR = Path(__file__).resolve().parent.parent / "config" / "quality_upgrade_cache"
WARM_MIN = 30
_LOCK = threading.Lock()
_BUSY: set[str] = set()                 # arka planda yenilenen anahtarlar (tek uçuş)
_WARMER: threading.Thread | None = None


def enabled() -> bool:
    from agile_sdlc_crew import pipeline_config as pc
    return bool(pc.get("CREW_TECHDEBT_UPGRADE_CACHE"))


def ttl_h() -> float:
    from agile_sdlc_crew import pipeline_config as pc
    try:
        return max(0.25, float(pc.get("CREW_TECHDEBT_UPGRADE_CACHE_H") or 6))
    except (TypeError, ValueError):
        return 6.0


def _key(lang: str, repo: str, to: str | None, team: str) -> str:
    return f"{lang}:{repo.lower()}:{to or ''}:{team or ''}"


def _path(cache_dir: Path, key: str) -> Path:
    return Path(cache_dir) / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()[:40]}.json"


def _read(cache_dir: Path, key: str) -> dict | None:
    try:
        doc = json.loads(_path(cache_dir, key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("key") != key or not isinstance(doc.get("result"), dict):
        return None
    at = doc.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    return doc


def _write(cache_dir: Path, key: str, lang: str, repo: str, to, team: str, result: dict, at: float) -> None:
    try:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        doc = {"key": key, "lang": lang, "repo": repo, "to": to or "", "team": team or "", "at": at, "result": result}
        tmp = _path(cache_dir, key).with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(_path(cache_dir, key))
    except OSError as e:
        log.warning(f"⚠️ upgrade önbelleği yazılamadı: {e}")


def _entries(cache_dir: Path):
    try:
        files = list(Path(cache_dir).glob("*.json"))
    except OSError:
        return
    for f in files:
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and isinstance(doc.get("key"), str) and isinstance(doc.get("result"), dict):
            yield f, doc


def invalidate(lang: str, repo: str, *, drop: bool = True, cache_dir: Path | None = None) -> int:
    """Reponun (her hedef / takım) kayıtları: `drop` → silinir (sonraki açılış taze kurar), değilse süresi doldurulur
    (sonraki açılış eskiyi gösterip arka planda yeniler). → etkilenen kayıt sayısı."""
    n = 0
    pre = f"{lang}:{str(repo).lower()}:"
    for f, doc in list(_entries(cache_dir or CACHE_DIR)):
        if not doc["key"].startswith(pre):
            continue
        try:
            if drop:
                f.unlink()
            else:
                doc["at"] = 0
                f.write_text(json.dumps(doc, ensure_ascii=False, default=str), encoding="utf-8")
            n += 1
        except OSError:
            pass
    return n


def summary(cache_dir: Path | None = None) -> dict[tuple[str, str], dict]:
    """(dil, repo küçük harf) → en yeni kaydın özeti (aşama, engelleyen, hedef, zaman): repo listesinin gruplu
    görünümü için. Tarama yapmaz."""
    out: dict[tuple[str, str], dict] = {}
    for _f, doc in _entries(cache_dir or CACHE_DIR):
        r = doc["result"]
        k = (str(doc.get("lang") or ""), str(doc.get("repo") or "").lower())
        at = doc.get("at") if isinstance(doc.get("at"), (int, float)) else 0
        if k in out and out[k]["at"] >= at:
            continue
        st = r.get("status") if isinstance(r.get("status"), dict) else {}
        out[k] = {"at": at, "stage": str(st.get("stage") or ""), "blocking": int(r.get("blocking") or 0),
                  "target": str(r.get("target") or ""), "up_to_date": bool(r.get("up_to_date"))}
    return out


def _stamp(result: dict, at: float, state: str) -> dict:
    from datetime import datetime
    return {**result, "cache": {"state": state, "at": datetime.fromtimestamp(at).isoformat(timespec="seconds"),
                                "ttl_h": ttl_h()}}


def _refresh_bg(key: str, build, store, spawn=None) -> bool:
    with _LOCK:
        if key in _BUSY:
            return False
        _BUSY.add(key)

    def job():
        try:
            store(build(), time.time())
        except Exception as e:  # noqa: BLE001 — eski sonuç kalır, sonraki açılış yeniden dener
            log.warning(f"⚠️ upgrade önbelleği yenilenemedi ({key}): {str(e)[:300]}")
        finally:
            with _LOCK:
                _BUSY.discard(key)
    try:
        (spawn or (lambda fn: threading.Thread(target=fn, daemon=True, name="upgrade-cache").start()))(job)
    except Exception:
        with _LOCK:
            _BUSY.discard(key)
        raise
    return True


def get(lang: str, repo: str, to: str | None, team: str, build, *, force: bool = False, now: float | None = None,
        spawn=None, cache_dir: Path | None = None) -> dict:
    """`build()` taze sonucu üretir (hata fırlatabilir — önbelleğe yazılmaz). Önbellek kapalıysa doğrudan kurar."""
    if not enabled():
        return build()
    cache_dir = cache_dir or CACHE_DIR
    key = _key(lang, repo, to, team)
    now = time.time() if now is None else now

    def store(res: dict, at: float) -> None:
        if isinstance(res, dict) and "error" not in res:
            _write(cache_dir, key, lang, repo, to, team, res, at)

    doc = None if force else _read(cache_dir, key)
    if doc is not None:
        age = now - doc["at"]
        if 0 <= age < ttl_h() * 3600:
            return _stamp(doc["result"], doc["at"], "taze")
        _refresh_bg(key, build, store, spawn)
        return _stamp(doc["result"], doc["at"], "yenileniyor")
    res = build()
    store(res, now)
    return _stamp(res, now, "yeni")


def warm_once(build_for, *, now: float | None = None, cache_dir: Path | None = None, limit: int = 20) -> int:
    """Süresi dolmuş kayıtları sırayla (en eskisi önce) yeniden kurar. `build_for(lang, repo, to, team)` taze sonuç.
    Kaydı olmayan repo taranmaz (bütün repoları körlemesine taramak saatler sürer). → yenilenen sayı."""
    cache_dir = cache_dir or CACHE_DIR
    now = time.time() if now is None else now
    old = sorted((doc for _f, doc in _entries(cache_dir) if not (0 <= now - float(doc.get("at") or 0) < ttl_h() * 3600)),
                 key=lambda d: float(d.get("at") or 0))
    n = 0
    for doc in old[:max(0, limit)]:
        key = doc["key"]
        with _LOCK:
            if key in _BUSY:
                continue
            _BUSY.add(key)
        try:
            res = build_for(doc.get("lang") or "", doc.get("repo") or "", doc.get("to") or None, doc.get("team") or "")
            if isinstance(res, dict) and "error" not in res:
                _write(cache_dir, key, doc.get("lang") or "", doc.get("repo") or "", doc.get("to") or None,
                       doc.get("team") or "", res, time.time())
                n += 1
        except Exception as e:  # noqa: BLE001
            msg = str(e)[:200]
            log.warning(f"⚠️ upgrade önbelleği ısıtılamadı ({key}): {msg}")
            if "değil" in msg or "Desteklenmeyen" in msg:       # repo silinmiş / dili değişmiş: kayıt atılır
                try:
                    _path(cache_dir, key).unlink()
                except OSError:
                    pass
        finally:
            with _LOCK:
                _BUSY.discard(key)
    return n


def start_warmer(build_for, *, interval_min=WARM_MIN, sleep=None, stop_event: threading.Event | None = None):
    """Süreç başına tek daemon thread: `interval_min`'de bir süresi dolan kayıtları yeniler (ilk tur bir aralık sonra).
    Önbellek kapalıyken tur boş geçer."""
    global _WARMER
    with _LOCK:
        if _WARMER is not None and _WARMER.is_alive():
            return _WARMER
        sleep = sleep or time.sleep
        stop_event = stop_event or threading.Event()

        def loop():
            while not stop_event.is_set():
                try:
                    v = interval_min() if callable(interval_min) else interval_min
                    sleep(max(5, int(v or WARM_MIN)) * 60)
                except Exception:  # noqa: BLE001
                    sleep(WARM_MIN * 60)
                if stop_event.is_set():
                    break
                if not enabled():
                    continue
                try:
                    n = warm_once(build_for)
                    if n:
                        log.info(f"⬆️ upgrade önbelleği: {n} repo taraması yenilendi")
                except Exception as e:  # noqa: BLE001
                    log.warning(f"⚠️ upgrade önbellek ısıtıcısı hata verdi: {str(e)[:300]}")

        _WARMER = threading.Thread(target=loop, daemon=True, name="upgrade-cache-warmer")
        _WARMER.start()
        return _WARMER
