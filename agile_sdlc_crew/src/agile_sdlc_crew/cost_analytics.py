"""Maliyet analitiği — para nereye gidiyor?

`llm_calls` tablosu her `claude -p` çağrısını adım/ajan/model kırılımıyla zaten
yazıyor (bkz. `claude_cli_llm` sink → `db.record_llm_call`), ama bugüne kadar
onu **iş bazında** okumanın tek yolu `db.get_job_cost_summary(job_id)` idi.
"Hangi adım bütçeyi yiyor", "hangi model çağrı başına kaça patlıyor",
"nerede döngüye giriyoruz" sorularını yanıtlamak için her seferinde elle SQL
yazmak gerekiyordu.

Bu modül o sorguları kalıcı hale getirir: pencere içindeki TÜM çağrıları tek
seferde çeker, gruplamayı Python'da yapar ve eşiğe bağlı **deterministik**
bulgular üretir. LLM çağrısı YOK, yazma YOK — maliyeti sıfır.

Neden gruplama Python'da: MySQL tarafında `LIMIT` + `IN` alt sorgusu bu sürümde
desteklenmiyor ve `out` gibi ayrılmış sözcükler takma ad olarak kullanılamıyor.
Çağrı hacmi (binler mertebesi) tek fetch için fazlasıyla küçük.

Okuma biçimi:
  llm_calls.cost_usd        → `claude -p` result.total_cost_usd (GERÇEK maliyet)
  llm_calls.turns           → bir `claude -p` oturumundaki tur sayısı
  cache_read/creation       → önbellek okuma/yazma; yazma en pahalı token sınıfı
  jobs.job_kind             → pipeline / pr_review / refinement ayrımı

DİKKAT — bir `llm_calls` satırı tek bir API isteği DEĞİL, tek bir `claude -p`
alt süreci (çok turlu) demektir. Token toplamları o oturumun tüm turlarının
toplamıdır; tek istek bağlam boyutu için `turns`'e bölmek gerekir.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta

log = logging.getLogger("pipeline")

# Bir adımın "döngüye girdi" sayılması için iş başına gereken çağrı sayısı.
# Medyanın bu katı kadar üstü + mutlak taban birlikte aranır ki tek pahalı
# adım tek başına uyarı üretmesin.
LOOP_FACTOR = 2.0
LOOP_MIN_CALLS = 4.0

# Çok çağrılı olması TASARIM GEREĞİ olan adımlar. Kickoff bir toplantıdır
# (dört ajan, ~37 çağrı — CLAUDE.md'de yazılı); build gate ise testler yeşile
# dönene kadar geliştiriciyi döndürür (CREW_PR_BUILD_MAX_RETRIES). Bunlar için
# yüksek çağrı sayısı arıza değil, yine de göz önünde dursun diye bilgi olarak
# raporlanır — uyarı olarak değil.
BY_DESIGN_MULTI = frozenset({"kickoff_meeting_task", "pr_build_gate"})

# Token sinifi fiyat carpanlari (girdi = 1.0 referans). Model markasindan
# bagimsiz ORAN olduklari icin model karisimi ne olursa olsun gecerli.
# Onbellek YAZMA TTL'e gore degisir: 5 dk 1.25x, 1 saat 2.00x — hangisinin
# kullanildigi `usage.cache_creation` altinda gelir ama llm_calls'ta o kirilim
# saklanmiyor, o yuzden ikisi de hesaplanip aralik olarak raporlanir.
PRICE_WEIGHTS = {
    "input": (1.00, 1.00),
    "cache_write": (1.25, 2.00),
    "cache_read": (0.10, 0.10),
    "output": (5.00, 5.00),
}

# Onbellek yazimi okumaya gore bu orandan azsa yazma agir basiyor demektir.
# DIKKAT — dusuk oran "onbellek calismiyor" ANLAMINA GELMEZ. Cok turlu bir
# `claude -p` oturumunda konusma her turda buyur ve buyuyen kisim bir sonraki
# tur icin yeniden yazilir; yazma bu akista dogaldir. Oranin degeri, yazmanin
# maliyette ne kadar yer kapladigini haber vermesi.
CACHE_RATIO_WARN = 5.0

# Model ailesi payı bu yüzdeyi aşarsa "tek sınıfa bağımlıyız" bulgusu çıkar.
FAMILY_SHARE_WARN = 80.0

_FAMILIES = ("opus", "sonnet", "haiku")

# Kırılımda anahtarı boş gelen satırların etiketi (adım/ajan atanmamış çağrı).
UNASSIGNED = "(atanmamış)"


def _f(x, default: float = 0.0) -> float:
    """Decimal/None karışık gelen sayısal alanları float'a indirger."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _i(x, default: int = 0) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def _pct(part: float, whole: float) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


# ── Toplama ──────────────────────────────────────────────────────────────

def collect(*, since_days: int | None = None, job_kind: str | None = None,
            limit: int = 20000) -> list[dict]:
    """Pencere içindeki LLM çağrıları + ait oldukları işin künyesi.

    job_kind None ise tüm türler gelir (pipeline + pr_review + refinement);
    dry-run işler her hâlükârda dışarıda. job_id NULL olan çağrılar (bir işe
    bağlanamamış) toplamlara girer ama iş bazlı kırılımda görünmez.
    """
    from agile_sdlc_crew import db

    where = ["COALESCE(j.dry_run,0)=0"]
    params: list = []
    if since_days:
        where.append("l.created_at >= %s")
        params.append(datetime.now() - timedelta(days=int(since_days)))
    if job_kind:
        where.append("COALESCE(j.job_kind,'pipeline')=%s")
        params.append(job_kind)

    sql = (
        "SELECT l.job_id, l.step_key, l.agent, l.model, l.provider, l.turns, "
        "       l.tool_calls, l.cost_usd, l.duration_ms, l.input_tokens, "
        "       l.output_tokens, l.cache_read_tokens, l.cache_creation_tokens, "
        "       l.created_at, l.stop_reason, j.work_item_id, j.status AS job_status, "
        "       COALESCE(j.job_kind,'pipeline') AS job_kind "
        "FROM llm_calls l LEFT JOIN jobs j ON j.id = l.job_id "
        "WHERE " + " AND ".join(where) + " ORDER BY l.id DESC LIMIT %s"
    )
    with db.get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, params + [int(limit)])
        return list(cur.fetchall() or [])


# ── Analiz ───────────────────────────────────────────────────────────────

def _family(model: str | None) -> str:
    """Model kimliğini fiyat sınıfına indirger (opus/sonnet/haiku).

    Sürüm ve `[1m]` soneki fiyat kararında değil, sınıf kararında gürültü;
    "hangi sınıfa ne kadar ödüyoruz" sorusu sürümden bağımsız sorulmalı.
    """
    m = (model or "").lower()
    for f in _FAMILIES:
        if f in m:
            return f
    return UNASSIGNED


def _bucket() -> dict:
    return {"calls": 0, "usd": 0.0, "jobs": set(), "turns": 0, "tool_calls": 0,
            "ms": 0, "tok_in": 0, "tok_out": 0, "cache_r": 0, "cache_w": 0,
            "models": set()}


def _add(b: dict, r: dict) -> None:
    b["calls"] += 1
    b["usd"] += _f(r.get("cost_usd"))
    if r.get("job_id"):
        b["jobs"].add(r["job_id"])
    b["turns"] += _i(r.get("turns"))
    b["tool_calls"] += _i(r.get("tool_calls"))
    b["ms"] += _i(r.get("duration_ms"))
    b["tok_in"] += _i(r.get("input_tokens"))
    b["tok_out"] += _i(r.get("output_tokens"))
    b["cache_r"] += _i(r.get("cache_read_tokens"))
    b["cache_w"] += _i(r.get("cache_creation_tokens"))
    if r.get("model"):
        b["models"].add(r["model"])


def _row(key: str, b: dict, total_usd: float) -> dict:
    jobs = len(b["jobs"])
    billed = b["tok_in"] + b["tok_out"] + b["cache_r"] + b["cache_w"]
    return {
        "key": key,
        "calls": b["calls"],
        "jobs": jobs,
        "usd": round(b["usd"], 2),
        "share_pct": _pct(b["usd"], total_usd),
        "usd_per_job": round(b["usd"] / jobs, 2) if jobs else None,
        "usd_per_call": round(b["usd"] / b["calls"], 3) if b["calls"] else 0.0,
        "calls_per_job": round(b["calls"] / jobs, 1) if jobs else None,
        "turns": b["turns"],
        "tool_calls": b["tool_calls"],
        "minutes": round(b["ms"] / 60000.0, 1),
        "tok_in": b["tok_in"],
        "tok_out": b["tok_out"],
        "cache_read": b["cache_r"],
        "cache_write": b["cache_w"],
        "billed_tokens": billed,
        # 1M faturalanan token başına $. Çağrı başına $'dan daha istikrarlı
        # ama modeller arası FİYAT KIYASI için kullanılamaz: önbellek okuma
        # token'ı girdi token'ının onda biri fiyatlanır, dolayısıyla çok
        # önbellek okuyan bir model ucuz görünür. Sınıf kıyası için by_family
        # payına bakın; bu sütun aynı model içindeki değişimi izlemek içindir.
        "usd_per_mtok": round(b["usd"] / (billed / 1e6), 2) if billed else None,
        "models": sorted(b["models"]),
    }


def _cost_share(tok_in: int, tok_out: int, cache_r: int, cache_w: int) -> dict:
    """Her token sinifinin MALIYETTEKI payi (token sayisindaki payi degil).

    Iki senaryo doner cunku onbellek yazma TTL'i (5 dk / 1 saat) llm_calls'ta
    saklanmiyor: {sinif: {"min": %, "max": %}}. Yuzdeler girdi-esdegeri
    agirliklar uzerinden, yani model karisimindan bagimsiz.
    """
    counts = {"input": tok_in, "cache_write": cache_w,
              "cache_read": cache_r, "output": tok_out}
    lo = {k: counts[k] * PRICE_WEIGHTS[k][0] for k in counts}
    hi = {k: counts[k] * PRICE_WEIGHTS[k][1] for k in counts}
    slo, shi = sum(lo.values()), sum(hi.values())
    if not slo or not shi:
        return {}
    return {
        k: {"min": round(min(100 * lo[k] / slo, 100 * hi[k] / shi), 1),
            "max": round(max(100 * lo[k] / slo, 100 * hi[k] / shi), 1)}
        for k in counts
    }


def analyze(rows: list[dict]) -> dict:
    """Çağrı satırlarından adım / model / ajan / iş kırılımı ve toplamlar."""
    total_usd = sum(_f(r.get("cost_usd")) for r in rows)
    by_step: dict[str, dict] = defaultdict(_bucket)
    by_model: dict[str, dict] = defaultdict(_bucket)
    by_family: dict[str, dict] = defaultdict(_bucket)
    by_agent: dict[str, dict] = defaultdict(_bucket)
    by_kind: dict[str, dict] = defaultdict(_bucket)
    by_job: dict[int, dict] = defaultdict(_bucket)
    job_meta: dict[int, dict] = {}

    for r in rows:
        _add(by_step[r.get("step_key") or UNASSIGNED], r)
        _add(by_model[r.get("model") or UNASSIGNED], r)
        _add(by_family[_family(r.get("model"))], r)
        _add(by_agent[r.get("agent") or UNASSIGNED], r)
        _add(by_kind[r.get("job_kind") or "pipeline"], r)
        jid = r.get("job_id")
        if jid:
            _add(by_job[jid], r)
            job_meta.setdefault(jid, {
                "work_item_id": r.get("work_item_id"),
                "status": r.get("job_status"),
                "job_kind": r.get("job_kind") or "pipeline",
            })

    def table(d: dict) -> list[dict]:
        return sorted((_row(k, b, total_usd) for k, b in d.items()),
                      key=lambda x: x["usd"], reverse=True)

    jobs_tbl = []
    for jid, b in by_job.items():
        row = _row(str(jid), b, total_usd)
        row.update({"job_id": jid, **job_meta.get(jid, {})})
        jobs_tbl.append(row)
    jobs_tbl.sort(key=lambda x: x["usd"], reverse=True)

    n_jobs = len(by_job)
    tok_in = sum(_i(r.get("input_tokens")) for r in rows)
    tok_out = sum(_i(r.get("output_tokens")) for r in rows)
    cache_r = sum(_i(r.get("cache_read_tokens")) for r in rows)
    cache_w = sum(_i(r.get("cache_creation_tokens")) for r in rows)
    dates = [r["created_at"] for r in rows if r.get("created_at")]
    # Butce cap'i kesilen cagrilar: harcandi ama cikti dondurmedi (bkz. findings).
    # stop_reason sutunu yeni — eski satirlarda bos, o yuzden gecmis 0 gorunur.
    _cut = [r for r in rows if (r.get("stop_reason") or "") == "error_max_budget_usd"]

    return {
        "totals": {
            "usd": round(total_usd, 2),
            "calls": len(rows),
            "jobs": n_jobs,
            "usd_per_job": round(total_usd / n_jobs, 2) if n_jobs else None,
            "calls_per_job": round(len(rows) / n_jobs, 1) if n_jobs else None,
            "minutes": round(sum(_i(r.get("duration_ms")) for r in rows) / 60000.0),
            "first": min(dates).isoformat(sep=" ", timespec="minutes") if dates else None,
            "last": max(dates).isoformat(sep=" ", timespec="minutes") if dates else None,
        },
        "token_mix": {
            "input": tok_in, "output": tok_out,
            "cache_read": cache_r, "cache_write": cache_w,
            # Okuma/yazma oranı: yüksekse yazılan prefix çok kez okunuyor.
            "cache_ratio": round(cache_r / cache_w, 1) if cache_w else None,
            # Token payı ≠ maliyet payı. Önbellek okuma girdinin ONDA BİRİ
            # fiyatlanır, yazma ise 1.25–2 KATI; dolayısıyla token sayısına
            # bakıp optimize etmek yanlış yeri hedefler. Ağırlıklı pay her
            # sınıfın maliyette gerçekte ne kadar yer kapladığını verir.
            "cost_share": _cost_share(tok_in, tok_out, cache_r, cache_w),
        },
        "budget_cut": {
            "calls": len(_cut),
            "usd": round(sum(_f(r.get("cost_usd")) for r in _cut), 2),
            "jobs": len({r.get("job_id") for r in _cut if r.get("job_id")}),
        },
        "by_step": table(by_step),
        "by_model": table(by_model),
        "by_family": table(by_family),
        "by_agent": table(by_agent),
        "by_kind": table(by_kind),
        "by_job": jobs_tbl,
    }


# ── Bulgular (eşiğe bağlı, deterministik) ────────────────────────────────

def findings(a: dict) -> list[dict]:
    """Sayılardan çıkan, tartışmaya açık olmayan gözlemler.

    Her bulgu {level, title, detail} döner. Öneri üretir ama hiçbir şeyi
    kendiliğinden değiştirmez — retrospektifteki kural önerileriyle aynı
    sözleşme: karar insanın.
    """
    out: list[dict] = []
    total = _f(a["totals"]["usd"])
    if total <= 0:
        return out

    # 1) Yoğunlaşma — ilk iki adım harcamanın yarısından fazlasını yiyorsa
    steps = [s for s in a["by_step"] if s["key"] != UNASSIGNED]
    top2 = steps[:2]
    if len(top2) == 2:
        share = sum(s["share_pct"] for s in top2)
        if share >= 50:
            out.append({
                "level": "info",
                "title": f"Harcama yoğunlaşması: %{share:.0f} iki adımda",
                "detail": " · ".join(
                    f"{s['key']} ${s['usd']:.2f} (%{s['share_pct']:.0f})" for s in top2
                ) + " — optimizasyon eforu önce buraya.",
            })

    # 2) Döngü şüphesi — iş başına çağrısı medyanın LOOP_FACTOR katı üstünde
    cpj = sorted(s["calls_per_job"] for s in steps if s["calls_per_job"])
    if cpj:
        median = cpj[len(cpj) // 2]
        for s in steps:
            c = s["calls_per_job"]
            if not c or c < max(median * LOOP_FACTOR, LOOP_MIN_CALLS):
                continue
            if s["key"] in BY_DESIGN_MULTI:
                out.append({
                    "level": "info",
                    "title": f"{s['key']}: iş başına {c:.1f} çağrı (tasarım gereği)",
                    "detail": (f"Bu adım zaten çok çağrılı — arıza değil. İş başına "
                               f"${s['usd_per_job']:.2f}; pahalı bulunuyorsa çözüm "
                               f"döngüyü aramak değil, adımı kısmak."),
                })
            else:
                out.append({
                    "level": "warn",
                    "title": f"{s['key']}: iş başına {c:.1f} çağrı",
                    "detail": (f"Adım medyanı {median:.1f}. Retry/yeniden-plan döngüsü "
                               f"ya da fazla ReAct iterasyonu olabilir; iş başına "
                               f"${s['usd_per_job']:.2f}."),
                })

    # 3) Model sınıfı yoğunlaşması. Modeller arası $ kıyası token karışımına
    #    boğulduğu için (bkz. usd_per_mtok notu) burada fiyat değil PAY konuşur:
    #    tek sınıfa bağımlıysak, ucuz sınıfa inebilecek adım var mı sorusu açılır.
    fams = [m for m in a["by_family"] if m["key"] != UNASSIGNED]
    if fams:
        top = fams[0]
        if top["share_pct"] >= FAMILY_SHARE_WARN:
            others = ", ".join(
                f"{m['key']} %{m['share_pct']:.0f}" for m in fams[1:]
            ) or "başka sınıf kullanılmamış"
            out.append({
                "level": "warn",
                "title": f"Harcama yoğunlaşması: %{top['share_pct']:.0f} tek model sınıfında ({top['key']})",
                "detail": (f"{top['calls']} çağrı, ${top['usd']:.2f}. Diğerleri: {others}. "
                           f"Her adım gerçekten bu sınıfı gerektiriyor mu — "
                           f"config/agent_llm_overrides.yaml ile ajan bazında düşürülebilir."),
            })

    # 4) Önbellek ekonomisi. Token payına bakıp karar vermeyin: okuma token'ı
    #    girdinin onda biri fiyatlanır, yazma 1.25-2 katı. Bu yüzden bulgu
    #    ağırlıklı pay üzerinden konuşur ve yüksek okuma payını ÖVER — o,
    #    tekrar gönderimin ucuzlatıldığı anlamına gelir, sorun değil.
    mix = a["token_mix"]
    share = mix.get("cost_share") or {}
    ratio = mix["cache_ratio"]
    if share:
        rd, wr = share.get("cache_read", {}), share.get("cache_write", {})
        tok_read_pct = _pct(mix["cache_read"],
                            mix["input"] + mix["cache_read"] + mix["cache_write"])
        out.append({
            "level": "info",
            "title": (f"Önbellek okuma token'ların %{tok_read_pct:.0f}'i ama "
                      f"maliyetin %{rd.get('min', 0):.0f}–%{rd.get('max', 0):.0f}'i"),
            "detail": (f"Okuma girdinin onda biri fiyatlanır — yüksek okuma payı İYİDİR, "
                       f"tekrar gönderim ucuzlatılıyor demektir. Asıl ağırlık yazmada: "
                       f"token'ların %{_pct(mix['cache_write'], mix['input'] + mix['cache_read'] + mix['cache_write']):.0f}'i "
                       f"ama maliyetin %{wr.get('min', 0):.0f}–%{wr.get('max', 0):.0f}'i."),
        })
    if ratio is not None and ratio < CACHE_RATIO_WARN:
        out.append({
            "level": "warn",
            "title": f"Önbellek okuma/yazma oranı {ratio:.1f}",
            "detail": ("Yazılan her prefix ortalama bu kadar kez okunuyor. Çok turlu "
                       "oturumlarda konuşma büyüdükçe yeniden yazma DOĞALDIR — bu "
                       "'önbellek bozuk' demek değil. Oranı yükseltmenin yolu tur "
                       "sayısını ya da oturum başına biriken içeriği azaltmak."),
        })

    # 4b) Bütçe cap'i kesen çağrılar — HARCANDI AMA ÇIKTI YOK.
    # `claude --max-budget-usd` sınıra çarpınca is_error=True döner, `result`
    # boş gelir ve stream'de text bloğu olmadığı için salvage de kurtaramaz.
    # Bu yüzden ayrı bir bulgu: pahalı adımın maliyeti değil, KAYBI.
    cut = a.get("budget_cut") or {}
    if cut.get("calls"):
        out.append({
            "level": "warn",
            "title": f"{cut['calls']} çağrı bütçe cap'inde kesildi — ${cut['usd']:.2f} karşılıksız",
            "detail": ("CREW_CLI_CALL_MAX_USD sınıra çarptığında çağrı çıktı DÖNDÜRMEZ "
                       "(salvage da boş) — para harcanır, iş kaybolur. Cap'i yükseltin "
                       "ya da 0 yapıp turu/prefix'i kısarak maliyeti düşürün."),
        })

    # 5) En pahalı iş, ortalamanın kaç katı
    jobs = a["by_job"]
    avg = a["totals"]["usd_per_job"]
    if jobs and avg:
        top = jobs[0]
        if top["usd"] >= avg * 3:
            out.append({
                "level": "warn",
                "title": f"#{top['job_id']} (WI {top.get('work_item_id') or '—'}): ${top['usd']:.2f}",
                "detail": (f"İş ortalaması ${avg:.2f} — bunun {top['usd'] / avg:.1f} katı. "
                           f"{top['calls']} çağrı. Bütçe zarfı bu işte tutmamış olabilir."),
            })
    return out


# ── Rapor ────────────────────────────────────────────────────────────────

def _tbl(rows: list[dict], cols: list[tuple[str, str, str]]) -> str:
    """cols: (başlık, anahtar, biçim) — biçim: s|usd|num|pct."""
    head = "| " + " | ".join(c[0] for c in cols) + " |"
    sep = "|" + "|".join("---" for _ in cols) + "|"
    lines = [head, sep]
    for r in rows:
        cells = []
        for _, key, fmt in cols:
            v = r.get(key)
            if v is None:
                cells.append("—")
            elif fmt == "usd":
                cells.append(f"${_f(v):.2f}")
            elif fmt == "pct":
                cells.append(f"%{_f(v):.0f}")
            elif fmt == "num":
                cells.append(f"{v:,}".replace(",", ".") if isinstance(v, int) else str(v))
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render_markdown(a: dict, *, title: str, fnd: list[dict] | None = None) -> str:
    t = a["totals"]
    parts = [f"# Maliyet Analizi — {title}", ""]
    parts.append(
        f"**${t['usd']:.2f}** · {t['calls']} çağrı · {t['jobs']} iş · "
        f"iş başına **${t['usd_per_job'] or 0:.2f}** ({t['calls_per_job'] or 0} çağrı) · "
        f"{t['minutes']} dk model süresi"
    )
    if t["first"]:
        parts.append(f"_Pencere: {t['first']} → {t['last']}_")
    parts.append("")

    if fnd:
        parts += ["## Bulgular", ""]
        for f in fnd:
            mark = "⚠️" if f["level"] == "warn" else "ℹ️"
            parts.append(f"- {mark} **{f['title']}** — {f['detail']}")
        parts.append("")

    parts += ["## Adım bazında", "", _tbl(a["by_step"], [
        ("Adım", "key", "s"), ("$", "usd", "usd"), ("Pay", "share_pct", "pct"),
        ("İş", "jobs", "s"), ("$/iş", "usd_per_job", "usd"),
        ("Çağrı/iş", "calls_per_job", "s"), ("dk", "minutes", "s"),
    ]), ""]

    parts += ["## Model sınıfı bazında", "", _tbl(a["by_family"], [
        ("Sınıf", "key", "s"), ("$", "usd", "usd"), ("Pay", "share_pct", "pct"),
        ("Çağrı", "calls", "s"), ("$/çağrı", "usd_per_call", "usd"),
    ]), ""]

    parts += ["## Model bazında", "", _tbl(a["by_model"], [
        ("Model", "key", "s"), ("$", "usd", "usd"), ("Pay", "share_pct", "pct"),
        ("Çağrı", "calls", "s"), ("$/çağrı", "usd_per_call", "usd"),
        ("$/1M token", "usd_per_mtok", "usd"),
    ]), ""]

    parts += ["## Ajan bazında", "", _tbl(a["by_agent"], [
        ("Ajan", "key", "s"), ("$", "usd", "usd"), ("Pay", "share_pct", "pct"),
        ("Çağrı", "calls", "s"), ("$/çağrı", "usd_per_call", "usd"),
    ]), ""]

    if len(a["by_kind"]) > 1:
        parts += ["## İş türü bazında", "", _tbl(a["by_kind"], [
            ("Tür", "key", "s"), ("$", "usd", "usd"), ("Pay", "share_pct", "pct"),
            ("İş", "jobs", "s"), ("$/iş", "usd_per_job", "usd"),
        ]), ""]

    mix = a["token_mix"]
    parts += ["## Token dağılımı", "",
              f"- Girdi: {mix['input']:,}".replace(",", ".") ,
              f"- Çıktı: {mix['output']:,}".replace(",", "."),
              f"- Önbellek okuma: {mix['cache_read']:,}".replace(",", "."),
              f"- Önbellek yazma: {mix['cache_write']:,}".replace(",", "."),
              f"- Okuma/yazma oranı: {mix['cache_ratio'] if mix['cache_ratio'] is not None else '—'}",
              ""]
    share = mix.get("cost_share") or {}
    if share:
        _ad = {"input": "Girdi", "cache_write": "Önbellek yazma",
               "cache_read": "Önbellek okuma", "output": "Çıktı"}
        parts += ["### Maliyet payı (token sayısı değil, ağırlıklı)", "",
                  "_Önbellek okuma girdinin 0.1 katı, yazma 1.25–2 katı, çıktı 5 katı "
                  "fiyatlanır — aralık yazma TTL'inden (5 dk / 1 saat) gelir._", ""]
        for k in ("input", "cache_write", "cache_read", "output"):
            v = share.get(k) or {}
            parts.append(f"- {_ad[k]}: %{v.get('min', 0):.0f}–%{v.get('max', 0):.0f}")
        parts.append("")

    top = a["by_job"][:10]
    if top:
        parts += ["## En pahalı işler", "", _tbl(top, [
            ("İş", "job_id", "s"), ("WI", "work_item_id", "s"),
            ("Tür", "job_kind", "s"), ("Durum", "status", "s"),
            ("$", "usd", "usd"), ("Çağrı", "calls", "s"),
        ]), ""]
    return "\n".join(parts)


def build_report(*, title: str, since_days: int | None = None,
                 job_kind: str | None = None) -> dict:
    """Dashboard/API için tek paket: toplamlar + tablolar + bulgular + markdown."""
    rows = collect(since_days=since_days, job_kind=job_kind)
    a = analyze(rows)
    fnd = findings(a)
    return {
        "title": title,
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "since_days": since_days,
        "job_kind": job_kind,
        **a,
        "findings": fnd,
        "markdown": render_markdown(a, title=title, fnd=fnd),
    }
