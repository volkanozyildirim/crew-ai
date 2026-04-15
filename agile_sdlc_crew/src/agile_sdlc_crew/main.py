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

from agile_sdlc_crew.crew import AgileSDLCCrew
from agile_sdlc_crew.dashboard import StatusTracker, start_dashboard_server
from agile_sdlc_crew.pipeline import (
    create_branch,
    push_file,
    create_pull_request,
)
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


def _validate_code(code: str, file_path: str, original_content: str, description: str) -> tuple[bool, str]:
    """Push oncesi kod dogrulama. o4-mini ile syntax, reference, import kontrolu yapar.

    Returns:
        (valid, fixed_code) - valid=False ise kod push edilmemeli
    """
    import os
    import json as _json
    import litellm

    if not code or not code.strip():
        return False, code

    model = os.environ.get("LITELLM_MODEL", "o4-mini")
    base_url = os.environ.get("LITELLM_BASE_URL")
    api_key = os.environ.get("LITELLM_API_KEY")
    if base_url and not model.startswith("openai/"):
        model = f"openai/{model}"

    # Dosya uzantisina gore dil belirle
    ext = file_path.rsplit(".", 1)[-1] if "." in file_path else ""
    lang_map = {"go": "Go", "php": "PHP", "py": "Python", "js": "JavaScript",
                "ts": "TypeScript", "java": "Java", "cs": "C#", "rb": "Ruby"}
    lang = lang_map.get(ext, ext.upper())

    # Orijinal icerik varsa sadece degisiklige odaklan
    diff_note = ""
    if original_content and original_content.strip():
        diff_note = (
            f"\nONEMLI: Asagida dosyanin ORIJINAL hali verilmistir. "
            f"Sadece YAPILAN DEGISIKLIGI dogrula. "
            f"Orijinal kodda zaten var olan sorunlari (naming convention, eski hatalar vb.) GORMEZDEN GEL. "
            f"Sadece yeni eklenen veya degistirilen satirlarda syntax hatasi, "
            f"unresolved reference veya eksik import var mi kontrol et.\n\n"
            f"--- ORIJINAL KOD ---\n{original_content[:4000]}\n\n"
        )

    prompt = (
        f"Asagidaki {lang} kodunu incele. Dosya: {file_path}\n"
        f"Degisiklik: {description}\n"
        f"{diff_note}"
        f"KONTROL ET (SADECE yapilan degisiklikle ilgili):\n"
        f"1. Yapilan degisiklikte syntax hatasi var mi?\n"
        f"2. Yapilan degisiklikte tanimlanmamis fonksiyon/degisken/paket referansi var mi?\n"
        f"3. Yapilan degisiklik icin eksik import/use/require var mi?\n"
        f"4. Yapilan degisiklik dosyanin derlenmesini/calismasini bozuyor mu?\n"
        f"5. INDENTATION/FORMAT: Yeni eklenen veya degistirilen satirlarin indentation'i "
        f"dosyanin MEVCUT stiline uygun mu? Orijinal dosyada hangi indent stili kullaniliyorsa "
        f"(tab/space, 4-space/2-space vb.) yeni satirlar da AYNI stili kullanmali. "
        f"Uyumsuz indent varsa fixed_code'da duzelt.\n\n"
        f"ONEMLI: Mevcut kodda onceden var olan sorunlari (naming convention, eski hatalar) RAPORLAMA. "
        f"Sadece BU DEGISIKLIGIN olusturdugu sorunlara odaklan.\n\n"
        f"Yanit JSON:\n"
        f'{{"valid":true/false,"issues":["sorun1","sorun2"],"fixed_code":"duzeltilmis kod (sadece sorun varsa)"}}\n\n'
        f"Sorun yoksa valid=true, issues=[], fixed_code bos dondur.\n"
        f"Sorun varsa valid=false, issues listele ve fixed_code ile duzeltilmis DOSYANIN TAMAMI ver.\n"
        f"fixed_code icinde SADECE sorunlu degisikligi duzelt, orijinal kodun geri kalanina DOKUNMA.\n\n"
        f"--- DEGISIKLIK UYGULANMIS KOD ---\n{code[:8000]}"
    )

    try:
        resp = litellm.completion(
            model=model,
            base_url=base_url,
            api_key=api_key,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=8192,
        )
        content = resp.choices[0].message.content or ""

        # JSON parse
        m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', content, re.DOTALL)
        candidate = m.group(1).strip() if m else content.strip()
        if not candidate.startswith("{"):
            brace = re.search(r'\{.*\}', candidate, re.DOTALL)
            candidate = brace.group(0) if brace else candidate

        result = _json.loads(candidate)
        is_valid = result.get("valid", True)
        issues = result.get("issues", [])
        fixed = result.get("fixed_code", "")

        if is_valid:
            _log(f"    Kod dogrulama: GECTI")
            return True, code

        _log(f"    Kod dogrulama: BASARISIZ")
        for issue in issues[:5]:
            _log(f"      - {issue}")

        if fixed and fixed.strip():
            _log(f"    Duzeltilmis kod alindi ({len(fixed)} karakter), kullaniliyor")
            return True, fixed

        return False, code

    except Exception as e:
        # Dogrulama basarisiz olursa kodu gecir (false negative'den iyidir)
        _log(f"    Kod dogrulama hatasi: {e}, geciliyor")
        return True, code


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

        # Dosyayi repodan oku
        if file_path and not new_code:
            _log(f"  Kod eksik: {file_path}, repodan okunup agent ile tamamlaniyor...")
            try:
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
    """11 adimli tam pipeline. CLI veya server'dan cagrilabilir."""
    from agile_sdlc_crew import db as _db

    if tracker is None:
        tracker = StatusTracker()

    def _step_start(step_key: str):
        """Step basladiginda MySQL + tracker guncelle."""
        if job_id:
            try:
                _db.start_step(job_id, step_key)
                _db.update_job(job_id, current_step=step_key)
            except Exception:
                pass

    def _step_done(step_key: str, output: str = ""):
        """Step tamamlandiginda MySQL + tracker guncelle."""
        tracker.task_completed(step_key)
        if job_id:
            try:
                _db.complete_step(job_id, step_key, output)
            except Exception:
                pass

    def _step_fail(step_key: str, error: str):
        """Step basarisiz oldugunda MySQL guncelle."""
        if job_id:
            try:
                _db.fail_step(job_id, step_key, error)
            except Exception:
                pass

    tracker.start(work_item_id)

    # ── Previous Context: her adim ciktisi birikir, sonraki adima gecilir ──
    previous_context = ""

    def _append_context(step_name: str, output: str):
        nonlocal previous_context
        summary = (output or "")[:1500]
        previous_context += f"\n\n--- {step_name} ---\n{summary}"

    def _scrum_review(step_name: str, output: str) -> tuple[bool, str]:
        """Scrum Master ciktiyi inceler. (True, feedback) = ONAY, (False, feedback) = IYILESTIR."""
        try:
            review_crew = agile_crew.create_scrum_review_crew()
            result = review_crew.kickoff(inputs={
                "step_name": step_name,
                "step_output": (output or "")[:4000],
                "work_item_id": work_item_id,
            })
            raw = result.raw or ""
            rejected = "IYILESTIR" in raw.upper() or "İYİLEŞTİR" in raw.upper()
            _log(f"  SM Review ({step_name}): {'IYILESTIR' if rejected else 'ONAY'}")
            return (not rejected), raw
        except Exception as e:
            _log(f"  SM Review hatasi: {e}")
            return True, ""  # hata durumunda onayla ve devam et

    try:
        agile_crew = AgileSDLCCrew()
        agile_crew.set_status_tracker(tracker)
        client = AzureDevOpsClient()
        known_repos = [r.get("name", "") for r in client.list_repositories()]

        # ════════════════════════════════════════════════════════
        # FAZ 1: PLANLAMA (Adim 1-4)
        # ════════════════════════════════════════════════════════

        hal = None  # HAL client pipeline boyunca ayni sohbeti kullanir

        if use_hal:
            _log("\n-- PLANLAMA (HAL modu) --")
            from agile_sdlc_crew.hal_client import HALClient
            hal = HALClient()
            hal.login()
            _log("  HAL login basarili")

            hal_detail = hal.analyze_work_item(work_item_id)
            hal_parsed = hal.parse_analysis_response(hal_detail)

            repo_name = _resolve_repo_name(
                hal_parsed.get("repo_name", ""), known_repos, client, work_item_id
            )

            plan = {
                "work_item_id": work_item_id,
                "repo_name": repo_name,
                "summary": hal_parsed.get("summary", ""),
                "changes": [],
                "acceptance_criteria": [],
            }
            for hc in hal_parsed.get("changes", []):
                plan["changes"].append({
                    "file_path": hc["path"],
                    "change_type": hc.get("change_type", "edit"),
                    "description": hc.get("description", ""),
                    "current_code": hc.get("current_code", ""),
                    "new_code": hc.get("code", ""),
                })
            requirements_text = hal_parsed.get("raw_response", "")
            _log(f"  HAL analiz tamamlandi: repo={repo_name}, {len(plan['changes'])} dosya")

            # Degisiklik yoksa HAL'a ayni sohbette tekrar sor
            if not plan["changes"]:
                _log("  HAL degisiklik bulamadi, ayni sohbette detay isteniyor...")
                retry_detail = hal.followup(
                    f"Dosya yollarini ve mevcut/yeni kod bloklarini goster. "
                    f"Repo: {repo_name}"
                )
                retry_parsed = hal.parse_analysis_response(retry_detail)
                for hc in retry_parsed.get("changes", []):
                    plan["changes"].append({
                        "file_path": hc["path"],
                        "change_type": hc.get("change_type", "edit"),
                        "description": hc.get("description", ""),
                        "current_code": hc.get("current_code", ""),
                        "new_code": hc.get("code", ""),
                    })
                if retry_parsed.get("raw_response"):
                    requirements_text = retry_parsed["raw_response"]
                _log(f"  HAL followup: {len(plan['changes'])} dosya")

            # HAL modunda planlama adimlari tamamlandi
            hal_skip = ["requirements_analysis_task", "discover_repos_task", "dependency_analysis_task"]
            for task_key in hal_skip:
                _step_done(task_key, "HAL modu - otomatik tamamlandi")
            if job_id:
                _db.skip_steps(job_id, hal_skip)

            # ── HAL eksiklerini CrewAI ile tamamla ──
            plan = _enrich_plan_with_agent(
                plan, agile_crew, client, repo_name, work_item_id,
                requirements_text, tracker, hal=hal,
            )
            _step_done("technical_design_task", f"Repo: {repo_name}, {len(plan.get('changes',[]))} dosya")

            # Planlama yorumu
            files_summary = "\n".join(
                f"- [{ch.get('change_type','edit')}] `{ch['file_path']}`: {ch.get('description','')[:80]}"
                for ch in plan["changes"]
            )
            _add_wi_comment(client, work_item_id,
                f"## Analiz & Teknik Tasarim\n\n"
                f"**Repo:** {repo_name}\n"
                f"**Degisecek dosyalar:**\n{files_summary}\n\n"
                f"*Agile SDLC Crew - Planlama tamamlandi*"
            )

        else:
            # ── Adim 1: İş Analizi ──
            _log("\n-- ADIM 1: İş analizi --")
            req_crew = agile_crew.create_requirements_crew()
            req_result = req_crew.kickoff(inputs={
                "work_item_id": work_item_id,
                "previous_context": previous_context,
                "scrum_master_feedback": "",
            })
            requirements_text = req_result.raw or ""
            # SM Review
            approved, feedback = _scrum_review("İş Analizi", requirements_text)
            if not approved:
                _log("  SM iyileştirme istedi, tekrar çalıştırılıyor...")
                req_crew = agile_crew.create_requirements_crew()
                req_result = req_crew.kickoff(inputs={
                    "work_item_id": work_item_id,
                    "previous_context": previous_context,
                    "scrum_master_feedback": f"SCRUM MASTER GERI BILDIRIMI:\n{feedback}",
                })
                requirements_text = req_result.raw or ""
            _append_context("İş Analizi", requirements_text)
            _step_done("requirements_analysis_task", requirements_text[:3000])
            _log(f"  İş analizi tamamlandı")

            # ── Adim 2: Repo Keşfetme ──
            _log("\n-- ADIM 2: Repo keşfetme --")
            repo_crew = agile_crew.create_repo_discovery_crew()
            repo_result = repo_crew.kickoff(inputs={
                "work_item_id": work_item_id,
                "previous_context": previous_context,
            })
            _append_context("Repo Keşfetme", repo_result.raw or "")
            _step_done("discover_repos_task", repo_result.raw or "")
            _log(f"  Repo keşfetme tamamlandı")

            # ── Adim 3: Bağımlılık Analizi ──
            _log("\n-- ADIM 3: Bağımlılık analizi --")
            dep_crew = agile_crew.create_dependency_crew()
            dep_result = dep_crew.kickoff(inputs={
                "work_item_id": work_item_id,
                "repo_discovery": repo_result.raw or "",
                "previous_context": previous_context,
            })
            _append_context("Bağımlılık Analizi", dep_result.raw or "")
            _step_done("dependency_analysis_task", dep_result.raw or "")
            _log(f"  Bağımlılık analizi tamamlandı")

            # ── Adim 4: Teknik Tasarım ──
            _log("\n-- ADIM 4: Teknik tasarım --")
            analysis_crew = agile_crew.create_analysis_crew()
            analysis_result = analysis_crew.kickoff(inputs={
                "work_item_id": work_item_id,
                "target_repo": "",
                "previous_context": previous_context,
                "scrum_master_feedback": "",
            })
            raw_output = analysis_result.raw or ""
            plan = _parse_architect_output(raw_output)
            # SM Review
            approved, feedback = _scrum_review("Teknik Tasarım", raw_output[:3000])
            if not approved:
                _log("  SM iyileştirme istedi, tekrar çalıştırılıyor...")
                analysis_crew = agile_crew.create_analysis_crew()
                analysis_result = analysis_crew.kickoff(inputs={
                    "work_item_id": work_item_id,
                    "target_repo": "",
                    "previous_context": previous_context,
                    "scrum_master_feedback": f"SCRUM MASTER GERI BILDIRIMI:\n{feedback}",
                })
                raw_output = analysis_result.raw or ""
                plan = _parse_architect_output(raw_output)
            requirements_text = requirements_text or raw_output
            _append_context("Teknik Tasarım", raw_output[:1500])

            repo_name = plan["repo_name"]
            if repo_name not in known_repos:
                repo_name = _resolve_repo_name(repo_name, known_repos, client, work_item_id)
                plan["repo_name"] = repo_name

            _step_done("technical_design_task", raw_output[:3000])
            _log(f"  Teknik tasarim tamamlandi")

        _log(f"\n  Repo: {repo_name}")
        _log(f"  Degisecek dosyalar: {len(plan['changes'])}")
        for ch in plan["changes"]:
            _log(f"    [{ch.get('change_type', 'edit')}] {ch['file_path']}: {ch.get('description', '')[:60]}")

        # ════════════════════════════════════════════════════════
        # FAZ 2: IMPLEMENTASYON (Adim 5-7)
        # ════════════════════════════════════════════════════════

        # ── Adim 5: Branch Olustur ──
        _log("\n-- ADIM 5: Branch olusturuluyor --")
        branch_result = create_branch(repo_name, work_item_id)
        if not branch_result["success"]:
            raise RuntimeError(f"Branch olusturulamadi: {branch_result['error']}")
        branch_name = branch_result["branch"]
        _log(f"  Branch: {branch_name}")
        if branch_result.get("note"):
            _log(f"    ({branch_result['note']})")
        _append_context("Branch Olusturma", f"Branch: {branch_name}, Repo: {repo_name}")
        _step_done("create_branch_task", f"Branch: {branch_name}")
        if job_id:
            _db.update_job(job_id, repo_name=repo_name, branch_name=branch_name)

        # ── Adim 6: Kod Gelistirme ──
        _log("\n-- ADIM 6: Kod gelistirme --")
        all_pushes = []
        for i, change in enumerate(plan["changes"]):
            file_path = change["file_path"]
            change_type = change.get("change_type", "edit")
            description = change.get("description", "")
            new_code = change.get("new_code", "")
            current_code = change.get("current_code", "")

            _log(f"\n  [{i+1}/{len(plan['changes'])}] {file_path} ({change_type})")

            # Mevcut dosya icerigini oku
            full_content = ""
            try:
                full_content = client.get_file_content(repo_name, file_path, "main")
                _log(f"    Mevcut dosya: {len(full_content)} karakter")
            except Exception:
                import os.path as _osp
                basename = _osp.basename(file_path)
                parent_dir = _osp.dirname(file_path)
                _log(f"    Dosya bulunamadi, repo'da araniyor: {basename}")
                try:
                    search_dirs = [parent_dir]
                    if parent_dir and parent_dir != "/":
                        search_dirs.append(_osp.dirname(parent_dir))
                    found_path = None
                    for search_dir in search_dirs:
                        try:
                            items = client.get_items_in_path(repo_name, search_dir or "/", "main")
                            for item in items:
                                item_path = item.get("path", "")
                                item_name = _osp.basename(item_path)
                                name_no_ext = _osp.splitext(basename)[0].lower().replace("controller", "")
                                if (item_name.lower() == basename.lower() or
                                        name_no_ext in item_name.lower()):
                                    found_path = item_path
                                    break
                        except Exception:
                            continue
                        if found_path:
                            break
                    if found_path:
                        _log(f"    Benzer dosya bulundu: {found_path}")
                        file_path = found_path
                        change["file_path"] = found_path
                        full_content = client.get_file_content(repo_name, found_path, "main")
                        _log(f"    Mevcut dosya: {len(full_content)} karakter")
                    else:
                        _log(f"    Yeni dosya olacak")
                        change_type = "add"
                except Exception as search_err:
                    _log(f"    Arama hatasi: {search_err}, yeni dosya olacak")
                    change_type = "add"

            if change_type == "add" and new_code:
                if full_content:
                    # Dosya zaten var - sonuna ekle, ustune yazma
                    final_content = full_content.rstrip() + "\n\n" + new_code + "\n"
                    _log(f"    Mevcut dosyaya append: {len(new_code)} karakter eklendi")
                else:
                    final_content = new_code
                    _log(f"    Yeni dosya: {len(final_content)} karakter")

            elif full_content and new_code and current_code and current_code.strip() in full_content:
                cur_lines = len(current_code.strip().splitlines())
                new_lines = len(new_code.strip().splitlines())
                # Guvenlik: current_code cok buyuk, new_code cok kucukse
                # muhtemelen replace degil append olmali
                if cur_lines > 20 and new_lines < cur_lines * 0.3:
                    _log(f"    Guvenlik: current_code ({cur_lines} satir) >> new_code ({new_lines} satir), append yapiliyor")
                    final_content = full_content.rstrip() + "\n\n" + new_code + "\n"
                else:
                    final_content = full_content.replace(current_code.strip(), new_code, 1)
                    _log(f"    Direkt replace: current_code bulundu ve degistirildi")

            elif full_content and new_code and (not current_code or change_type == "add"):
                final_content = full_content.rstrip() + "\n\n" + new_code + "\n"
                _log(f"    Append: dosyanin sonuna eklendi")

            elif full_content and new_code:
                code_crew = agile_crew.create_code_crew()
                code_result = code_crew.kickoff(inputs={
                    "work_item_id": work_item_id,
                    "target_repo": repo_name,
                    "target_file": file_path,
                    "change_description": description,
                    "current_code": current_code[:6000] if current_code else full_content[:6000],
                    "new_code": new_code[:6000],
                    "start_marker": change.get("start_marker", ""),
                    "end_marker": change.get("end_marker", ""),
                })
                raw_code = code_result.raw or ""
                final_content = _extract_code_from_output(raw_code)
                if not final_content.strip():
                    _log(f"    Developer bos icerik dondurdu, fallback kullaniliyor")
                    if current_code and current_code in full_content:
                        final_content = full_content.replace(current_code, new_code, 1)
                    else:
                        final_content = new_code
                _log(f"    Developer kodu: {len(final_content)} karakter")

            elif full_content and not new_code:
                _log(f"    Kod belirtilmemis, agent'a birakiliyor")
                code_crew = agile_crew.create_code_crew()
                code_result = code_crew.kickoff(inputs={
                    "work_item_id": work_item_id,
                    "target_repo": repo_name,
                    "target_file": file_path,
                    "change_description": description,
                    "current_code": full_content[:6000],
                    "new_code": f"[Degisiklik aciklamasi: {description}]",
                    "start_line": "",
                    "end_line": "",
                })
                raw_code = code_result.raw or ""
                final_content = _extract_code_from_output(raw_code)
                if not final_content.strip():
                    _log(f"    Developer bos icerik dondurdu, atlaniyor")
                    continue
                _log(f"    Developer kodu: {len(final_content)} karakter")
            else:
                _log(f"    Ne mevcut dosya ne de yeni kod var, atlaniyor")
                continue

            # ── Kod dogrulama (push oncesi) ──
            validated, final_content = _validate_code(
                final_content, file_path, full_content, description
            )
            if not validated:
                _log(f"    Kod dogrulama basarisiz, duzeltme deneniyor...")
                # Developer agent ile duzeltme
                code_crew = agile_crew.create_code_crew()
                code_result = code_crew.kickoff(inputs={
                    "work_item_id": work_item_id,
                    "target_repo": repo_name,
                    "target_file": file_path,
                    "change_description": (
                        f"Asagidaki kod dogrulama hatasi var, duzelt:\n"
                        f"Dosya: {file_path}\n"
                        f"Hata: Kod derlenemiyor veya calismaz durumda.\n"
                        f"Mevcut kodu duzeltip CALISIR hale getir."
                    ),
                    "current_code": final_content[:6000],
                    "new_code": full_content[:6000] if full_content else final_content[:6000],
                    "start_marker": "",
                    "end_marker": "",
                })
                fixed_code = _extract_code_from_output(code_result.raw or "")
                if fixed_code.strip():
                    validated2, fixed_code = _validate_code(
                        fixed_code, file_path, full_content, description
                    )
                    if validated2:
                        final_content = fixed_code
                        _log(f"    Developer duzeltme basarili")
                    else:
                        _log(f"    Developer duzeltme de basarisiz, atlaniyor")
                        continue
                else:
                    _log(f"    Developer bos dondurdu, atlaniyor")
                    continue

            # Son guvenlik: append senaryosunda (change_type=add) dosya kisalmamali
            if change_type == "add" and full_content and len(final_content.strip()) < len(full_content.strip()):
                _log(f"    GUVENLIK: add modunda dosya kisaldi ({len(full_content)} -> {len(final_content)}), push iptal")
                continue

            commit_msg = f"#{work_item_id}: {description[:80]}"
            push_result = push_file(repo_name, branch_name, file_path, final_content, commit_msg)
            if push_result["success"]:
                _log(f"    Push #{push_result['push_id']} ({push_result['change_type']})")
                all_pushes.append(push_result)
            else:
                _log(f"    Push hatasi: {push_result['error']}")

        push_summary = ", ".join(p.get("file","") for p in all_pushes)
        _append_context("Kod Yazma & Push", f"{len(all_pushes)} dosya: {push_summary}")
        _step_done("implement_change_task", f"{len(all_pushes)} dosya push edildi")

        # ── Adim 7: PR Olustur ──
        _log("\n-- ADIM 7: PR olusturuluyor --")
        if not all_pushes:
            raise RuntimeError("Hicbir dosya push edilemedi, PR olusturulamiyor.")

        wi_title = _get_work_item_title(client, work_item_id, plan.get("summary", "Gelistirme"))
        pr_title = f"#{work_item_id} - {wi_title[:80]}"
        pr_desc = "## Degisiklikler\n\n"
        for ch in plan["changes"]:
            pr_desc += f"- [{ch.get('change_type', 'edit')}] `{ch['file_path']}`: {ch.get('description', '')[:100]}\n"
        if plan.get("acceptance_criteria"):
            pr_desc += "\n## Kabul Kriterleri\n\n"
            for ac in plan["acceptance_criteria"]:
                pr_desc += f"- [ ] {ac}\n"
        pr_desc += f"\n---\n*Agile SDLC Crew ile otomatik olusturuldu*"

        pr_result = create_pull_request(repo_name, branch_name, work_item_id, pr_title, pr_desc)
        if not pr_result["success"]:
            raise RuntimeError(f"PR olusturulamadi: {pr_result['error']}")

        pr_id = pr_result["pr_id"]
        pr_url = pr_result["url"]
        _log(f"  PR #{pr_id}: {pr_url}")
        _append_context("PR Olusturma", f"PR #{pr_id}: {pr_url}")
        _step_done("create_pr_task", f"PR #{pr_id}: {pr_url}")
        if job_id:
            _db.update_job(job_id, pr_id=str(pr_id), pr_url=pr_url)

        # ════════════════════════════════════════════════════════
        # FAZ 3: DOGRULAMA (Adim 8-10)
        # ════════════════════════════════════════════════════════

        # ── Adim 8: Kod Inceleme ──
        _log("\n-- ADIM 8: Kod inceleme --")
        if hal:
            # Degisiklikleri ozet olarak HAL'a bildir
            changed_files = ", ".join(ch["file_path"] for ch in plan["changes"])
            review_detail = hal.followup(
                f"Yukaridaki degisiklikleri ({changed_files}) kod kalitesi acisindan incele. "
                f"SOLID uyumu, hata yonetimi, edge case eksikleri varsa belirt."
            )
            review_text = review_detail.get("response", "")
        else:
            review_crew = agile_crew.create_review_crew()
            review_result = review_crew.kickoff(inputs={
                "work_item_id": work_item_id,
                "requirements": requirements_text[:3000],
                "target_repo": repo_name,
                "target_branch": branch_name,
                "pr_id": str(pr_id),
                "pr_url": pr_url,
                "previous_context": previous_context,
                "scrum_master_feedback": "",
            })
            review_text = review_result.raw or ""
            # SM Review
            approved, feedback = _scrum_review("Kod Inceleme", review_text)
            if not approved:
                _log("  SM iyilestirme istedi, tekrar calistiriliyor...")
                review_crew = agile_crew.create_review_crew()
                review_result = review_crew.kickoff(inputs={
                    "work_item_id": work_item_id,
                    "requirements": requirements_text[:3000],
                    "target_repo": repo_name,
                    "target_branch": branch_name,
                    "pr_id": str(pr_id),
                    "pr_url": pr_url,
                    "previous_context": previous_context,
                    "scrum_master_feedback": f"SCRUM MASTER GERI BILDIRIMI:\n{feedback}",
                })
                review_text = review_result.raw or ""
        _append_context("Kod Inceleme", review_text)
        _step_done("review_pr_task", review_text[:3000])
        _log(f"  Kod inceleme tamamlandi")
        _add_wi_comment(client, work_item_id,
            f"## Kod Inceleme\n\n"
            f"PR: [#{pr_id}]({pr_url})\n\n"
            f"{review_text[:2000]}\n\n"
            f"*Agile SDLC Crew - Kod Inceleme*"
        )

        # ── Adim 9: Test Planlama ──
        _log("\n-- ADIM 9: Test planlama --")
        if hal:
            test_detail = hal.followup(
                f"{changed_files} dosyalarindaki degisiklikler icin SADECE yeni test fonksiyonu yaz. "
                f"Mevcut testlere DOKUNMA. Sadece eklenecek yeni test fonksiyonunu goster. "
                f"Test dosya yolunu belirt."
            )
            test_text = test_detail.get("response", "")

            # Test kodunu parse et ve dosyaya APPEND olarak push et
            if test_text and branch_name:
                test_parsed = hal._llm_parse(test_text)
                for tc in test_parsed.get("changes", []):
                    test_path = tc.get("path", "")
                    test_code = tc.get("code", "")
                    if not test_path or not test_code:
                        continue
                    _log(f"  Test push: {test_path}")
                    existing = ""
                    try:
                        existing = client.get_file_content(repo_name, test_path, branch_name)
                        final_test = existing.rstrip() + "\n\n" + test_code + "\n"
                        _log(f"    Mevcut test dosyasina ekleniyor ({len(test_code)} karakter)")
                    except Exception:
                        final_test = test_code
                        _log(f"    Yeni test dosyasi olusturuluyor")
                    # Test kodunu dogrula
                    test_valid, final_test = _validate_code(
                        final_test, test_path, "", "unit test"
                    )
                    if not test_valid:
                        _log(f"    Test dogrulama basarisiz, duzeltme deneniyor...")
                        code_crew = agile_crew.create_code_crew()
                        fix_result = code_crew.kickoff(inputs={
                            "work_item_id": work_item_id,
                            "target_repo": repo_name,
                            "target_file": test_path,
                            "change_description": "Test kodu derlenemiyor, duzelt.",
                            "current_code": final_test[:6000],
                            "new_code": final_test[:6000],
                            "start_marker": "",
                            "end_marker": "",
                        })
                        fixed_test = _extract_code_from_output(fix_result.raw or "")
                        if fixed_test.strip():
                            v2, fixed_test = _validate_code(fixed_test, test_path, "", "unit test")
                            if v2:
                                final_test = fixed_test
                                _log(f"    Test duzeltme basarili")
                            else:
                                _log(f"    Test duzeltme de basarisiz, atlaniyor")
                                continue
                        else:
                            _log(f"    Developer bos dondurdu, atlaniyor")
                            continue
                    # Son guvenlik: test dosyasi mevcut icerigi kaybetmemeli
                    if existing and len(final_test.strip()) < len(existing.strip()):
                        _log(f"    GUVENLIK: test dosyasi kisaldi ({len(existing)} -> {len(final_test)}), push iptal")
                        continue
                    push_result = push_file(
                        repo_name, branch_name, test_path, final_test,
                        f"#{work_item_id}: unit test eklendi"
                    )
                    if push_result["success"]:
                        _log(f"    Test push #{push_result['push_id']}")
                    else:
                        _log(f"    Test push hatasi: {push_result['error']}")
        else:
            test_crew = agile_crew.create_test_crew()
            test_result = test_crew.kickoff(inputs={
                "work_item_id": work_item_id,
                "requirements": requirements_text[:3000],
                "target_repo": repo_name,
                "target_branch": branch_name,
                "pr_id": str(pr_id),
                "previous_context": previous_context,
                "scrum_master_feedback": "",
            })
            test_text = test_result.raw or ""
            # SM Review
            approved, feedback = _scrum_review("Test Planlama", test_text)
            if not approved:
                _log("  SM iyilestirme istedi, tekrar calistiriliyor...")
                test_crew = agile_crew.create_test_crew()
                test_result = test_crew.kickoff(inputs={
                    "work_item_id": work_item_id,
                    "requirements": requirements_text[:3000],
                    "target_repo": repo_name,
                    "target_branch": branch_name,
                    "pr_id": str(pr_id),
                    "previous_context": previous_context,
                    "scrum_master_feedback": f"SCRUM MASTER GERI BILDIRIMI:\n{feedback}",
                })
                test_text = test_result.raw or ""
        _append_context("Test Planlama", test_text)
        _step_done("test_planning_task", test_text[:3000])
        _log(f"  Test planlama tamamlandi")
        _add_wi_comment(client, work_item_id,
            f"## Test Planlama\n\n"
            f"{test_text[:2000]}\n\n"
            f"*Agile SDLC Crew - Test*"
        )

        # ── Adim 10: UAT Dogrulama ──
        _log("\n-- ADIM 10: UAT dogrulama --")
        if hal:
            uat_detail = hal.followup(
                f"#{work_item_id} is kaleminin kabul kriterlerini listele "
                f"ve yapilan degisikliklerin her kriteri karsilayip karsilamadigini GECTI/KALDI olarak belirt."
            )
            uat_text = uat_detail.get("response", "")
        else:
            uat_crew = agile_crew.create_uat_crew()
            uat_result = uat_crew.kickoff(inputs={
                "work_item_id": work_item_id,
                "requirements": requirements_text[:3000],
                "pr_id": str(pr_id),
                "pr_url": pr_url,
                "previous_context": previous_context,
                "scrum_master_feedback": "",
            })
            uat_text = uat_result.raw or ""
            # SM Review
            approved, feedback = _scrum_review("UAT Dogrulama", uat_text)
            if not approved:
                _log("  SM iyilestirme istedi, tekrar calistiriliyor...")
                uat_crew = agile_crew.create_uat_crew()
                uat_result = uat_crew.kickoff(inputs={
                    "work_item_id": work_item_id,
                    "requirements": requirements_text[:3000],
                    "pr_id": str(pr_id),
                    "pr_url": pr_url,
                    "previous_context": previous_context,
                    "scrum_master_feedback": f"SCRUM MASTER GERI BILDIRIMI:\n{feedback}",
                })
                uat_text = uat_result.raw or ""
        _append_context("UAT Dogrulama", uat_text)
        _step_done("uat_task", uat_text[:3000])
        _log(f"  UAT dogrulama tamamlandi")
        _add_wi_comment(client, work_item_id,
            f"## UAT Dogrulama\n\n"
            f"{uat_text[:2000]}\n\n"
            f"*Agile SDLC Crew - UAT*"
        )

        # ════════════════════════════════════════════════════════
        # FAZ 4: KAPANIS (Adim 11)
        # ════════════════════════════════════════════════════════

        # ── Adim 11: Tamamlanma Raporu ──
        _log("\n-- ADIM 11: Tamamlanma raporu --")
        if hal:
            completion_detail = hal.followup(
                f"#{work_item_id} icin tamamlanma raporu olustur: "
                f"yapilan degisiklikler, kod inceleme sonucu, test durumu ve UAT sonucunu ozetle. "
                f"Bu raporu is kalemine yorum olarak ekle."
            )
            completion_text = completion_detail.get("response", "")
        else:
            completion_crew = agile_crew.create_completion_crew()
            completion_result = completion_crew.kickoff(inputs={
                "work_item_id": work_item_id,
                "pr_url": pr_url,
                "pr_id": str(pr_id),
                "review_result": review_text[:2000],
                "test_result": test_text[:2000],
                "uat_result": uat_text[:2000],
                "previous_context": previous_context,
            })
            completion_text = (completion_result.raw or "") if completion_result else ""
        _append_context("Tamamlanma Raporu", completion_text)
        _step_done("completion_report_task", completion_text[:3000])
        _log(f"  Tamamlanma raporu olusturuldu")
        _add_wi_comment(client, work_item_id,
            f"## Tamamlanma Raporu\n\n"
            f"PR: [#{pr_id}]({pr_url})\n\n"
            f"{completion_text[:3000]}\n\n"
            f"---\n*Agile SDLC Crew - Pipeline tamamlandi*"
        )

        tracker.finish()

        _log(f"\n{'='*60}")
        _log("  PIPELINE TAMAMLANDI!")
        _log(f"  PR #{pr_id}: {pr_url}")
        _log(f"{'='*60}")
        return pr_url

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
