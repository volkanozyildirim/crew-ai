"""Fazli pipeline - akilli icerik cikarma ve merge."""

import logging
import re
import time

import requests as _requests

from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient

log = logging.getLogger("pipeline")


# ── Icerik cikarma ──

def extract_relevant_section(
    full_content: str,
    keywords: list[str],
    context_lines: int = 30,
) -> tuple[str, int, int]:
    """Dosya iceriginden anahtar kelimelere en yakin bolumu cikarir.
    Returns: (section_text, start_line, end_line)
    """
    lines = full_content.split("\n")
    if not lines:
        return full_content, 0, 0

    # Anahtar kelimelerin gectiği satirlari bul
    match_lines = set()
    for i, line in enumerate(lines):
        line_lower = line.lower()
        for kw in keywords:
            if kw.lower() in line_lower:
                match_lines.add(i)

    if not match_lines:
        return "\n".join(lines[:80]), 0, min(80, len(lines))

    min_line = max(0, min(match_lines) - context_lines)
    max_line = min(len(lines), max(match_lines) + context_lines)
    section = "\n".join(lines[min_line:max_line])
    return section, min_line, max_line


def merge_section_into_file(
    full_content: str,
    new_section: str,
    start_line: int,
    end_line: int,
) -> str:
    """Degistirilen bolumu orijinal dosyaya geri birlestirir."""
    lines = full_content.split("\n")
    new_lines = new_section.split("\n")
    merged = lines[:start_line] + new_lines + lines[end_line:]
    return "\n".join(merged)


def find_repo_name(all_text: str, known_repos: list[str]) -> str | None:
    """Agent ciktilarinda bilinen repo adlarini arar."""
    text_lower = all_text.lower()
    counts = {}
    for repo in known_repos:
        c = text_lower.count(repo.lower())
        if c > 0:
            counts[repo] = c
    if counts:
        return max(counts, key=counts.get)
    return None


def parse_file_changes(design_text: str, repo_name: str) -> list[dict]:
    """Teknik tasarim ciktisindaki dosya yollarini cikarir."""
    changes = []
    seen_paths = set()
    code_ext = r'\.(php|js|ts|py|java|cs|go|rb|jsx|tsx|vue|json|yaml|yml|xml|env|sql|sh)$'
    for line in design_text.split("\n"):
        paths = re.findall(r'(/?[a-zA-Z0-9_./:-]+\.[a-zA-Z]{1,10})', line)
        for path in paths:
            if not re.search(code_ext, path, re.I):
                continue
            if not path.startswith("/"):
                path = "/" + path
            if path not in seen_paths:
                seen_paths.add(path)
                desc = line.strip().strip("-*[] ").strip()
                changes.append({"path": path, "description": desc[:120]})
    return changes


def extract_example_from_description(description: str) -> dict:
    """Work item description'dan ornek dosya ve kod bilgisi cikarir."""
    result = {"example_file": "", "example_method": "", "example_code": ""}

    # Ornek dosya yolu ara (app/Controller/... gibi)
    file_match = re.search(r'(app/[a-zA-Z0-9_/]+\.php|src/[a-zA-Z0-9_/]+\.\w+)', description)
    if file_match:
        result["example_file"] = "/" + file_match.group(1)

    # Metod adi ara (- methodName veya :: methodName)
    method_match = re.search(r'[-:]\s*(\w+Action|\w+Method|\w+)\s*$', description, re.M)
    if method_match:
        result["example_method"] = method_match.group(1)

    return result


# ── Git operasyonlari ──

def create_branch(repo_name: str, work_item_id: str) -> dict:
    client = AzureDevOpsClient()
    branch_name = f"feature/{work_item_id}"

    # Önce bu branch'te farklı bir iş için aktif PR var mı kontrol et
    existing = _find_existing_pr(client, repo_name, branch_name)
    if existing and existing.get("work_item_ids"):
        pr_wis = [str(w) for w in existing["work_item_ids"]]
        if str(work_item_id) not in pr_wis:
            # Farklı iş için PR var → alternatif branch oluştur
            alt_suffix = int(time.time()) % 10000
            branch_name = f"feature/{work_item_id}-{alt_suffix}"
            log.info(f"  Branch {f'feature/{work_item_id}'} başka iş için PR #{existing.get('pr_id')} (WI {pr_wis}) ile kullanılıyor, alternatif: {branch_name}")
        else:
            log.info(f"  Branch'te aynı iş için mevcut PR #{existing.get('pr_id')} var, devam ediliyor")

    try:
        client.create_branch(repo_name, branch_name)
        return {"success": True, "branch": branch_name, "repo": repo_name}
    except Exception as e:
        if "already exists" in str(e).lower() or "TF402455" in str(e):
            return {"success": True, "branch": branch_name, "repo": repo_name, "note": "zaten mevcut"}
        return {"success": False, "error": str(e)}


def push_file(repo_name: str, branch: str, file_path: str, content: str, message: str, repo_mgr=None) -> dict:
    client = AzureDevOpsClient()
    try:
        # Dosya varlik kontrolu: local repo varsa filesystem'den, yoksa API'den
        if repo_mgr:
            change_type = "edit" if repo_mgr.file_exists(repo_name, file_path, branch) else "add"
        else:
            try:
                client.get_file_content(repo_name, file_path, branch)
                change_type = "edit"
            except Exception:
                change_type = "add"
        changes = [{"changeType": change_type, "path": file_path, "content": content}]
        result = client.push_changes(repo_name, branch, changes, message)
        return {"success": True, "push_id": result.get("pushId", "?"), "change_type": change_type, "file": file_path}
    except Exception as e:
        return {"success": False, "error": str(e)}


def create_pull_request(repo_name: str, branch: str, work_item_id: str, title: str, description: str) -> dict:
    client = AzureDevOpsClient()
    try:
        result = client.create_pull_request(
            repo_name, branch, "main", title, description,
            [int(work_item_id)] if work_item_id else None,
        )
        pr_id = result.get("pullRequestId")
        repo_data = result.get("repository", {})
        project = repo_data.get("project", {}).get("name", "")
        repo = repo_data.get("name", repo_name)
        web_url = f"{client.org_url}/{project}/_git/{repo}/pullrequest/{pr_id}"
        return {"success": True, "pr_id": pr_id, "url": web_url}
    except Exception as e:
        error_str = str(e)
        # 409 Conflict: ayni branch'ten aktif PR zaten var, mevcut PR'i bul
        if "409" in error_str or "Conflict" in error_str:
            existing = _find_existing_pr(client, repo_name, branch)
            if existing:
                return existing
        return {"success": False, "error": error_str}


def _find_existing_pr(client: AzureDevOpsClient, repo_name: str, branch: str) -> dict | None:
    """Ayni branch'ten acik bir PR varsa bilgilerini dondurur (work item ID'leri dahil)."""
    try:
        proj = client._find_repo_project(repo_name)
        url = f"{client._project_api_url(proj)}/git/repositories/{repo_name}/pullrequests"
        params = {
            "searchCriteria.sourceRefName": f"refs/heads/{branch}",
            "searchCriteria.status": "active",
            "api-version": client.API_VERSION,
        }
        resp = _requests.get(url, headers=client._headers, params=params, timeout=30)
        resp.raise_for_status()
        prs = resp.json().get("value", [])
        if prs:
            pr = prs[0]
            pr_id = pr["pullRequestId"]
            repo_data = pr.get("repository", {})
            project = repo_data.get("project", {}).get("name", "")
            repo = repo_data.get("name", repo_name)
            web_url = f"{client.org_url}/{project}/_git/{repo}/pullrequest/{pr_id}"

            # PR'a bağlı work item ID'lerini al
            wi_ids = []
            try:
                wi_url = f"{client._project_api_url(proj)}/git/repositories/{repo_name}/pullrequests/{pr_id}/workitems"
                wi_params = {"api-version": client.API_VERSION}
                wi_resp = _requests.get(wi_url, headers=client._headers, params=wi_params, timeout=15)
                if wi_resp.ok:
                    for wi in wi_resp.json().get("value", []):
                        wi_ids.append(str(wi.get("id", "")))
            except Exception:
                pass

            return {
                "success": True,
                "pr_id": pr_id,
                "url": web_url,
                "title": pr.get("title", ""),
                "work_item_ids": wi_ids,
                "note": "mevcut PR kullanıldı",
            }
    except Exception:
        pass
    return None
