"""Urun kimligi sabitleri (Tempo).

Gorunen ad ve imza satirlari buradan okunur; Python paket adi, adim anahtarlari
ve CREW_* env degiskenleri degismez.
"""

PRODUCT_NAME = "Tempo"
PRODUCT_TAGLINE = "Sprintin ritmi"
PRODUCT_SIGNATURE = f"{PRODUCT_NAME} · {PRODUCT_TAGLINE}"

# WI / PR yorumlarinda botun kendi yorumunu tanimak icin (eski ad dahil).
BOT_COMMENT_MARKERS = ("Agile SDLC Crew", f"*{PRODUCT_NAME}")


def is_bot_comment(content: str) -> bool:
    """Yorum metni pipeline'in kendi imzasini tasiyor mu?"""
    return any(m in (content or "") for m in BOT_COMMENT_MARKERS)
