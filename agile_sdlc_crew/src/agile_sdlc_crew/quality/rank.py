"""Kalite işi alımı — gruplama, skor, SP ve bütçe seçimi.

Saf fonksiyonlar: ağa çıkmaz, LLM yok. Sıra: birim (parmak izi başına Group)
→ dedup (intake'te) → birleştirme/paketleme → seçim. Dedup birimler üzerinde
çalışır ki bir paketteki tek açık madde bütün paketi engellemesin.
"""

from __future__ import annotations

import hashlib
import re

from agile_sdlc_crew.quality.model import Finding, Group

FP_MAX = 512


def cap_fingerprint(fp: str, limit: int = FP_MAX) -> str:
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

    s = _QUOTED.sub(stash, (msg or "").strip())
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
