"""SonarQube toplayıcısı: açık BUG + VULNERABILITY (BLOCKER/CRITICAL) maddeleri.

Proje anahtarı `EcomBackend:webservice` biçiminde → repo doğrudan. Kapsam,
anahtarın önekinin AZURE_DEVOPS_REPO_PROJECTS içinde olmasıdır (FLOXO,
FloxoPLM vb. atlanır). secrets:* maddelerinin mesajı SAKLANMAZ — değer içerebilir.

Adres yalnızca ortamdan (.env CREW_SONAR_URL) okunur, dashboard knob'u DEĞİL:
SONAR_TOKEN bu adrese gönderilir; dashboard'dan değiştirilebilseydi token başka
bir sunucuya sızdırılabilirdi. Aynı nedenle https olmayan adrese istek atılmaz.
"""

from __future__ import annotations

import os
from urllib.parse import quote

import requests

from agile_sdlc_crew.quality.model import Finding, QualitySourceError

TYPES = "BUG,VULNERABILITY"
SEVERITIES = "BLOCKER,CRITICAL"
STATUSES = "OPEN,CONFIRMED"
DEFAULT_URL = "https://sonarqube.flo.com.tr"


def base_url() -> str:
    return str(os.environ.get("CREW_SONAR_URL") or DEFAULT_URL).rstrip("/")


def _get(path: str, params: dict) -> dict:
    tok = os.environ.get("SONAR_TOKEN", "")
    if not tok:
        raise QualitySourceError("SONAR_TOKEN tanımlı değil")
    if not base_url().startswith("https://"):
        raise QualitySourceError(f"Sonar adresi https değil, token gönderilmedi: {base_url()}")
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
