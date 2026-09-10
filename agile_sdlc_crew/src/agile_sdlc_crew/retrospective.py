"""Retrospektif — Scrum işlevleri, faz 3.

Sprint (ya da son N gün) boyunca pipeline'ın koştuğu işlerden **deterministik**
öğrenme raporu: ne bitti, ne takıldı, nerede takıldı, kaça ve kaç dakikaya;
tekrarlayan nedenler ve bunlardan türeyen kılavuz-kuralı önerileri. Veri zaten
MySQL'de (`jobs`, `job_steps`); LLM çağrısı YOK, maliyet sıfır.

Neden deterministik: retrospektifin değeri sayıların tartışılmaz olmasında.
Kural önerileri veriye bağlı eşiklerden üretilir ve **insan onayıyla**
(dashboard "Kılavuza ekle") `kickoff_guidance`'a girer — pipeline kendi
kendini kural yazarak değiştirmez.

Kaynaklar ve okuma biçimi:
  jobs.status/error_message  → sonuç sınıfı (classify_outcome)
  job_steps.status           → hangi adım kırıldı
  review_pr_task.output      → "N düzeltme turu", REVIEW_DECISION / Verdict
  pr_build_gate.output       → succeeded / failed / pipeline yok / atlandı
  uat_task.output            → parse_uat (ACCEPTED / REJECTED)
  jobs.estimate_sp           → teslim edilen SP, SP başına dk ve $
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

log = logging.getLogger("pipeline")

TERMINAL = ("completed", "failed", "needs_human", "needs_info")

_RE_RETRY = re.compile(r"(\d+)\s+düzeltme turu", re.IGNORECASE)
_RE_READINESS = re.compile(r"hazırlık skoru\s+(\d+)\s*/\s*100", re.IGNORECASE)


# ── Sınıflandırma ────────────────────────────────────────────────────────

def classify_outcome(status: str, error_message: str | None) -> str:
    """İşin sonucunu insan-okur bir nedene indirger (Türkçe etiket)."""
    e = (error_message or "").lower()
    if status == "completed":
        return "Tamamlandı"
    if status == "needs_info":
        return "WI detayı yetersiz (hazırlık kapısı)"
    if status == "needs_human":
        if "dod" in e:
            return "DoD geçilemedi"
        if "review" in e or "madde" in e:
            return "Review: kapanmayan madde"
        if "build" in e:
            return "PR build kırmızı / doğrulanamadı"
        return "İnsan kararı bekliyor"
    if status == "failed":
        if "sunucu yeniden" in e or "yarida kaldi" in e or "yarıda kaldı" in e:
            return "Altyapı: sunucu yeniden başlatıldı"
        if "plan-push" in e or "push edilemedi" in e or "push edilemedi" in e:
            return "Implement: dosya push edilemedi"
        if "butce" in e or "bütçe" in e or "budget" in e or "maliyet" in e:
            return "Bütçe aşımı"
        if "pr olusturulamadi" in e or "pr oluşturulamadı" in e:
            return "PR oluşturulamadı"
        if "plan" in e or "insufficient" in e or "tasarim" in e or "tasarım" in e:
            return "Plan üretilemedi"
        if "yetersiz" in e or "icerik" in e or "içerik" in e:
            return "WI içeriği yetersiz (eski kapı)"
        return "Diğer hata"
    return status or "bilinmiyor"


def _minutes(a, b) -> float | None:
    if not a or not b:
        return None
    try:
        return max(0.0, (b - a).total_seconds() / 60.0)
    except Exception:
        return None


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ── Toplama ──────────────────────────────────────────────────────────────

def collect(*, since_days: int | None = None, wi_ids: list[str] | None = None,
            limit: int = 500) -> list[dict]:
    """Pencere içindeki işler + adımları. kickoff-only ve dry-run işler dışarıda.
    wi_ids verilirse (sprint) yalnızca o WI'ların işleri."""
    from agile_sdlc_crew import db
    where = ["COALESCE(kickoff_only,0)=0", "COALESCE(dry_run,0)=0", "COALESCE(job_kind,'pipeline')='pipeline'"]
    params: list = []
    if since_days:
        where.append("created_at >= %s")
        params.append(datetime.now() - timedelta(days=int(since_days)))
    if wi_ids is not None:
        ids = [str(w) for w in wi_ids if str(w).strip()]
        if not ids:
            return []
        where.append("work_item_id IN (" + ",".join(["%s"] * len(ids)) + ")")
        params.extend(ids)
    with db.get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM jobs WHERE " + " AND ".join(where) + " ORDER BY id DESC LIMIT %s",
            params + [int(limit)],
        )
        jobs = list(cur.fetchall() or [])
        if not jobs:
            return []
        jid = [j["id"] for j in jobs]
        cur.execute(
            "SELECT job_id, step_key, status, output, error_message, cost_usd, started_at, finished_at "
            "FROM job_steps WHERE job_id IN (" + ",".join(["%s"] * len(jid)) + ") ORDER BY id",
            jid,
        )
        by_job: dict[int, list[dict]] = defaultdict(list)
        for s in cur.fetchall() or []:
            by_job[s["job_id"]].append(s)
    for j in jobs:
        j["steps"] = by_job.get(j["id"], [])
    return jobs


# ── Analiz ───────────────────────────────────────────────────────────────

def analyze(jobs: list[dict]) -> dict:
    from agile_sdlc_crew.wi_lifecycle import parse_uat

    jobs = [j for j in jobs if (j.get("status") or "") in TERMINAL]
    n = len(jobs)
    status_c = Counter(j.get("status") for j in jobs)
    outcome_c = Counter(classify_outcome(j.get("status", ""), j.get("error_message")) for j in jobs)
    step_fail_c: Counter = Counter()
    review = {"jobs": 0, "first_pass": 0, "retries_total": 0, "changes_required": 0, "needs_human": 0}
    build = Counter()
    uat = Counter()
    readiness_scores: list[int] = []
    cost_total = 0.0
    completed_min: list[float] = []
    completed_cost: list[float] = []
    sp_delivered = 0
    sp_min: list[float] = []
    sp_cost: list[float] = []
    wi_runs: Counter = Counter()
    top_cost: list[tuple[float, int, str, str]] = []

    for j in jobs:
        cost = _f(j.get("total_cost_usd"))
        cost_total += cost
        wi_runs[str(j.get("work_item_id"))] += 1
        top_cost.append((cost, j["id"], str(j.get("work_item_id")), j.get("status", "")))
        mins = _minutes(j.get("started_at"), j.get("finished_at"))
        if j.get("status") == "completed":
            if mins is not None:
                completed_min.append(mins)
            completed_cost.append(cost)
            sp = j.get("estimate_sp")
            if sp:
                sp_delivered += int(sp)
                if mins is not None:
                    sp_min.append(mins / int(sp))
                sp_cost.append(cost / int(sp))
        if j.get("status") == "needs_info":
            m = _RE_READINESS.search(j.get("error_message") or "")
            if m:
                readiness_scores.append(int(m.group(1)))
        for s in j.get("steps") or []:
            if s.get("status") == "failed":
                step_fail_c[s.get("step_key")] += 1
            out = s.get("output") or ""
            k = s.get("step_key")
            if k == "review_pr_task" and s.get("status") == "completed" and out and "DRY-RUN" not in out:
                review["jobs"] += 1
                m = _RE_RETRY.search(out)
                r = int(m.group(1)) if m else 0
                review["retries_total"] += r
                head = out[:400].upper()
                if "NEEDS_HUMAN" in head or "İNSAN MÜDAHALESİ" in out[:200]:
                    review["needs_human"] += 1
                elif "CHANGES_REQUIRED" in head and "APPROVE" not in head:
                    review["changes_required"] += 1
                elif r == 0 and "APPROVE" in head:
                    review["first_pass"] += 1
            elif k == "pr_build_gate" and out:
                lo = out.lower()
                if "succeeded" in lo:
                    build["yeşil"] += 1
                elif "failed" in lo or "partially" in lo or "canceled" in lo or "timeout" in lo:
                    build["kırmızı"] += 1
                elif "pipeline" in lo and "yok" in lo:
                    build["pipeline yok"] += 1
                else:
                    build["atlandı"] += 1
            elif k == "uat_task" and out and "DRY-RUN" not in out:
                u = parse_uat(out)
                if u["overall"] == "ACCEPTED":
                    uat["kabul"] += 1
                elif u["overall"] == "REJECTED" or u["fail"] > 0:
                    uat["red"] += 1

    completed = status_c.get("completed", 0)
    avg = lambda xs: (sum(xs) / len(xs)) if xs else None  # noqa: E731
    top_cost.sort(reverse=True)
    return {
        "jobs": n,
        "status": dict(status_c),
        "success_rate": (100.0 * completed / n) if n else None,
        "outcomes": outcome_c.most_common(),
        "step_failures": step_fail_c.most_common(6),
        "review": review,
        "build": dict(build),
        "uat": dict(uat),
        "readiness_avg": avg(readiness_scores),
        "cost_total": cost_total,
        "cost_avg_completed": avg(completed_cost),
        "minutes_avg_completed": avg(completed_min),
        "sp_delivered": sp_delivered,
        "min_per_sp": avg(sp_min),
        "cost_per_sp": avg(sp_cost),
        "rerun_wis": [(w, c) for w, c in wi_runs.most_common() if c > 1],
        "top_cost": top_cost[:5],
    }


# ── Öneriler ─────────────────────────────────────────────────────────────

def suggest_rules(a: dict) -> list[dict]:
    """Veriye bağlı eşiklerden kılavuz kuralı önerileri: [{text, why}].
    Metinler kickoff ekibine talimat dilinde (Türkçe); insan onaylayıp ekler."""
    out: list[dict] = []
    n = a.get("jobs") or 0
    if not n:
        return out
    st = a.get("status", {})
    ni = st.get("needs_info", 0)
    if ni and ni / n >= 0.2:
        out.append({
            "text": "Analizde kabul kriteri alanı boş ya da veri kaynağı/hedef repo belirsiz olan WI'larda "
                    "BA açıklamadan AC türetip eksikleri soru listesi olarak WI sahibine yazsın; "
                    "mimar bu sorular yanıtlanmadan plan üretmesin.",
            "why": f"{ni}/{n} iş hazırlık kapısında durdu"
                   + (f" (ort. skor {a['readiness_avg']:.0f}/100)" if a.get("readiness_avg") is not None else ""),
        })
    rv = a.get("review", {})
    if rv.get("jobs") and (rv.get("first_pass", 0) / rv["jobs"]) < 0.5:
        out.append({
            "text": "Geliştirici kod yazmadan önce değişen sınıfın MEVCUT testlerini ve test veri sağlayıcılarını okusun; "
                    "test beklentisi ile kaynak/çeviri dosyası aynı PR içinde tutarlı olsun.",
            "why": f"review ilk turda yalnızca {rv.get('first_pass', 0)}/{rv['jobs']} işte onaylandı, "
                   f"toplam {rv.get('retries_total', 0)} düzeltme turu",
        })
    if a.get("build", {}).get("kırmızı"):
        out.append({
            "text": "Teknik tasarım, değişen her sınıf için ilgili test dosyasını da plana alsın ve testin "
                    "çalıştırılma komutunu planda belirtsin; geliştirici push öncesi yerel testi çalıştırsın.",
            "why": f"{a['build']['kırmızı']} işte PR test build'i kırmızı",
        })
    if a.get("uat", {}).get("red"):
        out.append({
            "text": "UAT uzmanı her kabul kriteri için PR dosya listesiyle sınırlı kalmasın; repoda mevcut "
                    "kaynakları (çeviri dosyaları, konfigürasyon) kanıt olarak arasın, yoksa FAIL yerine "
                    "'doğrulanamadı' yazsın.",
            "why": f"{a['uat']['red']} UAT raporu REJECTED (reviewer onayı ve yeşil build'e rağmen)",
        })
    sf = dict(a.get("step_failures") or [])
    if sf.get("technical_design_task", 0) >= 3:
        out.append({
            "text": "Mimar plan JSON'unu üretmeden önce hedef repoda değişecek dosyaların gerçek içeriğini okusun; "
                    "dosya yolu bulamıyorsa INSUFFICIENT desin, tahmini yol yazmasın.",
            "why": f"teknik tasarım adımı {sf['technical_design_task']} kez kırıldı",
        })
    if a.get("rerun_wis"):
        w = a["rerun_wis"][0]
        out.append({
            "text": "Aynı WI ikinci kez kuyruğa alınırken kickoff önceki koşunun WI yorumlarını (needs_info soruları, "
                    "review bulguları) okuyup 'bu sefer farklı ne yapılacak' maddesi yazsın.",
            "why": f"{len(a['rerun_wis'])} WI birden fazla kez koştu (en çok #{w[0]}: {w[1]} kez)",
        })
    return out


def suggest_config(a: dict) -> list[str]:
    """Kural değil, yapılandırma önerileri (Türkçe cümleler)."""
    out: list[str] = []
    st = a.get("status", {})
    if a.get("uat", {}).get("red") and st.get("completed"):
        out.append("UAT RED'e rağmen iş `completed` bitiyor — birkaç temiz koşudan sonra `CREW_DOD_ENFORCE` açılabilir.")
    if st.get("failed") and any(o == "Altyapı: sunucu yeniden başlatıldı" for o, _ in a.get("outcomes", [])):
        out.append("Sunucu yeniden başlatma iş öldürüyor — `running:0` doğrulanmadan restart yapılmamalı; DELETE ?force=true gerçek iptal olmalı.")
    if a.get("cost_per_sp") and a["cost_per_sp"] > 3:
        out.append(f"SP başına ${a['cost_per_sp']:.2f} — zarf sınıfları (S/M/L) ve review retry tavanı gözden geçirilmeli.")
    if a.get("min_per_sp") and a["min_per_sp"] > 10:
        out.append(f"SP başına {a['min_per_sp']:.0f} dk — en uzun adımların (review pre-fetch, build poll) süresi izlenmeli.")
    return out


# ── Rapor ────────────────────────────────────────────────────────────────

def _fmt_min(x) -> str:
    return "—" if x is None else f"{x:.0f} dk"


def _fmt_usd(x) -> str:
    return "—" if x is None else f"${x:.2f}"


def render_markdown(a: dict, *, title: str, rules: list[dict] | None = None,
                    config: list[str] | None = None) -> str:
    st = a.get("status", {})
    n = a.get("jobs") or 0
    L: list[str] = [f"## 🔁 Retrospektif — {title}", ""]
    if not n:
        L.append("Bu pencerede tamamlanmış iş yok.")
        return "\n".join(L)
    L += [
        "### Özet",
        "",
        "| Metrik | Değer |",
        "|---|---|",
        f"| İş sayısı | {n} |",
        f"| Tamamlandı / needs_human / needs_info / failed | {st.get('completed', 0)} / {st.get('needs_human', 0)} / {st.get('needs_info', 0)} / {st.get('failed', 0)} |",
        f"| Başarı oranı | {a['success_rate']:.0f}% |" if a.get("success_rate") is not None else "| Başarı oranı | — |",
        f"| Toplam maliyet | {_fmt_usd(a.get('cost_total'))} |",
        f"| Tamamlanan iş başına ort. maliyet / süre | {_fmt_usd(a.get('cost_avg_completed'))} / {_fmt_min(a.get('minutes_avg_completed'))} |",
        f"| Teslim edilen SP | {a.get('sp_delivered', 0)} |",
        f"| SP başına | {_fmt_min(a.get('min_per_sp'))} · {_fmt_usd(a.get('cost_per_sp'))} |",
        "",
        "### Nerede takıldı",
        "",
        "| Sonuç | Adet |",
        "|---|---|",
    ]
    for o, c in a.get("outcomes", []):
        L.append(f"| {o} | {c} |")
    if a.get("step_failures"):
        L += ["", "**Kırılan adımlar:** " + ", ".join(f"{k} ({c})" for k, c in a["step_failures"])]
    rv = a.get("review", {})
    L += ["", "### Kalite kapıları", ""]
    if rv.get("jobs"):
        L.append(f"- **Review:** {rv['jobs']} iş incelendi; ilk turda onay {rv.get('first_pass', 0)}, "
                 f"toplam düzeltme turu {rv.get('retries_total', 0)}, kapanmayan madde (needs_human) {rv.get('needs_human', 0)}, "
                 f"RED ile biten {rv.get('changes_required', 0)}.")
    else:
        L.append("- **Review:** bu pencerede review çıktısı yok.")
    b = a.get("build", {})
    L.append("- **PR build:** " + (", ".join(f"{k} {v}" for k, v in b.items()) if b else "veri yok") + ".")
    u = a.get("uat", {})
    L.append("- **UAT:** " + (f"kabul {u.get('kabul', 0)}, red {u.get('red', 0)}" if u else "veri yok") + ".")
    if a.get("readiness_avg") is not None:
        L.append(f"- **Hazırlık kapısı:** needs_info işlerde ortalama skor {a['readiness_avg']:.0f}/100.")
    if a.get("rerun_wis"):
        L += ["", "**Tekrar koşan WI'lar:** " + ", ".join(f"#{w} ({c}×)" for w, c in a["rerun_wis"][:6])]
    if a.get("top_cost"):
        L += ["", "### En pahalı işler", "", "| Job | WI | Durum | Maliyet |", "|---|---|---|---|"]
        for cost, jid, wi, status in a["top_cost"]:
            L.append(f"| #{jid} | #{wi} | {status} | {_fmt_usd(cost)} |")
    if rules:
        L += ["", "### Önerilen kılavuz kuralları (onay bekler)", ""]
        for i, r in enumerate(rules, 1):
            L.append(f"{i}. {r['text']}  \n   *Neden:* {r['why']}")
    if config:
        L += ["", "### Yapılandırma önerileri", ""]
        for c in config:
            L.append(f"- {c}")
    return "\n".join(L)


def build_report(*, title: str, since_days: int | None = None, wi_ids: list[str] | None = None) -> dict:
    """Uçtan uca: topla → analiz → öneri → markdown. Sunucu endpoint'i bunu çağırır."""
    jobs = collect(since_days=since_days, wi_ids=wi_ids)
    a = analyze(jobs)
    rules = suggest_rules(a)
    cfg = suggest_config(a)
    return {
        "title": title,
        "jobs": a.get("jobs", 0),
        "summary": a,
        "suggested_rules": rules,
        "config_suggestions": cfg,
        "markdown": render_markdown(a, title=title, rules=rules, config=cfg),
    }
