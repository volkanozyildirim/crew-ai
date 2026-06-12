"""CrewAI Task guardrail'leri — yapisal-cikti task'larini dogrula.

Guardrail imzasi (CrewAI 1.14.x): Callable[[TaskOutput], tuple[bool, Any]].
  - (True, deger)  → gecti; `deger` task ciktisi olur
  - (False, hata)  → kaldi; CrewAI hatayi agent'a geri besleyip retry eder
                      (Task.guardrail_max_retries, default 3)

Tasarim: guardrail SADECE dogrular, donusturmez. Basariili durumda ham metni
aynen geri verir (passthrough) — boylece flow tarafindaki mevcut parse/merge
mantigi degismeden calismaya devam eder. Guardrail birincil retry; mevcut
Python parse (architect) / lint+ollama-fix (developer) FALLBACK olarak kalir.
"""

from __future__ import annotations


def _raw_of(output) -> str:
    """TaskOutput'tan ham metni guvenle al."""
    raw = getattr(output, "raw", None)
    if raw is None:
        raw = str(output)
    return (raw or "").strip()


def architect_json_guardrail(output) -> tuple[bool, str]:
    """Architect technical_design ciktisinin parse edilebilir/gecerli JSON plan
    oldugunu dogrular. _parse_architect_output'u (4 kademeli onarim + placeholder
    guard) yeniden kullanir; basariili olursa ham metni passthrough eder."""
    from agile_sdlc_crew.main import _parse_architect_output

    raw = _raw_of(output)
    if not raw:
        return (False, "Cikti bos. Gecerli JSON teknik tasarim plani uret.")
    try:
        _parse_architect_output(raw)
    except ValueError as e:
        return (
            False,
            f"JSON plan validation hatasi: {e} "
            f"SADECE gecerli JSON uret — tool cagirma, aciklama yazma, "
            f"placeholder (timestamp/class/tablo adi somut olmali) kullanma.",
        )
    return (True, raw)


# Developer ciktisi TAM DOSYA DEGIL, kismi kod blogu olabilir (flow merge eder).
# Bu yuzden LINT YAPMA — lint Python tarafinda merge edilmis dosyaya uygulanir.
# Burada sadece yapisal/cheap kontrol: bos degil, kod-benzeri, tool-call yok.
_REFUSAL_MARKERS = (
    "uzgunum", "yapamam", "yardimci olamam", "i cannot", "i'm sorry", "as an ai",
)


def developer_code_guardrail(output) -> tuple[bool, str]:
    """Developer kod ciktisinin bos/cop/halusinasyon olmadigini dogrular.
    Lint DEGIL — yapisal kontrol (lint Python fallback'te kalir)."""
    raw = _raw_of(output)
    if len(raw) < 10:
        return (False, "Cikti bos veya cok kisa. Istenen kod blogunu uret.")
    if "<tool_call>" in raw:
        return (False, "Tool cagrisi tespit edildi. Tool cagirma — sadece kod blogu yaz.")
    low = raw.lower()
    if any(m in low for m in _REFUSAL_MARKERS) and "```" not in raw:
        return (False, "Aciklama/ret degil — istenen kod degisikligini kod olarak yaz.")
    return (True, raw)
