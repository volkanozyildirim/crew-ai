"""Tahminleme (story point) — Scrum işlevleri, faz 2.

Takım (Boards Management / E-commerce Logistic Operations) story point'i
**Task** seviyesinde tutuyor: son 45 günün 153 Task'ının 89'unda
`Microsoft.VSTS.Scheduling.StoryPoints` dolu; Effort/OriginalEstimate hiç
kullanılmıyor; User Story'lerde SP yok. Pipeline'ın koştuğu WI'lar da Task
(73121, 73061 → SP boş). Bu modül iki kaynağı birleştirir:

* **BA tahmini** — `requirements_analysis_task` JSON'undaki `estimate`
  bloğu (`story_points`, `confidence`, `rationale`). Eski cache / bloğu
  atlayan model → None.
* **Yapısal tahmin** — zaten üretilen sinyallerden (FR+TR+AC sayısı, plan
  dosya sayısı, keşif gerekti mi) Fibonacci merdiveninde deterministik
  değer. Ek LLM çağrısı YOK. `_apply_envelope`'un S/M/L mantığıyla aynı
  girdiler; zarf gibi **yalnızca yükselir** (plan aşaması requirements'ı
  aşağı çekmez).

Nihai değer = max(BA, yapısal) → Fibonacci'ye oturtulur. Yazma yalnızca
`CREW_WI_WRITE_ESTIMATE` açık ve WI'da SP boşsa; insanın tahmini asla
ezilmez.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("pipeline")

FIB = [1, 2, 3, 5, 8, 13, 21]


def snap_fib(x) -> int | None:
    """En yakın Fibonacci puanı (eşitlikte büyüğü). 0/None → None."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    best = FIB[0]
    for f in FIB:
        if abs(f - v) < abs(best - v) or (abs(f - v) == abs(best - v) and f > best):
            best = f
    return best


def _bump(sp: int, steps: int) -> int:
    i = FIB.index(sp) if sp in FIB else FIB.index(snap_fib(sp) or FIB[0])
    return FIB[max(0, min(len(FIB) - 1, i + steps))]


def parse_ba_estimate(requirements_text: str) -> dict | None:
    """BA JSON'undaki `estimate` bloğu → {sp, confidence, rationale} | None."""
    txt = requirements_text or ""
    try:
        s0, e0 = txt.find("{"), txt.rfind("}")
        if s0 < 0 or e0 <= s0:
            return None
        d = json.loads(txt[s0:e0 + 1])
    except Exception:
        return None
    est = d.get("estimate") if isinstance(d, dict) else None
    if not isinstance(est, dict):
        return None
    sp = snap_fib(est.get("story_points", est.get("sp")))
    if sp is None:
        return None
    conf = None
    try:
        if est.get("confidence") is not None:
            conf = max(0, min(100, int(round(float(est["confidence"])))))
    except Exception:
        conf = None
    return {"sp": sp, "confidence": conf, "rationale": str(est.get("rationale") or "").strip()}


def structural_estimate(n_req: int, n_files: int, explored: bool, stage: str) -> tuple[int, list[str]]:
    """Yapısal sinyallerden Fibonacci puanı + gerekçe listesi (Türkçe).

    requirements: yalnızca gereksinim sayısı. plan: dosya sayısı ve keşif de
    merdivende basamak ekler (tavan 13; 21 yalnızca BA derse)."""
    reasons: list[str] = []
    n_req = int(n_req or 0)
    if n_req <= 3:
        sp = 2
    elif n_req <= 5:
        sp = 3
    elif n_req <= 8:
        sp = 5
    else:
        sp = 8
    reasons.append(f"{n_req} gereksinim → {sp}")
    if stage == "plan":
        n_files = int(n_files or 0)
        if n_files >= 6:
            sp = _bump(sp, 2)
            reasons.append(f"{n_files} dosya (+2 basamak)")
        elif n_files >= 3:
            sp = _bump(sp, 1)
            reasons.append(f"{n_files} dosya (+1 basamak)")
        if explored:
            sp = _bump(sp, 1)
            reasons.append("keşif gerekti (+1 basamak)")
    return min(sp, 13), reasons


def reconcile(ba_sp: int | None, structural_sp: int, previous_sp: int | None = None) -> tuple[int, str]:
    """Nihai puan ve kaynağı. max(BA, yapısal, önceki) — tahmin yalnızca yükselir."""
    cands = [(structural_sp, "yapısal")]
    if ba_sp:
        cands.append((ba_sp, "BA"))
    if previous_sp:
        cands.append((previous_sp, "önceki aşama"))
    sp, src = max(cands, key=lambda t: (t[0], t[1] == "BA"))
    if ba_sp and ba_sp == structural_sp:
        src = "BA + yapısal uyumlu"
    return snap_fib(sp) or structural_sp, src


def size_class(sp: int) -> str:
    return "S" if sp <= 2 else ("M" if sp <= 5 else "L")


def render_estimate_line(est: dict, *, elapsed_min: float | None = None, cost_usd: float | None = None,
                         team_sp: float | None = None) -> str:
    """Tamamlanma yorumu için tek satır: takım tahmini (WI'daki SP) · pipeline
    tahmini · gerçekleşen. Takım SP'si varsa iki tahmin yan yana okunur —
    retrospektifte 'kim ne kadar yanıldı' verisi."""
    if not est or not est.get("sp"):
        return ""
    parts = []
    if team_sp is not None:
        parts.append(f"**Takım tahmini:** {float(team_sp):g} SP")
    label = "**Pipeline tahmini:**" if team_sp is not None else "**Tahmin:**"
    parts.append(f"{label} {est['sp']} SP ({size_class(int(est['sp']))}, {est.get('source', '?')})")
    if elapsed_min is not None or cost_usd is not None:
        act = []
        if elapsed_min is not None:
            act.append(f"{elapsed_min:.0f} dk")
        if cost_usd is not None:
            act.append(f"${cost_usd:.2f}")
        parts.append("**Gerçekleşen:** " + " · ".join(act))
    return " · ".join(parts)


SP_FIELDS = (
    "Custom.StoryPoints",                       # org'un gerçek alanı (board, sprint raporu, velocity)
    "Microsoft.VSTS.Scheduling.StoryPoints",    # şablon alanı (eski/varsayılan)
    "Microsoft.VSTS.Scheduling.Effort",         # Scrum şablonu
)


def write_story_points(client, work_item_id: str, sp: int, logger=None) -> str:
    """SP alanına yaz — sırayla Custom.StoryPoints → StoryPoints → Effort; alan
    tipte yoksa (400) bir sonrakine düş. Yazılan alan adı ya da ''."""
    _l = logger or log.info
    for field in SP_FIELDS:
        try:
            value = int(sp) if field.startswith("Custom.") else float(sp)
            client.update_work_item(int(work_item_id), [
                {"op": "add", "path": f"/fields/{field}", "value": value},
            ])
            _l(f"  📐 WI #{work_item_id} {field.split('.')[-1]} = {sp}")
            return field
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "400" in msg or "TF" in msg or "field" in msg.lower():
                continue
            _l(f"  📐 Story point yazilamadi: {e}")
            return ""
    _l("  📐 Story point yazilamadi: tipte StoryPoints/Effort alanı yok")
    return ""
