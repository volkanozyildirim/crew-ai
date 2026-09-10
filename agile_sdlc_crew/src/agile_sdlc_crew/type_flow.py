"""İş tipine göre akış + Product Owner değerlendirmesi — Scrum işlevleri, faz 5.

Bug, User Story, Task ve Spike aynı 13 adımı aynı talimatlarla koşuyordu;
`System.WorkItemType` okunuyor ama kullanılmıyordu. Bu modül:

* **Akış türü** (`flow_kind`): tip + etiket + başlıktan deterministik —
  `bug` · `story` · `task` · `spike` · `other`. Spike YALNIZCA açık işaretle
  (tip Spike/Research, etiket `spike|poc|research|araştırma`, ya da başlık
  `[Spike] …` / `Spike: …`) — çıkarımla değil; yanlış pozitif kod üretmeyen
  bir iş demek.
* **Kılavuz** (`guidance`): adım context'ine eklenen İngilizce kural bloğu
  (Bug: reproduce-first + regresyon testi zorunlu + minimal fix; Story:
  AC'ye iz, dikey dilim; Spike: kod yok, rapor var). Çıktı dili Türkçe kalır.
* **Spike raporu** (`spike_report`): mimar keşif bulguları + BA analizi →
  WI yorumu; pipeline kod adımlarını atlar (`_SpikeStop`).
* **PO değerlendirmesi** (`parse_po`, `render_po_comment`): Product Owner
  ajanının JSON çıktısını deterministik parse eder ve WI yorumuna çevirir.
  Karar (GO/HOLD) danışma niteliğinde; kapı değil.

Proje dil kuralı: ajan talimatları İngilizce, kullanıcıya görünen her şey
Türkçe.
"""

from __future__ import annotations

import json
import re

SPIKE_TYPES = {"spike", "research", "araştırma", "arastirma"}
_SPIKE_TAG_RE = re.compile(r"(^|[;,\s])(spike|poc|research|araştırma|arastirma)([;,\s]|$)", re.IGNORECASE)
_SPIKE_TITLE_RE = re.compile(r"^\s*[\[\(]?\s*(spike|poc|research|araştırma|arastirma)\s*[\]\):\-–—]", re.IGNORECASE)

KIND_TR = {"bug": "hata düzeltme", "story": "kullanıcı hikâyesi", "task": "görev",
           "spike": "araştırma (kod üretilmez)", "other": "diğer"}


def is_spike(wi_type: str, tags: str = "", title: str = "") -> bool:
    if (wi_type or "").strip().lower() in SPIKE_TYPES:
        return True
    if tags and _SPIKE_TAG_RE.search(tags.replace(";", " ; ")):
        return True
    return bool(title and _SPIKE_TITLE_RE.match(title))


def flow_kind(wi_type: str, tags: str = "", title: str = "") -> str:
    if is_spike(wi_type, tags, title):
        return "spike"
    t = (wi_type or "").strip().lower()
    if t == "bug":
        return "bug"
    if t in ("user story", "product backlog item", "feature", "improvement"):
        return "story"
    if t == "task":
        return "task"
    return "other"


_GUIDANCE = {
    "bug": (
        "# WORK ITEM TYPE GUIDANCE: Bug\n"
        "- Reproduce first: before proposing any change, state the reproduction steps, "
        "the expected vs actual behaviour and the root-cause hypothesis (in Turkish, in your output).\n"
        "- The technical plan MUST include a regression test that fails before the fix and passes after it; "
        "a plan without a test for the reported scenario is incomplete.\n"
        "- Keep the fix minimal: no refactors, renames or unrelated cleanups in the same change.\n"
        "- Reviewer: reject (CHANGES_REQUIRED) if no test covers the reported scenario.\n"
        "- UAT: verify the original failing scenario AND one adjacent scenario for side effects."
    ),
    "story": (
        "# WORK ITEM TYPE GUIDANCE: User Story\n"
        "- Keep the user-visible outcome in focus: every planned change must trace to an acceptance criterion.\n"
        "- Prefer a complete vertical slice (data → logic → interface) over partial backend-only work; "
        "if the slice cannot be completed, say so explicitly instead of delivering half.\n"
        "- Call out any user-facing text or behaviour change in Turkish so the WI owner can confirm it."
    ),
    "spike": (
        "# WORK ITEM TYPE GUIDANCE: Spike (research)\n"
        "- Do NOT write production code. The deliverable is a research report.\n"
        "- Report: findings with file/path evidence, options with trade-offs, a recommendation, "
        "open questions, and an effort estimate (story points) for the follow-up story.\n"
        "- Write the report in Turkish."
    ),
}


def guidance(kind: str) -> str:
    return _GUIDANCE.get(kind or "", "")


# ── Spike raporu ─────────────────────────────────────────────────────────

def spike_report(title: str, requirements_text: str, findings: str, *, repo_name: str = "",
                 estimate_sp: int | None = None) -> str:
    """Mimar keşfi + BA analizi → Türkçe araştırma raporu (WI yorumu)."""
    L = ["## 🔬 Araştırma Raporu (Spike)", ""]
    if title:
        L.append(f"**İş:** {title.strip()}")
    if repo_name:
        L.append(f"**İncelenen repo:** `{repo_name}`")
    if estimate_sp:
        L.append(f"**Takip işi için tahmin:** {estimate_sp} SP")
    L.append("")
    summary, oq = _ba_summary_and_questions(requirements_text)
    if summary:
        L += ["### Kapsam (iş analizi)", "", summary, ""]
    body = (findings or "").strip()
    if body:
        L += ["### Bulgular (mimar keşfi)", "", body[:6000], ""]
    else:
        L += ["### Bulgular", "", "Repo klonu bulunamadığı için kod keşfi yapılamadı; rapor iş analizine dayanır.", ""]
    if oq:
        L += ["### Açık sorular", ""] + [f"- {q}" for q in oq[:10]] + [""]
    L += ["---", "*Bu iş araştırma (spike) olarak işaretlendi: kod üretilmedi, PR açılmadı. "
          "Takip işi için bu rapor kullanılabilir.*"]
    return "\n".join(L)


def _ba_summary_and_questions(requirements_text: str) -> tuple[str, list[str]]:
    txt = requirements_text or ""
    try:
        s0, e0 = txt.find("{"), txt.rfind("}")
        d = json.loads(txt[s0:e0 + 1]) if s0 >= 0 and e0 > s0 else {}
    except Exception:
        d = {}
    if not isinstance(d, dict):
        return "", []
    parts = []
    if d.get("summary"):
        parts.append(str(d["summary"]).strip())
    frs = [f"- **{x.get('id')}** {x.get('desc')}" for x in (d.get("functional_requirements") or []) if isinstance(x, dict)]
    if frs:
        parts.append("\n".join(frs[:12]))
    oq = [str(q).strip() for q in (d.get("open_questions") or []) if str(q).strip()]
    return "\n\n".join(parts), oq


# ── PO değerlendirmesi ───────────────────────────────────────────────────

def parse_po(text: str) -> dict | None:
    """PO ajanı JSON'u → normalize dict | None (parse edilemezse)."""
    txt = text or ""
    try:
        s0, e0 = txt.find("{"), txt.rfind("}")
        if s0 < 0 or e0 <= s0:
            return None
        d = json.loads(txt[s0:e0 + 1])
    except Exception:
        return None
    if not isinstance(d, dict):
        return None

    def _i(v, lo=1, hi=10):
        try:
            return max(lo, min(hi, int(round(float(v)))))
        except (TypeError, ValueError):
            return None

    decision = str(d.get("decision") or "").strip().upper()
    if decision not in ("GO", "HOLD"):
        decision = "GO" if decision.startswith("G") else ("HOLD" if decision.startswith("H") else "")
    prio = str(d.get("priority") or "").strip().upper()
    if not re.match(r"^P[1-4]$", prio):
        prio = ""
    return {
        "business_value": _i(d.get("business_value")),
        "urgency": _i(d.get("urgency")),
        "priority": prio,
        "decision": decision,
        "scope_decisions": [str(x).strip() for x in (d.get("scope_decisions") or []) if str(x).strip()][:10],
        "risks_if_delayed": str(d.get("risks_if_delayed") or "").strip(),
        "rationale": str(d.get("rationale") or "").strip(),
    }


def render_po_comment(po: dict) -> str:
    mark = {"GO": "✅ GO", "HOLD": "⏸️ HOLD"}.get(po.get("decision") or "", "—")
    L = ["## 🎯 PO Değerlendirmesi", "", "| Alan | Değer |", "|---|---|",
         f"| İş değeri | {po.get('business_value') if po.get('business_value') is not None else '—'} / 10 |",
         f"| Aciliyet | {po.get('urgency') if po.get('urgency') is not None else '—'} / 10 |",
         f"| Öncelik önerisi | {po.get('priority') or '—'} |",
         f"| Karar | {mark} |", ""]
    if po.get("scope_decisions"):
        L += ["**Kapsam kararları:**"] + [f"- {s}" for s in po["scope_decisions"]] + [""]
    if po.get("risks_if_delayed"):
        L += [f"**Gecikirse:** {po['risks_if_delayed']}", ""]
    if po.get("rationale"):
        L += [f"**Gerekçe:** {po['rationale']}", ""]
    L += ["---", "*Product Owner ajanı — danışma niteliğinde; kapı değil.*"]
    return "\n".join(L)
