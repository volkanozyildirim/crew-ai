"""Ortam değişkeni kuralı — repoda var olan adları bul, ajana ver.

PR 45061 (iş #235): Sonar "sabit kodlu kimlik bilgisi" işinde yalın akış repo
keşfini atladı, mimar yalnız parolanın geçtiği 389 karakterlik dosyayı gördü ve
`ORKESTRA_RO_DB_PASSWORD` adını uydurdu. Oysa repo `main/.env.example` ve
`main/init.php` içinde `DB_ORKESTRA_PROD_PASS` kuralını zaten taşıyordu ve
prod ortamında (FLODevOps `externalSecrets`, git'te değil) o ad tanımlı.

Burada: örnek env dosyalarından ve PHP'deki getenv/$_ENV/env() çağrılarından
AD'lar toplanır (asla değer değil; gerçek `.env` okunmaz), işin satırındaki
sözcüklerle sıralanır ve mimar/geliştirici/reviewer bağlamına "yeni ad
uydurma" kuralıyla eklenir.
"""

from __future__ import annotations

import re
from pathlib import Path

EXAMPLE_FILES = (".env.example", ".env.dist", ".env.sample", ".env.template", ".env.example.local")
SKIP_DIRS = ("vendor", "node_modules", ".git", "storage", "cache", "tmp")
MAX_NAMES = 80
MAX_USAGES = 200

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]{2,})\s*=")
_USAGE = re.compile(r"(?:getenv|env|\$_ENV|\$_SERVER)\s*[\(\[]\s*['\"]([A-Z][A-Z0-9_]{2,})['\"]")
_CRED_RULE = re.compile(r"\b(?:S6437|S2068|S2115|S6418|secrets:\S*)", re.I)
_CRED_WORDS = re.compile(r"hard-?coded|credential|secret|password|parola|kimlik bilgisi|api[ _-]?key|token", re.I)
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")
_STOP = {"new", "null", "true", "false", "string", "this", "self", "function", "return", "intl", "com", "www",
         "localhost", "getenv", "env"}


def _skip(rel: str) -> bool:
    parts = rel.split("/")
    return any(p in SKIP_DIRS for p in parts[:-1])


def collect(repo_root: Path, *, max_names: int = MAX_NAMES) -> dict:
    """{"example_files": [...], "names": [...], "usages": [{name, file, line}]} — yalnız AD'lar."""
    root = Path(repo_root)
    out = {"example_files": [], "names": [], "usages": []}
    if not root.is_dir():
        return out
    names: dict[str, None] = {}
    for p in sorted(root.rglob(".env*")):
        rel = p.relative_to(root).as_posix()
        if p.name not in EXAMPLE_FILES or _skip(rel):
            continue  # gerçek .env / .env.dev okunmaz: değer sızar, içindeki ad "kural" değildir
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out["example_files"].append(rel)
        for ln in text.splitlines():
            m = _ENV_LINE.match(ln)
            if m:
                names.setdefault(m.group(1))
    for p in sorted(root.rglob("*.php")):
        rel = p.relative_to(root).as_posix()
        if _skip(rel) or len(out["usages"]) >= MAX_USAGES:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "getenv" not in text and "$_ENV" not in text and "env(" not in text and "$_SERVER" not in text:
            continue
        for i, ln in enumerate(text.splitlines(), 1):
            for m in _USAGE.finditer(ln):
                names.setdefault(m.group(1))
                out["usages"].append({"name": m.group(1), "file": rel, "line": i})
                if len(out["usages"]) >= MAX_USAGES:
                    break
    out["names"] = list(names)[:max_names]
    return out


def _tokens(hints: list[str]) -> set[str]:
    toks: set[str] = set()
    for h in hints or []:
        for t in _TOKEN.findall(h or ""):
            t = t.lower()
            if t not in _STOP:
                toks.add(t)
                # camelCase / snake parçaları
                toks.update(x.lower() for x in re.findall(r"[A-Z]?[a-z]+|[A-Z]+", t) if len(x) > 2)
    return toks


def rank(names: list[str], hints: list[str]) -> list[str]:
    """İpucu satırlarındaki sözcüklerle (orkestra, prod, reporting…) örtüşen adlar önce.
    Adların yarısından çoğunda geçen parçalar (DB, APP) ayırt edici değildir, sayılmaz;
    en iyi adın ailesi (son parça hariç aynı önek: DB_ORKESTRA_PROD_*) birlikte yükselir."""
    toks = _tokens(hints)
    from collections import Counter
    freq: Counter = Counter()
    split = {n: [p.lower() for p in n.split("_") if p] for n in names}
    for parts in split.values():
        freq.update(set(parts))
    common = {p for p, c in freq.items() if names and c > len(names) / 2}

    def hits(n: str) -> int:
        return sum(1 for p in split[n] if p not in common and len(p) > 2
                   and (p in toks or any(p in t or t in p for t in toks if len(t) > 3)))
    scored = {n: hits(n) for n in names}
    best = max(scored.values(), default=0)
    fam = {tuple(split[n][:-1]) for n in names if scored[n] == best and best > 0 and len(split[n]) > 1}

    def key(n: str) -> tuple:
        bonus = 0.5 if tuple(split[n][:-1]) in fam else 0.0
        return (-(scored[n] + bonus), n)
    return sorted(names, key=key)


def is_credential_task(title: str, description: str, tags: str = "") -> bool:
    text = f"{title or ''} {description or ''}"
    return bool(_CRED_RULE.search(text) or _CRED_WORDS.search(title or ""))


def context_block(info: dict, hints: list[str], *, repo: str = "", top: int = 8) -> str:
    """Mimar / geliştirici / reviewer bağlamına giren İngilizce kural bloğu (prompt dili)."""
    names = list(info.get("names") or [])
    if not names:
        return ""
    ranked = rank(names, hints)
    usage_of: dict[str, str] = {}
    for u in info.get("usages") or []:
        usage_of.setdefault(u["name"], f"{u['file']}:{u['line']}")
    closest = ranked[:top]
    lines = [f"# Environment Variable Convention — repo `{repo}`" if repo else "# Environment Variable Convention",
             "This repo already reads configuration from the environment. When a change needs a value from the "
             "environment (removing a hard-coded credential, a URL, a host), NEVER invent a new variable name: "
             "reuse the existing name that holds that value. If none of the names below holds it, follow the same "
             "naming pattern as the closest names and ADD the new name (with a placeholder value) to the example file.",
             "Sources: " + ", ".join(f"`{f}`" for f in info.get("example_files") or []) + (
                 f"; `getenv()` / `$_ENV` usages in PHP ({len(info.get('usages') or [])} call sites)."
                 if info.get("usages") else "."),
             "Closest existing names for this task (most relevant first):"]
    for n in closest:
        where = usage_of.get(n)
        lines.append(f"- `{n}`" + (f" — used in {where}" if where else " — declared in the example file only"))
    rest = [n for n in ranked if n not in closest]
    if rest:
        lines.append("All other names: " + ", ".join(f"`{n}`" for n in rest))
    lines.append("Production values are injected by the deployment (Kubernetes secrets), so the NAME must match "
                 "what the deployment already defines — an invented name reads as empty in production.")
    return "\n".join(lines)
