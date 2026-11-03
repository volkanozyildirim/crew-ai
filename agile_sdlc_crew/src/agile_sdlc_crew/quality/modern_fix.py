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


def php_apply(root: Path, scope: dict[str, list[str]], version: str) -> None:
    """Rector'u repo dışındaki geçici bir yapılandırmayla çalıştır (repo'ya dosya yazılmaz).
    Ortam süzülür (Azure/Sonar anahtarları Rector'a geçmez)."""
    import tempfile
    from agile_sdlc_crew import verify
    from agile_sdlc_crew.quality import php_modern
    binp = php_modern.rector_bin()
    if not binp:
        raise RuntimeError("Rector kurulu değil (CREW_RECTOR_BIN / ~/.crew_tools/rector)")
    with tempfile.TemporaryDirectory(prefix="tempo-rector-") as td:
        cfg = Path(td) / "rector.php"
        cfg.write_text(rector_config(root, scope, version, Path(td) / "cache"), encoding="utf-8")
        r = subprocess.run([binp, "process", "--config", str(cfg), "--no-progress-bar", "--no-diffs",
                            "--memory-limit=2G"], cwd=str(root), capture_output=True, text=True, timeout=1200,
                           env=verify._env())
        if r.returncode != 0:  # process: 0 = tamam (değişiklik olsa da); ayrıştırma hatası ≠ 0
            raise RuntimeError("Rector hata verdi: " + ((r.stderr or r.stdout) or "").strip()[-600:])


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


def run(job_id: int, work_item_id: str) -> dict:
    from agile_sdlc_crew import db, pipeline, verify
    from agile_sdlc_crew import pipeline_config as pc
    from agile_sdlc_crew import wi_lifecycle as wl
    from agile_sdlc_crew.main import _add_wi_comment
    from agile_sdlc_crew.quality import modern
    from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient
    from agile_sdlc_crew.tools.local_repo import LocalRepoManager

    client = AzureDevOpsClient()
    job = db.get_job(job_id) or {}
    before = job.get("wi_state_before") or ""
    lifecycle = bool(pc.get("CREW_WI_LIFECYCLE"))
    try:
        fields = client.get_work_item(int(work_item_id)).get("fields") or {}
        title = fields.get("System.Title") or f"WI {work_item_id}"
        mgr = LocalRepoManager()
        # Kapsam işin kaydından (take'te onaylanan); WI açıklaması yalnız görüntü — düzenlenebilir
        lang, repo, scope, root = load_scope(job.get("job_payload") or "", mgr.base_dir)
        tv = modern.target(client, repo, root, lang, index=modern.build_index(client))
        log.info(f"\n-- ARAÇ DÜZELTMESİ #{job_id}: WI #{work_item_id} · {repo} ({lang} {tv['version']}) · "
                 f"{', '.join(scope)} --")
        if _git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip():
            raise RuntimeError(f"{repo} klonunda commit edilmemiş değişiklik var — araç çalıştırılmadı")
        mgr.set_remote_auth(repo)
        _git(root, "fetch", "-q", "origin", "main", check=True)
        prev = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
        _git(root, "checkout", "-q", "--detach", "origin/main", check=True)
        try:
            changed = apply_tool(root, lang, scope, tv["version"] or "")
            contents = {f: (root / f).read_text(encoding="utf-8") for f in changed}
            res = verify.verify(root, changed) if changed else {"ok": True, "ran": [], "issues": [], "skipped": []}
        finally:
            _git(root, "checkout", "--", ".")
            _git(root, "checkout", "-q", prev)
        if not changed:
            _add_wi_comment(client, work_item_id,
                            "## 🧰 Araç düzeltmesi\n\nAraç bu kapsamda değişiklik üretmedi — bulgular `main`'de zaten "
                            "giderilmiş olabilir. PR açılmadı.\n\n*Tempo — araç düzeltmesi*")
            return {"changed": 0}
        if not res["ok"]:
            raise RuntimeError("Doğrulayıcı geçmedi: " + "; ".join(res["issues"][:5]))
        br = pipeline.create_branch(repo, str(work_item_id))
        if not br.get("success"):
            raise RuntimeError(f"Branch açılamadı: {br.get('error')}")
        branch = br["branch"]
        if br.get("note") == "zaten mevcut":
            # Önceki (ör. başarısız LLM) koşunun dalı: üstüne yazmak eski commit'leri ve kapsam dışı
            # değişiklikleri bu PR'a taşırdı — araç düzeltmesi main'den TAZE dalda açılır.
            import time
            branch = f"feature/{work_item_id}-tool-{int(time.time()) % 100000}"
            client.create_branch(repo, branch)
            log.info(f"  feature/{work_item_id} zaten var (önceki koşu) — taze dal: {branch}")
        tool ={"go": "modernize -fix", "php": "rector"}.get(lang, "ruff --fix")
        client.push_changes(repo, branch, [{"changeType": "edit", "path": "/" + f, "content": c}
                                           for f, c in contents.items()],
                            f"#{work_item_id}: {title[:80]} ({tool})")
        desc = (f"Araçla uygulanan modernleştirme ({tool}, hedef {modern.LANGS[lang]['label']} {tv['version']}).\n\n"
                f"Kurallar: {', '.join(scope)}\n\nDeğişen dosyalar ({len(changed)}):\n"
                + "\n".join(f"- `{f}`" for f in changed)
                + f"\n\nDoğrulama: {', '.join(res['ran']) or 'doğrulayıcı yok'} geçti.\n\n*Tempo — araç düzeltmesi*")
        pr = pipeline.create_pull_request(repo, branch, str(work_item_id), f"#{work_item_id} - {title}", desc)
        if not pr.get("success"):
            raise RuntimeError(f"PR açılamadı: {pr.get('error')}")
        db.update_job(job_id, repo_name=repo, branch_name=branch, pr_id=str(pr["pr_id"]), pr_url=pr["url"])
        _add_wi_comment(client, work_item_id,
                        f"## 🧰 Araç düzeltmesi\n\nPR: [#{pr['pr_id']}]({pr['url']}) — {tool}, {len(changed)} dosya, "
                        f"doğrulama ({', '.join(res['ran']) or '-'}) geçti. LLM incelemesi PR'a yazılıyor.\n\n"
                        "*Tempo — araç düzeltmesi*")
        try:
            from agile_sdlc_crew.llm import resolver
            from agile_sdlc_crew.pr_review import run_pr_review
            # Kodu araç yazdı; inceleme mekanik bir diff'in doğrulaması — Opus gerekmez
            # (kullanıcı kararı 2026-11-03: "düşük modeller ile işlet"). Kapsam yalnız bu iş.
            with resolver.job_llm_scope():
                prof = str(pc.get("CREW_MODERN_FIX_REVIEW_PROFILE") or "").strip()
                if prof:
                    resolver.set_job_llm_overrides({"code_reviewer": prof})
                run_pr_review(work_item_id=str(work_item_id), pr_id=int(pr["pr_id"]), repo_name=repo, job_id=job_id)
        except Exception as e:  # noqa: BLE001 — PR açık; inceleme elle de istenebilir
            log.warning(f"  PR incelemesi yapılamadı: {e}")
        if lifecycle:
            info = wl.load_wi_context(client, str(work_item_id))
            target = wl.plan_transition(info["wi_state"], info["wi_states"], "review")
            if target:
                wl.apply_state(client, str(work_item_id), target, logger=log.info)
        return {"changed": len(changed), "pr_id": pr["pr_id"]}
    except Exception:
        if lifecycle and before:
            try:
                wl.revert_to(client, str(work_item_id), before, logger=log.info)
            except Exception as e2:  # noqa: BLE001
                log.warning(f"  WI geri alınamadı: {e2}")
        raise
