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
    "WI_COMMENTS": 4000,   # WI'daki insan yorumlari (ayri endpoint, alanlarda yok)
    # PR thread'leri (insan + onceki Tempo incelemesi + baska bot) — pr_review. 5000'di: #46052'de
    # baska bir aracin 30 thread'i vardi; once insan thread'leri secilir, sonra acik bot bulgulari.
    "PR_DISCUSSION": 8000,
    # Insan PR'i incelemesi (pr_review) — #45776: 34 dosyanin 22'si hic girmedi (12 dosya siniri).
    "PR_FILES": 120000,      # tum degisen dosyalar (oncelik sirasi), ~30K token
    "REPO_RULES": 12000,     # hedef reponun CLAUDE.md/AGENTS.md'si, ilgili bolumler
    "BASE_CONTRACTS": 16000, # PR'in extends/implements ettigi, degistirmedigi siniflar (hedef dal)
    "PR_BUILD": 3000,        # PR build hata ozeti
    "OTHER_REVIEW": 24000,   # PR'daki baska inceleme aracinin bulgulari (16000: #46052 33 bulgunun 17'si girmedi)
    # Kalite dogrudan akisinda mimara giden Task metni. 3000'di: #249'un 64 dosyalik Task'inin
    # 17 dosyasi gorundu, plan 9 dosyada kaldi. ~16K char Task ≈ 4K token (Sonnet'te kurus).
    "QUALITY_TASK_TEXT": 20000,
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
            # Bas'i tut: en baglayici icerik (WI, kickoff, requirements, kabul
            # kriterleri) _build_step_context'te basta; kuyruk daha dusuk degerli.
            text = text[:warn]
            log.warning(f"  ✂️ context[{label}] ilk {warn} char'a kirpildi")
    return text
