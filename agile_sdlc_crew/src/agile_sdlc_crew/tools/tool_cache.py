"""Tool call cache — ayni tool + ayni argumanlarla tekrarli cagrilari engelle.

Agent'lar bazen ayni tool'u ayni parametrelerle defalarca cagirabilir.
Sonuc degismeyecegi icin:
- 1. cagri: tool calistirilir, sonuc cache'lenir
- 2. cagri: [Cache] prefix ile cache'ten dondurulur
- 3+ cagri: [UYARI] ile agent baska sey denemeye tesvik edilir
"""

import hashlib
import json
import logging

log = logging.getLogger("pipeline")


def _hash_args(args: tuple, kwargs: dict) -> str:
    """Argumanlari stabil sekilde hash'le."""
    try:
        payload = json.dumps(
            {"args": list(args), "kwargs": kwargs},
            sort_keys=True,
            default=str,
        )
    except Exception:
        payload = repr((args, sorted(kwargs.items())))
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]


class CachedToolMixin:
    """BaseTool alt siniflarina cache + limit mekanizmasi ekler.
    _run_cached metodunu override et, mixin tool'un _run'ini sarar."""

    # Class-level (paylasimli) state — tum tool instance'lari arasinda
    _cache: dict = {}
    _call_count: dict = {}

    def _cached_wrap(self, original_run, *args, **kwargs):
        """Orijinal _run metodunu sar: cache + limit kontrolu.
        - 1. cagri: calistir
        - 2-3. cagri: cache'den, uyari yok
        - 4. cagri: uyari ile cache
        - 5+ cagri: HARD BLOCK — agent'a cevap yok, baska yaklasim dene"""
        key = (self.__class__.__name__, _hash_args(args, kwargs))
        count = self._call_count.get(key, 0) + 1
        self._call_count[key] = count

        if key in self._cache:
            cached = self._cache[key]
            if count >= 5:
                log.warning(f"  Tool BLOCKED: {self.__class__.__name__} {count}x ayni argumanla — hard block")
                return (
                    f"🛑 BLOKE: Bu tool'u bu argumanlarla {count} kez cagirdin. "
                    f"Ayni sonucu aliyorsun. DUR ve dusun: farkli bir sorgu dene "
                    f"veya elindeki bilgiyle kararini ver. BU TOOL'U AYNI ARGUMANLARLA "
                    f"TEKRAR CAGIRMA — cevap donmeyecek."
                )
            if count >= 4:
                log.warning(f"  Tool limit: {self.__class__.__name__} {count}x ayni argumanla")
                return (
                    f"[UYARI: {count}. kez ayni argumanla cagirdin. Sonuc AYNI kalacak. "
                    f"Farkli bir yaklasim dene.]\n\n{cached}"
                )
            return f"[Cache, {count}. cagri]\n{cached}"

        # Ilk cagri — calistir ve cache'e yaz
        result = original_run(*args, **kwargs)
        self._cache[key] = result
        return result


def reset_tool_cache():
    """Pipeline basina cache ve sayaclari sifirla."""
    CachedToolMixin._cache.clear()
    CachedToolMixin._call_count.clear()
    log.info("  Tool cache sifirlandi")


def get_tool_stats() -> dict:
    """Cache istatistiklerini dondur."""
    return {
        "cache_size": len(CachedToolMixin._cache),
        "total_calls": sum(CachedToolMixin._call_count.values()),
        "duplicate_calls": sum(c - 1 for c in CachedToolMixin._call_count.values() if c > 1),
    }
