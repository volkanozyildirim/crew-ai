"""Kickoff cikti kalite grader'i — Haiku ile 1-10 puanlama + iyilestirme prompt'u.

Akis (bkz: crew.run_kickoff_meeting):
    1. Kickoff'taki her uzman tek-task crew olarak calistirilir.
    2. Cikti grade_output() ile Haiku uzerinden JSON puanlanir.
    3. Puan CREW_KICKOFF_GRADE_THRESHOLD altindaysa build_improvement_description()
       ile zayifliklari + onerileri iceren zenginlestirilmis prompt uretilir,
       agent ayni gorev icin yeniden cagrilir (en fazla CREW_KICKOFF_GRADE_MAX_RETRIES).

Env toggle'lari:
    CREW_KICKOFF_GRADING            (default: 1)  — 0 ise grading kapali.
    CREW_KICKOFF_GRADE_THRESHOLD    (default: 8)  — passing esigi (1-10).
    CREW_KICKOFF_GRADE_MAX_RETRIES  (default: 2)  — esige takilirsa kac kez yeniden dene.
    CREW_KICKOFF_GRADER_PROFILE     (default: kickoff_grader) — llm_profiles.yaml profili.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

log = logging.getLogger("pipeline")


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes")


def grading_enabled() -> bool:
    return _env_bool("CREW_KICKOFF_GRADING", True)


def grade_threshold() -> int:
    return max(1, min(10, _env_int("CREW_KICKOFF_GRADE_THRESHOLD", 8)))


def grade_max_retries() -> int:
    return max(0, _env_int("CREW_KICKOFF_GRADE_MAX_RETRIES", 2))


def grader_profile() -> str:
    return os.environ.get("CREW_KICKOFF_GRADER_PROFILE", "kickoff_grader")


@dataclass
class GradeResult:
    score: int  # 1-10
    weaknesses: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    reasoning: str = ""
    raw: str = ""
    skipped: bool = False  # grader unavailable / parse failed → no retry

    def passing(self, threshold: int | None = None) -> bool:
        t = threshold if threshold is not None else grade_threshold()
        return self.score >= t


_GRADER_SYSTEM = (
    "Sen bir Scrum Master kalite degerlendiricisin. Sanal odak grup toplantisindaki "
    "uzman ciktilarini 1-10 olcusu ile puanlarsin. Cikti; istenen formata uygun, "
    "spesifik (somut/sayisal/dosya bazli), kanit-dayanakli, hareketle gecirilebilir "
    "ve onceki uzman ciktilarina referansli olmalidir.\n\n"
    "TRUNCATION KURALI: Cikti yarim/kesilmis ise (cumle ortasinda biten, ** ile "
    "kapanmamis bold, beklenen format bolumleri eksik, kabul kriteri baslayip "
    "tamamlanmamis vb.) score MAKSIMUM 5 olabilir — diger kriterler ne kadar iyi "
    "olursa olsun. Eksik cikti downstream'i bozar.\n\n"
    "CEVAP FORMATI — TEK SATIR JSON. Etrafinda metin/markdown yok. ALANLARIN "
    "UZUNLUK SINIRLARI (asma):\n"
    "  - reasoning: en fazla 280 karakter, TEK cumle, mutlaka nokta ile bitsin\n"
    "  - weaknesses: en fazla 3 oge; her oge en fazla 120 karakter, nokta ile bitsin\n"
    "  - suggestions: en fazla 3 oge; her oge en fazla 120 karakter, nokta ile bitsin\n"
    "Sinira ulasacaginsa cumlelerini kisalt — ASLA yarim birakma. JSON'i tamamla."
)


def _repair_truncated_json(s: str) -> str:
    """Cut-off JSON onarmaya calisir.

    Strateji: tek-tirnak/cift-tirnak string acik kaldiysa kapat, sonra eksik
    `]` ve `}` karakterlerini dengeleyerek tamamla. Mukemmel degil ama
    Haiku'nun yarim biraktigi durumlarin cogunu kurtarir."""
    s = s.rstrip()
    # Trailing string acik mi? (escape sayilarak son " incele)
    in_str = False
    escape = False
    depth_curly = 0
    depth_square = 0
    for ch in s:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth_curly += 1
        elif ch == "}":
            depth_curly -= 1
        elif ch == "[":
            depth_square += 1
        elif ch == "]":
            depth_square -= 1
    repair = ""
    if in_str:
        repair += '"'
    # Trailing comma temizligi (basit): "...xyz", → "...xyz"
    if (s + repair).rstrip().endswith(","):
        s = s.rstrip().rstrip(",")
    repair += "]" * max(0, depth_square)
    repair += "}" * max(0, depth_curly)
    return s + repair


def _detect_truncation_heuristic(text: str) -> tuple[bool, str]:
    """Yapisal (deterministik) truncation tespiti — yalniz markdown dengesi.

    Semantik tespit grader'in isi; burada sadece kesin-kesin yarim oldugunu
    soyleyebildigimiz seyler:
      - Bos cikti
      - Kapanmamis ** (bold) — markdown bozuk
      - Kapanmamis ``` (kod blogu) — markdown bozuk

    Returns (is_truncated, reason).
    """
    if not text or not text.strip():
        return True, "bos cikti"
    body = text.rstrip()
    if body.count("**") % 2 != 0:
        return True, "kapanmamis ** (bold cift sayida degil) — markdown bozuk"
    if body.count("```") % 2 != 0:
        return True, "kapanmamis ``` (kod fence cift sayida degil) — markdown bozuk"
    return False, ""


def _grader_user_prompt(
    task_key: str,
    agent_label: str,
    description: str,
    expected_output: str,
    actual_output: str,
    work_item_context: str,
) -> str:
    return (
        f"# DEGERLENDIRILECEK GOREV\n"
        f"task_key: {task_key}\n"
        f"agent: {agent_label}\n\n"
        f"## ORIJINAL GOREV TANIMI\n{description[:3000]}\n\n"
        f"## BEKLENEN CIKTI FORMATI\n{expected_output[:1500]}\n\n"
        f"## IS BAGLAMI (ozet)\n{work_item_context[:1500]}\n\n"
        f"## DEGERLENDIRILECEK AGENT CIKTISI\n{actual_output[:6000]}\n\n"
        f"# DEGERLENDIRME KRITERLERI (her biri 1-10)\n"
        f"- spesifiklik: somut, sayisal, dosya/modul/davranis bazli mi?\n"
        f"- kapsama: beklenen formattaki TUM alanlar dolu mu?\n"
        f"- baglam_kullanimi: is kalemine ve onceki uzman ciktilarina referans var mi?\n"
        f"- aksiyon_alinabilirligi: tasarim/test/karar icin dogrudan kullanilabilir mi?\n"
        f"- netlik: kisa, dolambacli olmayan, dolgu cumlesiz?\n\n"
        f"# CIKTI (SADECE JSON)\n"
        f'{{"score": <1-10 tum kriterlerin tartilmis ortalamasi, tam sayi>, '
        f'"weaknesses": ["zayiflik 1", "zayiflik 2"], '
        f'"suggestions": ["agent\'in sonraki denemede yapmasi gereken somut adim 1", "..."], '
        f'"reasoning": "tek paragraf gerekce"}}\n'
        f"\nSadece JSON dondur."
    )


def grade_output(
    task_key: str,
    agent_label: str,
    description: str,
    expected_output: str,
    actual_output: str,
    work_item_context: str = "",
) -> GradeResult:
    """Haiku ile ciktiyi 1-10 puanla.

    Grader baglanamazsa veya cevap parse edilemezse skipped=True ile 10 doner;
    boylece retry loop'u sonsuz donmez.
    """
    if not actual_output or not actual_output.strip():
        return GradeResult(score=1, weaknesses=["bos cikti"], suggestions=["beklenen formati doldur"])

    try:
        # Lazy import: circular dependency from crew.py'a karsi
        from agile_sdlc_crew.llm.resolver import build_for_profile
        llm = build_for_profile(grader_profile())
    except Exception as e:
        log.warning(f"  Grader LLM build hatasi ({grader_profile()}): {e} — grading skip")
        return GradeResult(score=10, reasoning=f"grader unavailable: {e}", skipped=True)

    prompt = _grader_user_prompt(
        task_key, agent_label, description, expected_output, actual_output, work_item_context,
    )
    # Yapisal truncation kontrolu — markdown dengesi bozuksa grader'i bile
    # cagirmaya gerek yok, dogrudan retry tetikle.
    is_trunc, trunc_reason = _detect_truncation_heuristic(actual_output)
    if is_trunc:
        log.warning(f"  Yapisal truncation tespit: {trunc_reason} — grader atlandi")
        return GradeResult(
            score=4,
            weaknesses=[f"[STRUCTURAL] {trunc_reason}"],
            suggestions=[
                "Markdown'i kapatarak ciktiyi tam uret: ** kapatilmamis bold'lari "
                "kapat, ``` kod fence'lerini esle, beklenen format basliklarinin "
                "hepsini sonlandir."
            ],
            reasoning="Yapisal kontrol basarisiz; LLM grader'a sorulmadan retry istendi.",
        )

    try:
        raw = llm.call([
            {"role": "system", "content": _GRADER_SYSTEM},
            {"role": "user", "content": prompt},
        ])
    except Exception as e:
        log.warning(f"  Grader call hatasi: {e} — grading skip")
        return GradeResult(score=10, reasoning=f"grader call failed: {e}", skipped=True)

    return _parse_grade(raw if isinstance(raw, str) else str(raw))


def _parse_grade(raw: str) -> GradeResult:
    raw = raw or ""
    start = raw.find("{")
    if start < 0:
        log.warning(f"  Grader JSON cikti bulamadi: {raw[:200]}")
        return GradeResult(score=10, reasoning="parse failed", raw=raw, skipped=True)
    end = raw.rfind("}")
    candidate = raw[start:end + 1] if end > start else raw[start:]
    doc = None
    try:
        doc = json.loads(candidate)
    except Exception:
        repaired = _repair_truncated_json(candidate)
        try:
            doc = json.loads(repaired)
            log.warning("  Grader JSON cut idi, repair ile parse edildi")
        except Exception as e:
            log.warning(f"  Grader JSON parse hatasi: {e} — {candidate[:200]}")
            return GradeResult(score=10, reasoning=f"parse failed: {e}", raw=raw, skipped=True)

    score = doc.get("score", 10)
    try:
        score = int(round(float(score)))
    except (TypeError, ValueError):
        score = 10
    score = max(1, min(10, score))

    def _strlist(val) -> list[str]:
        if not val:
            return []
        if isinstance(val, str):
            return [val]
        if isinstance(val, list):
            return [str(x).strip() for x in val if x and str(x).strip()]
        return [str(val)]

    return GradeResult(
        score=score,
        weaknesses=_strlist(doc.get("weaknesses")),
        suggestions=_strlist(doc.get("suggestions")),
        reasoning=str(doc.get("reasoning") or "").strip(),
        raw=raw,
    )


def build_improvement_description(
    base_description: str,
    prior_output: str,
    grade: GradeResult,
    attempt: int,
    threshold: int | None = None,
) -> str:
    """Retry icin agresif terseness modunda yeniden-uretim talimati.

    Onceki ciktiyi YALNIZ son 600 karakter referans olarak ekler (input'u
    kucuk tut → output butcesini buyut). Agent'a once eksik bolumleri yazip
    sonra geri kalanini sikistirmasini emrediyoruz.
    """
    t = threshold if threshold is not None else grade_threshold()
    weaknesses = "\n".join(f"- {w}" for w in grade.weaknesses) or "- (grader belirtmedi)"
    suggestions = "\n".join(f"- {s}" for s in grade.suggestions) or "- (grader belirtmedi)"
    # Sadece son ~600 karakter (kesilme noktasi gorulsun yeterli)
    tail = (prior_output or "").rstrip()[-600:]
    return (
        f"{base_description}\n\n"
        f"# ⚠️ RETRY MODU — Deneme #{attempt} (onceki: {grade.score}/10, esik: {t})\n\n"
        f"## Zayifliklar (oncelik sirasi)\n{weaknesses}\n\n"
        f"## Yapilacaklar\n{suggestions}\n\n"
        f"## Onceki ciktinin kesilme noktasi (sadece son 600 karakter)\n"
        f"```\n...{tail}\n```\n\n"
        f"# KESIN UYGULAMA KURALLARI\n"
        f"1. TERSENESS: Cikti yarim kesilmemeli. Her bolumu 2-4 satirda topla; uzun "
        f"dolgu cumlesi YASAK. Madde/bullet > paragraf. 'Genel olarak...', "
        f"'Bu durumda...' gibi acilis cumleleri yazma.\n"
        f"2. ONCELIK: Onceki ciktinda EKSIK kalan bolumleri ONCE yaz; var olan "
        f"dolu bolumleri kisaltarak butce yarat. Dolu bolumun aynisini "
        f"tekrarlama, daha sikistirilmis hali yetsin.\n"
        f"3. FORMAT KAPATMA: Tum ** bold etiketleri eslesik kapat. Numaralandirilmis "
        f"listeyi ortada birakma. Son satir nokta ile bitsin.\n"
        f"4. SCOPE: Beklenen format alanlarinin TUMUNU eksiksiz uret. Asik olmaktansa "
        f"kisa-tam ol."
    )
