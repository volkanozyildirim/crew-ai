"""PR açılmadan önce değişen dosyaları reponun dilinin doğrulayıcısıyla derle/lint et.

Neden: job #248 (gocrons) plandaki Go kodunu doğrudan uyguladı; import eksikti
(`undefined: webanalytics`, `"os" imported and not used`). Dosya başına
sözdizimi kontrolü (`gofmt -e`) bunu göremez, reviewer kodu derlemez; hata ancak
CI'da çıktı ve build-fix turları $4.4'e mal oldu. `go build` aynı hatayı yerel
klonda 3 saniyede veriyor.

Doğrulayıcılar: Go `go build ./...` + değişen paketlerde `go vet` (testleri de
derler), PHP `php -l`, Python `py_compile`, JS `node --check`, TS (yerel `tsc`
varsa) `tsc --noEmit`, JSON/YAML ayrıştırma. Araç yoksa dil atlanır — doğrulama
yapılamadı demek, geçti demek değil.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

LANG_BY_EXT = {
    ".go": "go", ".php": "php", ".py": "python",
    ".js": "js", ".mjs": "js", ".cjs": "js", ".ts": "ts", ".tsx": "ts",
    ".json": "json", ".yml": "yaml", ".yaml": "yaml",
}
MAX_ISSUES = 25
_LOC = re.compile(r"((?:[\w.@-]+/)*[\w.@-]+\.(?:go|php|py|js|mjs|cjs|ts|tsx|json|ya?ml))[:(](\d+)")


def _run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    return r.returncode, ((r.stdout or "") + "\n" + (r.stderr or "")).strip()


def _issue_lines(out: str, root: Path) -> list[str]:
    """Hata satırları: dosya:satır taşıyanlar önce; yoksa ilk satırlar. Mutlak yol
    repo-göreli yapılır (developer'a ve WI'ya giden metin)."""
    base = str(root.resolve()) + "/"
    lines = [ln.replace(base, "").rstrip() for ln in out.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not re.match(r"^(Deprecated|Notice|Warning|PHP Deprecated|PHP Notice|PHP Warning):", ln)]
    located = [ln for ln in lines if _LOC.search(ln)]
    return (located or lines)[:MAX_ISSUES]


def languages(paths: list[str]) -> list[str]:
    seen: list[str] = []
    for p in paths:
        lang = LANG_BY_EXT.get(Path(p).suffix.lower())
        if lang and lang not in seen:
            seen.append(lang)
    return seen


def referenced_files(issues: list[str]) -> list[str]:
    """Hata satırlarında geçen repo-göreli dosyalar (sırayla, tekrarsız)."""
    out: list[str] = []
    for ln in issues:
        for m in _LOC.finditer(ln):
            p = m.group(1).lstrip("./")
            if p not in out:
                out.append(p)
    return out


def verify(root, changed: list[str], *, php_bin: str = "php", timeout: int = 300) -> dict:
    """`changed` repo-göreli yollar (silinenler hariç, diskte güncel hâlleri).
    Dönen: {ok, ran:[dil], skipped:[(dil, neden)], issues:[satır]}."""
    root = Path(root)
    files = [p.lstrip("/") for p in changed if (root / p.lstrip("/")).is_file()]
    by_lang: dict[str, list[str]] = {}
    for p in files:
        lang = LANG_BY_EXT.get(Path(p).suffix.lower())
        if lang:
            by_lang.setdefault(lang, []).append(p)
    ran: list[str] = []
    skipped: list[tuple[str, str]] = []
    issues: list[str] = []

    def tool(lang: str, fn) -> None:
        try:
            got = fn()
        except FileNotFoundError:
            skipped.append((lang, "araç yok"))
            return
        except subprocess.TimeoutExpired:
            skipped.append((lang, f"zaman aşımı ({timeout}s)"))
            return
        ran.append(lang)
        issues.extend(got)

    if "go" in by_lang:
        if not (root / "go.mod").is_file():
            skipped.append(("go", "go.mod yok"))
        else:
            def _go():
                rc, out = _run(["go", "build", "./..."], root, timeout)
                if rc != 0:
                    return _issue_lines(out, root)
                pkgs = sorted({"./" + str(Path(p).parent) for p in by_lang["go"]})
                rc, out = _run(["go", "vet", *pkgs], root, timeout)
                return _issue_lines(out, root) if rc != 0 else []
            tool("go", _go)

    if "php" in by_lang:
        def _php():
            out: list[str] = []
            for p in by_lang["php"]:
                rc, o = _run([php_bin, "-l", p], root, 30)
                if rc != 0:
                    out += _issue_lines(o, root)
            return out
        tool("php", _php)

    if "python" in by_lang:
        def _py():
            rc, o = _run(["python3", "-m", "py_compile", *by_lang["python"]], root, 60)
            return _issue_lines(o, root) if rc != 0 else []
        tool("python", _py)

    if "js" in by_lang:
        def _js():
            out: list[str] = []
            for p in by_lang["js"]:
                rc, o = _run(["node", "--check", p], root, 30)
                if rc != 0:
                    out += _issue_lines(o, root)
            return out
        tool("js", _js)

    if "ts" in by_lang:
        tsc = root / "node_modules" / ".bin" / "tsc"
        if not (tsc.exists() and (root / "tsconfig.json").is_file()):
            skipped.append(("ts", "yerel tsc / tsconfig yok"))
        else:
            def _ts():
                rc, o = _run([str(tsc), "--noEmit", "-p", "."], root, timeout)
                return _issue_lines(o, root) if rc != 0 else []
            tool("ts", _ts)

    if "json" in by_lang:
        def _json():
            out: list[str] = []
            for p in by_lang["json"]:
                try:
                    json.loads((root / p).read_text(encoding="utf-8"))
                except ValueError as e:
                    out.append(f"{p}:{getattr(e, 'lineno', 1)}: geçersiz JSON: {e}")
            return out
        tool("json", _json)

    if "yaml" in by_lang:
        def _yaml():
            import yaml
            out: list[str] = []
            for p in by_lang["yaml"]:
                try:
                    list(yaml.safe_load_all((root / p).read_text(encoding="utf-8")))
                except yaml.YAMLError as e:
                    mark = getattr(e, "problem_mark", None)
                    out.append(f"{p}:{(mark.line + 1) if mark else 1}: geçersiz YAML: {e}")
            return out
        tool("yaml", _yaml)

    return {"ok": not issues, "ran": ran, "skipped": skipped, "issues": issues[:MAX_ISSUES]}
