"""CrewAI Agent Skills — pipeline ajanlarina baglanan dosya tabanli beceriler.

`knowledge/` ile farki **kademeli acilim** (progressive disclosure): knowledge
dosyasi agent'in backstory'sine (ya da RAG moduna) butunuyle girer ve her
cagrida token yazar. Bir skill ise dizindir — `SKILL.md` govdesi yuklenir,
`references/` altindaki derin icerik ajanin ihtiyac duydugunda actigi dosyalar
olarak katalogda durur. Boylece "PHP reposu inceliyorum" diyen ajan yalnizca
`php.md`'yi acar, Go/TS/Python referanslarini hic okumaz.

Yerlesim:

    skills/
      code-review/
        SKILL.md              # frontmatter + politika govdesi (hep yuklu)
        references/*.md       # dile ozgu derinlik (ihtiyac aninda)

Kullanim (crew.py):

    Agent(..., **skill_kwargs("code-review"))

Skill bulunamazsa ya da `CREW_AGENT_SKILLS=0` ise bos sozluk doner — ajan
eskisi gibi calisir, cagiran yeri degistirmek gerekmez.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("pipeline")

_SKILLS_DIR = Path(__file__).parent


def enabled() -> bool:
    """`CREW_AGENT_SKILLS` kapaliysa hicbir skill baglanmaz."""
    try:
        from agile_sdlc_crew import pipeline_config
        return bool(pipeline_config.get("CREW_AGENT_SKILLS"))
    except Exception:  # noqa: BLE001 — knob kayitli degil / yaml okunamadi
        import os
        return os.environ.get("CREW_AGENT_SKILLS", "1") not in ("0", "false", "False", "")


def skill_path(name: str) -> Path | None:
    """Skill dizininin yolu. `SKILL.md` yoksa None."""
    path = _SKILLS_DIR / name
    return path if (path / "SKILL.md").is_file() else None


def available() -> list[str]:
    """Paketteki skill adlari (SKILL.md iceren dizinler)."""
    return sorted(
        p.name for p in _SKILLS_DIR.iterdir()
        if p.is_dir() and (p / "SKILL.md").is_file()
    )


# `knowledge.detect_repo_type` ciktisi -> references/ dosyasi.
LANGUAGE_REFERENCES = {
    "php": "php",
    "butterfly": "php",
    "laravel": "php",
    "go": "go",
    "gin": "go",
    "nextjs": "typescript",
    "react": "typescript",
    "vue": "typescript",
    "javascript": "typescript",
    "typescript": "typescript",
    "python": "python",
}


def reference(skill_name: str, ref: str) -> str:
    """`skills/<skill>/references/<ref>.md` icerigi; yoksa bos string."""
    path = skill_path(skill_name)
    if path is None:
        return ""
    f = path / "references" / f"{ref}.md"
    return f.read_text(encoding="utf-8", errors="replace") if f.is_file() else ""


def reference_for(skill_name: str, language: str) -> tuple[str, str]:
    """Repo diline karsilik gelen referansi dondur: (ref_adi, icerik).

    Neden ajan kendisi acmiyor: `code_reviewer` claude_cli uzerinde kosuyor ve
    CrewAI'in Python arac dongusu o surece kopru kuramiyor (bkz. CLAUDE.md).
    Pipeline repo dilini `detect_repo_type` ile zaten kesin biliyor, dolayisiyla
    dogru referansi enjekte etmek hem daha ucuz hem daha guvenilir: bes
    referansin yalnizca biri prompt'a girer.

    Dil bilinmiyorsa ("unknown" vb.) bos doner — yanlis dilin kurallarini
    enjekte etmektense hic etmemek yeglenir.
    """
    ref = LANGUAGE_REFERENCES.get((language or "").strip().lower())
    if not ref:
        return "", ""
    return ref, reference(skill_name, ref)


def review_context(repo_name: str) -> str:
    """`code-review` skill'inin repoya uyan dil referansi + Sonar kurallari.

    Skill'in GOVDESI (kapsam disiplini, SOLID, guvenlik, Sonar politikasi)
    ajanin sistem prompt'una CrewAI tarafindan zaten giriyor; burada uretilen
    sey **derinlik**: bes dil referansindan yalnizca repoya uyan biri, arti
    dilden bagimsiz Sonar kural listesi.

    Neden ajan referansi kendi acmiyor: code_reviewer claude_cli uzerinde
    kosuyor ve CrewAI'in Python arac dongusu o surece kopru kuramiyor
    (bkz. CLAUDE.md). Repo dilini `detect_repo_type` ile kesin bildigimiz icin
    dogru referansi enjekte etmek hem daha ucuz hem daha guvenilir.

    Hem pipeline'daki inceleme adimi (flow.step8) hem insan PR incelemesi
    (pr_review) bunu cagirir. Dil cozulemezse dil referansi eklenmez —
    yanlis dilin kurallarini vermektense hic vermemek yeglenir.
    """
    if not enabled():
        return ""
    repo = (repo_name or "").strip()
    if not repo:
        return ""

    try:
        from agile_sdlc_crew.knowledge import detect_repo_type
        language = detect_repo_type(repo)
    except Exception:  # noqa: BLE001 — klon yoksa dil bilinmez
        language = "unknown"

    ref_name, ref_body = reference_for("code-review", language)
    sonar = reference("code-review", "sonarqube")
    if not ref_body and not sonar:
        return ""

    parts = []
    if ref_body:
        parts.append(
            f"\n# Inceleme Referansi — {ref_name} "
            f"({repo} reposu {language} olarak algilandi)\n{ref_body}"
        )
    else:
        log.info(f"Review skill: {repo} dili cozulemedi ({language}) — dil referansi eklenmedi")
    if sonar:
        parts.append(f"\n# Inceleme Referansi — SonarQube\n{sonar}")
    return "\n".join(parts)


def skill_kwargs(*names: str) -> dict:
    """Agent(**skill_kwargs("code-review")) icin `skills=[...]` sozlugu.

    CrewAI'a **onceden yuklenmis Skill nesneleri** verilir, dizin yolu degil:
    `load_skills` bir Path'i "arama dizini" sayip altindaki TUM skill'leri
    yukler, yani `skills/` yolunu gecmek her ajana her skill'i baglardi. Ada
    gore secip `activate_skill` ile INSTRUCTIONS seviyesine cikariyoruz —
    govde hep yuklu olsun (politika her incelemede gecerli), `references/`
    altindaki derinlik ajanin talebine kalsin.

    Bulunamayan skill sessizce atlanir (log'lanir): eksik bir dosya yuzunden
    pipeline'in patlamasindansa skill'siz calismasi yeglenir.
    """
    if not enabled():
        return {}

    wanted = {n for n in names}
    missing = {n for n in wanted if skill_path(n) is None}
    for name in sorted(missing):
        log.warning(f"Skill bulunamadi, atlaniyor: {name}")
    wanted -= missing
    if not wanted:
        return {}

    try:
        from crewai.skills import discover_skills
        from crewai.skills.loader import load_resources

        found = [s for s in discover_skills(_SKILLS_DIR) if s.frontmatter.name in wanted]
        # RESOURCES (3): govde + "Available Resources" katalogu prompt'a girer;
        # ajan hangi referanslarin var oldugunu bilir. Dosyayi kendisi ACAMAZ
        # (bkz. reference_for) ama katalog, enjekte edilen referansi baglama
        # oturtur.
        loaded = [load_resources(s) for s in found]
    except Exception as e:  # noqa: BLE001 — skill yuklenemezse ajan skill'siz kosar
        log.warning(f"Skill yuklenemedi ({', '.join(sorted(wanted))}): {e}")
        return {}

    got = {s.frontmatter.name for s in loaded}
    for name in sorted(wanted - got):
        log.warning(f"Skill cozumlenemedi, atlaniyor: {name}")
    return {"skills": loaded} if loaded else {}
