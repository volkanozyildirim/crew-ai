"""Kickoff agent'ları için per-section üretim planları.

Sorun: Stream top-down + sınırlı çıkış bütçesi (Claude CLI default effort'ta
Sonnet) → input büyüdükçe (Architect/Developer/SM her biri bir önceki agent'ın
çıktısını da bağlama alıyor) output bütçesi daralıyor → son bölümler kesiliyor.

Çözüm: Architect/Developer/SM her biri tek LLM çağrısı yerine N mini-çağrı
ile üretsin. Her sub-call yalnız tek bölüm üretir (300-800 char hedef);
truncation fiziksel olarak imkansız. Bölümler render order'da concat'lenip
tam agent çıktısı oluşturulur. Whole-output yine Haiku grader'a verilir
(cross-section coherence için).

BA dokunulmuyor; zaten tek çağrıda 9/10 alıyor.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SectionSpec:
    key: str                # 'repo_modul' — section'ı tanımlayan kararlı id
    label: str              # 'Tahmini Etkilenen Repo/Modül' — UI/log için
    instruction: str        # bu bölüm için spesifik ne yazılacağı talimatı
    expected_template: str  # çıktı format şablonu
    target_chars: int       # ~hedef uzunluk (hint, hard cap değil)


# ── Architect (kickoff_arch_task) ─────────────────────────────────────────

_ARCH_SECTIONS: list[SectionSpec] = [
    SectionSpec(
        key="repo_modul",
        label="Tahmini Etkilenen Repo/Modül",
        instruction=(
            "HEDEF REPO BAĞLAMI bloğunda repo adı + REPO_SUMMARY.md + DOSYA "
            "YAPISI verilmiş. **browse_repo aracını** kullanarak değişikliğin "
            "yapılacağı modül/dosyaya ulaş ve dosya yolunu KANITLI olarak "
            "belirt (ör. 'stock-api: pkg/handlers/inventory.go'). Eğer HEDEF "
            "REPO bağlamı boş ise (yalnızca o zaman) 'Belirsiz' yaz. "
            "Repo adı varken 'Belirsiz' yazmak KABUL EDİLMEZ."
        ),
        expected_template=(
            "**Tahmini Etkilenen Repo/Modül:** [repo_adi:dosya/path]\n"
            "- [ilgili modül/dosya 1 — neden etkileniyor, tek satır]\n"
            "- [ilgili modül/dosya 2 — opsiyonel]"
        ),
        target_chars=350,
    ),
    SectionSpec(
        key="kapsam_siniri",
        label="Kapsam Sınırı Önerisi",
        instruction=(
            "Bu WI'nin kapsamı nerede biter? Hangi değişiklik scope-creep "
            "sayılır, hangisi scope içinde? TEK cümlede net sınır çek."
        ),
        expected_template="**Kapsam Sınırı Önerisi:** [tek cümle, net sınır]",
        target_chars=200,
    ),
    SectionSpec(
        key="entegrasyon_riskleri",
        label="Entegrasyon Riskleri",
        instruction=(
            "Bu değişikliğin etkileyebileceği 2-3 entegrasyon riskini listele. "
            "Her risk: hangi sistem etkilenecek ve ne kırılabilir."
        ),
        expected_template=(
            "**Entegrasyon Riskleri:**\n"
            "- [risk 1 — etkilenen sistem + ne kırılabilir]\n"
            "- [risk 2 — ...]\n"
            "- [risk 3 opsiyonel]"
        ),
        target_chars=400,
    ),
    SectionSpec(
        key="kritik_sorular",
        label="3 Kritik Soru/Endişe",
        instruction=(
            "Team Lead (Can) olarak en çok endişelendiren 3 noktayı yaz: "
            "(a) entegrasyon (b) ekip/bağımlılık (c) scope. Her madde tek cümle."
        ),
        expected_template=(
            "**3 Kritik Soru/Endişe:**\n"
            "1. [entegrasyon — tek cümle]\n"
            "2. [koordinasyon — tek cümle]\n"
            "3. [scope — tek cümle]"
        ),
        target_chars=400,
    ),
    SectionSpec(
        key="deniz_yaniti",
        label="Deniz'in Endişelerine Yanıt",
        instruction=(
            "Deniz'in (BA) açık sorularına/endişelerine Team Lead gözünden TEK "
            "cümle ile yanıt ver. Onay/abartı ayrımını işaretle. Bu bölüm "
            "kesilirse downstream pipeline bozulmaz — yine de tamamla."
        ),
        expected_template=(
            "**Deniz'in Endişelerine Yanıt:**\n"
            "- [endişe 1]: [onay/abartı + tek cümle]\n"
            "- [endişe 2]: [onay/abartı + tek cümle]\n"
            "- [endişe 3 opsiyonel]"
        ),
        target_chars=500,
    ),
]


# ── Developer (kickoff_dev_task) ──────────────────────────────────────────

_DEV_SECTIONS: list[SectionSpec] = [
    SectionSpec(
        key="karmasiklik",
        label="Karmaşıklık",
        instruction=(
            "Bu değişikliğin karmaşıklığını puanla: Düşük | Orta | Yüksek. "
            "SADECE bu 3 kelimeden BIRINI seç — parantez/sayı/skala (örn. "
            "'3/5') YASAK. Ek olarak TEK cümle gerekçe."
        ),
        expected_template="**Karmaşıklık:** [Düşük | Orta | Yüksek] — [tek cümle gerekçe]",
        target_chars=150,
    ),
    SectionSpec(
        key="kod_noktalari",
        label="Dikkat Edilmesi Gereken Kod Noktaları",
        instruction=(
            "**browse_repo / search_code araçlarını çağırarak** repo içinde "
            "ilgili dosya/fonksiyonları BUL ve gerçek path:sembol olarak "
            "listele. 'log writer', 'helper', 'exception handler' gibi soyut "
            "isim YASAK; her madde tam `dosya/path.uzanti:fonksiyon_adi` "
            "formatında olsun. Repo dili HEDEF REPO BAĞLAMI'ndan görülür "
            "(PHP/Go/Python vs.) — 'PHP veya Go' yazma, KESİN dilde örnek ver."
        ),
        expected_template=(
            "**Dikkat Edilmesi Gereken Kod Noktaları:**\n"
            "- [tam/path/dosya.uzanti:funcName] — [neden, tek cümle]\n"
            "- [tam/path/dosya.uzanti:funcName] — [neden]\n"
            "- [tam/path/dosya.uzanti:funcName] — [neden]"
        ),
        target_chars=500,
    ),
    SectionSpec(
        key="teknik_yaklasim",
        label="Önerilen Teknik Yaklaşım",
        instruction=(
            "3-5 maddelik somut uygulama adımı. Her adım TEK cümle. 'Helper "
            "kullan' yuvarlak değil — 'X dosyasında Y fonksiyonunu Z gibi "
            "değiştir' diye somut. Repo dilini HEDEF REPO BAĞLAMI'ndan teyit "
            "et; 'PHP veya Go' gibi belirsiz dil seçimi YASAK, kesin dilde "
            "kod örneği ver."
        ),
        expected_template=(
            "**Önerilen Teknik Yaklaşım:**\n"
            "1. [adım — tek cümle, dosya:fonksiyon referanslı]\n"
            "2. [adım — tek cümle]\n"
            "3. [adım — tek cümle]\n"
            "4. [adım — opsiyonel]\n"
            "5. [adım — opsiyonel]"
        ),
        target_chars=700,
    ),
    SectionSpec(
        key="kritik_sorular",
        label="3 Kritik Soru/Endişe",
        instruction=(
            "Developer olarak 3 kritik endişe: (a) kod uyumu / yeniden yazma "
            "riski (b) zorunlu edge case (c) teknik borç riski. Her madde "
            "tek cümle."
        ),
        expected_template=(
            "**3 Kritik Soru/Endişe:**\n"
            "1. [kod uyumu — tek cümle]\n"
            "2. [edge case — tek cümle]\n"
            "3. [teknik borç — tek cümle]"
        ),
        target_chars=400,
    ),
    SectionSpec(
        key="denizcan_yaniti",
        label="Deniz ve Can'a Yanıt",
        instruction=(
            "Deniz ve Can'ın endişelerine Developer gözünden TEK cümle ile "
            "yanıt. Teknik açıdan doğrulanan / abartılı / daha riskli "
            "bulduğunu işaretle. Bu bölüm kesilirse downstream bozulmaz."
        ),
        expected_template=(
            "**Deniz ve Can'a Yanıt:**\n"
            "- [Deniz/Can'ın noktası 1]: [Developer yorumu — tek cümle]\n"
            "- [nokta 2]: [yorum]\n"
            "- [nokta 3 opsiyonel]"
        ),
        target_chars=500,
    ),
]


# ── SM Tutanak (kickoff_sm_close_task) ────────────────────────────────────
# ÜRETİM SIRASI: 4→3→2→1 (kritik fields önce, prose en son)
# RENDER SIRASI: 1→2→3→4 (sunumda özet üstte)

_SM_SECTIONS: list[SectionSpec] = [
    SectionSpec(
        key="bolum4_backlog",
        label="Bölüm 4 — Backlog Adayları",
        instruction=(
            "Backlog Adayları bölümünü üret. Bu downstream pipeline'ın "
            "(test planning + UAT + technical design) zorunlu girdisi. "
            "Nihai ölçülebilir Kabul Kriterleri (checkbox listesi), Edge "
            "Case'ler (3-5 madde), Açık Görevler/Hikayeler (varsa). "
            "Tüm uzmanların (Deniz/Barış/Can/Ece) bulgularını harmanla."
        ),
        expected_template=(
            "### 4. Backlog Adayları\n\n"
            "**Nihai Kabul Kriterleri:**\n"
            "- [ ] AC1: [ölçülebilir kriter]\n"
            "- [ ] AC2: [ölçülebilir kriter]\n"
            "- [ ] AC3: [ölçülebilir kriter]\n\n"
            "**Edge Case'ler:**\n"
            "- [edge case 1]\n"
            "- [edge case 2]\n"
            "- [edge case 3]\n\n"
            "**Açık Görevler:**\n"
            "- [görev/hikaye veya 'Yok']"
        ),
        target_chars=900,
    ),
    SectionSpec(
        key="bolum3_risk_tablosu",
        label="Bölüm 3 — Kritik Risk Tablosu",
        instruction=(
            "Tüm ekibin (Deniz/Barış/Can/Ece) ortaya koyduğu riskleri "
            "tablo halinde derle. 3-5 satır; her satır: risk | etki "
            "(Y/O/D) | önerilen önlem."
        ),
        expected_template=(
            "### 3. Kritik Risk Tablosu\n\n"
            "| Risk | Etki | Önerilen Önlem |\n"
            "|---|---|---|\n"
            "| [risk 1] | Yüksek/Orta/Düşük | [önlem] |\n"
            "| [risk 2] | ... | ... |\n"
            "| [risk 3] | ... | ... |"
        ),
        target_chars=700,
    ),
    SectionSpec(
        key="bolum2_disiplin_analizi",
        label="Bölüm 2 — Disiplin Bazlı Analiz",
        instruction=(
            "Her uzmanın ana bulgusunu 1-2 satır halinde özetle. Tek cümle "
            "tercih et."
        ),
        expected_template=(
            "### 2. Disiplin Bazlı Analiz\n\n"
            "- **Analiz (Deniz):** [ana bulgu]\n"
            "- **Yazılım (Barış):** [teknik]\n"
            "- **Yönetim (Can):** [entegrasyon]\n"
            "- **Test (Ece):** [risk]"
        ),
        target_chars=500,
    ),
    SectionSpec(
        key="bolum1_ozet",
        label="Bölüm 1 — Toplantı Özeti",
        instruction=(
            "Toplantı özeti: genel uygulanabilirlik değerlendirmesi, ekip "
            "konsensüsü, kapsam sınırı. 2-3 cümle yeter."
        ),
        expected_template="### 1. Toplantı Özeti\n\n[2-3 cümle özet]",
        target_chars=400,
    ),
]


# agent_key → ordered list of sections (üretim sırası — context bağımlılığı)
SECTION_PLANS: dict[str, list[SectionSpec]] = {
    "kickoff_arch_task": _ARCH_SECTIONS,
    "kickoff_dev_task": _DEV_SECTIONS,
    "kickoff_sm_close_task": _SM_SECTIONS,
}


# agent_key → render order (sunum sırası, üretim sırasından farklı olabilir)
RENDER_ORDER: dict[str, list[str]] = {
    "kickoff_arch_task": [s.key for s in _ARCH_SECTIONS],   # üretim = sunum
    "kickoff_dev_task": [s.key for s in _DEV_SECTIONS],     # üretim = sunum
    "kickoff_sm_close_task": [                              # ÜRETİM 4→1, SUNUM 1→4
        "bolum1_ozet",
        "bolum2_disiplin_analizi",
        "bolum3_risk_tablosu",
        "bolum4_backlog",
    ],
}


# Section sonrası eklenen kapanış satırları (sm tutanak için "tüm takım hazır")
CLOSING_SUFFIX: dict[str, str] = {
    "kickoff_sm_close_task": "\n\n*Tüm takım #{work_item_id} iş kalemine hazır.*",
}


# Section header'lar (sunumda render edilmiş çıktıyı zenginleştirir)
PRESENTATION_HEADER: dict[str, str] = {
    "kickoff_arch_task": "## Team Lead (Can) — Perspektif\n\n",
    "kickoff_dev_task": "## Developer (Barış) — Perspektif\n\n",
    "kickoff_sm_close_task": "## Kritik Tasarım Gözden Geçirme — Tutanak\n\n**İş Kalemi:** #{work_item_id}\n\n",
}


def _split_repo_context(wi_context: str) -> tuple[str, str]:
    """previous_context'ten HEDEF REPO + DOSYA YAPISI bloklarini ayikla.

    flow.py kickoff context'i su sirayla insa eder:
        [requirements/AC/BA] + '\n\n# HEDEF REPO: ...' + '\n\n# ... DOSYA YAPISI ...'
    Naive trunc bu kritik bilgiyi keser. Buradan ayri parametre olarak gecirilir
    → sub-call promptunda HER ZAMAN gorunur (kesilmez).

    Returns (general_context, repo_block).
    """
    if not wi_context:
        return "", ""
    idx = wi_context.find("# HEDEF REPO")
    if idx < 0:
        return wi_context, ""
    general = wi_context[:idx].rstrip()
    repo_block = wi_context[idx:].strip()
    return general, repo_block


def build_section_task_description(
    section: SectionSpec,
    persona_intro: str,
    wi_context: str,
    prior_agents_summary: str,
    own_prior_sections: dict[str, str],
) -> str:
    """Tek-bölüm task description'ı oluşturur.

    Section bazlı LLM çağrısı için optimize: persona kısa, bağlam kısaltılmış,
    yalnız bu bölümün talimatı ve formatı. Markdown kapanış disiplini her
    sub-call'da hatırlatılır.

    HEDEF REPO bilgisi context içinde gömülü kalmamak için ayrı bloga
    çıkarılır ve trunc'a tabi olmaz; her sub-call onu görsün.
    """
    general_ctx, repo_block = _split_repo_context(wi_context)

    own_prior_text = ""
    if own_prior_sections:
        own_prior_text = "\n\n# BU AGENT İÇİN DAHA ÖNCE ÜRETTİĞİN BÖLÜMLER (referans için, tekrar üretme):\n"
        for k, v in own_prior_sections.items():
            if v:
                own_prior_text += f"\n{v}\n"

    repo_section = ""
    if repo_block:
        # HEDEF REPO + DOSYA YAPISI'ni TAM koru (kesme); kritik teknik karar
        # icin gerekli ham bilgi
        repo_section = (
            f"\n\n# HEDEF REPO BAĞLAMI (kesme — bu blok aynen okumalıyız)\n"
            f"{repo_block[:5000]}"
        )

    return (
        f"{persona_intro.strip()}\n\n"
        f"# İŞ KALEMİ BAĞLAMI (özet)\n"
        f"{(general_ctx or '')[:2000]}"
        f"{repo_section}\n\n"
        f"# DİĞER UZMANLARIN BU TOPLANTIDAKİ KATKILARI (özet)\n"
        f"{(prior_agents_summary or '(yok)')[:1800]}"
        f"{own_prior_text}\n\n"
        f"# ÜRETMEN GEREKEN TEK BÖLÜM: {section.label}\n\n"
        f"## Talimat\n{section.instruction}\n\n"
        f"## Çıktı Formatı (aynen kullan)\n{section.expected_template}\n\n"
        f"# KURALLAR\n"
        f"- SADECE bu bölümü üret. Diğer bölümlere geçme, başlık ekleme.\n"
        f"- Hedef uzunluk ~{section.target_chars} karakter — aşma.\n"
        f"- Tüm `**bold**` etiketleri eşleşik kapansın.\n"
        f"- Cümlelerin tamam olsun, son karakter nokta/işaret olsun.\n"
        f"- Dolgu/açılış cümlesi yasak ('Genel olarak…', 'Bu bağlamda…').\n"
        f"- 'Belirsiz' ya da 'muhtemelen' yazmadan ÖNCE HEDEF REPO BAĞLAMI'nı tara; tool varsa kullan."
    )


def summarize_agent_output(text: str, max_chars: int = 800) -> str:
    """Önceki agent'ın çıktısını sub-call'a vermek için kısaltır.

    Naive: ilk N karakter + truncation işareti. Daha akıllı bir özetleme
    (LLM-based) gerekirse buradan değiştirilir.
    """
    if not text:
        return ""
    t = text.strip()
    if len(t) <= max_chars:
        return t
    return t[:max_chars] + "\n…(devamı kısaltıldı)"
