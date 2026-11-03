"""Araçla uygulanan modernleştirme işi (job_kind=modern_fix) — Go ve Python.

Teknik borçtan açılan Go/Python otomatik Task'ları pipeline'a (LLM developer) gitmez:
düzeltmeyi dilin resmi aracı uygular (Go `modernize -fix`, ruff `--fix`, yalnız güvenli
düzeltmeler), PR öncesi doğrulayıcı (verify.py) çalışır, branch + PR açılır ve LLM
yalnız bir kez inceler (pr_review). Kullanıcı kararı (2026-10-30): "araç uygulasın,
LLM yalnız incelesin".

Kapsam (dil, repo, kural × dosya) Teknik borçta iş açılırken işin kaydına yazılır
(`jobs.job_payload`); düzenlenebilir WI açıklaması okunmaz. Araç yalnız o dosyaları
değiştirir, başka bir dosyada çıkan değişiklik geri alınır.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger("agile_sdlc_crew.pipeline")


_REPO = re.compile(r"^[A-Za-z0-9][\w.-]*$")


def load_scope(payload: str, base_dir: Path) -> tuple[str, str, dict[str, list[str]], Path]:
    """İşin kaydındaki onaylı kapsam (take sırasında yazıldı). Repo adı ve her dosya yolu
    doğrulanır: kök dışına çıkan, mutlak ya da `..` içeren yol reddedilir."""
    from agile_sdlc_crew.quality import modern
    try:
        data = json.loads(payload or "")
    except ValueError:
        raise ValueError("İşin kapsam kaydı yok/bozuk — Teknik borç sekmesinden yeniden açın")
    lang, repo, scope = data.get("lang"), str(data.get("repo") or ""), data.get("scope") or {}
    if lang == "php":
        # PHP'de araç kural bazında: yalnız katalogda Rector sınıfı olan otomatik kural
        from agile_sdlc_crew.quality import php_modern
        allowed = php_modern.tool_rules(php_modern.load_rules())
        bad = [r for r in scope if r not in allowed]
        if not allowed or bad:
            raise ValueError(f"Rector bu kural(lar)ı uygulamıyor ya da kurulu değil: {bad or list(scope)}")
    elif lang not in modern.LANGS or not modern.LANGS[lang]["tool_fix"]:
        raise ValueError(f"Araç düzeltmesi bu dil için yok: {lang!r}")
    if not _REPO.match(repo) or ".." in repo:
        raise ValueError(f"Geçersiz repo adı: {repo!r}")
    root = (Path(base_dir) / repo).resolve()
    if not root.is_dir() or not root.is_relative_to(Path(base_dir).resolve()):
        raise ValueError(f"Repo yerelde yok: {repo!r}")
    clean: dict[str, list[str]] = {}
    for rule, files in scope.items():
        if not re.match(r"^[\w.:-]+$", str(rule)):
            raise ValueError(f"Geçersiz kural: {rule!r}")
        for f in files or []:
            f = str(f)
            if f.startswith(("/", "-")) or ".." in Path(f).parts or not (root / f).resolve().is_relative_to(root):
                raise ValueError(f"Kapsam dışı dosya yolu: {f!r}")
            clean.setdefault(rule, []).append(f)
    if not clean:
        raise ValueError("Kapsam boş")
    return lang, repo, clean, root


def _git(root: Path, *args, check: bool = False) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, timeout=120)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def go_apply(root: Path, scope: dict[str, list[str]]) -> list[str]:
    """modernize'ın önerdiği düzeltmeleri YALNIZ kapsamdaki analizör × dosya için uygula.
    `modernize -fix -<analizör>` tek analizörle paket yükleyemiyor ("package without types
    was imported"); tam -fix ise dosyaya Task'ta olmayan kuralları da işler. Bu yüzden -json
    çıktısındaki suggested_fixes düzenlemeleri (bayt aralığı) burada uygulanır, sonra gofmt."""
    import json
    from agile_sdlc_crew.quality import modern
    r = subprocess.run([modern.go_modernize_bin(), "-json", "./..."], cwd=str(root), capture_output=True,
                       text=True, timeout=600, env=modern._go_env(root))
    data = json.loads(r.stdout or "{}")
    want = {(rule, f) for rule, fs in scope.items() for f in fs}
    edits: dict[str, list[tuple[int, int, str]]] = {}
    for analyzers in (data or {}).values():
        if not isinstance(analyzers, dict):
            continue
        for name, diags in analyzers.items():
            if not isinstance(diags, list):
                continue
            for d in diags:
                fixes = d.get("suggested_fixes") or []
                if not fixes:
                    continue
                for e in fixes[0].get("edits") or []:
                    rel = modern._rel(root, e.get("filename", ""))
                    if (name, rel) in want:
                        edits.setdefault(rel, []).append((int(e["start"]), int(e["end"]), str(e.get("new", ""))))
    changed = []
    for rel, es in edits.items():
        path = root / rel
        data_b = path.read_bytes()
        last = len(data_b) + 1
        for start, end, new in sorted(set(es), key=lambda x: (-x[0], -x[1])):
            if end > last:  # çakışan düzenleme — atlanır
                continue
            data_b = data_b[:start] + new.encode("utf-8") + data_b[end:]
            last = start
        path.write_bytes(data_b)
        changed.append(rel)
    if changed:
        subprocess.run(["gofmt", "-w", *changed], cwd=str(root), capture_output=True, timeout=120)
    return changed


def _php_str(s: str) -> str:
    """Tek tırnaklı PHP dizgesi (çift tırnakta `$` enterpolasyonu olurdu)."""
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


def rector_config(root: Path, scope: dict[str, list[str]], version: str, cache_dir: Path) -> str:
    """Yalnız kapsamdaki kural(lar) × dosyalar için Rector yapılandırması. Sınıf adları
    katalogdan (güvenilir), yollar load_scope'tan geçmiş repo-göreli yollar."""
    from agile_sdlc_crew.quality import php_modern
    rules = {r.key: r for r in php_modern.load_rules()}
    classes = sorted({rules[k].rector for k in scope})
    files = sorted({f for fs in scope.values() for f in fs})
    major, _, minor = (version or "8.0").partition(".")
    php_id = int(major) * 10000 + int((minor or "0").split(".")[0] or 0) * 100
    return ("<?php\nuse Rector\\Config\\RectorConfig;\nreturn RectorConfig::configure()\n"
            "    ->withPaths([" + ", ".join(_php_str(str(root / f)) for f in files) + "])\n"
            "    ->withRules([" + ", ".join(_php_str(c) for c in classes) + "])\n"
            f"    ->withPhpVersion({php_id})\n"
            f"    ->withCache({_php_str(str(cache_dir))});\n")


def sensitive_apply(root: Path, files: list[str]) -> None:
    """`#[\\SensitiveParameter]`: Rector'un karşılığı kaldırıldı ("ada göre eşleşme belirsiz"),
    ama eklemenin bedeli düşük — yalnız hata izinde değer gizlenir, davranış değişmez. Tarayıcı
    bulguyu zaten token düzeyinde buluyor; düzeltme kipi aynı yerin önüne özniteliği yazar.
    Tarayıcı bizim dosyamız, kodu ÇALIŞTIRMAZ (token_get_all) — repo autoload'u yüklenmez."""
    from agile_sdlc_crew import verify
    from agile_sdlc_crew.quality import php_modern
    cfg = php_modern.load_cfg()
    r = subprocess.run([cfg.get("php_bin", "php"), "-d", "display_errors=stderr", str(php_modern.SCAN_PHP),
                        "--fix-sensitive", str(root), *files], capture_output=True, text=True, timeout=600,
                       env=verify._env())
    if r.returncode != 0:
        raise RuntimeError("SensitiveParameter düzeltmesi hata verdi: " + ((r.stderr or r.stdout) or "").strip()[-600:])


def php_apply(root: Path, scope: dict[str, list[str]], version: str) -> None:
    """Rector'u repo dışındaki geçici bir yapılandırmayla çalıştır (repo'ya dosya yazılmaz).
    Ortam süzülür (Azure/Sonar anahtarları Rector'a geçmez)."""
    import tempfile
    from agile_sdlc_crew import verify
    from agile_sdlc_crew.quality import php_modern
    rules = {r.key: r for r in php_modern.load_rules()}
    own = {k: v for k, v in scope.items() if php_modern.fixer_fixable(rules.get(k))}
    if own:
        sensitive_apply(root, sorted({f for fs in own.values() for f in fs}))
    # Rector yalnız KURULU sürümde sınıfı olan kurallar için; aracı olmayan satır-yerel kurallar
    # tamamen kalan-yer düzeltmesine (fix_leftovers) kalır.
    known = php_modern.rector_classes()
    scope = {k: v for k, v in scope.items()
             if k not in own and php_modern.rector_fixable(rules.get(k)) and rules[k].rector in known}
    if not scope:
        return
    binp = php_modern.rector_bin()
    if not binp:
        raise RuntimeError("Rector kurulu değil (CREW_RECTOR_BIN / ~/.crew_tools/rector)")
    with tempfile.TemporaryDirectory(prefix="tempo-rector-") as td:
        cfg = Path(td) / "rector.php"
        cfg.write_text(rector_config(root, scope, version, Path(td) / "cache"), encoding="utf-8")
        # cwd repo DIŞINDA: rector'un bin'i çalışma dizinindeki `vendor/autoload.php`'yi
        # include eder (bin/rector.php AutoloadIncluder) — repo kökünde koşsa reponun
        # composer autoload'u (ve `files` girdileri) sunucu kullanıcısıyla ÇALIŞIRDI.
        # Yollar yapılandırmada mutlak; hedef sürüm withPhpVersion'da, composer.json okunmaz.
        r = subprocess.run([binp, "process", "--config", str(cfg), "--no-progress-bar", "--no-diffs",
                            "--memory-limit=2G"], cwd=td, capture_output=True, text=True, timeout=1200,
                           env=verify._env())
        if r.returncode != 0:  # process: 0 = tamam (değişiklik olsa da); ayrıştırma hatası ≠ 0
            raise RuntimeError("Rector hata verdi: " + ((r.stderr or r.stdout) or "").strip()[-600:])


# ── Kalan yerler: araç kaçırdıysa LLM yalnız o satırların penceresini düzeltir ──────────
# #264 (`isset ? :` → `??`): Rector zincirleme atamayı (`$a = $b['x'] = isset($b['x']) ? … : null`)
# atladı, inceleme CHANGES_REQUIRED dedi, satır elle düzeltilmeliydi. Kullanıcı kararı
# (2026-11-03): "manuel müdahaleye gerek kalmasın". Araçtan SONRA tarayıcı kapsam dosyalarını
# yeniden tarar; kuralın kalan her yeri ±LEFTOVER_RADIUS satırlık pencerede düzeltilir. Yanıt
# pencereye benzemiyorsa / dosya php -l'den geçmiyorsa o dosya araç çıktısına geri döner.
LEFTOVER_RADIUS = 6
REVIEW_FIX_RADIUS = 8


def _dev_llm():
    """Pencere düzeltmesinin LLM'i: senior_developer persona'sı, iş kapsamlı profil
    (CREW_MODERN_FIX_DEV_PROFILE, varsayılan developer_cli = Sonnet). Çağıran job_llm_scope içinde."""
    from agile_sdlc_crew.llm import resolver
    return resolver.build_for_agent("senior_developer")


def _job_models(pc) -> dict:
    """modern_fix işinin iş kapsamlı modelleri: kodu araç yazar, LLM yalnız dar pencere düzeltir
    ve inceler — ikisi de Sonnet yeter (kullanıcı kararı 2026-11-03: "düşük modeller ile işlet")."""
    out = {}
    for agent, knob in (("code_reviewer", "CREW_MODERN_FIX_REVIEW_PROFILE"),
                        ("senior_developer", "CREW_MODERN_FIX_DEV_PROFILE")):
        prof = str(pc.get(knob) or "").strip()
        if prof:
            out[agent] = prof
    return out


def _merge_windows(lines: list[int], n: int, radius: int) -> list[tuple[int, int, list[int]]]:
    out: list[tuple[int, int, list[int]]] = []
    for ln in sorted(set(x for x in lines if 1 <= x <= n)):
        a, b = max(1, ln - radius), min(n, ln + radius)
        if out and a <= out[-1][1] + 1:
            pa, pb, pt = out[-1]
            out[-1] = (pa, max(pb, b), pt + [ln])
        else:
            out.append((a, b, [ln]))
    return out


def _php_lint_ok(path: Path) -> bool:
    from agile_sdlc_crew import verify
    from agile_sdlc_crew.quality import php_modern
    r = subprocess.run([php_modern.load_cfg().get("php_bin", "php"), "-l", str(path)], capture_output=True,
                       text=True, timeout=60, env=verify._env())
    return r.returncode == 0


# Bir çağrıya giren en çok pencere. #266: pencere başına bir claude -p çağrısı ($0.19, çoğu sabit
# önek) — 4 dosyada 7 çağrı $1.35 tuttu. Hepsi tek istemde, yanıtta pencere sırasıyla kod blokları.
BATCH_MAX = 20


def batch_window_fix(root: Path, items: list[tuple[str, list[int], str]], *, radius: int, ask=None) -> int:
    """items: (dosya, satırlar, talimat). Tüm pencereler TEK (≤BATCH_MAX'lık) istemde düzelttirilir;
    ask(prompt) -> yanıt (testte enjekte). Yanıttaki blok sayısı pencere sayısını tutmazsa o parti
    atılır. Düzyazı/kısa/çok farklı pencere uygulanmaz; php -l'den geçmeyen dosya eski hâline döner.
    Döner: uygulanan pencere sayısı."""
    import difflib
    from agile_sdlc_crew.edit_window import _FENCE, parse_reply
    by_file: dict[str, tuple[list[int], list[str]]] = {}
    for rel, lines, instr in items:
        lns, ins = by_file.setdefault(rel, ([], []))
        lns.extend(lines)
        if instr not in ins:
            ins.append(instr)
    rules: list[str] = []
    files: dict[str, tuple[str, list[str]]] = {}
    wins: list[tuple[str, int, int, list[int], list[int]]] = []  # (dosya, a, b, hedefler, kural no'ları)
    for rel, (lns, ins) in by_file.items():
        content = (root / rel).read_text(encoding="utf-8")
        files[rel] = (content, content.splitlines())
        nums = []
        for i in ins:
            if i not in rules:
                rules.append(i)
            nums.append(rules.index(i) + 1)
        for a, b, t in _merge_windows(lns, len(files[rel][1]), radius):
            wins.append((rel, a, b, t, nums))
    if not wins:
        return 0
    if ask is None:
        llm = _dev_llm()

        def ask(prompt: str) -> str:
            return str(llm.call([{"role": "user", "content": prompt}]) or "")
    replies: dict[int, list[str] | None] = {}
    for start in range(0, len(wins), BATCH_MAX):
        chunk = list(range(start, min(start + BATCH_MAX, len(wins))))
        parts = ["You fix several small windows of PHP files. RULES:"]
        parts += [f"R{n}. {r}" for n, r in enumerate(rules, 1)]
        parts.append(
            "Each window below shows numbered lines ('NNNNN| ' is NOT part of the file). In each window CHANGE "
            "ONLY its target lines, applying the rule(s) named for it; every other line stays byte-identical. "
            "If a target line cannot be changed safely, leave it as it is.\n"
            f"Return EXACTLY {len(chunk)} ```php code blocks, one per window, in the same order — each the "
            "COMPLETE new version of that window (unchanged lines included), WITHOUT the 'NNNNN| ' prefixes. "
            "No prose.")
        for k, wi in enumerate(chunk, 1):
            rel, a, b, t, nums = wins[wi]
            src = files[rel][1]
            body = "\n".join(f"{i:>5}| {src[i - 1]}" for i in range(a, b + 1))
            parts.append(f"\n### WINDOW {k} — {rel}, lines {a}-{b}; targets: {', '.join(map(str, t))}; "
                         f"rule: {', '.join(f'R{n}' for n in nums)}\n```\n{body}\n```")
        blocks = _FENCE.findall(ask("\n".join(parts)) or "")
        if len(blocks) != len(chunk):
            log.info(f"  🧰 Pencere yanıtı {len(blocks)} blok, beklenen {len(chunk)} — parti atlandı")
            continue
        for wi, body in zip(chunk, blocks):
            replies[wi] = parse_reply("```php\n" + body + "```")
    applied = 0
    for rel, (content, src) in files.items():
        src = list(src)
        n = 0
        for wi in sorted((w for w in replies if wins[w][0] == rel), key=lambda w: -wins[w][1]):
            _rel, a, b, _t, _n = wins[wi]
            new, old = replies[wi], src[a - 1:b]
            if not new or new == old or len(new) < len(old) * 0.6:
                continue
            if difflib.SequenceMatcher(None, "\n".join(old), "\n".join(new), autojunk=False).ratio() < 0.5:
                continue
            src[a - 1:b] = new
            n += 1
        if not n:
            continue
        path = root / rel
        path.write_text("\n".join(src) + ("\n" if content.endswith("\n") else ""), encoding="utf-8")
        if rel.endswith(".php") and not _php_lint_ok(path):
            path.write_text(content, encoding="utf-8")
            log.info(f"  🧰 {rel}: pencere düzeltmesi php -l'den geçmedi — geri alındı")
            continue
        applied += n
    return applied


def window_fix(root: Path, rel: str, lines: list[int], instruction: str, *, radius: int, ask=None) -> int:
    """Tek dosya için batch_window_fix."""
    return batch_window_fix(root, [(rel, lines, instruction)], radius=radius, ask=ask)


def _rule_instruction(r, version: str) -> str:
    return (f"Apply ONE mechanical PHP {version} modernization rule. Rule: {r.title}.\n"
            f"Before:\n{r.before}\nAfter:\n{r.after}\n"
            "Keep behaviour identical. Do not reformat, rename or touch anything else.")


def fix_leftovers(root: Path, scope: dict[str, list[str]], version: str, *, ask=None) -> dict:
    """Araçtan sonra kapsam dosyalarını yeniden tara; satır-yerel kuralların kalan yerlerini pencereyle
    düzelt. {"fixed": pencere, "remaining": [(dosya, satır, kural)], "skipped": [...satır-yerel olmayan]}"""
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew.quality import php_modern
    rules = {r.key: r for r in php_modern.load_rules()}
    files = sorted({f for fs in scope.values() for f in fs})
    php_bin = php_modern.load_cfg().get("php_bin", "php")

    def left() -> list[dict]:
        return [h for h in php_modern.scan_files(root, files, php_bin=php_bin)
                if h.get("rule") in scope and h.get("file") in scope[h["rule"]]]
    hits = left()
    local = [h for h in hits if php_modern.llm_line_fixable(rules.get(h["rule"]))]
    skipped = [(h["file"], h.get("line"), h["rule"]) for h in hits if h not in local]
    fixed = 0
    if local and pc.get("CREW_MODERN_FIX_LLM_LEFTOVERS"):
        by: dict[tuple[str, str], list[int]] = {}
        for h in local:
            by.setdefault((h["file"], h["rule"]), []).append(int(h.get("line") or 0))
        fixed = batch_window_fix(root, [(rel, lns, _rule_instruction(rules[rule], version))
                                        for (rel, rule), lns in by.items()], radius=LEFTOVER_RADIUS, ask=ask)
        log.info(f"  🧰 Araç {len(local)} yeri kaçırdı → {fixed} pencere LLM'le düzeltildi")
        hits = left()
    remaining = [(h["file"], h.get("line"), h["rule"]) for h in hits
                 if php_modern.llm_line_fixable(rules.get(h["rule"]))]
    return {"fixed": fixed, "remaining": remaining, "skipped": skipped}


def fix_review_issues(root: Path, issues: list[dict], files: set[str], *, ask=None) -> int:
    """İncelemenin bloklayan (blocker/major) maddelerini, maddenin satırı çevresinde düzelt."""
    by: dict[str, list[dict]] = {}
    for it in issues:
        rel = str(it.get("file") or "").lstrip("/")
        if rel in files and int(it.get("line") or 0) > 0:
            by.setdefault(rel, []).append(it)
    items = []
    for rel, its in by.items():
        instr = (f"In {rel}, fix these code-review findings. Keep behaviour identical apart from the fix; do not "
                 "touch unrelated code.\n" + "\n".join(
                     f"- line {it.get('line')}: {it.get('problem') or ''} FIX: {it.get('required_fix') or ''}"
                     for it in its))
        items.append((rel, [int(it["line"]) for it in its], instr))
    return batch_window_fix(root, items, radius=REVIEW_FIX_RADIUS, ask=ask) if items else 0


def _changed_in_scope(root: Path, files: list[str]) -> list[str]:
    changed = [ln for ln in _git(root, "diff", "--name-only").stdout.splitlines() if ln]
    stray = [f for f in changed if f not in files]
    if stray:
        _git(root, "checkout", "--", *stray)
    return [f for f in changed if f in files]


def apply_tool(root: Path, lang: str, scope: dict[str, list[str]], version: str) -> list[str]:
    """Aracı yalnız kapsamdaki kurallar/dosyalar için çalıştır; değişen kapsam dosyaları."""
    from agile_sdlc_crew.quality import modern
    files = sorted({f for fs in scope.values() for f in fs})
    if lang == "go":
        go_apply(root, scope)
    elif lang == "php":
        php_apply(root, scope, version)
    elif lang == "python":
        codes = sorted({r.split(":")[0] for r in scope if not r.endswith(":manual")})
        tv = "py" + version.replace(".", "")
        subprocess.run([*modern._ruff_cmd(), "check", "--select", ",".join(codes), "--fix", "--target-version", tv,
                        "--no-cache", "--exit-zero", *["./" + f for f in files]],
                       cwd=str(root), capture_output=True, text=True, timeout=600)
    else:
        raise ValueError(f"{lang} için araç düzeltmesi yok")
    changed = [ln for ln in _git(root, "diff", "--name-only").stdout.splitlines() if ln]
    stray = [f for f in changed if f not in files]
    if stray:
        _git(root, "checkout", "--", *stray)  # kapsam dışına taşan değişiklik alınmaz
    return [f for f in changed if f in files]


def _detached(root: Path):
    """Klonu origin/main'de ayır; çıkışta değişiklikleri at ve önceki dala dön (paylaşılan klon)."""
    import contextlib

    @contextlib.contextmanager
    def _cm():
        prev = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
        _git(root, "checkout", "-q", "--detach", "origin/main", check=True)
        try:
            yield
        finally:
            _git(root, "checkout", "--", ".")
            _git(root, "checkout", "-q", prev)
    return _cm()


def _tool_branch(client, repo: str, work_item_id: str) -> tuple[str, bool]:
    """(dal, mevcut araç PR'ı yeniden kullanılıyor mu). Aynı WI'ın ÖNCEKİ araç düzeltmesinin
    PR'ı hâlâ açıksa onun dalına itilir (PR güncellenir, yeni PR/terk gerekmez). Başka bir
    koşunun (ör. LLM pipeline'ı) dalı varsa main'den taze dal açılır. Varlık AÇIKÇA sorulur:
    Azure refs API var olan dal için 200 + success=false döner, hata fırlatmaz (#45721)."""
    from agile_sdlc_crew import db, pipeline
    base = f"feature/{work_item_id}"
    heads = {b.get("name") for b in client.list_branches(repo)}
    mine = set()
    with db.get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT branch_name, pr_id FROM jobs WHERE work_item_id=%s AND job_kind='modern_fix' "
                    "AND COALESCE(pr_id,'')<>''", (str(work_item_id),))
        mine = {(r["branch_name"], str(r["pr_id"])) for r in cur.fetchall() or []}
    for br, pid in sorted(mine, reverse=True):
        if f"refs/heads/{br}" in heads:
            pr = pipeline._find_existing_pr(client, repo, br)
            if pr and str(pr.get("pr_id")) == pid:
                log.info(f"  Önceki araç düzeltmesinin PR #{pid} açık — dalına ({br}) itiliyor")
                return br, True
    if f"refs/heads/{base}" in heads:
        import time
        br = f"{base}-tool-{int(time.time()) % 100000}"
        client.create_branch(repo, br)
        log.info(f"  {base} zaten var (önceki koşu) — taze dal: {br}")
        return br, False
    res = pipeline.create_branch(repo, str(work_item_id))
    if not res.get("success"):
        raise RuntimeError(f"Branch açılamadı: {res.get('error')}")
    return res["branch"], False


def _blocking(issues: list[dict]) -> list[dict]:
    from agile_sdlc_crew.pr_review import INLINE_SEVERITIES
    return [i for i in issues or [] if i.get("severity") in INLINE_SEVERITIES]


def run(job_id: int, work_item_id: str) -> dict:
    from agile_sdlc_crew import db, pipeline, verify
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew import wi_lifecycle as wl
    from agile_sdlc_crew.llm import resolver
    from agile_sdlc_crew.main import _add_wi_comment
    from agile_sdlc_crew.quality import modern
    from agile_sdlc_crew.tools import claude_cli_llm as _acct
    from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient
    from agile_sdlc_crew.tools.local_repo import LocalRepoManager

    client = AzureDevOpsClient()
    job = db.get_job(job_id) or {}
    before = job.get("wi_state_before") or ""
    lifecycle = bool(pc.get("CREW_WI_LIFECYCLE"))
    sig = "\n\n*Tempo — araç düzeltmesi*"
    try:
        fields = client.get_work_item(int(work_item_id)).get("fields") or {}
        title = fields.get("System.Title") or f"WI {work_item_id}"
        mgr = LocalRepoManager()
        # Kapsam işin kaydından (take'te onaylanan); WI açıklaması yalnız görüntü — düzenlenebilir
        lang, repo, scope, root = load_scope(job.get("job_payload") or "", mgr.base_dir)
        tv = modern.target(client, repo, root, lang, index=modern.build_index(client))
        version = tv["version"] or ""
        files_all = sorted({f for fs in scope.values() for f in fs})
        log.info(f"\n-- ARAÇ DÜZELTMESİ #{job_id}: WI #{work_item_id} · {repo} ({lang} {version}) · "
                 f"{', '.join(scope)} --")
        if _git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip():
            raise RuntimeError(f"{repo} klonunda commit edilmemiş değişiklik var — araç çalıştırılmadı")
        mgr.set_remote_auth(repo)
        _git(root, "fetch", "-q", "origin", "main", check=True)
        # LLM yalnız dar pencere (kalan yer / inceleme maddesi) ve inceleme için — iş kapsamlı model
        with resolver.job_llm_scope():
            resolver.set_job_llm_overrides(_job_models(pc))
            if job_id:
                _acct.set_call_context(job_id, "implement_change_task", "senior_developer")
                if getattr(_acct, "_call_sink", None) is None:
                    _acct.register_call_sink(db.record_llm_call)
            _acct.set_toolless(True)
            try:
                return _run_scoped(client, db, pipeline, verify, pc, wl, modern, _add_wi_comment, _acct,
                                   job_id, work_item_id, title, lang, repo, scope, root, version, files_all,
                                   lifecycle, sig)
            finally:
                _acct.set_toolless(False)
    except Exception as e:
        from agile_sdlc_crew.flow import NeedsHumanReview
        if lifecycle and before:
            try:
                wl.revert_to(client, str(work_item_id), before, logger=log.info)
            except Exception as e2:  # noqa: BLE001
                log.warning(f"  WI geri alınamadı: {e2}")
        if isinstance(e, NeedsHumanReview):
            db.needs_human_job(job_id, str(e))
        raise


def _run_scoped(client, db, pipeline, verify, pc, wl, modern, _add_wi_comment, _acct,
                job_id, work_item_id, title, lang, repo, scope, root, version, files_all, lifecycle, sig) -> dict:
    from agile_sdlc_crew.flow import NeedsHumanReview
    from agile_sdlc_crew.pr_review import run_pr_review
    left = {"fixed": 0, "remaining": [], "skipped": []}
    with _detached(root):
        apply_tool(root, lang, scope, version)
        if lang == "php":
            left = fix_leftovers(root, scope, version)
        changed = _changed_in_scope(root, files_all)
        contents = {f: (root / f).read_text(encoding="utf-8") for f in changed}
        # İnceleme TÜM dosyaların diff'ini görsün (pr_review.tool_diff_context) — eski hâl origin/main
        originals = {f: _git(root, "show", f"origin/main:{f}").stdout for f in changed}
        res = verify.verify(root, changed) if changed else {"ok": True, "ran": [], "issues": [], "skipped": []}
    if not changed:
        _add_wi_comment(client, work_item_id, "## 🧰 Araç düzeltmesi\n\nAraç bu kapsamda değişiklik üretmedi — "
                        "bulgular `main`'de zaten giderilmiş olabilir. PR açılmadı." + sig)
        return {"changed": 0}
    if not res["ok"]:
        raise RuntimeError("Doğrulayıcı geçmedi: " + "; ".join(res["issues"][:5]))
    from agile_sdlc_crew.quality import php_modern as _pm
    _own = lang == "php" and all(_pm.fixer_fixable(r) for r in
                                 [{x.key: x for x in _pm.load_rules()}.get(k) for k in scope])
    tool = {"go": "modernize -fix", "php": "tempo-fixer" if _own else "rector"}.get(lang, "ruff --fix")
    if left["fixed"]:
        tool += " + LLM pencere"
    branch, reused = _tool_branch(client, repo, work_item_id)
    client.push_changes(repo, branch, [{"changeType": "edit", "path": "/" + f, "content": c}
                                       for f, c in contents.items()], f"#{work_item_id}: {title[:80]} ({tool})")
    notes = _sites_note(left) if lang == "php" else ""  # yeniden tarama yalnız PHP'de
    desc = (f"Araçla uygulanan modernleştirme ({tool}, hedef {modern.LANGS[lang]['label']} {version}).\n\n"
            f"Kurallar: {', '.join(scope)}\n\nDeğişen dosyalar ({len(changed)}):\n"
            + "\n".join(f"- `{f}`" for f in changed)
            + f"\n\nDoğrulama: {', '.join(res['ran']) or 'doğrulayıcı yok'} geçti." + notes + sig)
    pr = pipeline.create_pull_request(repo, branch, str(work_item_id), f"#{work_item_id} - {title}", desc)
    if not pr.get("success"):
        raise RuntimeError(f"PR açılamadı: {pr.get('error')}")
    db.update_job(job_id, repo_name=repo, branch_name=branch, pr_id=str(pr["pr_id"]), pr_url=pr["url"])
    _add_wi_comment(client, work_item_id,
                    f"## 🧰 Araç düzeltmesi\n\nPR: [#{pr['pr_id']}]({pr['url']}) — {tool}, {len(changed)} dosya, "
                    f"doğrulama ({', '.join(res['ran']) or '-'}) geçti"
                    + (" (önceki araç PR'ı güncellendi)" if reused else "") + ". LLM incelemesi PR'a yazılıyor."
                    + f"\n\nDeğişen dosyalar ({len(changed)}):\n" + "\n".join(f"- `{f}`" for f in changed)
                    + notes + sig)

    def review(cur: dict[str, str]) -> dict:
        return run_pr_review(work_item_id=str(work_item_id), pr_id=int(pr["pr_id"]), repo_name=repo, job_id=job_id,
                             changes={f: (originals.get(f, ""), c) for f, c in cur.items()}, tool=tool,
                             extra_context=notes)
    rv = review(contents)
    rounds = int(pc.get("CREW_MODERN_FIX_REVIEW_ROUNDS") or 0)
    while _blocking(rv.get("issue_list")) and rounds > 0:
        rounds -= 1
        # Aracın bilerek bıraktığı (satır-yerel olmayan) yerler düzeltme hedefi DEĞİL — LLM switch→match
        # ya da promotion'ı pencerede yapmasın; inceleme yine de işaretlerse o madde insana kalır.
        keep = {(f, int(ln or 0)) for f, ln, _r in left["skipped"]}
        blocking = [i for i in _blocking(rv.get("issue_list"))
                    if (str(i.get("file") or "").lstrip("/"), int(i.get("line") or 0)) not in keep]
        if not blocking:
            break
        log.info(f"  🧰 İnceleme {len(blocking)} bloklayan madde buldu — pencereyle düzeltiliyor")
        if job_id:
            _acct.set_call_context(job_id, "implement_change_task", "senior_developer")
        with _detached(root):
            for f, c in contents.items():
                (root / f).write_text(c, encoding="utf-8")
            n = fix_review_issues(root, blocking, set(files_all))
            if lang == "php":
                more = fix_leftovers(root, scope, version)
                left = {**more, "fixed": left["fixed"] + more["fixed"]}
            new = {f: (root / f).read_text(encoding="utf-8") for f in _changed_in_scope(root, files_all)}
            fix_res = verify.verify(root, list(new)) if new else {"ok": True}
        upd = {f: c for f, c in new.items() if contents.get(f) != c}
        if not n or not upd or not fix_res.get("ok"):
            log.info("  🧰 İnceleme düzeltmesi ilerleme sağlamadı — insan incelemesine")
            break
        originals.update({f: _git(root, "show", f"origin/main:{f}").stdout for f in upd if f not in originals})
        contents.update(upd)
        client.push_changes(repo, branch, [{"changeType": "edit", "path": "/" + f, "content": c}
                                           for f, c in upd.items()],
                            f"#{work_item_id}: inceleme düzeltmesi ({len(upd)} dosya)")
        rv = review(contents)
    still = _blocking(rv.get("issue_list"))
    if still:
        raise NeedsHumanReview(f"Araç düzeltmesi PR #{pr['pr_id']}: incelemede {len(still)} bloklayan madde kaldı "
                               f"(otomatik düzeltme turları bitti) — PR açık")
    if lifecycle:
        info = wl.load_wi_context(client, str(work_item_id))
        target = wl.plan_transition(info["wi_state"], info["wi_states"], "review")
        if target:
            wl.apply_state(client, str(work_item_id), target, logger=log.info)
    return {"changed": len(changed), "pr_id": pr["pr_id"], "leftover_fixed": left["fixed"],
            "remaining": len(left["remaining"]), "verdict": rv.get("verdict")}


def _sites_note(left: dict) -> str:
    """PR/WI ve incelemeye: düzeltme SONRASI taramanın sonucu (yetkili liste), aracın bilerek
    bıraktığı (satır-yerel olmayan) yerler ve kalanlar.

    #264: inceleme Task'taki 'Bulunan yerler'e bakıp `ReturnAccepts.php:41` eksik dedi — ama o
    satır main'de Temmuz'da zaten `?? null` olmuştu; Task'ın kanıtı açıldığı andaki klona göreydi.
    Güncel main üzerindeki tarama sonucu yazılmazsa inceleme bayat kanıtla CHANGES_REQUIRED der."""
    out = ["\n\nDÜZELTME SONRASI TARAMA (güncel `main` + bu PR, kapsam dosyaları): bu kuralın kalan yeri "
           f"{len(left.get('remaining') or []) + len(left.get('skipped') or [])}. Task'taki 'Bulunan yerler' listesi "
           "Task açıldığı andaki koda göredir; listede olup PR'da değişmeyen bir yer güncel kodda artık yoksa "
           "(başka bir commit'le giderilmişse) EKSİK SAYILMAZ — yetkili liste bu taramadır."]
    if left.get("skipped"):
        out.append("\n\nAraç şu yerleri güvenli bulmadığı için DÖNÜŞTÜRMEDİ (kural blok/sınıf düzeyinde; "
                   "bilerek bırakıldı, eksik sayılmaz):\n"
                   + "\n".join(f"- `{f}:{ln}` ({r})" for f, ln, r in left["skipped"][:40]))
    if left.get("remaining"):
        out.append("\n\nDüzeltilemeyen yerler (pencere düzeltmesi tutmadı):\n"
                   + "\n".join(f"- `{f}:{ln}` ({r})" for f, ln, r in left["remaining"][:40]))
    return "".join(out)
