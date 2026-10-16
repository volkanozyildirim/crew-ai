"""Kalite işi alımı — gruplama, skor, SP ve bütçe seçimi.

Saf fonksiyonlar: ağa çıkmaz, LLM yok. Sıra: birim (parmak izi başına Group)
→ dedup (intake'te) → birleştirme/paketleme → seçim. Dedup birimler üzerinde
çalışır ki bir paketteki tek açık madde bütün paketi engellemesin.
"""

from __future__ import annotations

import hashlib
import math
import re

from agile_sdlc_crew.quality.model import Finding, Group

FP_MAX = 512
# Dış metin (NR mesajı) parmak izine girer; içinde "tempo-fp" geçerse board'dan
# geri okurken sahte satır gibi bölünür ve başka bir bulguyu bastırabilir.
FP_MARK = re.compile(r"tempo-fp", re.I)


def defang(text: str) -> str:
    return FP_MARK.sub("tempo_fp", text or "")


def cap_fingerprint(fp: str, limit: int = FP_MAX) -> str:
    fp = defang(fp)
    if len(fp) <= limit:
        return fp
    digest = hashlib.sha1(fp.encode("utf-8")).hexdigest()[:16]
    return fp[: limit - 17] + ":" + digest


# ── New Relic ────────────────────────────────────────────────────────────

_TX_PREFIX = re.compile(r"^(?:WebTransaction|OtherTransaction)/[^/]+/")


def _studly(part: str) -> str:
    return "".join(w[:1].upper() + w[1:] for w in re.split(r"[-_]", part) if w)


def action_of(transaction_name: str) -> str:
    """'WebTransaction/Action/api::order::integrationCancel' → 'Api/Order::integrationCancel'."""
    t = _TX_PREFIX.sub("", (transaction_name or "").strip())
    parts = t.split("::")
    if len(parts) < 2 or not all(parts):
        return t
    return "/".join(_studly(p) for p in parts[:-1]) + "::" + parts[-1]


_QUOTED = re.compile(r'"([^"]*)"|\'([^\']*)\'')
_ID_LIKE = re.compile(
    r"^(?:\d+|[0-9a-f]{8}-[0-9a-f-]{27}|[0-9a-f]{16,}|https?://\S+|[\w.+-]+@[\w.-]+)$", re.I)
_DYNAMIC = (
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "<email>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{16,}\b", re.I), "<hex>"),
    (re.compile(r"\d+"), "<n>"),
)


def normalize_message(msg: str) -> str:
    """Dinamik parçaları maskele; tırnak içindeki anahtar ADINI koru.

    'Undefined array key "host"' ≠ '"password"' (ayrı iş), ama
    'Order 12345 failed' == 'Order 98 failed' (aynı iş)."""
    kept: list[str] = []

    def stash(m: re.Match) -> str:
        dq = m.group(1) is not None
        inner = m.group(1) if dq else m.group(2)
        if _ID_LIKE.match(inner) or len(inner) > 60:
            inner = "<v>"
        q = '"' if dq else "'"
        kept.append(f"{q}{inner}{q}")
        return "\ue000" + chr(0xE100 + len(kept) - 1)

    # İşaretçi aralığı girdiden atılır; yoksa dış mesajdaki U+E000 geri yüklemeyi bozar.
    s = _QUOTED.sub(stash, re.sub("[\ue000-\uf8ff]", "", msg or "").strip())
    for rx, rep in _DYNAMIC:
        s = rx.sub(rep, s)
    s = re.sub("\ue000(.)", lambda m: kept[ord(m.group(1)) - 0xE100], s)
    return re.sub(r"\s+", " ", s).strip()[:300]


def nr_fingerprint(repo: str, action: str, err_class: str, norm: str) -> str:
    return cap_fingerprint(f"nr:{repo}:{action}:{err_class}:{norm}")


_UNDEF = re.compile(r"^Undefined (?:array key|index|offset|variable|property)\b", re.I)
_WARN_CLASSES = {"E_WARNING", "E_NOTICE", "E_USER_WARNING", "E_USER_NOTICE"}
_INFRA = re.compile(
    r"timed? ?out|timeout|connection (?:refused|reset|timed)|could not connect|"
    r"server has gone away|deadlock|curl error|name or service not known|"
    r"no route to host|too many connections|getaddrinfo", re.I)


def is_undefined_key(g: Group) -> bool:
    f = g.findings[0]
    return f.err_class.upper() in _WARN_CLASSES and bool(_UNDEF.search(f.message))


def _size_nr(g: Group) -> None:
    f = g.findings[0]
    if _INFRA.search(f.message) or _INFRA.search(f.err_class):
        g.sp, g.needs_human = 3, True
        g.human_reason = "Altyapı kökenli hata — kod değişikliğiyle çözülmeyebilir"
    elif is_undefined_key(g):
        g.sp = 1
    else:
        g.sp = 3


def nr_units(findings: list[Finding]) -> list[Group]:
    """Parmak izi başına bir Group; uygulamalar arası birleşir. Skor = adet × uygulama sayısı."""
    by: dict[str, Group] = {}
    for f in findings:
        norm = normalize_message(f.message)
        fp = nr_fingerprint(f.repo, f.action, f.err_class, norm)
        g = by.get(fp)
        if g is None:
            g = by[fp] = Group(source="newrelic", repo=f.repo, title=f"{f.action}: {norm}",
                               fingerprints=[fp], findings=[])
        g.findings.append(f)
    for g in by.values():
        _size_nr(g)
        g.score = float(g.total * max(1, len(g.apps)))
    return list(by.values())


def merge_undefined(units: list[Group]) -> list[Group]:
    """Aynı repo + action'daki tanımsız-anahtar birimlerini tek Task'ta topla.
    Parmak izleri anahtar başına kalır; ≤3 anahtar 1 SP, fazlası 2 SP."""
    out: list[Group] = []
    fam: dict[tuple[str, str], list[Group]] = {}
    for g in units:
        if g.needs_human or not is_undefined_key(g):
            out.append(g)
            continue
        fam.setdefault((g.repo, g.findings[0].action), []).append(g)
    for (repo, action), gs in fam.items():
        if len(gs) == 1:
            out.append(gs[0])
            continue
        gs.sort(key=lambda x: -x.score)
        m = Group(source="newrelic", repo=repo,
                  title=f"{action}: tanımsız dizi anahtarı ({len(gs)} anahtar)",
                  fingerprints=[fp for x in gs for fp in x.fingerprints],
                  findings=[f for x in gs for f in x.findings])
        m.sp = 1 if len(gs) <= 3 else 2
        m.score = sum(x.score for x in gs)
        m.recurring_of = next((x.recurring_of for x in gs if x.recurring_of), None)
        out.append(m)
    return out


# ── Sonar ────────────────────────────────────────────────────────────────

SONAR_WEIGHT = {
    ("VULNERABILITY", "BLOCKER"): 20,
    ("VULNERABILITY", "CRITICAL"): 10,
    ("BUG", "BLOCKER"): 8,
    ("BUG", "CRITICAL"): 4,
}


def sonar_fingerprint(repo: str, file: str, rule: str) -> str:
    return cap_fingerprint(f"sonar:{repo}:{file}:{rule}")


def sonar_unit_sp(count: int) -> int:
    return 1 if count <= 5 else 2 if count <= 20 else 3


def sonar_units(findings: list[Finding]) -> list[Group]:
    """repo + dosya + kural başına bir Group. Skor = ağırlık × (1 + log10(adet)):
    log10, 300 CSS uyarısının tek güvenlik açığını geçmesini engeller."""
    by: dict[str, Group] = {}
    for f in findings:
        fp = sonar_fingerprint(f.repo, f.file, f.rule)
        g = by.get(fp)
        if g is None:
            name = f.file.rsplit("/", 1)[-1] or f.project
            g = by[fp] = Group(source="sonar", repo=f.repo, title=f"{f.rule_name or f.rule} — {name}",
                               fingerprints=[fp], findings=[])
        g.findings.append(f)
    for g in by.values():
        w = max(SONAR_WEIGHT.get((f.itype.upper(), f.severity.upper()), 1) for f in g.findings)
        g.score = w * (1 + math.log10(max(1, g.total)))
        if g.findings[0].rule.startswith("secrets:"):
            g.sp, g.needs_human = 1, True
            g.human_reason = "Secret rotasyonu gerekir — koddan silmek yetmez"
        else:
            g.sp = sonar_unit_sp(g.total)
    return list(by.values())


def pack_sonar(units: list[Group], pack_sp: int = 3, pack_files: int = 5) -> list[Group]:
    """Aynı repo + kural birimlerini ≤pack_sp SP ve ≤pack_files dosyalık Task'larda topla.
    Paket skoru = en yüksek birim skoru (toplam değil — adet baskınlığı geri gelmesin)."""
    buckets: dict[tuple[str, str, bool], list[Group]] = {}
    for u in units:
        buckets.setdefault((u.repo, u.findings[0].rule, u.needs_human), []).append(u)
    out: list[Group] = []
    for (repo, _rule, _human), us in buckets.items():
        us.sort(key=lambda x: (-x.score, x.fingerprints[0]))
        cur: Group | None = None
        for u in us:
            if (cur is not None and cur.sp + u.sp <= pack_sp
                    and len(cur.files) + len(u.files) <= pack_files):
                cur.fingerprints += u.fingerprints
                cur.findings += u.findings
                cur.sp += u.sp
                cur.score = max(cur.score, u.score)
                cur.recurring_of = cur.recurring_of or u.recurring_of
                continue
            cur = Group(source="sonar", repo=repo, title=u.title, fingerprints=list(u.fingerprints),
                        findings=list(u.findings), score=u.score, sp=u.sp, needs_human=u.needs_human,
                        human_reason=u.human_reason, recurring_of=u.recurring_of)
            out.append(cur)
    for g in out:
        n = len(g.files)
        if n > 1:
            g.title = f"{g.title} (+{n - 1} dosya)"
    return out


# ── Seçim ────────────────────────────────────────────────────────────────

def select_budget(groups: list[Group], budget_sp: int, *, human_share: float = 0.3,
                  max_tasks: int = 8) -> list[Group]:
    """Skora göre açgözlü doldurma: sığmayan atlanır, sıradaki küçük denenir.
    needs_human işler bütçenin en çok human_share'ini (aşağı yuvarlanmış SP) kaplar."""
    ordered = sorted(groups, key=lambda g: (
        -g.score, -max((f.error_rate for f in g.findings), default=0.0), g.sp, g.fingerprints[0]))
    human_cap = int(budget_sp * human_share)
    picked: list[Group] = []
    used = human_used = 0
    for g in ordered:
        if len(picked) >= max_tasks:
            break
        if used + g.sp > budget_sp:
            continue
        if g.needs_human and human_used + g.sp > human_cap:
            continue
        picked.append(g)
        used += g.sp
        if g.needs_human:
            human_used += g.sp
    return picked
