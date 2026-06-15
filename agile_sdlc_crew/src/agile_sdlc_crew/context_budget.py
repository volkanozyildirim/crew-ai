"""Context boyut politikasi + gozlemlenebilirlik — deterministik (LLM cagrisi yok).

Named cap'ler env ile override edilir: CREW_CTX_<NAME>. measure() her adimda
ajana giden context boyutunu pipeline log'una yazar ve CREW_CTX_TOTAL_WARN
esigini asarsa uyarir (CREW_CTX_HARD_TRUNCATE=1 ise son N char'a kirpar).
"""

import logging
import os

log = logging.getLogger("pipeline")

# char cinsinden (PLAN_CHANGES adet cinsinden). Default'lar mevcut
# magic-number'larla ayni — sikilastirma env ile opsiyonel.
_DEFAULTS = {
    "KICKOFF": 4000,        # technical_design'a tasinan kickoff
    "KICKOFF_QA": 2500,     # test/uat'a tasinan kickoff
    "KICKOFF_REVIEW": 2000, # review'a tasinan kickoff
    "REQUIREMENTS": 3000,
    "REVIEW": 2500,
    "TEST": 2500,
    "UAT": 2500,
    "PLAN_CHANGES": 10,     # adet
    "SIMILAR": 200,         # benzer is icerik char
    "TOTAL_WARN": 24000,    # assemble edilen context icin uyari esigi
}


def cap(name: str) -> int:
    """Named cap degerini dondur (env CREW_CTX_<NAME> override eder)."""
    default = _DEFAULTS[name]
    raw = os.environ.get(f"CREW_CTX_{name}")
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def measure(label: str, text: str) -> str:
    """Context boyutunu logla; esik asilirsa uyar (ve toggle ile kirp)."""
    text = text or ""
    n = len(text)
    log.info(f"  📏 context[{label}]: {n} char ≈{n // 4} tok")
    warn = cap("TOTAL_WARN")
    if n > warn:
        log.warning(f"  ⚠️ context[{label}] {n} char > {warn} esigini asti")
        if os.environ.get("CREW_CTX_HARD_TRUNCATE", "0") == "1":
            text = text[-warn:]
            log.warning(f"  ✂️ context[{label}] son {warn} char'a kirpildi")
    return text
