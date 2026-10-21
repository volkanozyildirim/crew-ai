# Kalite İşi Alımı (New Relic + SonarQube) — Uygulama Planı

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** "New Relic'ten / Sonar'dan N SP al" komutuyla kalite bulgularını deterministik seçip sprintte User Story + child Task olarak açmak ve her Task'ı pipeline kuyruğuna almak.

**Architecture:** Yeni `src/agile_sdlc_crew/quality/` paketi. Toplayıcılar (`sonar.py`, `newrelic.py`) ham veriyi `Finding`'e çevirir; `app_map.py` NR uygulamasını FLODevOps build dosyalarından repoya eşler; `rank.py` (saf) birim → skor → SP → birleştirme/paket → bütçe seçimi yapar; `dedup.py` board'daki `tempo-fp:` satırlarından açık/kapalı durumu okur; `render.py` Türkçe WI metnini üretir; `intake.py` hepsini orkestre eder (preview/take). `server.py` dört endpoint, `index.html` Genel bakış'ta bir kart. Seçimde LLM yok.

**Tech Stack:** Python 3.13 (`.venv`), requests, PyYAML, pymysql, FastAPI; Azure DevOps REST, SonarQube 10.7 Web API, New Relic NerdGraph (EU).

**Spec:** `docs/superpowers/specs/2026-10-13-quality-intake-design.md`

## Global Constraints

- Yeni bağımlılık yok (requests, PyYAML, pymysql, fastapi, httpx zaten kurulu).
- Kullanıcıya görünen her metin (WI başlık/açıklama, hata mesajları, dashboard) **Türkçe**.
- `CREW_QUALITY_INTAKE` varsayılan **false**; kapalıyken `preview`/`take`/`unmapped` → **403**.
- New Relic uç noktası `https://api.eu.newrelic.com/graphql` (`NEW_RELIC_REGION` varsayılan `eu`).
- Sonar kapsamı: `types=BUG,VULNERABILITY`, `severities=BLOCKER,CRITICAL`, `issueStatuses=OPEN,CONFIRMED`.
- Etiketler: `tempo-quality`, `tempo-src-newrelic` | `tempo-src-sonar`, needs_human için `needs-human`.
- Story başlığı: `Kalite: New Relic hataları — <Sprint adı>` / `Kalite: Sonar bulguları — <Sprint adı>`.
- Task açıklamasının **ilk satırı** `<p><b>Repo:</b> <code><repo></code></p>`.
- Parmak izi satırı görünür: `<p><small>tempo-fp: <parmak izi></small></p>`; parmak izi ≤512 karakter.
- `secrets:*` madde mesajı hiçbir yere (Finding, WI) yazılmaz; WI metinleri `mask_secrets`'tan geçer.
- SP alanı `estimation.write_story_points` ile yazılır (Custom.StoryPoints → şablon alanları).
- Test altyapısı yok: testler `tests/test_katman0_gates.py` içinde `check()`/`skip()` ile, `main()` listesine eklenir. Tek test koşturma kalıbı:
  `.venv/bin/python -c "import sys; sys.path.insert(0,'tests'); import test_katman0_gates as t; t.<TEST>(); sys.exit(1 if t.FAIL else 0)"`
- pytest/ruff çağırma. Kod değişikliğinden sonra sunucu yeniden başlatılır — önce `curl -s localhost:8765/api/health` → `running:0` doğrulanır.

## Review Focus

- **Düzeltme merge edildi ama canlıya çıkmadı** (WI `Ready for Production`/`Resolved`) ve NR hâlâ hata sayıyor → aynı iş tekrar açılmamalı. (Task 5 testi)
- **İnsan bir kalite Task'ını Canceled yaptı** → o parmak izi bir daha asla açılmamalı. (Task 5 testi)
- **Sonar `secrets:*` mesajı ya da NR mesajında token/parola** → WI başlık/açıklamasına girmemeli. (Task 4 + Task 7 testleri)
- **Aynı hata farklı ID/sayılarla** (`Order 12345 failed` / `Order 98 failed`) → tek parmak izi; çok uzun mesaj → kararlı 512 karakterlik parmak izi. (Task 1 testi)
- **Bütçe en küçük adaydan küçük ya da aday yok** → hiçbir WI (story dahil) açılmamalı, açık mesaj dönmeli; aktif sprint yoksa da hiçbir şey açılmamalı. (Task 3 + Task 8 testleri)

---

### Task 1: Veri modeli + New Relic birimleri (normalizasyon, birleşme, SP, skor)

**Files:**
- Create: `src/agile_sdlc_crew/quality/__init__.py`
- Create: `src/agile_sdlc_crew/quality/model.py`
- Create: `src/agile_sdlc_crew/quality/rank.py`
- Test: `tests/test_katman0_gates.py` (yeni `test_quality_rank_nr`, `main()` listesine eklenir)

**Interfaces:**
- Produces: `model.Finding`, `model.Group` (property'ler `last_seen`, `apps`, `files`, `total`), `model.QualitySourceError`; `rank.cap_fingerprint(fp) -> str`, `rank.action_of(txn) -> str`, `rank.normalize_message(msg) -> str`, `rank.nr_fingerprint(repo, action, err_class, norm) -> str`, `rank.nr_units(findings) -> list[Group]`, `rank.is_undefined_key(group) -> bool`, `rank.merge_undefined(units) -> list[Group]`.

- [ ] **Step 1: Testi yaz**

`tests/test_katman0_gates.py` içine, `main()`'den önce ekle:

```python
def test_quality_rank_nr():
    print("\n[Q1] Kalite alımı — NR normalizasyon, birleşme, SP")
    from agile_sdlc_crew.quality import rank
    from agile_sdlc_crew.quality.model import Finding
    n = rank.normalize_message
    check("dizi anahtarı adı korunur",
          n('Undefined array key "host"') == 'Undefined array key "host"'
          and n('Undefined array key "host"') != n('Undefined array key "password"'))
    check("sayı/uuid/ID-benzeri tırnak içi maskelenir",
          n("Order 12345 not found") == n("Order 98 not found") == "Order <n> not found"
          and n('Product "550e8400-e29b-41d4-a716-446655440000" missing') == 'Product "<v>" missing'
          and n("Undefined array key 7") == "Undefined array key <n>")
    check("action: WebTransaction öneki atılır, controller StudlyCase",
          rank.action_of("WebTransaction/Action/api::order::integrationCancel") == "Api/Order::integrationCancel"
          and rank.action_of("WebTransaction/Custom/index::index") == "Index::index"
          and rank.action_of("WebTransaction/Action/web-hook::trigger") == "WebHook::trigger")
    fp1 = rank.nr_fingerprint("webservice", "A::b", "E_WARNING", "x" * 900)
    check("parmak izi 512'de kesilir ve kararlıdır",
          len(fp1) == 512 and fp1 == rank.nr_fingerprint("webservice", "A::b", "E_WARNING", "x" * 900))

    def nf(app, msg, cnt, action="Content::page", cls="E_WARNING", repo="webservice"):
        return Finding(source="newrelic", repo=repo, app=app, action=action, err_class=cls,
                       message=msg, count=cnt, last_seen=2000.0, error_rate=0.01)

    sites = [f"[Prod] site{i}.ru" for i in range(6)]
    units = rank.nr_units([nf(s, 'Undefined array key "value"', 100) for s in sites])
    check("6 uygulamadaki aynı hata → tek birim, 6 uygulama, skor = adet × uygulama, 1 SP",
          len(units) == 1 and len(units[0].apps) == 6 and units[0].score == 600 * 6 and units[0].sp == 1)
    u2 = rank.nr_units([nf("[Prod] a", "Order 1 failed", 5, cls="TypeError"),
                        nf("[Prod] a", "Order 2 failed", 7, cls="TypeError")])
    check("ID farkı aynı birime düşer; TypeError → 3 SP",
          len(u2) == 1 and u2[0].total == 12 and u2[0].sp == 3 and not u2[0].needs_human)
    infra = rank.nr_units([nf("[Prod] a", "cURL error 28: Operation timed out after 5000 ms", 50,
                              cls="GuzzleHttp\\Exception\\ConnectException")])
    check("altyapı hatası → 3 SP + needs_human", infra[0].sp == 3 and infra[0].needs_human)
    fam = rank.nr_units([nf("[Prod] a", f'Undefined array key "{c}"', 10, action="Api/Order::create")
                         for c in "abcde"]
                        + [nf("[Prod] a", 'Undefined array key "host"', 10, action="Index::index")])
    merged = rank.merge_undefined(fam)
    big = [g for g in merged if g.findings[0].action == "Api/Order::create"]
    check("aynı action'daki 5 tanımsız anahtar → tek Task, 5 parmak izi, 2 SP; başka action ayrı",
          len(merged) == 2 and len(big) == 1 and len(big[0].fingerprints) == 5 and big[0].sp == 2)
    three = rank.merge_undefined(rank.nr_units(
        [nf("[Prod] a", f"Undefined index: {c}", 10, action="X::y", cls="E_NOTICE") for c in ("ka", "kb", "kc")]))
    check("≤3 anahtar → 1 SP", len(three) == 1 and three[0].sp == 1 and len(three[0].fingerprints) == 3)
```

`main()` içindeki test tuple'ının sonuna `test_quality_rank_nr,` ekle (`test_wi_comments_context` satırından sonra).

- [ ] **Step 2: Testin düştüğünü gör**

Run: `.venv/bin/python -c "import sys; sys.path.insert(0,'tests'); import test_katman0_gates as t; t.test_quality_rank_nr(); sys.exit(1 if t.FAIL else 0)"`
Expected: `ModuleNotFoundError: No module named 'agile_sdlc_crew.quality'`

- [ ] **Step 3: Kodu yaz**

`src/agile_sdlc_crew/quality/__init__.py`:

```python
"""Kalite işi alımı: SonarQube + New Relic bulgularından sprint işi açma."""
```

`src/agile_sdlc_crew/quality/model.py`:

```python
"""Kalite işi alımı — ortak veri yapıları (Sonar + New Relic)."""

from __future__ import annotations

from dataclasses import dataclass, field


class QualitySourceError(RuntimeError):
    """Kaynak (Sonar / New Relic / FLODevOps) okunamadı — kısmi veriyle seçim yapılmaz."""


@dataclass
class Finding:
    source: str                     # "sonar" | "newrelic"
    repo: str = ""
    count: int = 1
    # Sonar
    project: str = ""               # "EcomBackend:webservice"
    file: str = ""                  # "app/Controller/Api/Order.php"
    rule: str = ""                  # "php:S2076"
    rule_name: str = ""
    severity: str = ""              # BLOCKER | CRITICAL
    itype: str = ""                 # BUG | VULNERABILITY
    lines: list[int] = field(default_factory=list)
    link: str = ""
    # New Relic
    app: str = ""
    action: str = ""                # "Api/Order::integrationCancel"
    err_class: str = ""
    message: str = ""               # ham mesaj; render maskeler. secrets:* için hep boş
    uris: list[str] = field(default_factory=list)
    first_seen: float = 0.0         # epoch saniye
    last_seen: float = 0.0
    error_rate: float = 0.0         # uygulamanın lookback error rate'i (0-1)


@dataclass
class Group:
    """Tek WI'a dönüşecek iş birimi. Birden çok parmak izi taşıyabilir (paket/aile)."""
    source: str
    repo: str
    title: str                      # repo öneki olmadan kısa başlık
    fingerprints: list[str]
    findings: list[Finding]
    score: float = 0.0
    sp: int = 1
    needs_human: bool = False
    human_reason: str = ""
    recurring_of: int | None = None

    @property
    def last_seen(self) -> float:
        return max((f.last_seen for f in self.findings), default=0.0)

    @property
    def apps(self) -> list[str]:
        return sorted({f.app for f in self.findings if f.app})

    @property
    def files(self) -> list[str]:
        out: list[str] = []
        for f in self.findings:
            if f.file and f.file not in out:
                out.append(f.file)
        return out

    @property
    def total(self) -> int:
        return sum(f.count for f in self.findings)
```

`src/agile_sdlc_crew/quality/rank.py`:

```python
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
        return "" + chr(0xE100 + len(kept) - 1)

    s = _QUOTED.sub(stash, (msg or "").strip())
    for rx, rep in _DYNAMIC:
        s = rx.sub(rep, s)
    s = re.sub("(.)", lambda m: kept[ord(m.group(1)) - 0xE100], s)
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
```

- [ ] **Step 4: Testin geçtiğini gör**

Run: `.venv/bin/python -c "import sys; sys.path.insert(0,'tests'); import test_katman0_gates as t; t.test_quality_rank_nr(); sys.exit(1 if t.FAIL else 0)"`
Expected: tüm satırlar ✅, çıkış 0.

- [ ] **Step 5: Commit**

```bash
git add src/agile_sdlc_crew/quality/ tests/test_katman0_gates.py
git commit -m "feat(kalite): veri modeli ve New Relic birim/SP/skor kuralları"
```

---

### Task 2: Sonar birimleri, skor ve paketleme

**Files:**
- Modify: `src/agile_sdlc_crew/quality/rank.py` (sona ekle)
- Test: `tests/test_katman0_gates.py` (`test_quality_rank_sonar`, `main()`'e ekle)

**Interfaces:**
- Consumes: `model.Finding`, `model.Group`, `rank.cap_fingerprint`.
- Produces: `rank.SONAR_WEIGHT`, `rank.sonar_fingerprint(repo, file, rule) -> str`, `rank.sonar_unit_sp(count) -> int`, `rank.sonar_units(findings) -> list[Group]`, `rank.pack_sonar(units, pack_sp=3, pack_files=5) -> list[Group]`.

- [ ] **Step 1: Testi yaz**

```python
def test_quality_rank_sonar():
    print("\n[Q2] Kalite alımı — Sonar birim, skor, paketleme")
    from agile_sdlc_crew.quality import rank
    from agile_sdlc_crew.quality.model import Finding

    def sf(file, rule="css:S4657", sev="CRITICAL", typ="BUG", repo="webservice", line=1):
        return Finding(source="sonar", repo=repo, project=f"EcomBackend:{repo}", file=file, rule=rule,
                       rule_name=rule, severity=sev, itype=typ, lines=[line])

    css = [sf("themes/a.css", line=i) for i in range(300)]
    vuln = [sf("app/Login.php", rule="php:S2076", sev="BLOCKER", typ="VULNERABILITY")]
    units = rank.sonar_units(css + vuln)
    by = {u.findings[0].rule: u for u in units}
    check("aynı dosya+kural → tek birim (300 madde)", len(units) == 2 and by["css:S4657"].total == 300)
    check("tek Vulnerability+Blocker, 300 maddelik Bug+Critical'ı geçer",
          by["php:S2076"].score > by["css:S4657"].score,
          f"{by['php:S2076'].score} vs {by['css:S4657'].score}")
    check("SP: 1 madde → 1, 300 madde → 3", by["php:S2076"].sp == 1 and by["css:S4657"].sp == 3)
    check("SP sınırları ≤5→1, ≤20→2, >20→3", [rank.sonar_unit_sp(n) for n in (5, 6, 20, 21)] == [1, 2, 2, 3])
    sec = rank.sonar_units([sf("config/app.php", rule="secrets:S6703", sev="BLOCKER", typ="VULNERABILITY")])[0]
    check("secrets:* → 1 SP + needs_human", sec.sp == 1 and sec.needs_human and "rotasyon" in sec.human_reason)
    twelve = rank.sonar_units([sf(f"themes/t{i}.css") for i in range(12)])
    packs = rank.pack_sonar(twelve, pack_sp=3, pack_files=5)
    check("12 dosya × 1 SP, paket ≤3 SP → 4 paket × 3 dosya",
          len(packs) == 4 and all(p.sp == 3 and len(p.files) == 3 for p in packs))
    packs5 = rank.pack_sonar(twelve, pack_sp=99, pack_files=5)
    check("dosya sınırı 5 → 5+5+2", sorted(len(p.files) for p in packs5) == [2, 5, 5])
    check("paket başlığı +N dosya, parmak izi birim başına",
          "(+4 dosya)" in max(packs5, key=lambda p: len(p.files)).title
          and sum(len(p.fingerprints) for p in packs5) == 12)
    mixed = rank.pack_sonar(rank.sonar_units([sf("a.css"), sf("b.php", rule="php:S1")]))
    check("farklı kural aynı pakete girmez", len(mixed) == 2)
```

`main()` tuple'ına `test_quality_rank_sonar,` ekle.

- [ ] **Step 2: Düştüğünü gör**

Run: `.venv/bin/python -c "import sys; sys.path.insert(0,'tests'); import test_katman0_gates as t; t.test_quality_rank_sonar(); sys.exit(1 if t.FAIL else 0)"`
Expected: `AttributeError: module 'agile_sdlc_crew.quality.rank' has no attribute 'sonar_units'` → ÇÖKTÜ.

- [ ] **Step 3: Kodu yaz** — `rank.py`'nin en üstündeki importlara `import math` ekle, dosyanın sonuna:

```python
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
```

- [ ] **Step 4: Geçtiğini gör** — Step 2'deki komut; Expected: tüm ✅.

- [ ] **Step 5: Commit**

```bash
git add src/agile_sdlc_crew/quality/rank.py tests/test_katman0_gates.py
git commit -m "feat(kalite): Sonar birim, log10 skor ve kural bazlı paketleme"
```

---

### Task 3: Bütçe seçimi

**Files:**
- Modify: `src/agile_sdlc_crew/quality/rank.py` (sona ekle)
- Test: `tests/test_katman0_gates.py` (`test_quality_select`, `main()`'e ekle)

**Interfaces:**
- Consumes: `model.Group`.
- Produces: `rank.select_budget(groups, budget_sp, *, human_share=0.3, max_tasks=8) -> list[Group]`.

- [ ] **Step 1: Testi yaz**

```python
def test_quality_select():
    print("\n[Q3] Kalite alımı — bütçe seçimi")
    from agile_sdlc_crew.quality import rank
    from agile_sdlc_crew.quality.model import Group

    def g(name, sp, score, human=False):
        return Group(source="sonar", repo="r", title=name, fingerprints=[name], findings=[],
                     score=score, sp=sp, needs_human=human)

    pick = rank.select_budget([g("a", 5, 100), g("b", 3, 90), g("c", 3, 80), g("d", 2, 70), g("e", 1, 60)], 10)
    check("açgözlü: sığmayan atlanır, küçük olan denenir, toplam ≤ bütçe",
          [x.title for x in pick] == ["a", "b", "d"] and sum(x.sp for x in pick) == 10)
    pick = rank.select_budget([g("h1", 3, 100, True), g("h2", 1, 99, True), g("x", 3, 50), g("y", 3, 40)], 10)
    check("needs_human payı ≤ %30 (10 SP → en çok 3)",
          [x.title for x in pick] == ["h1", "x", "y"] and sum(x.sp for x in pick if x.needs_human) <= 3)
    check("Task sayısı tavanı", len(rank.select_budget([g(str(i), 1, 100 - i) for i in range(20)], 40, max_tasks=8)) == 8)
    check("bütçeden büyük tek iş → boş seçim", rank.select_budget([g("big", 5, 1)], 3) == [])
    check("3 SP bütçede needs_human hiç girmez (pay 0)", rank.select_budget([g("h", 1, 9, True)], 3) == [])
```

`main()` tuple'ına `test_quality_select,` ekle.

- [ ] **Step 2: Düştüğünü gör** — `... t.test_quality_select() ...`; Expected: `AttributeError ... select_budget` → ÇÖKTÜ.

- [ ] **Step 3: Kodu yaz** — `rank.py` sonuna:

```python
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
```

- [ ] **Step 4: Geçtiğini gör** — Step 2 komutu; Expected: tüm ✅.

- [ ] **Step 5: Commit**

```bash
git add src/agile_sdlc_crew/quality/rank.py tests/test_katman0_gates.py
git commit -m "feat(kalite): SP bütçesine göre açgözlü seçim ve insan payı tavanı"
```

---

### Task 4: WI metinleri, secret maskeleme, parmak izi satırı

**Files:**
- Create: `src/agile_sdlc_crew/quality/render.py`
- Test: `tests/test_katman0_gates.py` (`test_quality_render`, `main()`'e ekle)

**Interfaces:**
- Consumes: `model.Group`, `rank.nr_units`, `rank.sonar_units` (yalnızca testte).
- Produces: `render.TAG`, `render.HUMAN_TAG`, `render.FP_PREFIX`, `render.SOURCE_LABEL`, `render.src_tag(source)`, `render.task_tags(g) -> str`, `render.mask_secrets(text) -> str`, `render.file_hints(action) -> list[str]`, `render.task_title(g) -> str`, `render.task_description(g, *, sonar_url="", nr_days=7) -> str`, `render.parse_fingerprints(desc) -> list[str]`, `render.story_title(source, sprint_name) -> str`, `render.story_description(source) -> str`.

- [ ] **Step 1: Testi yaz**

```python
def test_quality_render():
    print("\n[Q4] Kalite alımı — WI metinleri, maskeleme, parmak izi satırı")
    import html as _html
    from agile_sdlc_crew.quality import rank, render
    from agile_sdlc_crew.quality.model import Finding
    m = render.mask_secrets
    check("Bearer/Basic ve password=… maskelenir",
          "abc123def456" not in m("Authorization: Bearer abc123def456ghi")
          and m("password=Hunter2x&x=1") == "password=***&x=1"
          and "dXNlcjpwYXNz" not in m("Basic dXNlcjpwYXNz"))
    check("uzun anahtar adları maskelenmez",
          m('Undefined array key "is_prime_activate_on_preprod_web"')
          == 'Undefined array key "is_prime_activate_on_preprod_web"')
    check("uzun hex/token maskelenir", "***" in m("token 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b"))
    check("action → dosya ipucu (V1 varyantı dahil)",
          render.file_hints("Api/Order::integrationCancel") == [
              "app/Controller/Api/Order.php::integrationCancelAction",
              "app/Controller/V1/Api/Order.php::integrationCancelAction"]
          and render.file_hints("cron.php") == [])
    nr = rank.nr_units([Finding(source="newrelic", repo="webservice", app=f"[Prod] s{i}.ru", action="Content::page",
                                err_class="E_WARNING", message='Undefined array key "value"', count=10,
                                uris=["/ru/x"], first_seen=1.7e9, last_seen=1.7e9 + 60, error_rate=0.05)
                        for i in range(2)])[0]
    d = render.task_description(nr, nr_days=7)
    check("NR açıklaması: ilk satır Repo, iki uygulama, ipucu, kabul kriteri, parmak izi",
          d.startswith("<p><b>Repo:</b> <code>webservice</code></p>") and "[Prod] s0.ru" in d
          and "[Prod] s1.ru" in d and "app/Controller/Content.php::pageAction" in d
          and "Kabul kriteri" in d and render.parse_fingerprints(d) == nr.fingerprints)
    check("NR başlığı", render.task_title(nr) == '[webservice] Content::page: Undefined array key "value"')
    sec = rank.sonar_units([Finding(source="sonar", repo="webservice", project="EcomBackend:webservice",
                                    file="config/db.php", rule="secrets:S6703",
                                    rule_name="Database passwords should not be disclosed",
                                    severity="BLOCKER", itype="VULNERABILITY", lines=[12],
                                    message="password = 'S3cr3tValue!'")])[0]
    sd = render.task_description(sec, sonar_url="https://sonar.x", nr_days=7)
    check("secrets:* açıklamasına madde mesajı girmez; dosya:satır ve insan nedeni girer",
          "S3cr3tValue" not in sd and "config/db.php:12" in sd and "Neden insan" in sd)
    check("etiketler", render.task_tags(sec) == "tempo-quality; tempo-src-sonar; needs-human")
    nr.recurring_of = 75001
    check("tekrarlayan iş önceki WI'ı anar", "#75001" in render.task_description(nr, nr_days=7))
    fps = ['nr:webservice:A::b:E_WARNING:Undefined array key "x"', "sonar:webservice:a.css:css:S4657"]
    blob = "".join(f"<p><small>tempo-fp: {_html.escape(fp)}</small></p>" for fp in fps)
    check("parmak izi satırları geri okunur (HTML kaçışlı ve etiketsiz)",
          render.parse_fingerprints(blob) == fps
          and render.parse_fingerprints("tempo-fp: a:b tempo-fp: c:d") == ["a:b", "c:d"])
    check("story başlığı", render.story_title("newrelic", "Sprint 42") == "Kalite: New Relic hataları — Sprint 42")
```

`main()` tuple'ına `test_quality_render,` ekle.

- [ ] **Step 2: Düştüğünü gör** — `... t.test_quality_render() ...`; Expected: `ImportError: cannot import name 'render'` → ÇÖKTÜ.

- [ ] **Step 3: Kodu yaz** — `src/agile_sdlc_crew/quality/render.py`:

```python
"""Kalite işi alımı — WI başlık/açıklama metinleri (Türkçe), secret maskeleme,
parmak izi satırı.

Parmak izi GÖRÜNÜR bir satırdır (`<p><small>tempo-fp: …</small></p>`), HTML
yorumu değil: Azure Description HTML'ini sanitize eder; dedup bu satırları
board'dan geri okur (dedup.py)."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from agile_sdlc_crew.quality.model import Group

TAG = "tempo-quality"
HUMAN_TAG = "needs-human"
FP_PREFIX = "tempo-fp:"
SOURCE_LABEL = {"newrelic": "New Relic hataları", "sonar": "Sonar bulguları"}


def src_tag(source: str) -> str:
    return f"tempo-src-{source}"


def task_tags(g: Group) -> str:
    tags = [TAG, src_tag(g.source)]
    if g.needs_human:
        tags.append(HUMAN_TAG)
    return "; ".join(tags)


_AUTH = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{6,}", re.I)
_KV = re.compile(
    r"((?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)[\"']?\s*[=:]\s*[\"']?)"
    r"[^\s&\"',;]+", re.I)
_LONG = re.compile(r"[A-Za-z0-9+/=_-]{20,}")
_SNAKE = re.compile(r"[a-z]+(?:_[a-z0-9]+)+")


def _secretish(tok: str) -> bool:
    digits = sum(c.isdigit() for c in tok)
    letters = sum(c.isalpha() for c in tok)
    return digits >= 3 and letters >= 3 and not _SNAKE.fullmatch(tok)


def mask_secrets(text: str) -> str:
    s = _AUTH.sub(lambda m: f"{m.group(1)} ***", text or "")
    s = _KV.sub(lambda m: m.group(1) + "***", s)
    return _LONG.sub(lambda m: "***" if _secretish(m.group(0)) else m.group(0), s)


def file_hints(action: str) -> list[str]:
    """Butterfly kuralı: 'Api/Order::integrationCancel' → Controller dosyası + metodu."""
    if "::" not in (action or ""):
        return []
    ctrl, method = action.rsplit("::", 1)
    if not re.fullmatch(r"[A-Za-z0-9/]+", ctrl) or not method:
        return []
    return [f"app/Controller/{ctrl}.php::{method}Action",
            f"app/Controller/V1/{ctrl}.php::{method}Action"]


def task_title(g: Group) -> str:
    return mask_secrets(f"[{g.repo}] {g.title}")[:128].strip()


def _e(s) -> str:
    return html.escape(mask_secrets(str(s)))


def _ts(epoch: float) -> str:
    if not epoch:
        return "—"
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _nr_body(g: Group, nr_days: int) -> list[str]:
    parts = ["<p><b>Ne oluyor:</b></p><ul>"]
    seen: list[tuple[str, str]] = []
    for f in g.findings:
        k = (f.err_class, f.message)
        if k not in seen:
            seen.append(k)
    for cls, msg in seen[:10]:
        parts.append(f"<li><code>{_e(cls)}</code> {_e(msg)}</li>")
    parts.append("</ul>")
    per_app: dict[str, dict] = {}
    for f in g.findings:
        a = per_app.setdefault(f.app, {"n": 0, "rate": 0.0, "first": 0.0, "last": 0.0})
        a["n"] += f.count
        a["rate"] = max(a["rate"], f.error_rate)
        a["first"] = min(a["first"], f.first_seen) if a["first"] else f.first_seen
        a["last"] = max(a["last"], f.last_seen)
    parts.append(f"<p><b>Kanıt (son {int(nr_days)} gün):</b></p><table>"
                 "<tr><th>Uygulama</th><th>Hata</th><th>Error rate</th><th>İlk</th><th>Son</th></tr>")
    for app, a in sorted(per_app.items(), key=lambda kv: -kv[1]["n"]):
        parts.append(f"<tr><td>{_e(app)}</td><td>{a['n']:,}</td><td>%{a['rate'] * 100:.1f}</td>"
                     f"<td>{_ts(a['first'])}</td><td>{_ts(a['last'])}</td></tr>")
    parts.append("</table>")
    uris: list[str] = []
    for f in g.findings:
        for u in f.uris:
            if u not in uris:
                uris.append(u)
    if uris:
        parts.append("<p><b>Örnek istekler:</b> " + ", ".join(f"<code>{_e(u)}</code>" for u in uris[:3]) + "</p>")
    hints = file_hints(g.findings[0].action)
    if hints:
        parts.append("<p><b>Olası yer:</b></p><ul>" + "".join(f"<li><code>{_e(h)}</code></li>" for h in hints) + "</ul>")
    apps = ", ".join("'" + a.replace("'", "\\'") + "'" for a in g.apps)
    cls = g.findings[0].err_class.replace("'", "\\'")
    parts.append("<p><b>Kaynak (NRQL):</b></p><pre>"
                 + _e(f"SELECT count(*) FROM TransactionError WHERE appName IN ({apps}) "
                      f"AND error.class = '{cls}' FACET error.message, transactionName "
                      f"SINCE {int(nr_days)} days ago")
                 + "</pre>")
    parts.append("<p><b>Kabul kriteri:</b> Bu action'da bu hata sınıfı/mesajı bir daha üretilmez; "
                 "mevcut davranış korunur.</p>")
    return parts


def _sonar_body(g: Group, sonar_url: str) -> list[str]:
    f0 = g.findings[0]
    secret = f0.rule.startswith("secrets:")
    parts = [f"<p><b>Ne oluyor:</b> {_e(f0.rule_name or f0.rule)} (<code>{_e(f0.rule)}</code>, "
             f"{_e(f0.itype)} / {_e(f0.severity)})</p>"]
    if not secret:
        msgs: list[str] = []
        for f in g.findings:
            if f.message and f.message not in msgs:
                msgs.append(f.message)
        if msgs:
            parts.append("<ul>" + "".join(f"<li>{_e(x)}</li>" for x in msgs[:5]) + "</ul>")
    locs = [(f, ln) for f in g.findings for ln in (f.lines or [0])]
    parts.append(f"<p><b>Kanıt ({g.total} madde):</b></p><ul>")
    for f, ln in locs[:20]:
        label = _e(f"{f.file}:{ln}" if ln else f.file)
        parts.append(f'<li><a href="{html.escape(f.link)}">{label}</a></li>' if f.link else f"<li>{label}</li>")
    if len(locs) > 20:
        parts.append(f"<li>+{len(locs) - 20} madde daha</li>")
    parts.append("</ul>")
    if sonar_url:
        for proj in sorted({f.project for f in g.findings if f.project}):
            url = (f"{sonar_url}/project/issues?id={proj}&rules={f0.rule}"
                   "&issueStatuses=OPEN,CONFIRMED")
            parts.append(f'<p><b>Kaynak:</b> <a href="{html.escape(url)}">{html.escape(proj)}</a></p>')
    if secret:
        parts.append("<p><b>Kabul kriteri:</b> Secret döndürülür (rotate) ve koddan kaldırılır; "
                     "yeni değer gizli yönetiminden okunur.</p>")
    else:
        parts.append("<p><b>Kabul kriteri:</b> Listelenen maddeler Sonar'da kapanır; değişen satırlarda "
                     "yeni madde eklenmez.</p>")
    return parts


def task_description(g: Group, *, sonar_url: str = "", nr_days: int = 7) -> str:
    parts = [f"<p><b>Repo:</b> <code>{html.escape(g.repo)}</code></p>"]
    if g.recurring_of:
        parts.append(f"<p><b>Tekrarladı</b> — önceki: #{int(g.recurring_of)}</p>")
    if g.needs_human:
        parts.append(f"<p><b>Neden insan:</b> {_e(g.human_reason)}</p>")
    parts += _nr_body(g, nr_days) if g.source == "newrelic" else _sonar_body(g, sonar_url)
    parts += [f"<p><small>{FP_PREFIX} {html.escape(fp)}</small></p>" for fp in g.fingerprints]
    return "".join(parts)


_FP_LINE = re.compile(r"tempo-fp:\s*(.+?)(?=\s*tempo-fp:|<|[\r\n]|$)")


def parse_fingerprints(desc: str) -> list[str]:
    return [html.unescape(m.group(1)).strip() for m in _FP_LINE.finditer(desc or "")]


def story_title(source: str, sprint_name: str) -> str:
    return f"Kalite: {SOURCE_LABEL[source]} — {sprint_name}".strip()


def story_description(source: str) -> str:
    return (f"<p>Tempo'nun {SOURCE_LABEL[source]} için açtığı kalite işleri. Alt Task'lar skor ve "
            "SP bütçesine göre otomatik seçilir ve pipeline kuyruğuna alınır; "
            f"<code>{HUMAN_TAG}</code> etiketliler insan bekler.</p>")
```

- [ ] **Step 4: Geçtiğini gör** — Step 2 komutu; Expected: tüm ✅.

- [ ] **Step 5: Commit**

```bash
git add src/agile_sdlc_crew/quality/render.py tests/test_katman0_gates.py
git commit -m "feat(kalite): Türkçe WI metinleri, secret maskeleme ve parmak izi satırı"
```

---

### Task 5: Board'dan dedup

**Files:**
- Create: `src/agile_sdlc_crew/quality/dedup.py`
- Test: `tests/test_katman0_gates.py` (`test_quality_dedup`, `main()`'e ekle)

**Interfaces:**
- Consumes: `render.parse_fingerprints`, `AzureDevOpsClient.query_work_items(wiql)`.
- Produces: `dedup.WIQL`, `dedup.fetch_board(client) -> list[dict]`, `dedup.index_board(work_items) -> dict[str, dict]` (`{"wi": int, "state": str, "closed_at": float}`), `dedup.verdict(entry, *, last_seen, now, grace_days=3) -> tuple[str, int | None]` (`"new" | "blocked" | "suppressed" | "recurring"`), `dedup._epoch(iso) -> float`.

- [ ] **Step 1: Testi yaz**

```python
def test_quality_dedup():
    print("\n[Q5] Kalite alımı — board'dan dedup")
    from agile_sdlc_crew.quality import dedup

    def wi(i, state, fps, closed=""):
        return {"id": i, "fields": {
            "System.State": state, "Microsoft.VSTS.Common.ClosedDate": closed,
            "System.Description": "".join(f"<p><small>tempo-fp: {fp}</small></p>" for fp in fps)}}

    idx = dedup.index_board([
        wi(1, "In Progress", ["a"]), wi(2, "Ready for Production", ["b"]), wi(3, "Canceled", ["c"]),
        wi(4, "Done", ["d"], "2026-10-01T00:00:00Z"), wi(5, "Done", ["e"], "2026-10-01T00:00:00Z"),
        wi(6, "Done", ["f"], "2026-09-01T00:00:00Z"), wi(7, "To Do", ["f"])])
    day = 86400
    closed = dedup._epoch("2026-10-01T00:00:00Z")
    now = closed + 10 * day

    def v(fp, seen=0.0):
        return dedup.verdict(idx.get(fp), last_seen=seen, now=now, grace_days=3)

    check("açık WI → blocked", v("a") == ("blocked", 1))
    check("Ready for Production → blocked (düzeltme canlıda değil)", v("b") == ("blocked", 2))
    check("Canceled → kalıcı bastırma", v("c") == ("suppressed", 3))
    check("Done + grace sonrası hâlâ görülüyor → recurring", v("d", seen=closed + 5 * day) == ("recurring", 4))
    check("Done + grace içinde görülmüş → blocked", v("e", seen=closed + 1 * day) == ("blocked", 5))
    check("Sonar (last_seen yok) → şimdiye göre", v("d") == ("recurring", 4))
    check("aynı parmak izi iki WI'da → en yeni (büyük id) kazanır", v("f") == ("blocked", 7))
    check("hiç yok → new", v("zzz") == ("new", None))
```

`main()` tuple'ına `test_quality_dedup,` ekle.

- [ ] **Step 2: Düştüğünü gör** — `... t.test_quality_dedup() ...`; Expected: `ImportError ... dedup` → ÇÖKTÜ.

- [ ] **Step 3: Kodu yaz** — `src/agile_sdlc_crew/quality/dedup.py`:

```python
"""Kalite işi alımı — aynı bulgunun iki kez açılmaması.

Kaynak board'dur: `tempo-quality` etiketli Task'ların açıklamasındaki
`tempo-fp:` satırları okunur. quality_items tablosu yalnızca geçmiştir —
DB kaybı dedup'ı bozmaz.

Durum kuralı:
  Done/Closed/Completed          → kapanış + grace sonrası hâlâ görülüyorsa yeniden açılabilir
  Canceled/Removed/Rejected      → kalıcı bastırma (insan "yapılmayacak" dedi)
  diğer her şey (Resolved, Ready for Production dahil — düzeltme canlıda değil) → açık
"""

from __future__ import annotations

from datetime import datetime

from agile_sdlc_crew.quality.render import TAG, parse_fingerprints

CLOSED_FOR_GOOD = {"canceled", "cancelled", "removed", "rejected"}
CLOSED_DONE = {"done", "closed", "completed"}

WIQL = ("SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
        f"AND [System.WorkItemType] = 'Task' AND [System.Tags] CONTAINS '{TAG}'")


def fetch_board(client) -> list[dict]:
    return client.query_work_items(WIQL)


def _epoch(iso: str) -> float:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def index_board(work_items: list[dict]) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for wi in work_items or []:
        f = wi.get("fields") or {}
        entry = {
            "wi": int(wi.get("id") or 0),
            "state": (f.get("System.State") or "").strip(),
            "closed_at": _epoch(f.get("Microsoft.VSTS.Common.ClosedDate")
                                or f.get("Microsoft.VSTS.Common.StateChangeDate") or ""),
        }
        for fp in parse_fingerprints(f.get("System.Description") or ""):
            prev = idx.get(fp)
            if prev is None or entry["wi"] > prev["wi"]:
                idx[fp] = entry
    return idx


def verdict(entry: dict | None, *, last_seen: float, now: float,
            grace_days: int = 3) -> tuple[str, int | None]:
    if not entry:
        return "new", None
    state = (entry.get("state") or "").lower()
    if state in CLOSED_FOR_GOOD:
        return "suppressed", entry["wi"]
    if state not in CLOSED_DONE:
        return "blocked", entry["wi"]
    closed = entry.get("closed_at") or 0.0
    seen = last_seen or now
    if closed and seen > closed + grace_days * 86400:
        return "recurring", entry["wi"]
    return "blocked", entry["wi"]
```

- [ ] **Step 4: Geçtiğini gör** — Step 2 komutu; Expected: tüm ✅.

- [ ] **Step 5: Commit**

```bash
git add src/agile_sdlc_crew/quality/dedup.py tests/test_katman0_gates.py
git commit -m "feat(kalite): board'daki tempo-fp satırlarından dedup ve yeniden açma kuralı"
```

---

### Task 6: NR uygulama → repo eşlemesi (FLODevOps)

**Files:**
- Modify: `src/agile_sdlc_crew/tools/azure_devops_base.py:255-279` (`get_items_in_path`), `:281-310` (`get_file_content`) — `project` parametresi
- Create: `src/agile_sdlc_crew/quality/app_map.py`
- Create: `src/agile_sdlc_crew/config/quality_app_map_overrides.yaml`
- Modify: `.gitignore` (önbellek dosyası)
- Test: `tests/test_katman0_gates.py` (`test_quality_app_map`, `main()`'e ekle)

**Interfaces:**
- Consumes: `AzureDevOpsClient.get_items_in_path(repo, path, branch=None, recursion_level="oneLevel", project=None)`, `AzureDevOpsClient.get_file_content(repo, path, branch=None, project=None)`, `model.QualitySourceError`.
- Produces: `app_map.parse_build_dir(jenkinsfile, dockerfile) -> dict | None` (`{"project", "repo", "apps"}`), `app_map.build_map(client) -> dict[str, dict]`, `app_map.load_map(client=None, *, max_age_h=24, force=False, cache_path=..., overrides_path=...) -> dict[str, dict]` (anahtar küçük harf app adı → `{"repo", "project", "src"}`), `app_map.resolve(app, mapping) -> str`, `app_map.QualitySourceError` (yeniden ihraç).

- [ ] **Step 1: Testi yaz**

```python
def test_quality_app_map():
    print("\n[Q6] Kalite alımı — NR uygulama → repo eşlemesi (FLODevOps)")
    import inspect
    import yaml
    from agile_sdlc_crew.quality import app_map
    from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient
    check("Azure istemcisi başka projedeki repoyu okuyabilir (project parametresi)",
          "project" in inspect.signature(AzureDevOpsClient.get_file_content).parameters
          and "project" in inspect.signature(AzureDevOpsClient.get_items_in_path).parameters)
    jf_tr = 'String gitOriginApp = "git@ssh.dev.azure.com:v3/flotechnology/ecombackend/webservice"\n'
    df_tr = ("ARG website=tr-stocks.flo.com.tr\n"
             "&& sed -ie 's/[ ;]*newrelic.appname[[:space:]]=.*/newrelic.appname=\"[Prod] tr-stocks.flo.com.tr\"/' "
             "$(php-config --ini-dir)/newrelic.ini \\\n")
    e = app_map.parse_build_dir(jf_tr, df_tr)
    check("literal appname + gitOriginApp",
          e == {"project": "ecombackend", "repo": "webservice", "apps": ["[Prod] tr-stocks.flo.com.tr"]}, str(e))
    jf_web = ('String gitOriginApp = "git@ssh.dev.azure.com:v3/flotechnology/ecombackend/webservice"\n'
              "   flokz       : [website: 'flo.com.kz', themePath: 'flo-kz'],\n"
              "   floma       : [website: 'flo.co.ma', themePath: 'flo-ma'],\n"
              '   newrelic_app_name = "[Prod] ${website}"\n'
              "   sh \"docker build --build-arg newrelic_app_name='${newrelic_app_name}' .\"\n")
    df_web = ("ARG website\nARG newrelic_app_name\n"
              '&& sed -ie "s/[ ;]*newrelic.appname[[:space:]]=.*/newrelic.appname=\\"${newrelic_app_name}\\"/" x\n')
    e2 = app_map.parse_build_dir(jf_web, df_web)
    check("${website} şablonu her siteye açılır, ${newrelic_app_name} yok sayılır",
          e2 and e2["apps"] == ["[Prod] flo.com.kz", "[Prod] flo.co.ma"], str(e2))
    check("gitOriginApp yoksa None", app_map.parse_build_dir("", df_tr) is None)

    calls = {"n": 0}

    class FakeClient:
        def get_items_in_path(self, repo, path, branch=None, recursion_level="oneLevel", project=None):
            calls["n"] += 1
            assert project == "FLODevOps" and branch == "production"
            if repo != "global":
                return []
            return [{"path": "/build", "isFolder": True}, {"path": "/build/trstock", "isFolder": True},
                    {"path": "/build/.DS_Store", "isFolder": False}]

        def get_file_content(self, repo, path, branch=None, project=None):
            return {"/build/trstock/JenkinsfileBuild": jf_tr, "/build/trstock/Dockerfile": df_tr}[path]

    class Broken(FakeClient):
        def get_items_in_path(self, *a, **k):
            raise RuntimeError("401")

    with tempfile.TemporaryDirectory() as td:
        cache, ovr = Path(td) / "map.yaml", Path(td) / "ovr.yaml"
        ovr.write_text(yaml.safe_dump({"[Prod] Stock Api": "stock-api"}), encoding="utf-8")
        m = app_map.load_map(FakeClient(), cache_path=cache, overrides_path=ovr)
        check("FLODevOps'tan kurulur, büyük/küçük harf duyarsız, override eklenir",
              app_map.resolve("[PROD] TR-STOCKS.FLO.COM.TR", m) == "webservice"
              and app_map.resolve("[Prod] Stock Api", m) == "stock-api" and app_map.resolve("yok", m) == "")
        n = calls["n"]
        app_map.load_map(FakeClient(), cache_path=cache, overrides_path=ovr)
        check("taze önbellek varken FLODevOps tekrar okunmaz", calls["n"] == n)
        m2 = app_map.load_map(Broken(), cache_path=cache, overrides_path=ovr, force=True)
        check("FLODevOps okunamazsa eski önbellek kullanılır",
              app_map.resolve("[Prod] tr-stocks.flo.com.tr", m2) == "webservice")
        cache.unlink()
        try:
            app_map.load_map(Broken(), cache_path=cache, overrides_path=ovr)
            ok = False
        except app_map.QualitySourceError:
            ok = True
        check("önbellek de yoksa QualitySourceError", ok)
```

`main()` tuple'ına `test_quality_app_map,` ekle. (`tempfile` ve `Path` dosyanın başında zaten import'lu.)

- [ ] **Step 2: Düştüğünü gör** — `... t.test_quality_app_map() ...`; Expected: ilk check ❌ (project parametresi yok) ve ardından `ImportError ... app_map` → ÇÖKTÜ.

- [ ] **Step 3a: Azure istemcisine `project` parametresi** — `azure_devops_base.py`:

`get_items_in_path` imzası ve URL satırı:

```python
    def get_items_in_path(
        self,
        repo_id_or_name: str,
        path: str = "/",
        branch: str | None = None,
        recursion_level: str = "oneLevel",
        project: str | None = None,
    ) -> list[dict]:
        """Bir repoda belirtilen dizindeki dosya/klasorleri listeler.
        project: repo AZURE_DEVOPS_REPO_PROJECTS disindaysa (ör. FLODevOps)."""
        url = f"{self._repo_api_url(repo_id_or_name, project)}/items"
```

`get_file_content` imzası ve URL satırı:

```python
    def get_file_content(
        self,
        repo_id_or_name: str,
        file_path: str,
        branch: str | None = None,
        project: str | None = None,
    ) -> str:
        """Bir repodaki dosyanin icerigini (text) dondurur."""
        url = f"{self._repo_api_url(repo_id_or_name, project)}/items"
```

(Gövdenin geri kalanı değişmez; mevcut çağıranlar `project` geçmediği için davranış aynı.)

- [ ] **Step 3b: `src/agile_sdlc_crew/quality/app_map.py`**

```python
"""New Relic uygulama adı → kaynak repo eşlemesi.

Kaynak: FLODevOps projesindeki `anatolia` (TR), `global`, `russia` repoları,
`production` dalı. Her `build/<servis>/` için:
  JenkinsfileBuild → gitOriginApp (…/v3/<org>/<proje>/<repo>)
  Dockerfile / JenkinsfileBuild → newrelic.appname="…" (literal) ya da
  websites şablonu newrelic_app_name = "[Prod] ${website}" + website: '…' listesi.
Sonuç config/quality_app_map.yaml'a önbelleklenir (git'te değil); elle
düzeltmeler config/quality_app_map_overrides.yaml'da ({app adı: repo}) ve
her zaman önceliklidir.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import yaml

from agile_sdlc_crew.quality.model import QualitySourceError

log = logging.getLogger("pipeline")

DEVOPS_PROJECT = "FLODevOps"
DEVOPS_REPOS = ("anatolia", "global", "russia")
DEVOPS_BRANCH = "production"

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
CACHE_PATH = _CONFIG_DIR / "quality_app_map.yaml"
OVERRIDES_PATH = _CONFIG_DIR / "quality_app_map_overrides.yaml"

_ORIGIN = re.compile(r'gitOriginApp\s*=\s*"[^"]*?v3/[^/"]+/([^/"]+)/([^/"]+)"')
_APPNAME = re.compile(r'newrelic\.appname\s*=\s*\\?"([^"\\]+)')
_TEMPLATE = re.compile(r'newrelic_app_name\s*=\s*"([^"]+)"')
_WEBSITE = re.compile(r"website:\s*'([^']+)'")
_ARG_WEBSITE = re.compile(r"^\s*ARG\s+website=(\S+)", re.M)


def parse_build_dir(jenkinsfile: str, dockerfile: str) -> dict | None:
    m = _ORIGIN.search(jenkinsfile or "")
    if not m:
        return None
    jf, df = jenkinsfile or "", dockerfile or ""
    websites = _WEBSITE.findall(jf) + _ARG_WEBSITE.findall(df)
    names: list[str] = []
    for raw in _APPNAME.findall(df) + _APPNAME.findall(jf) + _TEMPLATE.findall(jf):
        raw = raw.strip()
        if "${website}" in raw:
            names += [raw.replace("${website}", w) for w in websites]
        elif "$" not in raw:
            names.append(raw)
    apps = list(dict.fromkeys(names))
    return {"project": m.group(1), "repo": m.group(2), "apps": apps}


def _read(client, devrepo: str, path: str) -> str:
    try:
        return client.get_file_content(devrepo, path, branch=DEVOPS_BRANCH, project=DEVOPS_PROJECT) or ""
    except Exception:  # noqa: BLE001 — servis dizininde dosya olmayabilir (404)
        return ""


def build_map(client) -> dict[str, dict]:
    """Ağdan kur. Dizin listelenemezse hata yükselir (load_map önbelleğe düşer)."""
    out: dict[str, dict] = {}
    for devrepo in DEVOPS_REPOS:
        items = client.get_items_in_path(devrepo, "/build", branch=DEVOPS_BRANCH,
                                         recursion_level="oneLevel", project=DEVOPS_PROJECT)
        for it in items:
            path = (it.get("path") or "").rstrip("/")
            if not it.get("isFolder") or path in ("", "/build"):
                continue
            entry = parse_build_dir(_read(client, devrepo, f"{path}/JenkinsfileBuild"),
                                    _read(client, devrepo, f"{path}/Dockerfile"))
            if not entry:
                continue
            for app in entry["apps"]:
                out[app] = {"repo": entry["repo"], "project": entry["project"], "src": f"{devrepo}{path}"}
    return out


def _read_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def load_map(client=None, *, max_age_h: int = 24, force: bool = False,
             cache_path: Path = CACHE_PATH, overrides_path: Path = OVERRIDES_PATH) -> dict[str, dict]:
    cached = _read_yaml(cache_path)
    apps: dict = cached.get("apps") or {}
    fresh = bool(apps) and (time.time() - float(cached.get("built_at") or 0)) < max_age_h * 3600
    if force or not fresh:
        try:
            if client is None:
                from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient
                client = AzureDevOpsClient()
            apps = build_map(client)
            cache_path.write_text(yaml.safe_dump({"built_at": time.time(), "apps": apps},
                                                 allow_unicode=True, sort_keys=True), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            if not apps:
                raise QualitySourceError(f"FLODevOps okunamadı ve önbellek yok: {e}") from e
            log.warning(f"⚠️ Kalite: FLODevOps okunamadı, eski eşleme kullanılıyor: {e}")
    merged = {str(k).lower(): v for k, v in apps.items()}
    for name, repo in (_read_yaml(overrides_path) or {}).items():
        merged[str(name).lower()] = {"repo": str(repo), "project": "", "src": "override"}
    return merged


def resolve(app: str, mapping: dict[str, dict]) -> str:
    return (mapping.get((app or "").lower()) or {}).get("repo", "")
```

- [ ] **Step 3c: Override dosyası ve .gitignore**

`src/agile_sdlc_crew/config/quality_app_map_overrides.yaml`:

```yaml
# New Relic uygulama adı → repo. FLODevOps build dosyalarından çıkarılamayan
# eşlemeler buraya yazılır; otomatik eşlemeyi her zaman ezer.
# Örnek: "[Prod] Stock Api" Dockerfile'da appname olarak geçmiyor.
"[Prod] Stock Api": stock-api
```

`.gitignore` sonuna:

```
# Kalite alımı: FLODevOps'tan kurulan NR → repo önbelleği (her 24 saatte yeniden kurulur)
src/agile_sdlc_crew/config/quality_app_map.yaml
```

- [ ] **Step 4: Geçtiğini gör** — Step 2 komutu; Expected: tüm ✅.

- [ ] **Step 5: Commit**

```bash
git add src/agile_sdlc_crew/tools/azure_devops_base.py src/agile_sdlc_crew/quality/app_map.py \
        src/agile_sdlc_crew/config/quality_app_map_overrides.yaml .gitignore tests/test_katman0_gates.py
git commit -m "feat(kalite): FLODevOps build dosyalarından NR uygulama → repo eşlemesi"
```

---

### Task 7: Toplayıcılar (SonarQube + New Relic)

**Files:**
- Create: `src/agile_sdlc_crew/quality/sonar.py`
- Create: `src/agile_sdlc_crew/quality/newrelic.py`
- Test: `tests/test_katman0_gates.py` (`test_quality_sources`, `main()`'e ekle)

**Interfaces:**
- Consumes: `model.Finding`, `model.QualitySourceError`, `rank.action_of`, `pipeline_config.get("CREW_SONAR_URL" | "NEW_RELIC_REGION")` (knob'lar Task 8'de eklenir; `get` bilinmeyen anahtarda `None` döner — kod `or` ile varsayılana düşer).
- Produces: `sonar.fetch_issues(page_size=500, max_items=10000) -> tuple[list[dict], dict[str, str]]`, `sonar.issues_to_findings(issues, rules, *, base_url) -> list[Finding]`, `sonar.in_scope(project_key, repo_projects) -> bool`, `sonar.base_url() -> str`, `sonar._get(path, params) -> dict`; `newrelic.nrql(query, *, region=None) -> list[dict]`, `newrelic.rows_to_findings(err_rows, tx_rows, *, is_prod) -> list[Finding]`, `newrelic.fetch(days, *, is_prod) -> list[Finding]`, `newrelic.is_prod_name(app) -> bool`, `newrelic.QualitySourceError`.

- [ ] **Step 0: `pipeline_config.get` bilinmeyen anahtar davranışını doğrula**

Run: `.venv/bin/python -c "from agile_sdlc_crew import pipeline_config as p; print(repr(p.get('CREW_YOK_BOYLE_BIR_SEY')))"`
Expected: `None`. Hata verirse, Task 8 Step 3a'daki knob kayıtlarını bu task'ın başında yap.

- [ ] **Step 1: Testi yaz**

```python
def test_quality_sources():
    print("\n[Q7] Kalite alımı — Sonar/NR yanıtlarını bulguya çevirme")
    import os
    from agile_sdlc_crew.quality import newrelic, sonar
    err = [
        {"facet": ["[Prod] tr-stocks.flo.com.tr", "E_WARNING", 'Undefined array key "host"',
                   "WebTransaction/Custom/index::index"],
         "count": 645052, "first": 1791290877564.0, "last": 1791895671566.0, "uris": ["/en"]},
        {"facet": ["[Preprod] preprod.flo.com.kz", "E_WARNING", "x", "WebTransaction/Custom/Page::notFound"],
         "count": 5, "uris": []},
        {"facet": ["Marketplace API", "TypeError", "y", "WebTransaction/Action/api::x"], "count": 3},
    ]
    tx = [{"facet": "[Prod] tr-stocks.flo.com.tr", "appName": "[Prod] tr-stocks.flo.com.tr", "count": 1290104},
          {"facet": "Marketplace API", "count": 0}]
    fs = newrelic.rows_to_findings(err, tx, is_prod=lambda a: newrelic.is_prod_name(a) or a == "Marketplace API")
    check("prod filtresi: [Preprod] elenir, eşlenmiş köşeli parantezsiz prod kalır",
          [f.app for f in fs] == ["[Prod] tr-stocks.flo.com.tr", "Marketplace API"])
    f0 = fs[0]
    check("NR satırı → Finding (action, saniye, error rate, uri)",
          f0.action == "Index::index" and f0.count == 645052 and abs(f0.last_seen - 1791895671.566) < 1e-3
          and abs(f0.error_rate - 0.5) < 0.01 and f0.uris == ["/en"])
    check("işlem sayısı 0 → error rate 0", fs[1].error_rate == 0.0)
    check("prod adı kalıpları",
          all(newrelic.is_prod_name(a) for a in ("[Prod] x", "[PROD] y", "[Production] FLO Orkestra V2",
                                                  "[GCP-Prod] panel"))
          and not newrelic.is_prod_name("[Preprod] x"))
    issues = [
        {"key": "k1", "rule": "php:S2076", "severity": "BLOCKER", "type": "VULNERABILITY",
         "project": "EcomBackend:webservice", "component": "EcomBackend:webservice:app/Login.php",
         "line": 40, "message": "Make sure..."},
        {"key": "k2", "rule": "secrets:S6703", "severity": "BLOCKER", "type": "VULNERABILITY",
         "project": "EcomBackend:webservice", "component": "EcomBackend:webservice:config/db.php",
         "line": 3, "message": "password = 'S3cret'"},
    ]
    sf = sonar.issues_to_findings(issues, {"php:S2076": "OS commands should not be vulnerable"},
                                  base_url="https://sonar.x")
    check("Sonar: proje → repo, component → dosya, kural adı, link",
          sf[0].repo == "webservice" and sf[0].file == "app/Login.php"
          and sf[0].rule_name == "OS commands should not be vulnerable" and sf[0].lines == [40]
          and "open=k1" in sf[0].link)
    check("secrets:* mesajı hiç saklanmaz", sf[1].message == "")
    check("kapsam: AZURE_DEVOPS_REPO_PROJECTS önekine göre (büyük/küçük harf duyarsız)",
          sonar.in_scope("EcomBackend:webservice", ["ecombackend", "EcomLojistik"])
          and not sonar.in_scope("FLOXO:tedweb", ["EcomBackend"]))
    pages = {1: {"issues": [{"key": "a"}] * 500, "paging": {"total": 863}, "rules": [{"key": "r", "name": "R"}]},
             2: {"issues": [{"key": "b"}] * 363, "paging": {"total": 863}}}
    seen = []
    orig = sonar._get
    sonar._get = lambda path, params: (seen.append(params["p"]), pages[params["p"]])[1]
    try:
        iss, rules = sonar.fetch_issues()
    finally:
        sonar._get = orig
    check("Sonar sayfalama: 863 madde iki sayfada", len(iss) == 863 and seen == [1, 2] and rules == {"r": "R"})
    saved = {k: os.environ.pop(k, None) for k in ("NEW_RELIC_ACCOUNT_ID", "NEW_RELIC_API_KEY")}
    try:
        try:
            newrelic.nrql("SELECT 1")
            ok = False
        except newrelic.QualitySourceError:
            ok = True
    finally:
        for k, val in saved.items():
            if val is not None:
                os.environ[k] = val
    check("NR kimlik bilgisi yoksa QualitySourceError", ok)
```

`main()` tuple'ına `test_quality_sources,` ekle.

- [ ] **Step 2: Düştüğünü gör** — `... t.test_quality_sources() ...`; Expected: `ImportError ... newrelic` → ÇÖKTÜ.

- [ ] **Step 3a: `src/agile_sdlc_crew/quality/sonar.py`**

```python
"""SonarQube toplayıcısı: açık BUG + VULNERABILITY (BLOCKER/CRITICAL) maddeleri.

Proje anahtarı `EcomBackend:webservice` biçiminde → repo doğrudan. Kapsam,
anahtarın önekinin AZURE_DEVOPS_REPO_PROJECTS içinde olmasıdır (FLOXO,
FloxoPLM vb. atlanır). secrets:* maddelerinin mesajı SAKLANMAZ — değer içerebilir.
"""

from __future__ import annotations

import os
from urllib.parse import quote

import requests

from agile_sdlc_crew import pipeline_config
from agile_sdlc_crew.quality.model import Finding, QualitySourceError

TYPES = "BUG,VULNERABILITY"
SEVERITIES = "BLOCKER,CRITICAL"
STATUSES = "OPEN,CONFIRMED"
DEFAULT_URL = "https://sonarqube.flo.com.tr"


def base_url() -> str:
    return str(pipeline_config.get("CREW_SONAR_URL") or DEFAULT_URL).rstrip("/")


def _get(path: str, params: dict) -> dict:
    tok = os.environ.get("SONAR_TOKEN", "")
    if not tok:
        raise QualitySourceError("SONAR_TOKEN tanımlı değil")
    try:
        r = requests.get(f"{base_url()}{path}", auth=(tok, ""), params=params, timeout=60)
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as e:
        raise QualitySourceError(f"SonarQube isteği başarısız: {e}") from e


def fetch_issues(page_size: int = 500, max_items: int = 10000) -> tuple[list[dict], dict[str, str]]:
    """Tüm sayfalar (Sonar 10.000 sonuçtan fazlasını vermez). Döner: (issues, {kural: ad})."""
    issues: list[dict] = []
    rules: dict[str, str] = {}
    p = 1
    while True:
        d = _get("/api/issues/search", {
            "types": TYPES, "severities": SEVERITIES, "issueStatuses": STATUSES,
            "ps": page_size, "p": p, "additionalFields": "rules"})
        batch = d.get("issues") or []
        issues += batch
        for r in d.get("rules") or []:
            rules[r.get("key", "")] = r.get("name", "")
        total = int((d.get("paging") or {}).get("total") or d.get("total") or 0)
        if not batch or p * page_size >= min(total, max_items):
            break
        p += 1
    return issues, rules


def issues_to_findings(issues: list[dict], rules: dict[str, str], *, base_url: str) -> list[Finding]:
    out: list[Finding] = []
    for i in issues:
        proj = i.get("project") or ""
        if ":" not in proj:
            continue
        repo = proj.split(":", 1)[1]
        comp = i.get("component") or ""
        file = comp[len(proj) + 1:] if comp.startswith(proj + ":") else ""
        rule = i.get("rule") or ""
        out.append(Finding(
            source="sonar", repo=repo, project=proj, file=file, rule=rule,
            rule_name=rules.get(rule, rule), severity=i.get("severity") or "", itype=i.get("type") or "",
            lines=[int(i["line"])] if i.get("line") else [],
            message="" if rule.startswith("secrets:") else (i.get("message") or ""),
            link=f"{base_url}/project/issues?id={quote(proj)}&open={quote(i.get('key') or '')}"))
    return out


def in_scope(project_key: str, repo_projects: list[str]) -> bool:
    prefix = (project_key or "").split(":", 1)[0].lower()
    return prefix in {p.lower() for p in repo_projects or []}
```

- [ ] **Step 3b: `src/agile_sdlc_crew/quality/newrelic.py`**

```python
"""New Relic toplayıcısı (NerdGraph NRQL, EU region varsayılan).

TransactionError'da stack trace yok: sınıf + mesaj + transactionName + URI.
Prod filtresi çağırandan gelir (ad kalıbı ya da FLODevOps production
eşlemesi), çünkü 'Marketplace API' gibi prod uygulamaların adında [Prod] yok.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict

import requests

from agile_sdlc_crew import pipeline_config
from agile_sdlc_crew.quality.model import Finding, QualitySourceError
from agile_sdlc_crew.quality.rank import action_of

ENDPOINTS = {"eu": "https://api.eu.newrelic.com/graphql", "us": "https://api.newrelic.com/graphql"}
_GQL = "query($a:Int!,$q:Nrql!){actor{account(id:$a){nrql(query:$q,timeout:90){results}}}}"
ERRORS_NRQL = ("SELECT count(*), uniques(request.uri, 3) AS uris, min(timestamp) AS first, "
               "max(timestamp) AS last FROM TransactionError "
               "FACET appName, error.class, error.message, transactionName "
               "SINCE {days} days ago LIMIT MAX")
TX_NRQL = "SELECT count(*) FROM Transaction FACET appName SINCE {days} days ago LIMIT MAX"
_PROD = re.compile(r"^\[(?:prod|production|gcp-prod)\]", re.I)


def is_prod_name(app: str) -> bool:
    return bool(_PROD.match(app or ""))


def nrql(query: str, *, region: str | None = None) -> list[dict]:
    acc = os.environ.get("NEW_RELIC_ACCOUNT_ID", "")
    key = os.environ.get("NEW_RELIC_API_KEY", "")
    if not acc or not key:
        raise QualitySourceError("NEW_RELIC_ACCOUNT_ID / NEW_RELIC_API_KEY tanımlı değil")
    reg = str(region or pipeline_config.get("NEW_RELIC_REGION") or "eu").lower()
    try:
        r = requests.post(ENDPOINTS.get(reg, ENDPOINTS["eu"]),
                          headers={"API-Key": key, "Content-Type": "application/json"},
                          json={"query": _GQL, "variables": {"a": int(acc), "q": query}}, timeout=120)
        r.raise_for_status()
        d = r.json()
    except (requests.RequestException, ValueError) as e:
        raise QualitySourceError(f"New Relic isteği başarısız: {e}") from e
    if d.get("errors"):
        raise QualitySourceError(f"New Relic hatası: {(d['errors'][0] or {}).get('message', '')}")
    acct = (((d.get("data") or {}).get("actor") or {}).get("account") or {})
    return (acct.get("nrql") or {}).get("results") or []


def rows_to_findings(err_rows: list[dict], tx_rows: list[dict], *, is_prod) -> list[Finding]:
    tx: dict[str, int] = {}
    for r in tx_rows or []:
        facet = r.get("facet")
        name = r.get("appName") or (facet[0] if isinstance(facet, list) else facet) or ""
        tx[name] = int(r.get("count") or 0)
    err_total: dict[str, int] = defaultdict(int)
    for r in err_rows or []:
        facet = r.get("facet") or []
        if facet:
            err_total[str(facet[0])] += int(r.get("count") or 0)
    out: list[Finding] = []
    for r in err_rows or []:
        facet = r.get("facet") or []
        if len(facet) < 4:
            continue
        app, cls, msg, txn = (str(x or "") for x in facet[:4])
        if not is_prod(app):
            continue
        rate = err_total[app] / tx[app] if tx.get(app) else 0.0
        out.append(Finding(
            source="newrelic", app=app, err_class=cls, message=msg, action=action_of(txn),
            count=int(r.get("count") or 0), uris=[u for u in (r.get("uris") or []) if u][:3],
            first_seen=float(r.get("first") or 0) / 1000, last_seen=float(r.get("last") or 0) / 1000,
            error_rate=min(rate, 1.0)))
    return out


def fetch(days: int, *, is_prod) -> list[Finding]:
    err = nrql(ERRORS_NRQL.format(days=int(days)))
    tx = nrql(TX_NRQL.format(days=int(days)))
    return rows_to_findings(err, tx, is_prod=is_prod)
```

- [ ] **Step 4: Geçtiğini gör** — Step 2 komutu; Expected: tüm ✅.

- [ ] **Step 5: Gerçek kaynaklara salt-okunur duman testi**

Run:
```bash
.venv/bin/python - <<'EOF'
from pathlib import Path
from dotenv import load_dotenv; load_dotenv(Path(".env"))
from agile_sdlc_crew.quality import sonar, newrelic
iss, rules = sonar.fetch_issues()
print("sonar", len(iss), len(rules))
fs = newrelic.fetch(7, is_prod=newrelic.is_prod_name)
print("nr", len(fs), max(fs, key=lambda f: f.count).app if fs else None)
EOF
```
Expected: `sonar 863 …` civarı (birkaç fark olabilir) ve `nr <yüzlerce> [Prod] tr-stocks.flo.com.tr` (en çok hata). Hata verirse Task'ı bitmiş sayma.

- [ ] **Step 6: Commit**

```bash
git add src/agile_sdlc_crew/quality/sonar.py src/agile_sdlc_crew/quality/newrelic.py tests/test_katman0_gates.py
git commit -m "feat(kalite): SonarQube ve New Relic toplayıcıları"
```

---

### Task 8: Ayarlar, `quality_items` tablosu ve orkestratör

**Files:**
- Modify: `src/agile_sdlc_crew/pipeline_config.py` (SCHEMA, `CREW_COST_ANALYTICS` girdisinden sonra)
- Modify: `src/agile_sdlc_crew/db.py` (`SCHEMA` sonuna tablo; `record_llm_call`'dan önce iki fonksiyon)
- Create: `src/agile_sdlc_crew/quality/intake.py`
- Test: `tests/test_katman0_gates.py` (`test_quality_intake`, `main()`'e ekle)

**Interfaces:**
- Consumes: Task 1–7'nin tümü; `estimation.write_story_points(client, wi_id, sp)`, `db.create_job(wi_id, use_hal, wi_title=...)`, `AzureDevOpsClient.list_iterations/query_work_items/get_team_area_path/get_authenticated_user/create_work_item/get_work_item/update_work_item`, `client.repo_projects`, `client.org_url`, `client.project`.
- Produces: `intake.Deps` (NamedTuple: `gather, board, repo_ok, queue, record, now, cfg`), `intake.default_deps()`, `intake.load_cfg() -> dict`, `intake.gather(source, client, cfg) -> (findings, skipped)`, `intake.build_candidates(source, findings, *, board_index, now, repo_ok, cfg) -> (groups, skipped)`, `intake.preview(client, source, sp, *, team="", deps=None) -> dict`, `intake.take(client, source, sp, *, story_id=None, team="", deps=None) -> dict`, `intake.unmapped(client, source="newrelic") -> dict`, istisnalar `intake.Busy`, `intake.NoSprint`, (`ValueError` story_id için); `db.record_quality_item(**kw)`, `db.list_quality_items(limit=30) -> list[dict]`.
- Dönüş şekli (`preview`/`take`): `{"source", "budget_sp", "total_sp", "candidates", "tasks": [{"id"|None, "title", "repo", "sp", "score", "needs_human", "fingerprints", "job_id"|None, "error"?}], "skipped": [≤50], "skipped_counts": {sebep: adet}, "story": {"id","url","created"}|None, "message"?}`.

- [ ] **Step 1: Testi yaz**

```python
def test_quality_intake():
    print("\n[Q8] Kalite alımı — orkestratör (sahte Azure/DB)")
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.quality import intake, rank
    from agile_sdlc_crew.quality.model import Finding
    keys = ("CREW_QUALITY_INTAKE", "CREW_SONAR_URL", "NEW_RELIC_REGION", "CREW_QUALITY_NR_LOOKBACK_DAYS",
            "CREW_QUALITY_NR_MIN_ERRORS", "CREW_QUALITY_SONAR_PACK_SP", "CREW_QUALITY_SONAR_PACK_FILES",
            "CREW_QUALITY_HUMAN_SHARE", "CREW_QUALITY_MAX_TASKS", "CREW_QUALITY_REOPEN_GRACE_DAYS")
    check("knob'lar kayıtlı, ana anahtar varsayılan kapalı",
          all(k in pc._SCHEMA_BY_KEY for k in keys) and pc._SCHEMA_BY_KEY["CREW_QUALITY_INTAKE"]["default"] is False)
    cfg = {"nr_days": 7, "nr_min_errors": 100, "pack_sp": 3, "pack_files": 5, "human_share": 0.3,
           "max_tasks": 8, "grace_days": 3, "sonar_url": "https://sonar.x"}

    def nf(app, key, cnt, action="Index::index", repo="webservice", cls="E_WARNING", msg=None):
        return Finding(source="newrelic", repo=repo, app=app, action=action, err_class=cls,
                       message=msg or f'Undefined array key "{key}"', count=cnt, last_seen=1e9)

    findings = [nf("[Prod] a", "host", 645000), nf("[Prod] b", "password", 900, action="Api/Order::create", repo="channel"),
                nf("[Prod] a", "low", 50), nf("[Prod] c", "x", 5000, repo="sites")]
    board = {rank.nr_fingerprint("channel", "Api/Order::create", "E_WARNING", 'Undefined array key "password"'):
             {"wi": 9, "state": "Active", "closed_at": 0}}
    groups, skipped = intake.build_candidates("newrelic", findings, board_index=board, now=2e9,
                                              repo_ok=lambda r: r != "sites", cfg=cfg)
    reasons = " | ".join(s["reason"] for s in skipped)
    check("eşik altı, açık WI ve yerelde olmayan repo elenir",
          [g.repo for g in groups] == ["webservice"] and len(groups[0].fingerprints) == 1
          and "yerelde" in reasons and "#9" in reasons and "eşik" in reasons, reasons)

    class FakeClient:
        org_url, project, repo_projects = "https://dev.azure.com/o", "P", ["EcomBackend"]

        def __init__(self):
            self.created, self.updates, self.stories = [], [], []
            self.iters = [{"name": "Sprint 42", "path": "P\\Sprint 42", "attributes": {"timeFrame": "current"}}]

        def list_iterations(self, team=""):
            return self.iters

        def query_work_items(self, wiql, limit=0):
            return self.stories

        def get_team_area_path(self, team=""):
            return "P\\Team"

        def get_authenticated_user(self):
            return {"uniqueName": "me@flo.com.tr"}

        def create_work_item(self, type_name, fields, parent_id=None):
            self.created.append((type_name, fields, parent_id))
            return {"id": 1000 + len(self.created) - 1, "fields": fields}

        def update_work_item(self, wid, ops):
            self.updates.append((wid, ops))
            return {}

        def get_work_item(self, wid):
            return {"id": wid, "fields": {"System.WorkItemType": "Bug", "System.State": "Active"}}

    queued, recorded = [], []
    deps = intake.Deps(
        gather=lambda source, client, cfg_: (list(findings), [{"app": "[Prod] ?", "reason": "eşlenemedi"}]),
        board=lambda client: {}, repo_ok=lambda r: True,
        queue=lambda wid, title: (queued.append(wid), 500 + len(queued))[1],
        record=lambda **kw: recorded.append(kw), now=lambda: 2e9, cfg=lambda: cfg)
    c = FakeClient()
    res = intake.take(c, "newrelic", 10, team="Team", deps=deps)
    story = c.created[0]
    check("story yoksa aktif sprintte açılır (başlık, etiket, iterasyon, area)",
          story[0] == "User Story" and story[1]["System.Title"] == "Kalite: New Relic hataları — Sprint 42"
          and "tempo-src-newrelic" in story[1]["System.Tags"] and story[1]["System.IterationPath"] == "P\\Sprint 42"
          and story[1]["System.AreaPath"] == "P\\Team")
    tasks = [x for x in c.created if x[0] == "Task"]
    check("Task'lar story'nin child'ı; area/iteration story'den; atama PAT sahibi",
          tasks and all(t[2] == 1000 and t[1]["System.IterationPath"] == "P\\Sprint 42"
                        and t[1]["System.AreaPath"] == "P\\Team"
                        and t[1]["System.AssignedTo"] == "me@flo.com.tr" for t in tasks))
    check("her Task kuyruğa alınır ve parmak izi başına kaydedilir",
          len(queued) == len(tasks) and res["tasks"][0]["job_id"] == 501 and len(recorded) >= len(tasks))
    check("toplam SP bütçeyi aşmaz; eşlenemeyen raporlanır",
          res["total_sp"] <= 10 and any(s.get("reason") == "eşlenemedi" for s in res["skipped"]))
    check("story SP = açılan Task'ların toplamı",
          any(w == 1000 and ops[0]["path"] == "/fields/Custom.StoryPoints" and ops[0]["value"] == res["total_sp"]
              for w, ops in c.updates))

    c2 = FakeClient()
    c2.stories = [{"id": 77, "fields": {"System.AreaPath": "P\\A", "System.IterationPath": "P\\Sprint 42",
                                        "Custom.StoryPoints": 2}}]
    res2 = intake.take(c2, "newrelic", 10, deps=deps)
    check("sprintte bu kaynağın story'si varsa onu kullanır; SP üstüne eklenir",
          c2.created and all(x[0] == "Task" and x[2] == 77 for x in c2.created)
          and any(w == 77 and ops[0]["value"] == 2 + res2["total_sp"] for w, ops in c2.updates))

    hum = [nf("[Prod] a", "", 99999, action="X::y", cls="PDOException",
              msg="SQLSTATE[HY000] [2002] Connection refused")]
    c3 = FakeClient()
    q0 = len(queued)
    res3 = intake.take(c3, "newrelic", 10, deps=deps._replace(gather=lambda s, cl, cf: (hum + findings, [])))
    h = [t for t in res3["tasks"] if t["needs_human"]]
    check("needs_human Task açılır, kuyruğa alınmaz, needs-human etiketi taşır",
          len(h) == 1 and h[0]["job_id"] is None and len(queued) - q0 == len(res3["tasks"]) - 1
          and any("needs-human" in x[1].get("System.Tags", "") for x in c3.created if x[0] == "Task"))

    c4 = FakeClient()
    res4 = intake.take(c4, "newrelic", 10, deps=deps._replace(gather=lambda s, cl, cf: ([], [])))
    check("aday yoksa hiçbir WI (story dahil) açılmaz", c4.created == [] and res4["story"] is None
          and res4["tasks"] == [] and res4.get("message"))
    c5 = FakeClient()
    c5.iters = []
    try:
        intake.take(c5, "newrelic", 10, deps=deps)
        ok = False
    except intake.NoSprint:
        ok = True
    check("aktif sprint yok ve story_id yok → NoSprint, WI açılmaz", ok and c5.created == [])
    c6 = FakeClient()
    try:
        intake.take(c6, "newrelic", 10, story_id=5, deps=deps)
        ok = False
    except ValueError:
        ok = True
    check("story_id User Story değilse ValueError, WI açılmaz", ok and c6.created == [])
    intake._LOCK.acquire()
    try:
        try:
            intake.take(FakeClient(), "newrelic", 10, deps=deps)
            ok = False
        except intake.Busy:
            ok = True
    finally:
        intake._LOCK.release()
    check("eşzamanlı ikinci alım → Busy", ok)
    pv = intake.preview(FakeClient(), "newrelic", 10, deps=deps)
    check("preview yazmaz ama aynı seçimi döner", pv["total_sp"] == res["total_sp"] and pv["story"] is None)
    try:
        from agile_sdlc_crew import db
        db.init_db()
        check("quality_items tablosu oluşur ve okunur", isinstance(db.list_quality_items(limit=1), list))
    except Exception as e:  # noqa: BLE001
        skip("quality_items tablosu", f"DB erişilemedi: {e}")
```

`main()` tuple'ına `test_quality_intake,` ekle.

- [ ] **Step 2: Düştüğünü gör** — `... t.test_quality_intake() ...`; Expected: ilk check ❌ (knob yok) sonra `ImportError ... intake` → ÇÖKTÜ.

- [ ] **Step 3a: Knob'lar** — `pipeline_config.py` SCHEMA'da `CREW_COST_ANALYTICS` girdisinin kapanan `},` satırından sonra:

```python
    # ── Kalite işi alımı (New Relic + SonarQube) ──
    {
        "key": "CREW_QUALITY_INTAKE",
        "label": "Kalite İşi Alımı (Genel bakış · Kalite kartı)",
        "type": "bool",
        "default": False,
        "desc": "New Relic / SonarQube bulgularını skor + SP bütçesine göre seçip sprintte User Story + child Task açar ve her Task'ı pipeline kuyruğuna alır (needs_human olanlar hariç). Board'a yazar ve maliyetli iş başlatır — bu yüzden varsayılan kapalı. /api/quality/preview yazmadan aynı seçimi gösterir.",
    },
    {
        "key": "CREW_SONAR_URL",
        "label": "Kalite · SonarQube adresi",
        "type": "str",
        "default": "https://sonarqube.flo.com.tr",
        "desc": "SonarQube kök adresi. Token .env'deki SONAR_TOKEN'dan okunur.",
    },
    {
        "key": "NEW_RELIC_REGION",
        "label": "Kalite · New Relic bölgesi",
        "type": "str",
        "default": "eu",
        "desc": "eu → api.eu.newrelic.com, us → api.newrelic.com. Bu hesap EU'da; yanlış bölge 'not authorized for account region' döner.",
    },
    {
        "key": "CREW_QUALITY_NR_LOOKBACK_DAYS",
        "label": "Kalite · NR geriye bakış (gün)",
        "type": "int",
        "default": 7,
        "min": 1,
        "desc": "New Relic hata sayıları ve error rate bu kadar günden hesaplanır.",
    },
    {
        "key": "CREW_QUALITY_NR_MIN_ERRORS",
        "label": "Kalite · NR minimum hata",
        "type": "int",
        "default": 100,
        "min": 1,
        "desc": "Geriye bakış süresinde bundan az hata üreten gruplar aday olmaz.",
    },
    {
        "key": "CREW_QUALITY_SONAR_PACK_SP",
        "label": "Kalite · Sonar paket SP tavanı",
        "type": "int",
        "default": 3,
        "min": 1,
        "desc": "Aynı repo + kuraldaki Sonar maddeleri en fazla bu SP'lik Task'larda paketlenir.",
    },
    {
        "key": "CREW_QUALITY_SONAR_PACK_FILES",
        "label": "Kalite · Sonar paket dosya tavanı",
        "type": "int",
        "default": 5,
        "min": 1,
        "desc": "Bir Sonar Task'ı en fazla bu kadar dosyaya dokunur (dar plan = güvenilir geliştirici).",
    },
    {
        "key": "CREW_QUALITY_HUMAN_SHARE",
        "label": "Kalite · insan işi payı",
        "type": "float",
        "default": 0.3,
        "desc": "needs_human işler (secret rotasyonu, altyapı hatası) bütçenin en çok bu oranını kaplar.",
    },
    {
        "key": "CREW_QUALITY_MAX_TASKS",
        "label": "Kalite · alım başına en çok Task",
        "type": "int",
        "default": 8,
        "min": 1,
        "desc": "Her Task bir pipeline işi demek — maliyet tavanı.",
    },
    {
        "key": "CREW_QUALITY_REOPEN_GRACE_DAYS",
        "label": "Kalite · yeniden açma bekleme (gün)",
        "type": "int",
        "default": 3,
        "min": 0,
        "desc": "Done olmuş kalite işinin hatası kapanıştan bu kadar gün sonra hâlâ görülüyorsa yeni Task açılır. Canceled işler hiç yeniden açılmaz.",
    },
```

- [ ] **Step 3b: Tablo ve DB fonksiyonları** — `db.py` `SCHEMA` string'inde `llm_calls` tablosunun `) ENGINE=InnoDB;` satırından sonra, kapanan `"""`'den önce:

```sql

CREATE TABLE IF NOT EXISTS quality_items (
    id INT AUTO_INCREMENT PRIMARY KEY,
    fingerprint VARCHAR(512) NOT NULL,
    source ENUM('sonar','newrelic') NOT NULL,
    repo VARCHAR(128) DEFAULT '',
    story_id INT NULL,
    wi_id INT NULL,
    job_id INT NULL,
    sp DECIMAL(4,1) DEFAULT 0,
    score DOUBLE DEFAULT 0,
    needs_human TINYINT(1) DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_fp (fingerprint(191)),
    INDEX idx_wi (wi_id)
) ENGINE=InnoDB;
```

`def record_llm_call` satırından önce:

```python
def record_quality_item(*, fingerprint: str, source: str, repo: str, story_id: int | None,
                        wi_id: int | None, job_id: int | None, sp: float, score: float,
                        needs_human: bool) -> None:
    """Kalite alımının geçmiş kaydı (dashboard). Dedup buna DEĞİL board'a bakar."""
    with get_conn() as conn:
        conn.cursor().execute(
            "INSERT INTO quality_items (fingerprint, source, repo, story_id, wi_id, job_id, sp, score, "
            "needs_human) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (fingerprint[:512], source, repo[:128], story_id, wi_id, job_id, sp, score, int(bool(needs_human))))


def list_quality_items(limit: int = 30) -> list[dict]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, fingerprint, source, repo, story_id, wi_id, job_id, sp, score, needs_human, "
                    "created_at FROM quality_items ORDER BY id DESC LIMIT %s", (int(limit),))
        rows = cur.fetchall() or []
    for r in rows:
        r["sp"] = float(r.get("sp") or 0)
        r["created_at"] = str(r.get("created_at") or "")
    return rows
```

- [ ] **Step 3c: `src/agile_sdlc_crew/quality/intake.py`**

```python
"""Kalite işi alımı — orkestratör.

  gather (ağ) → build_candidates (saf: repo filtresi, eşik, dedup, birleştirme/paket)
  → rank.select_budget → [take] story bul/aç → child Task'lar → kuyruk → quality_items

Seçimde LLM yok. take() tam otonomdur (kullanıcı kararı): onay listesi yok;
yanlış tıklamaya karşı tek koruma CREW_QUALITY_INTAKE anahtarı ve bütçe/Task
tavanlarıdır. Aynı anda tek alım (_LOCK) — ikisi aynı parmak izini açmasın.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Callable, NamedTuple

from agile_sdlc_crew import pipeline_config
from agile_sdlc_crew.quality import app_map, dedup, newrelic, rank, render, sonar
from agile_sdlc_crew.quality.model import Finding, Group

log = logging.getLogger("pipeline")

SOURCES = ("newrelic", "sonar")
_LOCK = threading.Lock()
_DONE = {"done", "closed", "completed", "removed", "canceled", "cancelled"}


class Busy(RuntimeError):
    """Başka bir kalite alımı sürüyor."""


class NoSprint(RuntimeError):
    """Takımın aktif sprinti yok ve story_id verilmedi."""


class Deps(NamedTuple):
    gather: Callable
    board: Callable
    repo_ok: Callable
    queue: Callable
    record: Callable
    now: Callable
    cfg: Callable


def load_cfg() -> dict:
    g = pipeline_config.get
    return {
        "nr_days": int(g("CREW_QUALITY_NR_LOOKBACK_DAYS") or 7),
        "nr_min_errors": int(g("CREW_QUALITY_NR_MIN_ERRORS") or 100),
        "pack_sp": int(g("CREW_QUALITY_SONAR_PACK_SP") or 3),
        "pack_files": int(g("CREW_QUALITY_SONAR_PACK_FILES") or 5),
        "human_share": float(g("CREW_QUALITY_HUMAN_SHARE") if g("CREW_QUALITY_HUMAN_SHARE") is not None else 0.3),
        "max_tasks": int(g("CREW_QUALITY_MAX_TASKS") or 8),
        "grace_days": int(g("CREW_QUALITY_REOPEN_GRACE_DAYS") if g("CREW_QUALITY_REOPEN_GRACE_DAYS") is not None else 3),
        "sonar_url": sonar.base_url(),
    }


def repo_is_local(repo: str, base: Path | None = None) -> bool:
    """Pipeline yalnızca ~/.crew_repos altında klonu olan repoda çalışabilir."""
    base = base or Path(os.environ.get("CREW_REPOS_DIR", "~/.crew_repos")).expanduser()
    if not repo or not base.is_dir():
        return False
    if (base / repo).is_dir():
        return True
    low = repo.lower()
    return any(p.is_dir() and p.name.lower() == low for p in base.iterdir())


def gather(source: str, client, cfg: dict) -> tuple[list[Finding], list[dict]]:
    skipped: list[dict] = []
    if source == "newrelic":
        mapping = app_map.load_map(client)
        found = newrelic.fetch(cfg["nr_days"],
                               is_prod=lambda a: newrelic.is_prod_name(a) or a.lower() in mapping)
        out: list[Finding] = []
        unmapped: Counter = Counter()
        for f in found:
            repo = app_map.resolve(f.app, mapping)
            if not repo:
                unmapped[f.app] += f.count
                continue
            f.repo = repo
            out.append(f)
        skipped += [{"app": a, "reason": "eşlenemedi", "count": n} for a, n in unmapped.most_common()]
        return out, skipped
    issues, rules = sonar.fetch_issues()
    found = sonar.issues_to_findings(issues, rules, base_url=cfg["sonar_url"])
    projects = list(getattr(client, "repo_projects", []) or [])
    out = [f for f in found if sonar.in_scope(f.project, projects)]
    skipped += [{"project": p, "reason": "kapsam dışı proje"}
                for p in sorted({f.project for f in found if not sonar.in_scope(f.project, projects)})]
    return out, skipped


def build_candidates(source: str, findings: list[Finding], *, board_index: dict, now: float,
                     repo_ok: Callable, cfg: dict) -> tuple[list[Group], list[dict]]:
    skipped: list[dict] = []
    kept: list[Finding] = []
    missing: set[str] = set()
    for f in findings:
        if repo_ok(f.repo):
            kept.append(f)
        elif f.repo not in missing:
            missing.add(f.repo)
            skipped.append({"repo": f.repo, "reason": "repo yerelde yok"})
    if source == "newrelic":
        units = []
        for u in rank.nr_units(kept):
            if u.total < cfg["nr_min_errors"]:
                skipped.append({"fingerprint": u.fingerprints[0],
                                "reason": f"eşik altı (<{cfg['nr_min_errors']})"})
            else:
                units.append(u)
    else:
        units = rank.sonar_units(kept)
    live: list[Group] = []
    for u in units:
        fp = u.fingerprints[0]
        v, prev = dedup.verdict(board_index.get(fp), last_seen=u.last_seen, now=now,
                                grace_days=cfg["grace_days"])
        if v == "blocked":
            skipped.append({"fingerprint": fp, "reason": f"açık WI #{prev}"})
            continue
        if v == "suppressed":
            skipped.append({"fingerprint": fp, "reason": f"iptal edilmiş WI #{prev}"})
            continue
        if v == "recurring":
            u.recurring_of = prev
        live.append(u)
    if source == "newrelic":
        groups = rank.merge_undefined(live)
    else:
        groups = rank.pack_sonar(live, pack_sp=cfg["pack_sp"], pack_files=cfg["pack_files"])
    return groups, skipped


def default_deps() -> Deps:
    from agile_sdlc_crew import db
    return Deps(
        gather=gather,
        board=lambda client: dedup.index_board(dedup.fetch_board(client)),
        repo_ok=repo_is_local,
        queue=lambda wid, title: db.create_job(str(wid), False, wi_title=title[:200]),
        record=db.record_quality_item,
        now=time.time,
        cfg=load_cfg,
    )


def _row(g: Group) -> dict:
    return {"id": None, "title": render.task_title(g), "repo": g.repo, "sp": g.sp,
            "score": round(g.score, 2), "needs_human": g.needs_human, "fingerprints": g.fingerprints,
            "job_id": None}


def _plan(client, source: str, sp: int, deps: Deps) -> tuple[list[Group], dict, dict]:
    if source not in SOURCES:
        raise ValueError(f"Geçersiz kaynak: {source}")
    cfg = deps.cfg()
    findings, skipped = deps.gather(source, client, cfg)
    groups, sk2 = build_candidates(source, findings, board_index=deps.board(client), now=deps.now(),
                                   repo_ok=deps.repo_ok, cfg=cfg)
    skipped += sk2
    picked = rank.select_budget(groups, int(sp), human_share=cfg["human_share"], max_tasks=cfg["max_tasks"])
    result = {
        "source": source, "budget_sp": int(sp), "total_sp": sum(g.sp for g in picked),
        "candidates": len(groups), "tasks": [_row(g) for g in picked], "skipped": skipped[:50],
        "skipped_counts": dict(Counter(s["reason"] if not s["reason"].startswith(("açık WI", "iptal"))
                                       else s["reason"].split(" #")[0] for s in skipped)),
        "story": None,
    }
    if not picked:
        result["message"] = "Bütçeye uyan aday yok."
    return picked, result, cfg


def preview(client, source: str, sp: int, *, team: str = "", deps: Deps | None = None) -> dict:
    _picked, result, _cfg = _plan(client, source, sp, deps or default_deps())
    return result


def _wi_url(client, wid: int) -> str:
    return f"{client.org_url}/{client.project}/_workitems/edit/{wid}"


def resolve_story(client, source: str, story_id: int | None, team: str) -> dict:
    if story_id:
        f = (client.get_work_item(int(story_id)) or {}).get("fields") or {}
        if f.get("System.WorkItemType") != "User Story":
            raise ValueError(f"#{story_id} bir User Story değil ({f.get('System.WorkItemType') or '?'})")
        if (f.get("System.State") or "").lower() in _DONE:
            raise ValueError(f"#{story_id} kapalı ({f.get('System.State')})")
        return {"id": int(story_id), "area": f.get("System.AreaPath", ""),
                "iteration": f.get("System.IterationPath", ""), "sp": float(f.get("Custom.StoryPoints") or 0),
                "created": False}
    cur = next((i for i in client.list_iterations(team)
                if ((i.get("attributes") or {}).get("timeFrame") or "").lower() == "current"), None)
    if not cur:
        raise NoSprint("Takımın aktif sprinti yok — story_id verin")
    ipath = cur.get("path") or ""
    esc = ipath.replace("'", "''")
    found = client.query_work_items(
        "SELECT [System.Id] FROM WorkItems WHERE [System.WorkItemType] = 'User Story' "
        f"AND [System.IterationPath] = '{esc}' AND [System.Tags] CONTAINS '{render.TAG}' "
        f"AND [System.Tags] CONTAINS '{render.src_tag(source)}' "
        "AND [System.State] NOT IN ('Done', 'Closed', 'Removed', 'Canceled') ORDER BY [System.Id] DESC",
        limit=1)
    if found:
        f = found[0].get("fields") or {}
        return {"id": int(found[0]["id"]), "area": f.get("System.AreaPath", ""),
                "iteration": f.get("System.IterationPath", ipath),
                "sp": float(f.get("Custom.StoryPoints") or 0), "created": False}
    area = client.get_team_area_path(team) or ""
    fields = {"System.Title": render.story_title(source, cur.get("name") or ipath),
              "System.Description": render.story_description(source),
              "System.Tags": f"{render.TAG}; {render.src_tag(source)}",
              "System.IterationPath": ipath}
    if area:
        fields["System.AreaPath"] = area
    wi = client.create_work_item("User Story", fields)
    wf = wi.get("fields") or {}
    return {"id": int(wi["id"]), "area": area or wf.get("System.AreaPath", ""), "iteration": ipath,
            "sp": 0.0, "created": True}


def _assignee(client) -> str:
    try:
        return (client.get_authenticated_user() or {}).get("uniqueName", "")
    except Exception:  # noqa: BLE001
        return ""


def take(client, source: str, sp: int, *, story_id: int | None = None, team: str = "",
         deps: Deps | None = None) -> dict:
    from agile_sdlc_crew import estimation
    deps = deps or default_deps()
    if not _LOCK.acquire(blocking=False):
        raise Busy("Başka bir kalite alımı sürüyor")
    try:
        picked, result, cfg = _plan(client, source, sp, deps)
        if not picked:
            return result
        story = resolve_story(client, source, story_id, team)
        result["story"] = {"id": story["id"], "url": _wi_url(client, story["id"]), "created": story["created"]}
        me = _assignee(client)
        rows = []
        added = 0
        for g in picked:
            row = _row(g)
            fields = {"System.Title": row["title"],
                      "System.Description": render.task_description(g, sonar_url=cfg["sonar_url"],
                                                                    nr_days=cfg["nr_days"]),
                      "System.Tags": render.task_tags(g),
                      "System.IterationPath": story["iteration"]}
            if story["area"]:
                fields["System.AreaPath"] = story["area"]
            if me:
                fields["System.AssignedTo"] = me
            try:
                wid = int(client.create_work_item("Task", fields, parent_id=story["id"])["id"])
            except Exception as e:  # noqa: BLE001
                row["error"] = f"Task açılamadı: {e}"
                rows.append(row)
                continue
            row["id"] = wid
            added += g.sp
            estimation.write_story_points(client, str(wid), g.sp, logger=log.info)
            if not g.needs_human:
                try:
                    row["job_id"] = deps.queue(wid, row["title"])
                except Exception as e:  # noqa: BLE001
                    row["error"] = f"kuyruğa alınamadı: {e}"
            for fp in g.fingerprints:
                try:
                    deps.record(fingerprint=fp, source=source, repo=g.repo, story_id=story["id"], wi_id=wid,
                                job_id=row["job_id"], sp=g.sp, score=g.score, needs_human=g.needs_human)
                except Exception as e:  # noqa: BLE001
                    log.warning(f"⚠️ Kalite: quality_items yazılamadı ({fp[:60]}): {e}")
            rows.append(row)
        if added:
            estimation.write_story_points(client, str(story["id"]), int(story["sp"] + added), logger=log.info)
        result["tasks"] = rows
        result["total_sp"] = added
        log.info(f"🧹 Kalite alımı ({source}): story #{story['id']} · {len(rows)} Task · {added} SP")
        return result
    finally:
        _LOCK.release()


def unmapped(client, source: str = "newrelic") -> dict:
    """Eşlenemeyen NR uygulamaları / kapsam dışı Sonar projeleri (yazma yok)."""
    deps = default_deps()
    _f, skipped = deps.gather(source, client, deps.cfg())
    return {"source": source, "items": skipped}
```

Not: `story["sp"]` testte `Custom.StoryPoints: 2` → `int(2 + added)`; yeni story'de 0.

- [ ] **Step 4: Geçtiğini gör** — Step 2 komutu; Expected: tüm ✅ (DB erişilemezse son satır ⏭️ ATLANIR, bu kabul).

- [ ] **Step 5: Tüm regresyon dosyası** — `.venv/bin/python tests/test_katman0_gates.py`; Expected: `BAŞARISIZ 0`.

- [ ] **Step 6: Commit**

```bash
git add src/agile_sdlc_crew/pipeline_config.py src/agile_sdlc_crew/db.py src/agile_sdlc_crew/quality/intake.py tests/test_katman0_gates.py
git commit -m "feat(kalite): knob'lar, quality_items tablosu ve preview/take orkestratörü"
```

---

### Task 9: Endpoint'ler ve dashboard kartı

**Files:**
- Modify: `src/agile_sdlc_crew/server.py` (yeni `QualityTakeRequest` modeli `SprintQueueRequest`'in yanına; endpoint'ler `/api/sprint-plan/queue`'dan sonra)
- Modify: `src/agile_sdlc_crew/web/index.html` (kart `ovStuckCard`'dan sonra; JS `renderOvStuck`'tan sonra; `loadQuality()` `DOMContentLoaded` içinde)
- Test: `tests/test_katman0_gates.py` (`test_quality_endpoints`, `main()`'e ekle)

**Interfaces:**
- Consumes: `intake.preview/take/unmapped`, `intake.Busy`, `intake.NoSprint`, `model.QualitySourceError`, `db.list_quality_items`, `_ensure_worker()`, `wiLink(id)` (JS), `esc()` (JS).
- Produces: `GET /api/quality/preview?source=&sp=&team=`, `POST /api/quality/take`, `GET /api/quality/unmapped?source=`, `GET /api/quality/recent` → `{"enabled": bool, "items": [...]}`; JS `loadQuality()`, `qualityRun(take)`.

- [ ] **Step 1: Testi yaz**

```python
def test_quality_endpoints():
    print("\n[Q9] Kalite alımı — endpoint kapıları + dashboard kartı")
    from fastapi.testclient import TestClient
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew import server
    orig = pc.get
    pc.get = lambda k: False if k == "CREW_QUALITY_INTAKE" else orig(k)
    try:
        tc = TestClient(server.app)
        check("kapalıyken take → 403", tc.post("/api/quality/take", json={"source": "newrelic", "sp": 10}).status_code == 403)
        check("kapalıyken preview → 403", tc.get("/api/quality/preview?source=sonar&sp=5").status_code == 403)
        check("kapalıyken unmapped → 403", tc.get("/api/quality/unmapped").status_code == 403)
        check("recent kapalıyken enabled:false", tc.get("/api/quality/recent").json().get("enabled") is False)
    finally:
        pc.get = orig
    pc.get = lambda k: True if k == "CREW_QUALITY_INTAKE" else orig(k)
    try:
        tc = TestClient(server.app)
        check("geçersiz kaynak → 400", tc.post("/api/quality/take", json={"source": "jira", "sp": 10}).status_code == 400)
        check("SP sınırı (1..40) → 400", tc.post("/api/quality/take", json={"source": "sonar", "sp": 0}).status_code == 400
              and tc.get("/api/quality/preview?source=sonar&sp=99").status_code == 400)
    finally:
        pc.get = orig
    html = (Path(__file__).resolve().parent.parent / "src/agile_sdlc_crew/web/index.html").read_text(encoding="utf-8")
    check("dashboard Kalite kartı ve çağrıları",
          'id="ovQualityCard"' in html and "/api/quality/take" in html and "/api/quality/preview" in html
          and "function loadQuality" in html and "loadQuality();" in html)
```

`main()` tuple'ına `test_quality_endpoints,` ekle.

- [ ] **Step 2: Düştüğünü gör** — `... t.test_quality_endpoints() ...`; Expected: 403 check'leri ❌ (404 döner), kart check'i ❌.

- [ ] **Step 3a: `server.py`** — `class SprintQueueRequest` tanımından sonra:

```python
class QualityTakeRequest(BaseModel):
    source: str
    sp: int = 10
    story_id: int | None = None
    team: str = ""
```

`/api/sprint-plan/queue` endpoint'inden sonra:

```python
def _quality_guard(source: str, sp: int | None = None):
    """Ortak kapı: anahtar kapalı → 403, kaynak/SP geçersiz → 400. None = geçti."""
    from agile_sdlc_crew import pipeline_config as _pc
    if not _pc.get("CREW_QUALITY_INTAKE"):
        return JSONResponse({"error": "Kalite işi alımı kapalı (CREW_QUALITY_INTAKE)"}, status_code=403)
    if source not in ("newrelic", "sonar"):
        return JSONResponse({"error": "source 'newrelic' ya da 'sonar' olmalı"}, status_code=400)
    if sp is not None and not 1 <= int(sp) <= 40:
        return JSONResponse({"error": "sp 1 ile 40 arasında olmalı"}, status_code=400)
    return None


def _quality_error(e: Exception) -> JSONResponse:
    from agile_sdlc_crew.quality import intake as _qi
    from agile_sdlc_crew.quality.model import QualitySourceError
    if isinstance(e, QualitySourceError):
        return JSONResponse({"error": str(e)}, status_code=502)
    if isinstance(e, (_qi.Busy, _qi.NoSprint)):
        return JSONResponse({"error": str(e)}, status_code=409)
    if isinstance(e, ValueError):
        return JSONResponse({"error": str(e)}, status_code=400)
    log.exception("Kalite alımı hatası")
    return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/quality/preview")
async def quality_preview(source: str = "newrelic", sp: int = 10, team: str = ""):
    """Kalite alımının seçimini hiçbir şey yazmadan döndürür."""
    from agile_sdlc_crew.quality import intake as _qi
    bad = _quality_guard(source, sp)
    if bad:
        return bad
    try:
        return JSONResponse(await asyncio.to_thread(_qi.preview, AzureDevOpsClient(), source, sp, team=team))
    except Exception as e:  # noqa: BLE001
        return _quality_error(e)


@app.post("/api/quality/take")
async def quality_take(req: QualityTakeRequest):
    """Tam otonom: seç → story bul/aç → child Task'lar → kuyruk. needs_human Task kuyruğa girmez."""
    from agile_sdlc_crew.quality import intake as _qi
    bad = _quality_guard(req.source, req.sp)
    if bad:
        return bad
    try:
        res = await asyncio.to_thread(_qi.take, AzureDevOpsClient(), req.source, req.sp,
                                      story_id=req.story_id, team=req.team)
        if any(t.get("job_id") for t in res.get("tasks") or []):
            _ensure_worker()
        return JSONResponse(res, status_code=201 if res.get("story") else 200)
    except Exception as e:  # noqa: BLE001
        return _quality_error(e)


@app.get("/api/quality/unmapped")
async def quality_unmapped(source: str = "newrelic"):
    """Eşlenemeyen NR uygulamaları / kapsam dışı Sonar projeleri."""
    from agile_sdlc_crew.quality import intake as _qi
    bad = _quality_guard(source)
    if bad:
        return bad
    try:
        return JSONResponse(await asyncio.to_thread(_qi.unmapped, AzureDevOpsClient(), source))
    except Exception as e:  # noqa: BLE001
        return _quality_error(e)


@app.get("/api/quality/recent")
async def quality_recent(limit: int = 30):
    """Kartın görünürlüğü + son alımlar. Kapalıyken DB'ye dokunmaz."""
    from agile_sdlc_crew import pipeline_config as _pc
    if not _pc.get("CREW_QUALITY_INTAKE"):
        return JSONResponse({"enabled": False, "items": []})
    try:
        items = await asyncio.to_thread(db.list_quality_items, max(1, min(int(limit or 30), 200)))
        return JSONResponse({"enabled": True, "items": items})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"enabled": True, "items": [], "error": str(e)})
```

`log` adının `server.py`'de tanımlı olduğunu doğrula: `grep -n "^log = " src/agile_sdlc_crew/server.py`. Farklı bir adla tanımlıysa (`logger` vb.) `_quality_error` içindeki `log.exception`'ı ona çevir.

- [ ] **Step 3b: `index.html` kartı** — `ovStuckCard` div'inin kapanan `</div>`'inden (satır ~470) sonra:

```html
      <div class="card" id="ovQualityCard" style="display:none;flex-direction:column;overflow:hidden">
        <div class="card-head" style="flex-wrap:wrap;gap:8px"><span class="h">Kalite işi alımı</span><span style="flex:1"></span>
          <select class="sprint-select" id="qSource" title="Kaynak"><option value="newrelic">New Relic</option><option value="sonar">SonarQube</option></select>
          <input class="sprint-select" id="qSp" type="number" min="1" max="40" value="10" style="width:64px" title="SP bütçesi">
          <input class="sprint-select" id="qStory" type="number" placeholder="Story ID (ops.)" style="width:130px" title="Boşsa takımın aktif sprintinde story bulunur/açılır">
          <button class="sm-btn ghost small" onclick="qualityRun(false)">Önizle</button>
          <button class="sm-btn small" onclick="qualityRun(true)">Al</button>
        </div>
        <div id="qResult" style="padding:0 14px"></div>
        <div id="qRecent" style="padding:8px 14px 12px;font-size:12px;color:var(--text-dim)"></div>
      </div>
```

- [ ] **Step 3c: `index.html` JS** — `function renderOvStuck(){…}` bloğunun kapanışından sonra:

```js
// Genel bakis · kalite isi alimi (New Relic / Sonar). Kart yalnizca
// CREW_QUALITY_INTAKE aciksa gorunur; secim, SP ve dedup kurallari sunucuda.
async function loadQuality(){
  const card=document.getElementById('ovQualityCard');if(!card)return;
  try{
    const r=await (await fetch('/api/quality/recent')).json();
    if(!r.enabled){card.style.display='none';return}
    card.style.display='flex';
    const uniq=[...new Map((r.items||[]).filter(i=>i.wi_id).map(i=>[i.wi_id,i])).values()].slice(0,10);
    document.getElementById('qRecent').innerHTML=uniq.length
      ?'Son alımlar: '+uniq.map(i=>`${wiLink(i.wi_id)} ${esc(i.source)} · ${i.sp} SP${i.job_id?` · iş #${i.job_id}`:''}${i.needs_human?' · insan':''}`).join(' &nbsp;·&nbsp; ')
      :'Henüz alım yok.';
  }catch(e){card.style.display='none'}
}
async function qualityRun(take){
  const box=document.getElementById('qResult');
  const source=document.getElementById('qSource').value;
  const sp=parseInt(document.getElementById('qSp').value||'10',10);
  const story=parseInt(document.getElementById('qStory').value||'0',10)||null;
  const team=(document.getElementById('teamSel')||{}).value||'';
  box.innerHTML=`<div class="board-empty" style="padding:12px 0">${take?'İşler açılıyor…':'Seçim hesaplanıyor…'}</div>`;
  try{
    const resp=take
      ?await fetch('/api/quality/take',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source,sp,story_id:story,team})})
      :await fetch(`/api/quality/preview?source=${encodeURIComponent(source)}&sp=${sp}&team=${encodeURIComponent(team)}`);
    const r=await resp.json();
    if(!resp.ok){box.innerHTML=`<div class="board-empty" style="padding:12px 0;color:var(--danger)">${esc(r.error||('HTTP '+resp.status))}</div>`;return}
    const rows=(r.tasks||[]).map(t=>`<tr><td>${t.id?wiLink(t.id):'—'}</td><td>${esc(t.title)}</td><td class="num">${t.sp}</td><td>${t.needs_human?'<span class="chip human">insan</span>':(t.job_id?`iş #${t.job_id}`:esc(t.error||''))}</td></tr>`).join('');
    const head=r.story?`Story <a href="${esc(r.story.url)}" target="_blank">#${r.story.id}</a>${r.story.created?' (yeni)':''} · `:'';
    const sk=Object.entries(r.skipped_counts||{}).map(([k,v])=>`${esc(k)}: ${v}`).join(' · ');
    box.innerHTML=`<div style="font-size:12px;padding:6px 0">${head}${(r.tasks||[]).length} Task · ${r.total_sp}/${r.budget_sp} SP${sk?` · atlanan — ${sk}`:''}</div>`
      +(rows?`<table class="perf"><tr><th>WI</th><th>İş</th><th class="num">SP</th><th>Durum</th></tr>${rows}</table>`
            :`<div class="board-empty" style="padding:8px 0">${esc(r.message||'Bütçeye uyan aday yok.')}</div>`);
    if(take)loadQuality();
  }catch(e){box.innerHTML=`<div class="board-empty" style="padding:12px 0;color:var(--danger)">${esc(String(e))}</div>`}
}
```

`document.addEventListener('DOMContentLoaded',()=>{` bloğunda (satır ~1367) `showTab(t);` satırından sonra `loadQuality();` ekle.

- [ ] **Step 4: Geçtiğini gör** — Step 2 komutu; Expected: tüm ✅. Ardından `.venv/bin/python -c "from agile_sdlc_crew.server import app"` hatasız.

- [ ] **Step 5: Commit**

```bash
git add src/agile_sdlc_crew/server.py src/agile_sdlc_crew/web/index.html tests/test_katman0_gates.py
git commit -m "feat(kalite): /api/quality endpoint'leri ve Genel bakış Kalite kartı"
```

---

### Task 10: Dokümantasyon, sunucu ve canlı doğrulama

**Files:**
- Modify: `CLAUDE.md` ("Notes for editing" listesinin sonuna bir madde)
- Modify: `docs/superpowers/specs/2026-10-13-quality-intake-design.md` (Durum satırı)

- [ ] **Step 1: CLAUDE.md maddesi** — "Notes for editing" listesinin sonuna:

```markdown
- **Quality intake** (`quality/` package, `POST /api/quality/take`, `GET /api/quality/preview|unmapped|recent`, Genel bakış **Kalite** card; `CREW_QUALITY_INTAKE` default **off**): "take N SP from New Relic / Sonar" picks findings deterministically (no LLM) and opens a User Story (the team's *current* sprint, reused per source via `tempo-quality` + `tempo-src-<source>` tags, or a given `story_id`) with child Tasks, then queues one pipeline job per Task. Fully autonomous by user decision — the toggle and the SP/task caps are the only guards. Sources: Sonar BUG+VULNERABILITY / BLOCKER+CRITICAL (`api/issues/search`; project key `EcomBackend:webservice` → repo; scope = key prefix in `AZURE_DEVOPS_REPO_PROJECTS`); New Relic NerdGraph on the **EU** endpoint (`api.newrelic.com` answers "not authorized for account region"). NR app → repo comes from **FLODevOps** `anatolia`/`global`/`russia` (branch `production`): `build/<svc>/JenkinsfileBuild` `gitOriginApp` + `newrelic.appname` / the websites template `"[Prod] ${website}"`; cached 24h in `config/quality_app_map.yaml` (gitignored), hand fixes in `config/quality_app_map_overrides.yaml`. **Dedup reads the board, not MySQL**: the visible `tempo-fp:` lines in Task descriptions (an HTML comment would not survive Azure's sanitizer). `Resolved` / `Ready for Production` still block (the fix is not live yet), `Canceled` suppresses forever, `Done` reopens only after `CREW_QUALITY_REOPEN_GRACE_DAYS`. Scoring: Sonar `weight × (1 + log10(count))` — log2 would let 300 CSS warnings outrank a single blocker vulnerability; NR `errors × apps`. `secrets:*` messages are never stored; Tasks for secrets and infra errors are `needs-human` and are not queued.
```

- [ ] **Step 2: Spec durum satırını güncelle** — `**Durum:** tasarım onayı bekliyor` → `**Durum:** uygulandı (feature/quality-intake)`.

- [ ] **Step 3: Tüm regresyon** — `.venv/bin/python tests/test_katman0_gates.py`; Expected: `BAŞARISIZ 0`.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-10-13-quality-intake-design.md
git commit -m "docs(kalite): CLAUDE.md notu ve spec durumu"
```

- [ ] **Step 5: Sunucuyu yeniden başlat** — Önce `curl -s localhost:8765/api/health` → `running` 0 olmalı (değilse bekle; koşan işi bozma). Sonra `./start.sh`, ardından `curl -s localhost:8765/api/health` sağlıklı.

- [ ] **Step 6: Anahtarı aç** — `curl -s -X PUT localhost:8765/api/pipeline/config -H 'Content-Type: application/json' -d '{"CREW_QUALITY_INTAKE": true}'`

- [ ] **Step 7: Canlı önizleme (yazma yok)**

```bash
curl -s 'localhost:8765/api/quality/preview?source=newrelic&sp=10' | .venv/bin/python -m json.tool | head -80
curl -s 'localhost:8765/api/quality/preview?source=sonar&sp=10' | .venv/bin/python -m json.tool | head -80
curl -s 'localhost:8765/api/quality/unmapped?source=newrelic' | .venv/bin/python -m json.tool | head -40
```
Expected: NR'de ilk sırada `[webservice] Index::index: Undefined array key "host"`; `total_sp` ≤ 10; Sonar'da Vulnerability maddeleri CSS paketlerinin önünde; `unmapped` listesi makul (EcomFrontend/BusinessProcess repoları "repo yerelde yok" olarak `skipped_counts`'ta). Beklenmedik sıralama varsa dur, kullanıcıya göster.

- [ ] **Step 8: Kullanıcıya sor, sonra canlı 3 SP alımı** — Board'a WI açar ve pipeline işi başlatır; kullanıcı onay verirse:
`curl -s -X POST localhost:8765/api/quality/take -H 'Content-Type: application/json' -d '{"source":"newrelic","sp":3}' | .venv/bin/python -m json.tool`
Expected: `story` (yeni ya da mevcut) + 1–3 Task + `job_id`'ler. Azure'da Task açıklamasının ilk satırı `Repo:` ve sonunda `tempo-fp:` satırları görünmeli. İlk işi `/api/jobs/<id>` ile sonuna kadar izle: `discover_repos_task` doğru repoyu seçmeli, iş PR'a ulaşmalı ya da açık bir needs_human/needs_info sebebiyle durmalı. İşi yarım bırakma. Aynı komutu tekrar çalıştırınca aynı parmak izleri `açık WI` sebebiyle atlanmalı (dedup canlı doğrulaması).
```
