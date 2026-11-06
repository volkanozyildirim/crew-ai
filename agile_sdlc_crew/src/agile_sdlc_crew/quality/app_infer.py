"""FLODevOps'ta tanımı bulunmayan New Relic uygulamasının repoyu hatalarının kendisinden bulmak.

New Relic hatası iki iz taşır:
  - işlem adı → `action` ("Api/V1/Basket::retrieveCardLoyalty"): dosya son eki + metot
  - mesajdaki yol → "/var/www/app/app/Controller/Api/V1/Order.php:1625"
Her iz, yerel klonlarda (`CREW_REPOS_DIR`) aranır: ilki `git grep -F "function <metot>" -- "*<son ek>.php"`,
ikincisi dosyanın varlığı. Bir repo, en az `MIN_EVIDENCE` ayrı izle ve ikinci adaydan en az `DOMINANCE`
kat fazla izle öne çıkarsa uygulama o repoya eşlenir; aksi hâlde (belirsiz) eşlenmeden kalır — yanlış
repoya Task açmak, hiç açmamaktan pahalı.

Neden (2026-11-06): `[Prod] checkout-be.<marka>` uygulamaları (~7.3K hata) FLODevOps build
dizinlerinde yoktu; ekip "checkout-be'ler webservice" dedi, ama kullanıcı haklı olarak her uygulamayı
tek tek elle eşlemek istemiyor. Kanıt kodun kendisinde: `api::v1::basket::retrieveCardLoyalty`
webservice'in `app/Controller/Api/V1/Basket.php`'sinde.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path

import yaml

log = logging.getLogger("pipeline")

MIN_EVIDENCE = 2
DOMINANCE = 2.0
MAX_CLUES = 12            # uygulama başına denenen en çok iz (en sık görülenler)
CACHE_TTL_H = 24
_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
CACHE_PATH = _CONFIG_DIR / "quality_app_map_inferred.yaml"

_PATH_IN_MSG = re.compile(r"/var/www/[^/\s]+/((?:[\w.-]+/)*[\w.-]+\.(?:php|py|go|js|ts))(?::\d+| on line \d+)?")
_ACTION = re.compile(r"^([A-Za-z][\w/]*)::(\w+)$")
_METHOD = re.compile(r"^[A-Za-z_]\w*$")


def clues(findings) -> list[tuple[str, str]]:
    """Bulgulardan iz listesi, sıklığa göre: ("method", "<son ek>.php|<metot>") ya da ("path", "<yol>")."""
    weight: dict[tuple[str, str], int] = {}
    for f in findings:
        m = _ACTION.match(f.action or "")
        if m and _METHOD.match(m.group(2)):
            key = ("method", f"{m.group(1)}.php|{m.group(2)}")
            weight[key] = weight.get(key, 0) + int(f.count or 1)
        for p in _PATH_IN_MSG.findall(f.message or ""):
            weight[("path", p)] = weight.get(("path", p), 0) + int(f.count or 1)
    return [k for k, _ in sorted(weight.items(), key=lambda kv: -kv[1])][:MAX_CLUES]


def code_clues(app: str, days: int = 7, nrql=None) -> list[tuple[str, str]]:
    """NR ajanının yazdığı kod konumu (Go/Python/Node ajanları doldurur, PHP E_WARNING boş bırakır):
    `code.filepath` → dosya yolu izi, `code.namespace`'in ilk parçası → Go modül adı izi
    ("loyalty-360/internal/api/…" → go.mod `module loyalty-360`)."""
    if nrql is None:
        from agile_sdlc_crew.quality.newrelic import nrql
    safe = app.replace("'", "\\'")
    rows = nrql(f"SELECT uniques(code.filepath, 10) AS f, uniques(code.namespace, 10) AS n FROM TransactionError "
                f"WHERE appName = '{safe}' SINCE {int(days)} days ago") or [{}]
    out: list[tuple[str, str]] = []
    for fp in rows[0].get("f") or []:
        out += [("path", m) for m in _PATH_IN_MSG.findall(fp or "")]
    for ns in rows[0].get("n") or []:
        mod = (ns or "").split("/")[0]
        if "/" in (ns or "") and re.fullmatch(r"[\w.-]+", mod):
            out.append(("module", mod))
    return list(dict.fromkeys(out))


def _git(repo: Path, *args) -> str:
    try:
        r = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=str(repo),
                           capture_output=True, text=True, timeout=30)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _hit(repo: Path, clue: tuple[str, str]) -> bool:
    kind, val = clue
    if kind == "module":
        try:
            first = (repo / "go.mod").read_text(encoding="utf-8").splitlines()[0].split()
        except (OSError, IndexError):
            return False
        return len(first) == 2 and first[0] == "module" and (first[1] == val or first[1].endswith("/" + val))
    if kind == "method":
        suffix, method = val.split("|", 1)
        return bool(_git(repo, "grep", "-l", "-F", "-e", f"function {method}", "--", f"*{suffix}").strip())
    # Konteyner kökü repo kökü olmayabilir (/var/www/app/app/... → app/...): son ek eşleşmesi yeter
    rel = val.lstrip("/")
    parts = rel.split("/")
    return any(bool(_git(repo, "ls-files", "--", "/".join(parts[i:])).strip())
               for i in range(0, max(1, len(parts) - 2)))


def infer(app_clues: list[tuple[str, str]], repos: list[Path], min_evidence: int = MIN_EVIDENCE) -> dict | None:
    """Kanıtla en güçlü repo ya da None. Dönüş: {repo, score, runner_up, evidence}."""
    if not app_clues:
        return None
    scores: dict[str, list[str]] = {}
    for repo in repos:
        hits = [v for c in app_clues if _hit(repo, c) for v in [c[1]]]
        if hits:
            scores[repo.name] = hits
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: -len(kv[1]))
    best, hits = ranked[0]
    second = len(ranked[1][1]) if len(ranked) > 1 else 0
    if len(hits) < min_evidence or len(hits) < DOMINANCE * second:
        return None
    return {"repo": best, "score": len(hits), "runner_up": second, "evidence": hits[:5]}


def _repos(base: Path) -> list[Path]:
    return sorted(p for p in base.iterdir() if (p / ".git").exists()) if base.is_dir() else []


def _family(app: str) -> str:
    """'[Prod] checkout-be.butigo.com.tr' → 'checkout-be' (markaya göre çoğaltılmış servis ailesi)."""
    name = re.sub(r"^\[[^\]]*\]\s*", "", app or "")
    return name.split(".", 1)[0] if "." in name else ""


def resolve_unmapped(by_app: dict[str, list], *, base: Path | None = None, cache_path: Path = CACHE_PATH,
                     now=time.time, code=code_clues) -> dict[str, str]:
    """{uygulama: repo}. Sonuç (belirsiz olanlar dahil) CACHE_TTL_H saat önbelleklenir —
    67 repoda git grep her gather'da (~2 dk'da bir) koşmasın."""
    base = base or Path(os.environ.get("CREW_REPOS_DIR", "~/.crew_repos")).expanduser()
    try:
        cache = yaml.safe_load(cache_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        cache = {}
    out, dirty, repos = {}, False, None
    # Çok hatalı (çok kanıtlı) uygulama önce: kardeş kuralı onların sonucuna dayanır
    for app, findings in sorted(by_app.items(), key=lambda kv: -sum(int(f.count or 1) for f in kv[1])):
        hit = cache.get(app)
        if hit and now() - float(hit.get("at") or 0) < CACHE_TTL_H * 3600:
            if hit.get("repo"):
                out[app] = hit["repo"]
            continue
        repos = repos if repos is not None else _repos(base)
        cl = clues(findings)
        try:
            cl += [c for c in code(app) if c not in cl]
        except Exception as e:  # noqa: BLE001
            log.info(f"  NR kod konumu okunamadı ({app}): {e}")
        res = infer(cl, repos)
        if not res and _family(app):
            # Az hatalı kardeş (checkout-be.butigo: 5 hata, tek iz): aynı ailenin eşlenmiş üyeleri
            # tek repoda birleşiyorsa ve bu uygulamanın izi de o repoyu gösteriyorsa yeter.
            sib = {v.get("repo") for k, v in cache.items() if k != app and v.get("repo")
                   and _family(k) == _family(app) and now() - float(v.get("at") or 0) < CACHE_TTL_H * 3600}
            one = infer(cl, repos, min_evidence=1)
            if one and sib == {one["repo"]}:
                res = {**one, "sibling": True}
        cache[app] = {"repo": (res or {}).get("repo", ""), "at": now(),
                      "evidence": (res or {}).get("evidence", []), "score": (res or {}).get("score", 0),
                      "runner_up": (res or {}).get("runner_up", 0)}
        dirty = True
        if res:
            out[app] = res["repo"]
            log.info(f"  🔎 NR '{app}' → {res['repo']} (kanıt {res['score']}, ikinci aday {res['runner_up']})")
    if dirty:
        try:
            cache_path.write_text(yaml.safe_dump(cache, allow_unicode=True, sort_keys=True), encoding="utf-8")
        except OSError as e:
            log.info(f"  NR çıkarım önbelleği yazılamadı: {e}")
    return out
