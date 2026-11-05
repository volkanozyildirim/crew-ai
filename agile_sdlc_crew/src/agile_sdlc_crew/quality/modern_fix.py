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


_PROP_DEFAULT = re.compile(r"^\s*(?:public|protected|private)(?:\s+readonly)?(?:\s+(?P<type>[?\w\\|]+))?"
                           r"\s+\$(?P<name>\w+)\s*=\s*(?P<val>[^;]+);", re.M)


def lost_property_defaults(old: str, new: str) -> list[str]:
    """Bildirimi varsayılan değer taşıyan ve dönüşümde KAYBOLAN property'ler (promotion).
    #45729 / PR testleri: `public array $config = [];` + `__construct($config)` →
    `__construct(public array $config)`. Varsayılan gitti; parent::__construct'ı çağırmayan alt
    sınıf (Hepsijet) ya da constructor'sız mock'ta property artık başlatılmamış → "must not be
    accessed before initialization". Tipsiz `= null` güvenli (tipsiz property zaten null başlar)."""
    kept = {m.group("name") for m in _PROP_DEFAULT.finditer(new)}
    lost = []
    for m in _PROP_DEFAULT.finditer(old):
        if m.group("name") in kept:
            continue
        if not m.group("type") and m.group("val").strip().lower() == "null":
            continue
        lost.append(m.group("name"))
    return lost


def build_culprits(summary: str, files) -> list[str]:
    """Kırmızı build özetinde (test hata yığınları) adı geçen, bu PR'ın değiştirdiği dosyalar."""
    return sorted(f for f in files if f and f in (summary or ""))


def wait_pr_build(client, repo: str, pr_id: int, since: str, *, timeout_s: int, interval_s: int = 30,
                  grace_s: int = 180, sleep=None) -> tuple[str, dict | None]:
    """PR'ın `since`'ten (UTC ISO) SONRA kuyruğa alınan build'ini bekle — önceki push'un build'i
    sayılmaz (Azure yeni build'i birkaç saniye geç kuyruğa alır; #189'da bayat sonuç okunmuştu).
    Dönüş: ("completed", build) | ("no_pipeline", None) | ("timeout", build|None)."""
    import time as _t
    sleep = sleep or _t.sleep
    waited, last = 0, None
    while waited < timeout_s:
        try:
            b = client.get_pr_build(repo, int(pr_id))
        except Exception as e:  # noqa: BLE001
            log.info(f"  Build sorgu hatası: {e}")
            b = None
        if b is None and waited >= grace_s:
            return "no_pipeline", None
        if b and (b.get("queue_time") or "") >= since:
            last = b
            if b.get("status") == "completed":
                return "completed", b
        sleep(interval_s)
        waited += interval_s
    return "timeout", last


def _utc_now() -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%S")


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


def _tool_branch(client, repo: str, work_item_id: str) -> tuple[str, bool, str]:
    """(dal, mevcut araç PR'ı yeniden kullanılıyor mu, yerine geçilen PR). Aynı WI'ın ÖNCEKİ araç
    düzeltmesinin PR'ı hâlâ açıksa onun dalına itilir (PR güncellenir, yeni PR/terk gerekmez).
    Başka bir koşunun (ör. LLM pipeline'ı) dalı varsa main'den taze dal açılır. Varlık AÇIKÇA
    sorulur: Azure refs API var olan dal için 200 + success=false döner, hata fırlatmaz (#45721).

    Açık PR main ile ÇAKIŞIYORSA (mergeStatus=conflicts) dalı yeniden kullanılmaz: yeni içerik
    güncel main'den üretilse de üç yollu birleştirmede aynı satırlar iki tarafta farklı değiştiği
    için çakışma kalır. #45729: #45728 (`#[\SensitiveParameter]`) aynı Ekol kurucularını değiştirip
    merge edildi, 7 dosya çakıştı. Eski PR terk edilir, main'den taze dal + PR açılır."""
    from agile_sdlc_crew import db, pipeline
    base = f"feature/{work_item_id}"
    heads = {b.get("name") for b in client.list_branches(repo)}
    mine = set()
    with db.get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT branch_name, pr_id FROM jobs WHERE work_item_id=%s AND job_kind='modern_fix' "
                    "AND COALESCE(pr_id,'')<>''", (str(work_item_id),))
        mine = {(r["branch_name"], str(r["pr_id"])) for r in cur.fetchall() or []}
    superseded = ""
    for br, pid in sorted(mine, reverse=True):
        if f"refs/heads/{br}" in heads:
            pr = pipeline._find_existing_pr(client, repo, br)
            if pr and str(pr.get("pr_id")) == pid:
                try:
                    merge = client.get_pull_request(repo, int(pid)).get("mergeStatus")
                except Exception:  # noqa: BLE001
                    merge = ""
                if merge == "conflicts":
                    client.abandon_pull_request(repo, int(pid))
                    log.info(f"  Önceki araç PR #{pid} main ile çakışıyor — terk edildi, main'den taze dal")
                    superseded = pid
                    break
                log.info(f"  Önceki araç düzeltmesinin PR #{pid} açık — dalına ({br}) itiliyor")
                return br, True, ""
    if f"refs/heads/{base}" in heads:
        import time
        br = f"{base}-tool-{int(time.time()) % 100000}"
        client.create_branch(repo, br)
        log.info(f"  {base} zaten var (önceki koşu) — taze dal: {br}")
        return br, False, superseded
    res = pipeline.create_branch(repo, str(work_item_id))
    if not res.get("success"):
        raise RuntimeError(f"Branch açılamadı: {res.get('error')}")
    return res["branch"], False, superseded


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
    dropped: list[str] = []
    dropped_why: dict[str, str] = {}
    for f in list(changed) if lang == "php" else []:
        lost = lost_property_defaults(originals.get(f, ""), contents[f])
        if lost:
            changed.remove(f)
            contents.pop(f)
            dropped.append(f)
            dropped_why[f] = ("varsayılan değerli property promote edildi (" + ", ".join(f"`${n}`" for n in lost)
                              + ") — constructor'ı çağırmayan alt sınıf/mock'ta başlatılmamış kalır")
    if dropped:
        log.info(f"  🧰 {len(dropped)} dosyada promotion property varsayılanını kaldırıyordu — PR'a alınmadı")
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
    branch, reused, superseded = _tool_branch(client, repo, work_item_id)
    pushed_at = _utc_now()
    client.push_changes(repo, branch, [{"changeType": "edit", "path": "/" + f, "content": c}
                                       for f, c in contents.items()], f"#{work_item_id}: {title[:80]} ({tool})")
    notes = _sites_note(left) if lang == "php" else ""  # yeniden tarama yalnız PHP'de
    desc = (f"Araçla uygulanan modernleştirme ({tool}, hedef {modern.LANGS[lang]['label']} {version}).\n\n"
            f"Kurallar: {', '.join(scope)}\n\nDeğişen dosyalar ({len(changed)}):\n"
            + "\n".join(f"- `{f}`" for f in changed)
            + f"\n\nDoğrulama: {', '.join(res['ran']) or 'doğrulayıcı yok'} geçti." + notes
            + (f"\n\nBu PR, main ile çakışan #{superseded}'in yerine güncel main'den açıldı." if superseded else "")
            + sig)
    # Kullanıcı kararı (2026-11-04): "repoda tanımlıysa unit test çalıştırıp doğrulamadan PR açma".
    # Testler yalnız PR build politikasında koşuyor (Core-test: MySQL konteyneri + AWS havuzu; YAML
    # yml-files'ta, core'u `$(Build.SourceBranchName)` ile çekiyor → dal üzerinde elle kuyruklanamaz,
    # yerelde de koşmaz). Bu yüzden PR TASLAK açılır: insana düşmez, build koşar; inceleme + build
    # yeşilse yayınlanır, değilse taslak kalır.
    draft = bool(pc.get("CREW_MODERN_FIX_BUILD_GATE"))
    pr = pipeline.create_pull_request(repo, branch, str(work_item_id), f"#{work_item_id} - {title}", desc,
                                      draft=draft)
    if not pr.get("success"):
        raise RuntimeError(f"PR açılamadı: {pr.get('error')}")
    db.update_job(job_id, repo_name=repo, branch_name=branch, pr_id=str(pr["pr_id"]), pr_url=pr["url"])
    # PR/WI'a YALNIZ sonuç yazılır (#45729: ara turların 16 satır yorumu + 3 özet + çıkarma notları
    # PR'ı doldurdu; ekip elle sildi). Ara incelemeler sessiz; aynı PR'ı güncelleyen yeniden koşuda
    # önceki koşunun bot yorumları silinir (insanınkine dokunulmaz).
    if reused:
        try:
            n_del = client.delete_own_pr_comments(repo, int(pr["pr_id"]), BOT_COMMENT_MARKERS)
            if n_del:
                log.info(f"  🧹 Önceki koşunun {n_del} bot yorumu silindi")
        except Exception as e:  # noqa: BLE001
            log.info(f"  Eski yorumlar silinemedi: {e}")

    def review(cur: dict[str, str]) -> dict:
        return run_pr_review(work_item_id=str(work_item_id), pr_id=int(pr["pr_id"]), repo_name=repo, job_id=job_id,
                             changes={f: (originals.get(f, ""), c) for f, c in cur.items()}, tool=tool,
                             extra_context=notes, post=False)
    rv = review(contents)
    rounds = int(pc.get("CREW_MODERN_FIX_REVIEW_ROUNDS") or 0)
    rules_by = {x.key: x for x in _pm.load_rules()} if lang == "php" else {}
    # Kuralın tamamı blok/sınıf düzeyindeyse (promotion, match) inceleme maddesi pencereyle düzeltilmez.
    # #269: Rector Ekol SOAP sınıflarında yalnız $apiKey'i promote etti, property bildirim sırası değişti
    # (SoapClient nesneyi bildirim sırasıyla serileştirir → XML eleman sırası). Böyle dosya PR'dan
    # ÇIKARILIR (main hâline döner), geri kalanı yeniden incelenir — insan beklemez.
    non_local = lang == "php" and not any(_pm.llm_line_fixable(rules_by.get(k)) for k in scope)

    def _rel(i) -> str:
        return str(i.get("file") or "").lstrip("/")
    while True:
        blocking = _blocking(rv.get("issue_list"))
        if not blocking:
            break
        keep = {(f, int(ln or 0)) for f, ln, _r in left["skipped"]}
        to_drop = sorted({_rel(i) for i in blocking if _rel(i) in contents
                          and (non_local or (_rel(i), int(i.get("line") or 0)) in keep)})
        fixable = [i for i in blocking if _rel(i) not in to_drop]
        if to_drop and pc.get("CREW_MODERN_FIX_DROP_RISKY"):
            if len(to_drop) >= len(contents):
                _post_final(client, repo, pr, work_item_id, rv, blocking, tool=tool, changed=sorted(contents),
                            res=res, dropped=dropped, dropped_why=dropped_why, left=left, reused=reused, sig=sig,
                            add_wi=_add_wi_comment, desc=desc)
                raise NeedsHumanReview(f"Araç düzeltmesi PR #{pr['pr_id']}: incelemede değişen dosyaların TÜMÜ "
                                       "davranış riski taşıyor — PR açık, insan karar versin")
            pushed_at = _utc_now()
            client.push_changes(repo, branch, [{"changeType": "edit", "path": "/" + f,
                                                "content": originals.get(f, "")} for f in to_drop],
                                f"#{work_item_id}: incelemede riskli bulunan {len(to_drop)} dosya PR'dan çıkarıldı")
            for f in to_drop:
                contents.pop(f, None)
            dropped += to_drop
            for i in blocking:
                if _rel(i) in to_drop:
                    dropped_why.setdefault(_rel(i), (i.get("problem") or "")[:200])
            log.info(f"  🧰 {len(to_drop)} riskli dosya PR'dan çıkarıldı — kalan {len(contents)} dosya yeniden inceleniyor")
            notes += ("\n\nİNCELEMEDE RİSKLİ BULUNUP PR'DAN ÇIKARILAN dosyalar (main hâlinde, kapsam dışı — "
                      "eksik sayılmaz): " + ", ".join(f"`{f}`" for f in dropped))
            rv = review(contents)
            continue
        if not fixable or rounds <= 0:
            break
        rounds -= 1
        log.info(f"  🧰 İnceleme {len(fixable)} bloklayan madde buldu — pencereyle düzeltiliyor")
        if job_id:
            _acct.set_call_context(job_id, "implement_change_task", "senior_developer")
        with _detached(root):
            for f, c in contents.items():
                (root / f).write_text(c, encoding="utf-8")
            n = fix_review_issues(root, fixable, set(contents))
            if lang == "php":
                more = fix_leftovers(root, {k: [f for f in v if f in contents] for k, v in scope.items()}, version)
                left = {**more, "fixed": left["fixed"] + more["fixed"]}
            new = {f: (root / f).read_text(encoding="utf-8") for f in _changed_in_scope(root, list(contents))}
            fix_res = verify.verify(root, list(new)) if new else {"ok": True}
        upd = {f: c for f, c in new.items() if contents.get(f) != c}
        if not n or not upd or not fix_res.get("ok"):
            log.info("  🧰 İnceleme düzeltmesi ilerleme sağlamadı — insan incelemesine")
            break
        contents.update(upd)
        pushed_at = _utc_now()
        client.push_changes(repo, branch, [{"changeType": "edit", "path": "/" + f, "content": c}
                                           for f, c in upd.items()],
                            f"#{work_item_id}: inceleme düzeltmesi ({len(upd)} dosya)")
        rv = review(contents)
    still = _blocking(rv.get("issue_list"))
    build_fail = ""
    if not still and pc.get("CREW_MODERN_FIX_BUILD_GATE"):
        build_fail, pushed_at = _build_gate(client, repo, pr, branch, work_item_id, contents, originals,
                                            dropped, dropped_why, pushed_at, pc)
    _post_final(client, repo, pr, work_item_id, rv, still, tool=tool, changed=sorted(contents), res=res,
                dropped=dropped, dropped_why=dropped_why, left=left, reused=reused, sig=sig,
                add_wi=_add_wi_comment, desc=desc, build_fail=build_fail)
    if build_fail:
        raise NeedsHumanReview(f"Araç düzeltmesi PR #{pr['pr_id']}: PR build'i kırmızı — {build_fail[:200]}"
                               + (" (PR taslak kaldı)" if pr.get("draft") else ""))
    if still:
        raise NeedsHumanReview(f"Araç düzeltmesi PR #{pr['pr_id']}: incelemede {len(still)} bloklayan madde kaldı "
                               f"(otomatik düzeltme turları bitti) — PR açık")
    if pr.get("draft"):
        client.set_pr_draft(repo, int(pr["pr_id"]), False)
        log.info(f"  🧪 İnceleme + testler yeşil — PR #{pr['pr_id']} yayınlandı")
    if lifecycle:
        info = wl.load_wi_context(client, str(work_item_id))
        target = wl.plan_transition(info["wi_state"], info["wi_states"], "review")
        if target:
            wl.apply_state(client, str(work_item_id), target, logger=log.info)
    return {"changed": len(contents), "pr_id": pr["pr_id"], "leftover_fixed": left["fixed"],
            "remaining": len(left["remaining"]), "dropped": dropped, "verdict": rv.get("verdict")}


BUILD_DROP_ROUNDS = 2


def _build_gate(client, repo, pr, branch, work_item_id, contents, originals, dropped, dropped_why,
                pushed_at, pc) -> tuple[str, str]:
    """PR'ın CI build'i (testler) yeşil olmadan WI Code Review'a geçmez. #45729: inceleme onayladı,
    WI Code Review'a geçti, `Core-test` 4 testte kırmızıydı (promotion `$config = []` varsayılanını
    kaldırmıştı). Kırmızıysa hata yığınında adı geçen değişen dosyalar PR'dan çıkarılır (main hâline
    döner) ve build yeniden beklenir; suçlu bulunamazsa / turlar biterse iş insan incelemesine.
    Dönüş: (build_fail — boşsa yeşil / pipeline yok, son push zamanı)."""
    timeout = int(pc.get("CREW_MODERN_FIX_BUILD_WAIT_MIN") or 0) * 60
    for _ in range(BUILD_DROP_ROUNDS + 1):
        st, b = wait_pr_build(client, repo, int(pr["pr_id"]), pushed_at, timeout_s=timeout)
        if st == "no_pipeline":
            if pr.get("draft"):
                # Taslakta build politikası koşmuyor olabilir: yayınla, build gelirse onu bekle
                client.set_pr_draft(repo, int(pr["pr_id"]), False)
                pr["draft"] = False
                pushed_at = _utc_now()
                log.info("  🧪 Taslak PR'da build gelmedi — PR yayınlandı, build bekleniyor")
                continue
            log.info("  🧪 Bu repoda PR build'i yok — build kapısı atlandı")
            return "", pushed_at
        if st == "timeout":
            return f"build {timeout // 60} dk içinde bitmedi ({(b or {}).get('url') or 'build yok'})", pushed_at
        if b.get("result") in ("succeeded", "partiallySucceeded"):
            log.info(f"  🧪 PR build'i yeşil ({b.get('result')})")
            return "", pushed_at
        try:
            summary = client.get_build_failure_summary(b["project"], int(b["build_id"]), max_chars=12000)
        except Exception as e:  # noqa: BLE001
            summary = f"(özet alınamadı: {e})"
        culprits = build_culprits(summary, contents)
        fail = f"build {b.get('result')} ({b.get('url')})"
        if not culprits or len(culprits) >= len(contents):
            return fail + (" — hata yığınında değişen dosya yok" if not culprits else " — tüm dosyalar suçlu"), pushed_at
        pushed_at = _utc_now()
        client.push_changes(repo, branch, [{"changeType": "edit", "path": "/" + f, "content": originals.get(f, "")}
                                           for f in culprits],
                            f"#{work_item_id}: testleri kıran {len(culprits)} dosya PR'dan çıkarıldı")
        for f in culprits:
            contents.pop(f, None)
            dropped.append(f)
            dropped_why[f] = f"PR build'inde test kırdı ({b.get('url')})"
        log.info(f"  🧪 Build kırmızı — {len(culprits)} dosya çıkarıldı, build yeniden bekleniyor")
    return fail + " — çıkarma turları bitti", pushed_at


BOT_COMMENT_MARKERS = ("inceleme katkısı", "İnceleme Katkısı", "Tempo — araç düzeltmesi")


def _post_final(client, repo, pr, work_item_id, rv, still, *, tool, changed, res, dropped, dropped_why,
                left, reused, sig, add_wi, desc: str = "", build_fail: str = "") -> None:
    """Araç düzeltmesinin TEK çıktısı. Kullanıcı kararı (2026-11-04): "atılan her yorum incelenmesi
    gereken bir durum" — #45729'da 14 major yorum, PR'dan zaten çıkarılmış dosyalar üzerindeydi.
    - Bilgi (çıkarılan dosyalar, bilerek bırakılan / LLM'le düzeltilen yerler) PR AÇIKLAMASINA yazılır.
    - Yorum yalnız sonda hâlâ bloklayan madde varsa: PR'a tek özet + o maddeler satıra, WI'a tek yorum.
    - Onaylanan PR'a ve WI'a yorum yazılmaz (WI Code Review'a geçer, PR WI'a bağlı)."""
    from agile_sdlc_crew.pr_review import post_review
    info = []
    if dropped:
        info.append(f"**PR'dan çıkarılan {len(dropped)} dosya** (davranış riski; elle düzeltilmedi, "
                    "`main` hâlinde kaldı):\n"
                    + "\n".join(f"- `{f}` — {dropped_why.get(f, '')}" for f in dropped))
    if left.get("skipped"):
        info.append(f"Araç {len(left['skipped'])} yeri güvenli bulmadığı için dönüştürmedi (bilerek bırakıldı).")
    if left.get("fixed"):
        info.append(f"Aracın kaçırdığı yerlerden {left['fixed']} pencere LLM ile düzeltildi.")
    verdict = "✅ onaylandı" if not still else f"🔴 {len(still)} bloklayan madde — insan incelemesi bekliyor"
    if build_fail:
        verdict += f"; 🔴 PR build'i: {build_fail}"
    try:
        client.update_pr_description(repo, int(pr["pr_id"]), (desc or "").split("\n\n### Sonuç")[0]
                                     + f"\n\n### Sonuç\nİnceleme: {verdict}. Değişen dosya: {len(changed)}."
                                     + ("\n\n" + "\n\n".join(info) if info else ""))
    except Exception as e:  # noqa: BLE001
        log.info(f"  PR açıklaması güncellenemedi: {e}")
    if not still and not build_fail:
        log.info("  🧰 İnceleme onayladı — PR/WI'a yorum yazılmadı (sonuç PR açıklamasında)")
        return
    if build_fail and not still:
        try:
            add_wi(client, work_item_id,
                   f"## 🙋 Araç düzeltmesi — insan incelemesi gerekli\n\nPR: [#{pr['pr_id']}]({pr['url']}) — {tool}, "
                   f"{len(changed)} dosya. İnceleme onayladı ama **PR build'i kırmızı**: {build_fail}. "
                   "Hatayı değişen bir dosyaya bağlayamadığım için PR'dan çıkarma yapılmadı." + sig)
        except Exception as e:  # noqa: BLE001
            log.info(f"  WI yorumu yazılamadı: {e}")
        return
    try:
        post_review(client, repo, int(pr["pr_id"]), str(work_item_id), rv.get("summary") or "", still,
                    rv.get("file_bodies") or {}, only_blocking=True, wi=False)
    except Exception as e:  # noqa: BLE001
        log.info(f"  PR özeti yazılamadı: {e}")
    try:
        add_wi(client, work_item_id,
               f"## 🙋 Araç düzeltmesi — insan incelemesi gerekli\n\nPR: [#{pr['pr_id']}]({pr['url']}) — {tool}, "
               f"{len(changed)} dosya. İncelemede **{len(still)} bloklayan madde** otomatik düzeltilemedi; "
               "maddeler PR'da satır yorumu olarak duruyor." + sig)
    except Exception as e:  # noqa: BLE001
        log.info(f"  WI yorumu yazılamadı: {e}")


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
