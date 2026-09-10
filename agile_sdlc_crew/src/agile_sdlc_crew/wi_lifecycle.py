"""WI yaşam döngüsü + Definition of Done — Scrum işlevleri, faz 1.

Pipeline bugüne kadar iş kaydının (WI) `System.State` alanına hiç dokunmadı:
iş başladı, PR açıldı, reviewer onayladı, build yeşil oldu — board'da WI hâlâ
"To Do"da durdu; insanlar elle taşıdı (73121 → "Ready for Production",
73061 → "QA To Do" hep elle). Bu modül iki şeyi deterministik yapar:

1. **Durum geçişi** — süreçten bağımsız *tercih listeleri* ile takımın kendi
   durum adlarından seçer (FLO süreci: Backlog → To Do → In Progress →
   Code Review → QA To Do/QA → UAT → … ; Agile şablonu: New → Active →
   Resolved). Yalnızca pipeline'ın **sahiplik aralığındaki** durumlardan
   geçiş yapar: Proposed kategorisi + In Progress + Code Review + Blocked.
   İnsanın ilerlettiği bir WI'ı (QA, UAT, Ready for Production, Done) asla
   geri çekmez.

2. **Definition of Done** — tamamlanma raporuna deterministik kontrol listesi:
   reviewer açık onayı, build yeşil, açık review maddesi yok, UAT kabul,
   test dosyası dahil (CREW_REQUIRE_TESTS ise zorunlu), PR bağlı. Job #189
   `completed` bitti ama UAT raporu **REJECTED** (AC2 FAIL) idi — terminal
   sözleşme bunu görmüyordu; DoD tablosu görünür kılar, `CREW_DOD_ENFORCE`
   açıksa `needs_human`a çevirir.

LLM çağrısı yok; bütün fonksiyonlar saf, testlenebilir. Azure erişimi
`apply_state` / `WiLifecycle` içinde ve her zaman try/except — WI durumu
yazılamaması pipeline'ı asla durdurmaz.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("pipeline")

# ── Olay → tercih edilen durum adları (ilk eşleşen kazanır) ──────────────
# Süreç şablonları farklı adlar kullanır; takımın workitemtype/states listesinde
# hangisi varsa o seçilir. Eşleşme büyük/küçük harf duyarsız.
STATE_PREFS: dict[str, list[str]] = {
    # iş kuyruktan alındı, pipeline çalışıyor
    "start":   ["In Progress", "Active", "Doing", "In Development", "Development", "Committed"],
    # PR açıldı, inceleme bekliyor
    "review":  ["Code Review", "In Review", "Review", "In Progress", "Active"],
    # needs_info / needs_human — insan bekleniyor
    "wait":    ["Blocked", "On Hold", "Waiting", "Impeded"],
    # DoD geçti, QA'ya devir
    "handoff": ["QA To Do", "Ready for QA", "Ready for Test", "QA", "Testing", "Resolved"],
    # pipeline'ın kendi açtığı alt iş (child Task) dosyası push edildi
    "complete": ["Done", "Closed", "Completed", "Resolved"],
}

# Pipeline'ın sahiplendiği durum adları (Proposed kategorisi bunlara eklenir).
_OWNED_NAMES = {n.lower() for k in ("start", "review", "wait") for n in STATE_PREFS[k]}

_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|spec|__tests__)(/|$)|(_test|Test|\.test|\.spec|_spec)\.[a-zA-Z]+$",
)


# ── Saf yardımcılar ──────────────────────────────────────────────────────

def pick_state(available: list[dict], event: str) -> str | None:
    """`available` = [{name, category}] (Azure workitemtypes/{type}/states).
    Olayın tercih listesinde takımın süreciyle ilk eşleşen adı döndürür."""
    names = {(s.get("name") or "").lower(): s.get("name") for s in available or []}
    for pref in STATE_PREFS.get(event, []):
        hit = names.get(pref.lower())
        if hit:
            return hit
    return None


def owned_states(available: list[dict]) -> set[str]:
    """Pipeline'ın dokunmasına izin verilen durumlar (küçük harf)."""
    out: set[str] = set()
    for s in available or []:
        name = (s.get("name") or "")
        cat = (s.get("category") or "")
        if cat == "Proposed" or name.lower() in _OWNED_NAMES:
            out.add(name.lower())
    return out


def plan_transition(current: str, available: list[dict], event: str) -> str | None:
    """Geçiş yapılmalı mı? Hedef adı ya da None (dokunma).

    None nedenleri: süreçte uygun durum yok · WI zaten hedefte · WI sahiplik
    aralığı dışında (insan ilerletmiş: QA/UAT/Done…)."""
    target = pick_state(available, event)
    if not target:
        return None
    cur = (current or "").strip()
    if cur.lower() == target.lower():
        return None
    if cur and cur.lower() not in owned_states(available):
        return None
    return target


def plan_revert(current: str, initial: str, available: list[dict], pr_exists: bool) -> str | None:
    """İş `failed` bitti: pipeline'ın kendi taşıdığı WI'ı başlangıç durumuna
    geri al. PR açılmışsa geri alma YOK (kod var, Code Review'da kalsın —
    insan karar verir). Başlangıç durumu sahiplik aralığında değilse de yok."""
    if pr_exists:
        return None
    cur = (current or "").strip().lower()
    ini = (initial or "").strip()
    if not ini or cur == ini.lower():
        return None
    owned = owned_states(available)
    if cur not in owned or ini.lower() not in owned:
        return None
    # Yalnızca pipeline'ın koyduğu durumlardan geri dön (start/review/wait).
    if cur not in _OWNED_NAMES:
        return None
    return ini


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH_RE.search((path or "").replace("\\", "/")))


# ── UAT çıktısı parse ─────────────────────────────────────────────────────

_UAT_OVERALL_RE = re.compile(
    r"Overall\s+Evaluation\s*:?\**\s*:?\s*\**\s*(ACCEPTED|REJECTED|KABUL|RED)",
    re.IGNORECASE,
)
_UAT_ITEM_RE = re.compile(r"(?ms)^\s*(\d+)\.\s+(.*?)(?=^\s*\d+\.\s|^\s*\*\*Overall|\Z)")
_UAT_VERDICT_RE = re.compile(r"\b(PASS|FAIL)\b")


def parse_uat(uat_text: str) -> dict:
    """→ {overall: 'ACCEPTED'|'REJECTED'|None, pass: n, fail: n, items: [(no, verdict)]}.
    Kriter bloğunda ilk PASS/FAIL kelimesi karar sayılır (açıklama metninde
    sonradan geçen 'fail' kelimeleri değil)."""
    text = uat_text or ""
    overall = None
    m = _UAT_OVERALL_RE.search(text)
    if m:
        tok = m.group(1).upper()
        overall = "ACCEPTED" if tok in ("ACCEPTED", "KABUL") else "REJECTED"
    items: list[tuple[int, str]] = []
    for it in _UAT_ITEM_RE.finditer(text):
        v = _UAT_VERDICT_RE.search(it.group(2))
        if v:
            items.append((int(it.group(1)), v.group(1).upper()))
    return {
        "overall": overall,
        "pass": sum(1 for _, v in items if v == "PASS"),
        "fail": sum(1 for _, v in items if v == "FAIL"),
        "items": items,
    }


# ── Definition of Done ───────────────────────────────────────────────────

@dataclass
class DodItem:
    key: str
    label: str
    ok: bool | None          # True geçti · False kaldı · None doğrulanamadı
    note: str = ""
    mandatory: bool = True

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "ok": self.ok,
                "note": self.note, "mandatory": self.mandatory}


@dataclass
class DodResult:
    items: list[DodItem] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Zorunlu maddelerden hiçbiri açıkça KALMADI (None bloklamaz)."""
        return not any(i.mandatory and i.ok is False for i in self.items)

    @property
    def unverified(self) -> list[DodItem]:
        return [i for i in self.items if i.ok is None]

    @property
    def failed(self) -> list[DodItem]:
        return [i for i in self.items if i.ok is False]


def evaluate_dod(
    *,
    review_approved: bool | None,
    open_review_issues: int | None,
    build_status: str,
    uat_text: str,
    pushed_files: list[str],
    require_tests: bool,
    pr_id: str,
) -> DodResult:
    items: list[DodItem] = []

    # 1. Reviewer açık onayı (REVIEW_DECISION: APPROVE) — escalation/RED değil
    if review_approved is None:
        items.append(DodItem("review", "Kod incelemesi onaylandı", None, "review çıktısı yok"))
    else:
        items.append(DodItem("review", "Kod incelemesi onaylandı", bool(review_approved),
                             "" if review_approved else "açık onay yok (RED / escalation)"))

    # 2. Açık review maddesi kalmadı
    if open_review_issues is None:
        items.append(DodItem("issues", "Açık review maddesi yok", None, "yapısal madde takibi kapalı", mandatory=False))
    else:
        items.append(DodItem("issues", "Açık review maddesi yok", open_review_issues == 0,
                             "" if open_review_issues == 0 else f"{open_review_issues} madde açık"))

    # 3. PR test build'i yeşil
    bs = (build_status or "").lower()
    if bs == "succeeded":
        items.append(DodItem("build", "PR test build'i yeşil", True))
    elif bs in ("failed", "partiallysucceeded", "canceled", "timeout", "stale"):
        items.append(DodItem("build", "PR test build'i yeşil", False, f"build sonucu: {bs}"))
    else:
        why = {"no_pipeline": "repoda PR-test pipeline'ı yok", "disabled": "build kapısı kapalı",
               "skipped": "atlandı (PR yok / dry-run)"}.get(bs, "build bilgisi yok")
        items.append(DodItem("build", "PR test build'i yeşil", None, why))

    # 4. UAT kabul
    uat = parse_uat(uat_text)
    if uat["overall"] is None and not uat["items"]:
        items.append(DodItem("uat", "UAT kabul etti", None, "UAT raporu parse edilemedi"))
    else:
        ok = uat["overall"] == "ACCEPTED" and uat["fail"] == 0
        if uat["overall"] is None:
            ok = uat["fail"] == 0 and uat["pass"] > 0
        note = f"{uat['pass']} PASS · {uat['fail']} FAIL" + (f" · {uat['overall']}" if uat["overall"] else "")
        items.append(DodItem("uat", "UAT kabul etti", ok, note))

    # 5. Test dosyası dahil
    has_test = any(is_test_path(p) for p in (pushed_files or []))
    if has_test:
        items.append(DodItem("tests", "Test dosyası dahil", True, "", mandatory=require_tests))
    elif require_tests:
        items.append(DodItem("tests", "Test dosyası dahil", False, "push edilen dosyalarda test yok", mandatory=True))
    else:
        items.append(DodItem("tests", "Test dosyası dahil", None, "CREW_REQUIRE_TESTS kapalı, test yok", mandatory=False))

    # 6. PR iş kaydına bağlı
    items.append(DodItem("pr", "PR açık ve iş kaydına bağlı", bool(pr_id), "" if pr_id else "PR yok"))

    return DodResult(items)


def render_dod(result: DodResult, *, enforce: bool) -> str:
    """WI yorumu için Türkçe Markdown tablo (`_md_to_html` ### + | tablo bilir)."""
    mark = {True: "✅", False: "❌", None: "⚪"}
    lines = ["### Definition of Done", "", "| Madde | Durum | Not |", "|---|---|---|"]
    for it in result.items:
        z = "" if it.mandatory else " *(bilgi)*"
        lines.append(f"| {it.label}{z} | {mark[it.ok]} | {it.note or '—'} |")
    lines.append("")
    if result.passed:
        tail = "**DoD geçti**"
        if result.unverified:
            tail += f" — {len(result.unverified)} madde doğrulanamadı (⚪)"
        lines.append(tail + ".")
    else:
        names = ", ".join(i.label for i in result.failed if i.mandatory)
        lines.append(f"**DoD geçilemedi:** {names}."
                     + (" İş `needs_human` durumuna alındı." if enforce
                        else " Zorlama kapalı (`CREW_DOD_ENFORCE`), iş tamamlandı sayıldı."))
    return "\n".join(lines)


# ── Azure tarafı ─────────────────────────────────────────────────────────

def load_wi_context(client, work_item_id: str, fields: dict | None = None) -> dict:
    """WI tipi, mevcut durumu, atanan kişi ve tipin durum listesi.
    `fields` verilmişse WI yeniden çekilmez (step1 zaten çekmiş oluyor)."""
    if fields is None:
        wi = client.get_work_item(int(work_item_id))
        fields = (wi or {}).get("fields", {}) or {}
    wtype = fields.get("System.WorkItemType", "") or ""
    assigned = fields.get("System.AssignedTo")
    if isinstance(assigned, dict):
        assigned = assigned.get("uniqueName") or assigned.get("displayName") or ""
    states: list[dict] = []
    if wtype:
        try:
            states = client.get_work_item_type_states(wtype)
        except Exception as e:  # noqa: BLE001
            log.warning(f"  WI durum listesi alinamadi ({wtype}): {e}")
    # Org'un gercek SP alani Custom.StoryPoints (son 45 gun: 153 Task'in 150'sinde
    # dolu; board/sprint raporu/velocity bunu okur). Microsoft.VSTS...StoryPoints
    # eski/varsayilan (degerlerin %99'u 3.0). Sira: Custom → Microsoft → Effort.
    sp = None
    for _k in ("Custom.StoryPoints", "Microsoft.VSTS.Scheduling.StoryPoints",
               "Microsoft.VSTS.Scheduling.Effort"):
        if fields.get(_k) is not None:
            sp = fields.get(_k)
            break
    return {
        "wi_type": wtype,
        "wi_state": fields.get("System.State", "") or "",
        "wi_assigned_to": assigned or "",
        "wi_states": [{"name": s.get("name", ""), "category": s.get("category", "")} for s in states],
        # faz 2 (tahmin + alt iş): mevcut SP (insan tahmini ezilmez), alan/iterasyon (child'lara kopyalanır)
        "wi_story_points": float(sp) if isinstance(sp, (int, float)) else None,
        "wi_area_path": fields.get("System.AreaPath", "") or "",
        "wi_iteration_path": fields.get("System.IterationPath", "") or "",
        # faz 5 (is tipine gore akis): etiket + baslik → spike tespiti
        "wi_tags": fields.get("System.Tags", "") or "",
        "wi_title": fields.get("System.Title", "") or "",
    }


def apply_state(client, work_item_id: str, target: str, logger=None) -> bool:
    """Durumu yaz; hata pipeline'ı durdurmaz (False döner)."""
    _l = logger or log.info
    try:
        client.set_work_item_state(int(work_item_id), target)
        _l(f"  🗂️ WI #{work_item_id} durumu → {target}")
        return True
    except Exception as e:  # noqa: BLE001
        _l(f"  🗂️ WI durumu yazilamadi ({target}): {e}")
        return False


def assign_if_empty(client, work_item_id: str, current_assignee: str, logger=None) -> str:
    """WI atanmamışsa PAT sahibine ata; atanan kimliği (uniqueName) döndür, yoksa ''."""
    _l = logger or log.info
    if (current_assignee or "").strip():
        return ""
    try:
        me = client.get_authenticated_user()
        ident = me.get("uniqueName") or me.get("displayName") or ""
        if not ident:
            return ""
        client.assign_work_item(int(work_item_id), ident)
        _l(f"  🗂️ WI #{work_item_id} atandı → {ident}")
        return ident
    except Exception as e:  # noqa: BLE001
        _l(f"  🗂️ WI atanamadi: {e}")
        return ""
