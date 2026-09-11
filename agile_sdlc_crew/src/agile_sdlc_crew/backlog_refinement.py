"""Backlog refinement — Scrum işlevleri, faz 6.

Hazırlık skoru şimdiye kadar yalnızca pipeline İÇİNDE, ücretli BA çağrısından
sonra hesaplanıyordu (Hazırlık Kapısı, KN-30). Sprint planlama ise seçili
sprintin adaylarını sıralıyordu ama "bu iş sprint'e girmeye hazır mı?"
sorusuna bakmıyordu. Bu modül backlog'daki (Proposed durumundaki) işleri
**pipeline'a girmeden, LLM'siz** Definition of Ready kontrolünden geçirir:

  * açıklama var mı / yeterli mi, kabul kriteri (Bug'da repro adımı) var mı,
    SP ve öncelik girilmiş mi, Task/Bug'ın ebeveyni var mı, SP çok büyük mü
    (bölünmeli), başlık anlamlı mı, iş bayatlamış mı
  * her eksik bir ceza; skor 0-100; eşik `CREW_REFINEMENT_MIN_SCORE`
  * SP yoksa yapısal öneri (`estimation.structural_estimate`, AC sayısından)

Yazma eylemleri (`CREW_REFINEMENT_WRITE`, kapalı) insan tıklamasıyla, satır
başına: eksikleri WI yorumu olarak yaz, boşsa SP yaz (insan tahminini asla
ezmez), `needs-refinement` etiketi ekle/kaldır. LLM adımı
(`CREW_REFINEMENT_LLM`, kapalı) yine satır başına: İş Analisti ajanı
netleştirme soruları ve taslak AC üretir; maliyet `job_kind='refinement'`
işine yazılır. Durum değiştirilmez, kuyruğa alınmaz.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from html import unescape

from agile_sdlc_crew.branding import PRODUCT_NAME
from agile_sdlc_crew.sprint_planning import PROPOSED_FALLBACK, is_proposed

log = logging.getLogger("pipeline")

REFINEMENT_TYPES = ("User Story", "Product Backlog Item", "Bug", "Task", "Improvement")
NEEDS_TAG = "needs-refinement"
SIGNATURE = f"*{PRODUCT_NAME} — Backlog Refinement*"
SPLIT_SP = 8          # bu değerin ÜSTÜ "bölünmeli"
MIN_TITLE_CHARS = 15
MIN_DESC_CHARS = 20   # bunun altı "açıklama yok" sayılır

F_TITLE = "System.Title"
F_TYPE = "System.WorkItemType"
F_STATE = "System.State"
F_DESC = "System.Description"
F_AC = "Microsoft.VSTS.Common.AcceptanceCriteria"
F_REPRO = "Microsoft.VSTS.TCM.ReproSteps"
F_PRIO = "Microsoft.VSTS.Common.Priority"
F_TAGS = "System.Tags"
F_CREATED = "System.CreatedDate"
F_ASSIGNED = "System.AssignedTo"
F_ITER = "System.IterationPath"
F_AREA = "System.AreaPath"
SP_FIELDS = ("Custom.StoryPoints", "Microsoft.VSTS.Scheduling.StoryPoints", "Microsoft.VSTS.Scheduling.Effort")

# Eksik kodları: (etiket, ceza)
GAPS = {
    "no_description": ("Açıklama yok", 30),
    "short_description": ("Açıklama çok kısa", 15),
    "no_ac": ("Kabul kriteri yok", 25),
    "ac_implicit": ("Kabul kriteri açık değil — açıklamadan çıkarılır", 10),
    "no_repro": ("Repro adımları yok", 25),
    "no_question": ("Araştırma sorusu yok", 25),
    "no_sp": ("SP girilmemiş", 10),
    "too_big": ("SP > 8 — bölünmeli", 10),
    "no_priority": ("Öncelik yok", 5),
    "no_parent": ("Ebeveyn (Story/Feature) yok", 10),
    "short_title": ("Başlık çok kısa", 5),
    "stale": ("Bayat — uzun süredir backlog'da", 5),
}


# ── metin yardımcıları ──────────────────────────────────────────────────

def plain(html: str) -> str:
    s = re.sub(r"<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", html or "", flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = unescape(s)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", s)).strip()


def count_criteria(text: str) -> int:
    """Kabul kriteri sayısı: madde imi / numaralı satır / 'Given…' cümlesi; yoksa dolu satır sayısı."""
    t = plain(text)
    if not t:
        return 0
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    bullets = [ln for ln in lines if re.match(r"^([-*•·]|\d+[.)]|AC\s*\d+|Given\b|When\b|Then\b)", ln, re.I)]
    if bullets:
        return len(bullets)
    return min(len(lines), 8)


def has_question(text: str) -> bool:
    return "?" in plain(text)


AC_HEADING = re.compile(r"(kabul\s*kriter|acceptance\s*criteria|beklenen\s*(davranış|sonuç)|expected\s*(behavio|result)"
                        r"|başarı\s*kriter|success\s*criteria|test\s*senaryo|definition\s*of\s*done)", re.I)


def ac_from_description(desc_html: str) -> tuple[int, str]:
    """Bu org'un sürecinde AcceptanceCriteria alanı yok; AC açıklamanın içinde yazılır.
    → ('explicit', n): 'Kabul Kriterleri' benzeri bir başlık altında maddeler var
    → ('implicit', n): başlık yok ama ≥2 madde/satır var (pipeline da bunları kriter sayar, KN-30)
    → ('none', 0)"""
    t = plain(desc_html)
    if not t:
        return 0, "none"
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if AC_HEADING.search(ln) and len(ln) < 80:
            block = []
            for nxt in lines[i + 1:]:
                if len(nxt) < 80 and nxt.endswith(":") and block:
                    break
                block.append(nxt)
                if len(block) >= 15:
                    break
            n = count_criteria("\n".join(block))
            if n:
                return n, "explicit"
    bullets = [ln for ln in lines if re.match(r"^([-*•·]|\d+[.)])\s*\S", ln)]
    if len(bullets) >= 2:
        return len(bullets), "implicit"
    if len(lines) >= 3:
        return min(len(lines), 8), "implicit"
    return 0, "none"


def _sp_of(fields: dict):
    for f in SP_FIELDS:
        v = fields.get(f)
        if v not in (None, "", 0, "0"):
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _parent_id(relations: list[dict] | None):
    for r in relations or []:
        if (r.get("rel") or "") == "System.LinkTypes.Hierarchy-Reverse":
            m = re.search(r"/(\d+)$", r.get("url") or "")
            if m:
                return int(m.group(1))
    return None


def _age_days(created: str, now: datetime) -> int | None:
    if not created:
        return None
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (now - dt).days)
    except ValueError:
        return None


# ── sorgu ──────────────────────────────────────────────────────────────

def proposed_state_names(client, types=REFINEMENT_TYPES) -> list[str]:
    """Tiplerin süreçteki Proposed kategorisindeki durum adları (birleşim). Yoksa ad tabanlı yedek."""
    names: list[str] = []
    for t in types:
        try:
            for st in client.get_work_item_type_states(t) or []:
                if (st.get("category") or "") == "Proposed" and st.get("name") and st["name"] not in names:
                    names.append(st["name"])
        except Exception:  # noqa: BLE001 — tip süreçte yoksa 404
            continue
    return names or [s.title() if s != "to do" else "To Do" for s in sorted(PROPOSED_FALLBACK)]


def backlog_wiql(*, area_path: str = "", iteration_path: str = "", states: list[str], types=REFINEMENT_TYPES) -> str:
    q = lambda s: s.replace("'", "''")  # noqa: E731
    wiql = ("SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
            f"AND [System.State] IN ({', '.join(chr(39) + q(s) + chr(39) for s in states)}) "
            f"AND [System.WorkItemType] IN ({', '.join(chr(39) + q(t) + chr(39) for t in types)}) ")
    if iteration_path:
        wiql += f"AND [System.IterationPath] = '{q(iteration_path)}' "
    elif area_path:
        wiql += f"AND [System.AreaPath] UNDER '{q(area_path)}' "
    wiql += "ORDER BY [Microsoft.VSTS.Common.Priority] ASC, [System.CreatedDate] ASC"
    return wiql


def fetch_candidates(client, *, team: str = "", iteration_path: str = "", scope: str = "backlog",
                     limit: int = 300) -> tuple[list[dict], dict]:
    """Ham WI listesi (fields + relations) ve sorgu meta'sı. scope: 'sprint' → yalnızca iteration_path;
    'backlog' → takımın alan yolu altındaki tüm Proposed işler (sprint'e atanmış olanlar dahil)."""
    states = proposed_state_names(client)
    area = ""
    if scope != "sprint":
        try:
            area = client.get_team_area_path(team) if team else ""
        except Exception:  # noqa: BLE001
            area = ""
    ip = iteration_path if scope == "sprint" else ""
    if scope == "sprint" and not ip:
        raise ValueError("Sprint kapsamı için iteration_path gerekli")
    wiql = backlog_wiql(area_path=area, iteration_path=ip, states=states)
    raw = client.query_work_items(wiql, limit=limit)
    return raw, {"states": states, "area_path": area, "iteration_path": ip, "scope": scope}


def normalize(raw: dict, base_url: str = "") -> dict:
    f = raw.get("fields", {}) or {}
    a = f.get(F_ASSIGNED)
    assigned = a.get("displayName", "") if isinstance(a, dict) else (a or "")
    return {
        "id": raw.get("id"),
        "title": f.get(F_TITLE, "") or "",
        "type": f.get(F_TYPE, "") or "",
        "state": f.get(F_STATE, "") or "",
        "priority": f.get(F_PRIO),
        "sp": _sp_of(f),
        "assignedTo": assigned,
        "tags": f.get(F_TAGS, "") or "",
        "description": f.get(F_DESC, "") or "",
        "acceptance": f.get(F_AC, "") or "",
        "repro": f.get(F_REPRO, "") or "",
        "created": f.get(F_CREATED, "") or "",
        "iterationPath": f.get(F_ITER, "") or "",
        "areaPath": f.get(F_AREA, "") or "",
        "parent_id": _parent_id(raw.get("relations")),
        "url": f"{base_url}/{raw.get('id')}" if base_url and raw.get("id") else "",
    }


# ── değerlendirme ──────────────────────────────────────────────────────

def assess(item: dict, *, min_score: int = 70, stale_days: int = 30, min_desc_chars: int = 100,
           now: datetime | None = None) -> dict:
    """Bir WI için Definition of Ready: eksik listesi, skor, hazır mı, SP önerisi. Saf fonksiyon."""
    from agile_sdlc_crew.estimation import structural_estimate
    from agile_sdlc_crew.type_flow import flow_kind

    now = now or datetime.now(timezone.utc)
    kind = flow_kind(item.get("type", ""), item.get("tags", ""), item.get("title", ""))
    gaps: list[dict] = []

    def gap(code):
        label, pen = GAPS[code]
        gaps.append({"code": code, "label": label, "penalty": pen})

    desc = plain(item.get("description", ""))
    ac_text = item.get("acceptance", "") or ""
    repro = plain(item.get("repro", ""))
    n_ac = count_criteria(ac_text)
    ac_source = "field" if n_ac else "none"
    if not n_ac:
        n_ac, ac_source = ac_from_description(item.get("description", ""))

    if len(desc) < MIN_DESC_CHARS:
        gap("no_description")
    elif len(desc) < min_desc_chars:
        gap("short_description")

    if kind == "bug":
        if not repro and not re.search(r"(adım|step|reproduce|tekrar)", desc, re.I):
            gap("no_repro")
    elif kind == "spike":
        if not has_question(desc) and not has_question(ac_text):
            gap("no_question")
    else:
        if n_ac == 0:
            gap("no_ac")
        elif ac_source == "implicit":
            gap("ac_implicit")

    sp = item.get("sp")
    if sp in (None, 0):
        gap("no_sp")
    elif sp > SPLIT_SP:
        gap("too_big")
    if item.get("priority") in (None, "", 0):
        gap("no_priority")
    if item.get("type") in ("Task", "Bug") and not item.get("parent_id"):
        gap("no_parent")
    if len((item.get("title") or "").strip()) < MIN_TITLE_CHARS:
        gap("short_title")
    age = _age_days(item.get("created", ""), now)
    if age is not None and stale_days > 0 and age > stale_days:
        gap("stale")

    score = max(0, 100 - sum(g["penalty"] for g in gaps))
    suggest_sp, suggest_why = None, ""
    if sp in (None, 0) and kind != "spike":
        est, reasons = structural_estimate(max(n_ac, 1), 0, False, "requirements")
        suggest_sp, suggest_why = est, "; ".join(reasons)

    return {
        **{k: item.get(k) for k in ("id", "title", "type", "state", "priority", "sp", "assignedTo", "tags",
                                     "iterationPath", "parent_id", "url")},
        "kind": kind,
        "n_ac": n_ac,
        "ac_source": ac_source,
        "desc_chars": len(desc),
        "age_days": age,
        "gaps": gaps,
        "score": score,
        "ready": score >= min_score,
        "suggest_sp": suggest_sp,
        "suggest_sp_why": suggest_why,
        "tagged": NEEDS_TAG.lower() in [t.strip().lower() for t in (item.get("tags") or "").split(";")],
    }


def summarize(rows: list[dict]) -> dict:
    gap_counts: dict[str, int] = {}
    for r in rows:
        for g in r["gaps"]:
            gap_counts[g["code"]] = gap_counts.get(g["code"], 0) + 1
    sp_total = sum(float(r["sp"] or 0) for r in rows)
    return {
        "total": len(rows),
        "ready": sum(1 for r in rows if r["ready"]),
        "not_ready": sum(1 for r in rows if not r["ready"]),
        "sp_total": sp_total,
        "sp_missing": sum(1 for r in rows if not r["sp"]),
        "sp_suggested": sum(int(r["suggest_sp"] or 0) for r in rows),
        "gap_counts": gap_counts,
        "gap_labels": {k: v[0] for k, v in GAPS.items()},
        "by_type": {t: sum(1 for r in rows if r["type"] == t) for t in sorted({r["type"] for r in rows})},
    }


def build_report(client, *, team: str = "", iteration_path: str = "", scope: str = "backlog",
                 min_score: int = 70, stale_days: int = 30, min_desc_chars: int = 100,
                 base_url: str = "") -> dict:
    raw, meta = fetch_candidates(client, team=team, iteration_path=iteration_path, scope=scope)
    now = datetime.now(timezone.utc)
    rows = []
    for r in raw:
        it = normalize(r, base_url)
        if not is_proposed(it["state"], None) and it["state"] not in meta["states"]:
            continue
        rows.append(assess(it, min_score=min_score, stale_days=stale_days,
                           min_desc_chars=min_desc_chars, now=now))
    rows.sort(key=lambda r: (r["ready"], r["priority"] if r["priority"] not in (None, "") else 99, r["score"], r["id"] or 0))
    return {**meta, "team": team, "min_score": min_score, "stale_days": stale_days,
            "rows": rows, "summary": summarize(rows)}


def assess_single(client, work_item_id: int, *, min_score: int = 70, stale_days: int = 30,
                  min_desc_chars: int = 100, base_url: str = "") -> tuple[dict, dict]:
    raw = client.get_work_item(int(work_item_id))
    item = normalize(raw, base_url)
    return assess(item, min_score=min_score, stale_days=stale_days, min_desc_chars=min_desc_chars), item


# ── yazma eylemleri (insan tıklar; CREW_REFINEMENT_WRITE) ──────────────

def gaps_comment_markdown(row: dict) -> str:
    lines = [f"## Backlog Refinement — #{row['id']}",
             f"**Hazırlık skoru:** {row['score']}/100 · {'hazır' if row['ready'] else 'eksik'} "
             f"({row['type']}, {row['state']})", ""]
    if row["gaps"]:
        lines.append("**Eksikler:**")
        for g in row["gaps"]:
            lines.append(f"- {g['label']}")
    else:
        lines.append("Definition of Ready koşulları sağlanıyor.")
    if row.get("suggest_sp"):
        lines += ["", f"**SP önerisi:** {row['suggest_sp']} (yapısal: {row.get('suggest_sp_why') or 'kabul kriteri sayısından'})"]
    lines += ["", "Bu değerlendirme deterministik kurallardan üretildi; sprint'e almadan önce eksikleri tamamlayın.",
              "---", SIGNATURE]
    return "\n".join(lines)


def post_gaps_comment(client, row: dict) -> bool:
    from agile_sdlc_crew.main import _add_wi_comment
    _add_wi_comment(client, str(row["id"]), gaps_comment_markdown(row))
    return True


def write_suggested_sp(client, row: dict) -> str:
    """Boşsa SP'yi yaz; insan tahmini varsa dokunma. Yazılan alan adı ya da ''."""
    from agile_sdlc_crew.estimation import write_story_points
    if row.get("sp"):
        return ""
    sp = int(row.get("suggest_sp") or 0)
    if sp <= 0:
        return ""
    return write_story_points(client, str(row["id"]), sp, logger=log.info) or ""


def set_tag(client, work_item_id: int, tag: str = NEEDS_TAG, add: bool = True) -> str:
    """Etiketi ekle/kaldır; diğer etiketler korunur. Yeni etiket dizesini döndürür."""
    wi = client.get_work_item(int(work_item_id))
    cur = [t.strip() for t in ((wi.get("fields", {}) or {}).get(F_TAGS, "") or "").split(";") if t.strip()]
    low = [t.lower() for t in cur]
    if add and tag.lower() not in low:
        cur.append(tag)
    if not add:
        cur = [t for t in cur if t.lower() != tag.lower()]
    new = "; ".join(cur)
    client.update_work_item(int(work_item_id), [{"op": "add", "path": f"/fields/{F_TAGS}", "value": new}])
    return new


# ── LLM: netleştirme soruları (CREW_REFINEMENT_LLM) ────────────────────

def wi_context_text(item: dict, row: dict) -> str:
    parts = [f"# Work Item #{item['id']} — {item['title']}",
             f"Type: {item['type']} · State: {item['state']} · Priority: {item.get('priority') or '-'} · "
             f"SP: {item.get('sp') or '-'}",
             "", "## Description", plain(item.get("description", "")) or "(empty)",
             "", "## Acceptance Criteria", plain(item.get("acceptance", "")) or "(empty)"]
    if item.get("repro"):
        parts += ["", "## Repro Steps", plain(item["repro"])]
    parts += ["", "## Deterministic gaps found", *(f"- {g['label']}" for g in row["gaps"])]
    return "\n".join(parts)[:6000]


def parse_questions(text: str) -> dict | None:
    m = re.search(r"```json\s*(\{.*?\})\s*```", text or "", re.S) or re.search(r"(\{.*\})", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    qs = d.get("questions") or []
    return {
        "questions": [q if isinstance(q, dict) else {"question": str(q), "why": ""} for q in qs][:8],
        "draft_acceptance_criteria": [str(x) for x in (d.get("draft_acceptance_criteria") or [])][:10],
        "summary": str(d.get("summary") or ""),
    }


def questions_comment_markdown(row: dict, q: dict) -> str:
    lines = [f"## Backlog Refinement — netleştirme soruları (#{row['id']})"]
    if q.get("summary"):
        lines += [q["summary"], ""]
    if q.get("questions"):
        lines.append("**Sorular:**")
        for i, it in enumerate(q["questions"], 1):
            why = f" — _{it.get('why')}_" if it.get("why") else ""
            lines.append(f"- S{i}. {it.get('question', '')}{why}")
    if q.get("draft_acceptance_criteria"):
        lines += ["", "**Taslak kabul kriterleri (öneri):**"]
        for i, ac in enumerate(q["draft_acceptance_criteria"], 1):
            lines.append(f"- AC{i}. {ac}")
    lines += ["", "Sorular İş Analisti ajanı tarafından üretildi; yanıtlar WI'a işlendikçe hazırlık skoru yükselir.",
              "---", SIGNATURE]
    return "\n".join(lines)


def generate_questions(client, work_item_id: int, *, job_id: int | None = None, post: bool = False,
                       min_score: int = 70, stale_days: int = 30, min_desc_chars: int = 100) -> dict:
    """İş Analisti (tool'suz) tek çağrı: sorular + taslak AC. post=True ise WI yorumu yazar."""
    from agile_sdlc_crew.crew import AgileSDLCCrew
    from agile_sdlc_crew.tools import claude_cli_llm as _acct
    from agile_sdlc_crew import db

    row, item = assess_single(client, work_item_id, min_score=min_score, stale_days=stale_days,
                              min_desc_chars=min_desc_chars)
    ctx = wi_context_text(item, row)
    if job_id:
        try:
            db.update_job(job_id, wi_title=(item.get("title") or "")[:200], current_step="requirements_analysis_task")
            db.start_step(job_id, "requirements_analysis_task")
            _acct.set_call_context(job_id, "requirements_analysis_task", "business_analyst")
            if getattr(_acct, "_call_sink", None) is None:
                _acct.register_call_sink(db.record_llm_call)
        except Exception:  # noqa: BLE001
            pass
    try:
        crew = AgileSDLCCrew().create_refinement_crew()
        result = crew.kickoff(inputs={"work_item_id": str(work_item_id), "wi_context": ctx})
    finally:
        try:
            _acct.clear_call_context()
        except Exception:  # noqa: BLE001
            pass
    text = (result.raw or "") if result else ""
    q = parse_questions(text) or {"questions": [], "draft_acceptance_criteria": [], "summary": text[:1500]}
    posted = False
    if post and (q["questions"] or q["draft_acceptance_criteria"]):
        try:
            from agile_sdlc_crew.main import _add_wi_comment
            _add_wi_comment(client, str(work_item_id), questions_comment_markdown(row, q))
            posted = True
        except Exception as e:  # noqa: BLE001
            log.info(f"  Refinement soruları WI'a yazılamadı: {e}")
    if job_id:
        try:
            db.complete_step(job_id, "requirements_analysis_task", text[:50_000])
        except Exception:  # noqa: BLE001
            pass
    return {"work_item_id": work_item_id, "row": row, **q, "posted": posted, "raw": text[:4000]}
