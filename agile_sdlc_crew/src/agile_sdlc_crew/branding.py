"""Urun kimligi sabitleri (Tempo).

Gorunen ad ve imza satirlari buradan okunur; Python paket adi, adim anahtarlari
ve CREW_* env degiskenleri degismez.
"""

PRODUCT_NAME = "Tempo"
PRODUCT_TAGLINE = "Sprintin ritmi"
PRODUCT_SIGNATURE = f"{PRODUCT_NAME} · {PRODUCT_TAGLINE}"

# WI / PR yorumlarinda botun kendi yorumunu tanimak icin (eski ad dahil).
BOT_COMMENT_MARKERS = ("Agile SDLC Crew", f"*{PRODUCT_NAME}")

# Imza her zaman metnin SONUNDA duruyor; bu kadar kuyruga bakmak yeterli.
_SIGNATURE_TAIL = 400


def is_bot_comment(content: str) -> bool:
    """Yorum metni pipeline'in kendi imzasini tasiyor mu?

    Azure DevOps yorumu markdown olarak alip HTML olarak geri veriyor:
    yazdigimiz `*Tempo — Hazırlık Kapısı*` disariya
    `<em>Tempo — Hazırlık Kapısı</em>` olarak donuyor, yani YILDIZLAR KAYBOLUYOR.
    Marker `*Tempo` (yildizli) oldugu icin Tempo yeniden adlandirmasindan beri
    kendi WI/PR yorumlarimizin HICBIRI taninmiyordu — yalnizca eski
    `Agile SDLC Crew` imzali yorumlar (duz ad, yildiz gerekmiyor) tutuyordu.

    2026-09-28'de gorunur oldu: WI yorumlarini gereksinim kaynagi yapinca
    (`wi_comments_block`) hazirlik kapisinin kendi "su detaylar eksik" yorumu
    INSAN yorumu sayilip bir sonraki degerlendirmeye girdi — bot kendi retini
    gereksinim diye okuyor. WI #73061'de olculdu: 1868 karakterlik kendi
    yorumumuz insan yorumu kumesine eklendi (1687 → 3595).

    Cozum: etiketler soyulup imza KUYRUKTA aranir. Kuyruk sarti yanlis
    pozitifi engelliyor — "Tempo" kelimesini metin icinde gecen bir insan
    yorumu botmus gibi dusurulmesin."""
    import re as _re_bc
    text = content or ""
    if any(m in text for m in BOT_COMMENT_MARKERS):
        return True
    plain = _re_bc.sub(r"<[^>]+>", " ", text)
    plain = _re_bc.sub(r"\s+", " ", plain).strip()
    tail = plain[-_SIGNATURE_TAIL:]
    return any(f"{PRODUCT_NAME} {d}" in tail for d in ("—", "-", "·"))
