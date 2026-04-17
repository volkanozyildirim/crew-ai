#!/usr/bin/env python
"""Agile SDLC Crew - Full 11-step pipeline with 7 agents."""

import json
import logging
import re
import sys
import warnings
import webbrowser
from pathlib import Path

# .env dosyasini otomatik yukle (password'daki ozel karakterler icin guvenli)
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
except ImportError:
    pass

from agile_sdlc_crew.crew import AgileSDLCCrew  # train/replay/test icin
from agile_sdlc_crew.dashboard import StatusTracker, start_dashboard_server
from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient

warnings.filterwarnings("ignore", category=SyntaxWarning, module="pysbd")

# Pipeline logger — server.py tarafından kurulur, yoksa basit fallback
log = logging.getLogger("pipeline")
if not log.handlers:
    log.setLevel(logging.INFO)
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    log.addHandler(_h)


def _log(msg: str):
    """Pipeline log mesaji yaz."""
    log.info(msg)

DASHBOARD_PORT = 8765


# ── Yardimci fonksiyonlar ──────────────────────────────────

def _parse_architect_output(raw_output: str) -> dict:
    """Software Architect agent ciktisini JSON olarak parse eder."""
    text = raw_output.strip()

    json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if json_match:
        text = json_match.group(1).strip()

    try:
        plan = json.loads(text)
    except json.JSONDecodeError:
        brace_match = re.search(r'\{.*\}', text, re.DOTALL)
        if brace_match:
            try:
                plan = json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                raise ValueError(
                    f"Architect ciktisi JSON formatinda degil.\n"
                    f"Cikti:\n{raw_output[:500]}"
                )
        else:
            raise ValueError(
                f"Architect ciktisinda JSON bulunamadi.\n"
                f"Cikti:\n{raw_output[:500]}"
            )

    # Tool argumanlari sizdi mi? (agent tool cagrisini final output olarak bastiysa)
    tool_arg_indicators = {"repo_name", "path", "branch", "include_file_content", "search_text", "query"}
    plan_keys = set(plan.keys())
    if plan_keys.issubset(tool_arg_indicators) and "changes" not in plan:
        raise ValueError(
            f"Architect final JSON yerine tool argumani dondurdu: {list(plan.keys())}. "
            f"Agent'in tool cagrilarini bitirip JSON plan uretmesi gerekiyor."
        )

    required = ["repo_name", "changes"]
    for field in required:
        if field not in plan:
            raise ValueError(f"Architect ciktisinda '{field}' alani eksik: {list(plan.keys())}")

    if not plan["changes"]:
        raise ValueError("Architect ciktisinda degisiklik listesi bos.")

    for i, change in enumerate(plan["changes"]):
        for field in ["file_path", "change_type", "new_code"]:
            if field not in change:
                raise ValueError(
                    f"Architect ciktisi changes[{i}] icinde '{field}' alani eksik: {list(change.keys())}"
                )

    return plan


def _extract_code_from_output(raw_output: str) -> str:
    """Developer agent ciktisindaki kod blogunu cikarir."""
    text = raw_output.strip()
    code_match = re.search(r'```(?:\w+)?\s*\n(.*?)\n```', text, re.DOTALL)
    if code_match:
        return code_match.group(1)
    return text


def _validate_code(code: str, file_path: str, original_content: str, description: str, repo_name: str = "") -> tuple[bool, str]:
    """Push oncesi kod dogrulama. Local linter oncelikli, Ollama fallback.

    1. Dile gore native linter calistir (php -l, go vet, python -m py_compile vb.)
    2. Linter yoksa veya dil desteklenmiyorsa Ollama ile kontrol et
    3. Hata varsa Ollama'dan duzeltme iste

    Returns:
        (valid, fixed_code) - valid=False ise kod push edilmemeli
    """
    import subprocess
    import tempfile
    import os

    if not code or not code.strip():
        return False, code

    ext = file_path.rsplit(".", 1)[-1] if "." in file_path else ""

    # ── 1. Native Linter ──
    lint_result = _lint_with_native(code, ext, repo_name=repo_name)
    if lint_result is not None:
        if lint_result["valid"]:
            _log(f"    Kod dogrulama (linter): GECTI")
            return True, code
        else:
            _log(f"    Kod dogrulama (linter): BASARISIZ")
            for issue in lint_result.get("issues", [])[:3]:
                _log(f"      - {issue}")
            # Ollama ile duzeltme dene
            fixed = _fix_with_ollama(code, file_path, lint_result["issues"], original_content)
            if fixed:
                # Duzeltilmis kodu tekrar lint et
                recheck = _lint_with_native(fixed, ext)
                if recheck is None or recheck["valid"]:
                    _log(f"    Ollama duzeltme basarili")
                    return True, fixed
                _log(f"    Ollama duzeltme de lint'ten gecemedi")
            return False, code

    # ── 2. Linter desteklemiyor → Ollama ile kontrol ──
    ollama_result = _check_with_ollama(code, file_path, original_content, description)
    if ollama_result is None:
        # Ollama da calismiyorsa gecir
        _log(f"    Kod dogrulama: atlandı (linter ve ollama yok)")
        return True, code

    if ollama_result["valid"]:
        _log(f"    Kod dogrulama (ollama): GECTI")
        return True, code

    _log(f"    Kod dogrulama (ollama): BASARISIZ")
    for issue in ollama_result.get("issues", [])[:3]:
        _log(f"      - {issue}")

    fixed = ollama_result.get("fixed_code", "")
    if fixed and fixed.strip():
        _log(f"    Duzeltilmis kod alindi ({len(fixed)} karakter)")
        return True, fixed

    return False, code


def _resolve_php_binary(repo_name: str = "") -> str:
    """Repo'nun composer.json'daki PHP versiyonuna gore dogru binary'yi sec.
    /opt/homebrew/opt/php@8.2/bin/php gibi versiyonlu binary kullanir."""
    import json as _json
    from pathlib import Path
    import os

    # Repo'dan PHP versiyon bilgisi al
    if repo_name:
        repos_dir = Path(os.environ.get("CREW_REPOS_DIR", "~/.crew_repos")).expanduser()
        composer = repos_dir / repo_name / "composer.json"
        if composer.exists():
            try:
                cj = _json.loads(composer.read_text(encoding="utf-8", errors="replace"))
                php_req = cj.get("require", {}).get("php", "")
                # "^8.2", ">=8.2", "~8.2" gibi formatlardan major.minor cikar
                import re as _re
                m = _re.search(r'(\d+\.\d+)', php_req)
                if m:
                    ver = m.group(1)  # "8.2", "8.4" vb.
                    versioned = f"/opt/homebrew/opt/php@{ver}/bin/php"
                    if Path(versioned).exists():
                        return versioned
            except Exception:
                pass

    # Default: PATH'teki php
    return "php"


# Repo bazli PHP binary cache (her seferinde composer.json okumamak icin)
_php_binary_cache: dict[str, str] = {}


def _lint_with_native(code: str, ext: str, repo_name: str = "") -> dict | None:
    """Dile gore native linter calistir. None = dil desteklenmiyor.
    PHP icin repo'nun gerektirdigi versiyonu kullanir."""
    import subprocess
    import tempfile
    import os

    if ext not in ("php", "py", "go"):
        return None

    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=f".{ext}", delete=False, encoding="utf-8") as f:
            f.write(code)
            tmp_path = f.name

        if ext == "php":
            # Repo bazli PHP binary sec
            if repo_name not in _php_binary_cache:
                _php_binary_cache[repo_name] = _resolve_php_binary(repo_name)
            php_bin = _php_binary_cache[repo_name]
            result = subprocess.run([php_bin, "-l", tmp_path], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return {"valid": True, "issues": []}
            errors = result.stdout.strip() or result.stderr.strip()
            return {"valid": False, "issues": [errors]}

        elif ext == "py":
            result = subprocess.run(
                ["python3", "-m", "py_compile", tmp_path],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return {"valid": True, "issues": []}
            errors = result.stderr.strip()
            return {"valid": False, "issues": [errors]}

        elif ext == "go":
            result = subprocess.run(
                ["gofmt", "-e", tmp_path],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return {"valid": True, "issues": []}
            errors = result.stderr.strip()
            return {"valid": False, "issues": [errors]}

    except FileNotFoundError:
        return None
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    return None


def _check_with_ollama(code: str, file_path: str, original_content: str, description: str) -> dict | None:
    """Ollama (qwen2.5-coder) ile kod dogrulama. None = Ollama calismiyorsa."""
    import json as _json

    ext = file_path.rsplit(".", 1)[-1] if "." in file_path else ""
    lang_map = {"go": "Go", "php": "PHP", "py": "Python", "js": "JavaScript",
                "ts": "TypeScript", "java": "Java", "cs": "C#", "rb": "Ruby"}
    lang = lang_map.get(ext, ext.upper())

    prompt = (
        f"Check this {lang} code for syntax errors only. File: {file_path}\n"
        f"Respond in JSON: {{\"valid\":true/false,\"issues\":[\"issue1\"],\"fixed_code\":\"only if invalid\"}}\n"
        f"If valid, return {{\"valid\":true,\"issues\":[],\"fixed_code\":\"\"}}\n\n"
        f"```\n{code[:6000]}\n```"
    )

    try:
        import requests
        resp = requests.post(
            "http://localhost:11434/api/generate",
            json={"model": "qwen2.5-coder:7b", "prompt": prompt, "stream": False},
            timeout=30,
        )
        if resp.status_code != 200:
            return None
        text = resp.json().get("response", "")

        # JSON parse
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if not m:
            return None
        result = _json.loads(m.group(0))
        return {
            "valid": result.get("valid", True),
            "issues": result.get("issues", []),
            "fixed_code": result.get("fixed_code", ""),
        }
    except Exception:
        return None


def _fix_with_ollama(code: str, file_path: str, issues: list[str], original_content: str) -> str | None:
    """Ollama ile hatalı kodu düzelt. None = başarısız."""
    issues_text = "\n".join(f"- {i}" for i in issues[:5])
    prompt = (
        f"Fix these syntax errors in {file_path}:\n{issues_text}\n\n"
        f"Return ONLY the fixed complete file content, no explanation.\n\n"
        f"```\n{code[:6000]}\n```"
    )

    try:
        import requests
        resp = requests.post(
            "http://localhost:11434/api/generate",
            json={"model": "qwen2.5-coder:7b", "prompt": prompt, "stream": False},
            timeout=60,
        )
        if resp.status_code != 200:
            return None
        text = resp.json().get("response", "")
        # Kod blogunu cikar
        code_match = re.search(r'```(?:\w+)?\s*\n(.*?)\n```', text, re.DOTALL)
        if code_match:
            return code_match.group(1)
        # Kod blogu yoksa tum ciktiyi dondur (sadece kod oldugunu varsay)
        if text.strip():
            return text.strip()
        return None
    except Exception:
        return None


def _resolve_repo_name(repo_name: str, known_repos: list[str], client: AzureDevOpsClient, work_item_id: str) -> str:
    """Repo adini dogrula, bulunamazsa work item'dan cikar."""
    from agile_sdlc_crew.pipeline import find_repo_name

    if repo_name and repo_name in known_repos:
        return repo_name

    if repo_name:
        matched = find_repo_name(repo_name, known_repos)
        if matched:
            return matched
        _log(f"  Repo '{repo_name}' bilinen repolarda yok, work item'dan denenecek")

    # Work item description'dan _git/ URL'i ara
    try:
        wi = client.get_work_item(int(work_item_id))
        desc = ""
        fields = wi.get("fields", {})
        if fields:
            desc = fields.get("System.Description", "") or ""
        if not desc:
            desc = wi.get("aciklama", "") or ""
        git_url_match = re.search(r'_git/([A-Za-z0-9._-]+)', desc)
        if git_url_match:
            candidate = git_url_match.group(1).strip("/")
            if candidate in known_repos:
                _log(f"  Work item URL'den repo bulundu: '{candidate}'")
                return candidate
            matched = find_repo_name(candidate, known_repos)
            if matched:
                _log(f"  Work item URL'den repo eslesti: '{candidate}' -> '{matched}'")
                return matched
    except Exception:
        pass

    raise ValueError("Repo adi belirlenemedi")


def _enrich_plan_with_agent(
    plan: dict,
    agile_crew,
    client: AzureDevOpsClient,
    repo_name: str,
    work_item_id: str,
    requirements_text: str,
    tracker,
    hal=None,
    repo_mgr=None,
) -> dict:
    """HAL'in eksik biraktigi alanlari tamamlar. HAL varsa HAL ile, yoksa agent ile.

    Eksik durumlar:
    - Degisiklik listesi bos
    - Dosya yolu var ama current_code/new_code yok
    """
    changes = plan.get("changes", [])

    # 1. Hic degisiklik yoksa
    if not changes:
        if hal:
            # HAL ile tekrar dene - daha spesifik sor
            _log("  HAL plan bos, ayni sohbette tekrar deneniyor...")
            retry = hal.followup(
                f"#{work_item_id} icin {repo_name} reposunda degisecek dosyalari, "
                f"mevcut kodu ve yeni kodu goster."
            )
            retry_parsed = hal.parse_analysis_response(retry)
            for hc in retry_parsed.get("changes", []):
                plan["changes"].append({
                    "file_path": hc["path"],
                    "change_type": hc.get("change_type", "edit"),
                    "description": hc.get("description", ""),
                    "current_code": hc.get("current_code", ""),
                    "new_code": hc.get("code", ""),
                })
            if not plan["changes"]:
                _log("  HAL 2. deneme de bos, architect agent deneniyor...")
                try:
                    analysis_crew = agile_crew.create_analysis_crew()
                    result = analysis_crew.kickoff(inputs={
                        "work_item_id": work_item_id,
                        "target_repo": repo_name,
                    })
                    agent_plan = _parse_architect_output(result.raw or "")
                    plan["changes"] = agent_plan.get("changes", [])
                    _log(f"  Architect agent: {len(plan['changes'])} dosya")
                except Exception as e:
                    _log(f"  Architect agent basarisiz: {e}")
            else:
                _log(f"  HAL 2. deneme: {len(plan['changes'])} dosya")
        else:
            _log("  Plan bos, architect agent ile tamamlaniyor...")
            try:
                analysis_crew = agile_crew.create_analysis_crew()
                result = analysis_crew.kickoff(inputs={
                    "work_item_id": work_item_id,
                    "target_repo": repo_name,
                })
                agent_plan = _parse_architect_output(result.raw or "")
                plan["changes"] = agent_plan.get("changes", [])
                plan["summary"] = plan.get("summary") or agent_plan.get("summary", "")
                _log(f"  Architect agent: {len(plan['changes'])} dosya")
            except Exception as e:
                _log(f"  Architect agent basarisiz: {e}")
        return plan

    # 2. Degisiklik var ama kod eksik -> dosyayi repodan oku, agent ile tamamla
    enriched = []
    for ch in changes:
        file_path = ch.get("file_path", "")
        current_code = ch.get("current_code", "")
        new_code = ch.get("new_code", "")
        description = ch.get("description", "")

        # current_code ve new_code varsa -> tamam, dokunma
        if current_code and new_code:
            enriched.append(ch)
            continue

        # Dosyayi repodan oku (local oncelikli)
        if file_path and not new_code:
            _log(f"  Kod eksik: {file_path}, repodan okunup agent ile tamamlaniyor...")
            full_content = ""
            try:
                if repo_mgr:
                    full_content = repo_mgr.get_file_content(repo_name, file_path, "main")
                else:
                    full_content = client.get_file_content(repo_name, file_path, "main")
            except Exception:
                full_content = ""

            if full_content:
                # Developer agent ile kodu olustur
                code_crew = agile_crew.create_code_crew()
                code_result = code_crew.kickoff(inputs={
                    "work_item_id": work_item_id,
                    "target_repo": repo_name,
                    "target_file": file_path,
                    "change_description": description or requirements_text[:2000],
                    "current_code": full_content[:6000],
                    "new_code": f"[Degisiklik: {description}]",
                    "start_marker": "",
                    "end_marker": "",
                })
                raw_code = _extract_code_from_output(code_result.raw or "")
                if raw_code.strip():
                    ch["new_code"] = raw_code
                    ch["current_code"] = full_content
                    _log(f"    Agent kod uretti: {len(raw_code)} karakter")
                else:
                    _log(f"    Agent bos dondurdu, HAL ciktisi korunuyor")

        enriched.append(ch)

    plan["changes"] = enriched
    return plan


def _md_to_html(md: str) -> str:
    """Basit Markdown → HTML donusumu (Azure DevOps yorumlari icin)."""
    import html as _html
    lines = md.split("\n")
    result = []
    in_list = False
    in_code = False
    for line in lines:
        # Code block
        if line.strip().startswith("```"):
            if in_code:
                result.append("</pre>")
                in_code = False
            else:
                result.append("<pre>")
                in_code = True
            continue
        if in_code:
            result.append(_html.escape(line))
            continue
        # Liste kapat
        if in_list and not line.strip().startswith("- ") and not line.strip().startswith("* "):
            result.append("</ul>")
            in_list = False
        stripped = line.strip()
        if not stripped:
            result.append("<br>")
            continue
        # Headings
        if stripped.startswith("## "):
            result.append(f"<h3>{_html.escape(stripped[3:])}</h3>")
            continue
        if stripped.startswith("# "):
            result.append(f"<h2>{_html.escape(stripped[2:])}</h2>")
            continue
        # Horizontal rule
        if stripped == "---":
            result.append("<hr>")
            continue
        # List items
        if stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                result.append("<ul>")
                in_list = True
            item = stripped[2:]
            # Checkbox
            if item.startswith("[ ] "):
                item = "☐ " + item[4:]
            elif item.startswith("[x] ") or item.startswith("[X] "):
                item = "☑ " + item[4:]
            result.append(f"<li>{_format_inline(item)}</li>")
            continue
        # Normal paragraf
        result.append(f"<p>{_format_inline(stripped)}</p>")
    if in_list:
        result.append("</ul>")
    if in_code:
        result.append("</pre>")
    return "\n".join(result)


def _format_inline(text: str) -> str:
    """Inline markdown: bold, italic, code, link."""
    import html as _html
    # Links: [text](url)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)
    # Inline code: `code`
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    # Bold: **text** veya __text__
    text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'__([^_]+)__', r'<strong>\1</strong>', text)
    # Italic: *text* veya _text_
    text = re.sub(r'\*([^*]+)\*', r'<em>\1</em>', text)
    return text


def _add_wi_comment(client: AzureDevOpsClient, work_item_id: str, text: str):
    """Is kalemine HTML formatlı yorum ekler. Hata olursa sessizce gecer."""
    try:
        html_text = _md_to_html(text)
        client.add_comment(int(work_item_id), html_text)
    except Exception as e:
        _log(f"  Yorum eklenemedi: {e}")


def _get_work_item_title(client: AzureDevOpsClient, work_item_id: str, fallback: str = "Gelistirme") -> str:
    """Work item basligini al."""
    try:
        wi = client.get_work_item(int(work_item_id))
        return (
            wi.get("fields", {}).get("System.Title", "")
            or wi.get("baslik", "")
            or fallback
        )
    except Exception:
        return fallback


# ── Ana Pipeline ──────────────────────────────────

def run_pipeline(work_item_id: str, use_hal: bool = False, tracker: StatusTracker | None = None, job_id: int | None = None):
    """11 adimli tam pipeline. CLI veya server'dan cagrilabilir.

    Orkestrasyon AgileSDLCFlow (CrewAI Flow) tarafindan yapilir.
    Bu fonksiyon geriye uyumlu ince bir wrapper'dir.
    """
    from agile_sdlc_crew import db as _db
    from agile_sdlc_crew.flow import AgileSDLCFlow

    if tracker is None:
        tracker = StatusTracker()

    flow = AgileSDLCFlow()
    flow._tracker = tracker

    try:
        flow.kickoff(inputs={
            "work_item_id": str(work_item_id),
            "use_hal": use_hal,
            "job_id": job_id,
        })
        tracker.finish()

        _log(f"\n{'='*60}")
        _log("  PIPELINE TAMAMLANDI!")
        _log(f"  PR #{flow.state.pr_id}: {flow.state.pr_url}")
        _log(f"{'='*60}")
        return flow.state.pr_url

    except Exception as e:
        tracker.finish()
        if job_id:
            try:
                _db.fail_job(job_id, str(e))
            except Exception:
                pass
        _log(f"\nHata: {e}")
        raise


# ── CLI Entry Points ──────────────────────────────────

def run():
    """CLI'dan pipeline calistir."""
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    use_hal = "--hal" in sys.argv

    if args:
        work_item_id = args[0]
    else:
        work_item_id = input("Azure DevOps Work Item ID: ").strip()

    if not work_item_id:
        _log("Hata: Work Item ID girilmedi.")
        sys.exit(1)

    tracker = StatusTracker()
    server = start_dashboard_server(port=DASHBOARD_PORT)
    _log(f"\n{'='*60}")
    _log(f"  DASHBOARD: http://localhost:{DASHBOARD_PORT}")
    _log(f"{'='*60}\n")
    try:
        webbrowser.open(f"http://localhost:{DASHBOARD_PORT}")
    except Exception:
        pass

    try:
        result = run_pipeline(work_item_id, use_hal=use_hal, tracker=tracker)
        return result
    finally:
        server.shutdown()


def serve():
    """Always-on server baslat."""
    from agile_sdlc_crew.server import main as server_main
    server_main()


def train():
    inputs = {"work_item_id": "0", "target_repo": ""}
    try:
        AgileSDLCCrew().create_analysis_crew().train(
            n_iterations=int(sys.argv[1]) if len(sys.argv) > 1 else 1,
            filename=sys.argv[2] if len(sys.argv) > 2 else "training_data.pkl",
            inputs=inputs,
        )
    except Exception as e:
        raise Exception(f"Egitim hatasi: {e}")


def replay():
    try:
        AgileSDLCCrew().create_analysis_crew().replay(
            task_id=sys.argv[1] if len(sys.argv) > 1 else ""
        )
    except Exception as e:
        raise Exception(f"Replay hatasi: {e}")


def test():
    inputs = {"work_item_id": "0", "target_repo": ""}
    try:
        AgileSDLCCrew().create_analysis_crew().test(
            n_iterations=int(sys.argv[1]) if len(sys.argv) > 1 else 1,
            eval_llm=sys.argv[2] if len(sys.argv) > 2 else None,
            inputs=inputs,
        )
    except Exception as e:
        raise Exception(f"Test hatasi: {e}")


if __name__ == "__main__":
    run()
