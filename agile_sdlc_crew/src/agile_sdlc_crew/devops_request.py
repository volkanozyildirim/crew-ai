"""DevOps ekibinin yapması gereken altyapı değişikliği için ayrı iş.

Kod değişikliği tek başına yetmeyen işlerde (yeni ortam değişkeni / secret, imaja
PHP eklentisi) DevOps takımının alanında, aktif sprintinde bir Task açılır;
kaynak işe Predecessor olarak bağlanır, akışı durdurmaz. Takımın kendi açtığı
"ENV tanımlamaları" işi (#77061) aynı ihtiyacı elle karşılıyordu: hangi repo,
hangi anahtar, hangi ortam — değerler özel kanaldan.

Açılış kuralları:
- PR açılınca diff'ten: repoda daha önce geçmeyen bir ortam değişkeni okunuyorsa
  (`new_env_names`), ya da kimlik dosyası silindiyse.
- Teknik borçta: kural bir PHP eklentisi gerektiriyor ve repoda eklentinin
  kullanıldığına dair kanıt yoksa.
Aynı kaynak iş + aynı istek anahtarı için ikinci iş açılmaz (`tempo-devops-key:`).
"""

from __future__ import annotations

import html
import logging
import re
import subprocess
from pathlib import Path

import yaml

log = logging.getLogger("agile_sdlc_crew.pipeline")

TAG = "tempo-devops"
KEY_PREFIX = "tempo-devops-key:"

# Ortam değişkeni okuma biçimleri (PHP / Go / Python / JS) ve sabite konmuş adlar
# (#244: `const AUTH_ENV_KEY = 'MODERATION_AI_AUTH_JSON'` + getenv(self::AUTH_ENV_KEY)).
_ENV_READ = re.compile(
    r"""(?:\bgetenv|\benv|getEnvOrDefault|os\.Getenv|os\.LookupEnv|os\.getenv|os\.environ\.get)\s*\(\s*['"]([A-Z][A-Z0-9_]{2,})['"]"""
    r"""|\$_(?:ENV|SERVER)\[\s*['"]([A-Z][A-Z0-9_]{2,})['"]\s*\]"""
    r"""|os\.environ\[\s*['"]([A-Z][A-Z0-9_]{2,})['"]\s*\]"""
    r"""|process\.env\.([A-Z][A-Z0-9_]{2,})"""
    r"""|\b\w*ENV\w*\s*(?::=|=)\s*['"]([A-Z][A-Z0-9_]{2,})['"]""")
_SECRET_CONTENT = re.compile(
    r"(?i)\"private_key\"|BEGIN [A-Z ]*PRIVATE KEY|\"client_secret\"|"
    r"\b[\w.-]*(?:secret|token|passw(?:or)?d|api[_-]?key)[\w.-]*[\"']?\s*(?:=>|[:=])\s*[\"']?[^\s\"']{8,}")


def looks_like_secret(text: str) -> bool:
    """Silinen dosya bir kimlik bilgisi mi taşıyordu (#248: analytics.json — adı ele vermiyordu)."""
    return bool(_SECRET_CONTENT.search(text or ""))


def env_names(text: str) -> dict[str, int]:
    """{AD: ilk satır} — metinde okunan ortam değişkeni adları."""
    out: dict[str, int] = {}
    for i, line in enumerate((text or "").splitlines(), 1):
        for m in _ENV_READ.finditer(line):
            name = next(g for g in m.groups() if g)
            out.setdefault(name, i)
    return out


def new_env_names(old: str, new: str, in_repo) -> dict[str, int]:
    """PR'ın bu dosyada eklediği ve repoda başka yerde de geçmeyen adlar.
    `in_repo(name) -> bool`: ad değişmemiş repoda (main) herhangi bir yerde geçiyor mu."""
    before = env_names(old)
    return {n: ln for n, ln in env_names(new).items() if n not in before and not in_repo(n)}


def repo_has_name(root: Path, ref: str, name: str) -> bool:
    try:
        r = subprocess.run(["git", "grep", "-q", "-w", "-F", name, ref, "--", ".", ":!vendor", ":!node_modules"],
                           cwd=str(root), capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:  # noqa: BLE001 — bilinmiyorsa "yeni değil" say (gereksiz iş açma)
        return True


# ── Dağıtım bilgisi (FLODevOps production) ────────────────────────────────

def deployments(client, repo: str, amap: dict | None = None) -> list[dict]:
    """Repo'nun FLODevOps'taki servisleri: [{devrepo, service, secret, images}].
    secret = charts/apps/<servis>/values.yaml externalSecrets.name."""
    from agile_sdlc_crew.quality import app_map
    amap = amap if amap is not None else app_map.load_map(client)
    srcs = sorted({e.get("src", "") for e in amap.values()
                   if isinstance(e, dict) and (e.get("repo") or "").lower() == repo.lower() and e.get("src")})
    out: list[dict] = []
    for src in srcs:
        devrepo, _, path = src.partition("/")
        service = path.rstrip("/").rsplit("/", 1)[-1]
        secret = ""
        try:
            values = yaml.safe_load(app_map._read(client, devrepo, f"/charts/apps/{service}/values.yaml") or "") or {}
            secret = str(((values.get("externalSecrets") or {}).get("name")) or "")
        except Exception:  # noqa: BLE001
            pass
        df = app_map._read(client, devrepo, "/" + path.strip("/") + "/Dockerfile") or ""
        images = re.findall(r"(?im)^FROM\s+(\S+)", df)
        out.append({"devrepo": devrepo, "service": service, "secret": secret, "images": images})
    return out


def _deploy_rows(deps: list[dict]) -> str:
    if not deps:
        return "<p><i>FLODevOps'ta bu repoya bağlı servis bulunamadı — dağıtım yeri ekiple netleştirilmeli.</i></p>"
    rows = "".join(
        f"<tr><td>{html.escape(d['devrepo'])}</td><td>{html.escape(d['service'])}</td>"
        f"<td>{html.escape(d['secret'] or '—')}</td><td>{html.escape(', '.join(d['images']) or '—')}</td></tr>"
        for d in deps)
    return ("<table><tr><th>FLODevOps</th><th>Servis</th><th>externalSecrets</th><th>İmaj</th></tr>"
            f"{rows}</table>")


def env_request_body(*, repo: str, names: dict[str, str], deleted_files: list[str], deps: list[dict],
                     pr_url: str, source_wi: str) -> str:
    """names: {AD: 'dosya:satır'}."""
    items = "".join(f"<li><code>{html.escape(n)}</code> — okunduğu yer: <code>{html.escape(w)}</code></li>"
                    for n, w in names.items())
    gone = "".join(f"<li><code>{html.escape(f)}</code></li>" for f in deleted_files)
    return (
        f"<p><b>Repo:</b> {html.escape(repo)} · <b>PR:</b> <a href=\"{html.escape(pr_url)}\">{html.escape(pr_url)}</a>"
        f" · <b>Kaynak iş:</b> #{html.escape(str(source_wi))}</p>"
        "<p><b>Ne gerekiyor:</b> Aşağıdaki ortam değişkenleri bu PR ile koda girdi ve repoda daha önce "
        "kullanılmıyordu. Uygulamanın çalıştığı her ortamda (prod ve kullanılıyorsa global / russia) "
        "tanımlanmaları gerekiyor; tanımlanmadan PR birleştirilirse ilgili kod çalışma anında hata verir.</p>"
        f"<ul>{items}</ul>"
        + (f"<p><b>Repodan silinen kimlik dosyaları</b> (içerikleri değişkene taşınmalı, tek satır):</p><ul>{gone}</ul>"
           if gone else "")
        + "<p><b>Dağıtım yerleri (FLODevOps production):</b></p>" + _deploy_rows(deps)
        + "<p><b>Değerler bu işte yazılmaz</b> — anahtar sahibinden özel kanaldan alınmalı. Silinen dosya bir "
          "sır taşıyorsa git geçmişinde kaldığı için yeni anahtar üretilip eskisi iptal edilmeli.</p>"
          "<p><b>Bitti sayılması için:</b> değişkenler tüm ortamlarda tanımlı; kaynak işe/PR'a not düşüldü.</p>"
    )


def ext_request_body(*, repo: str, exts: list[str], reason: str, deps: list[dict], source_wi: str) -> str:
    items = "".join(f"<li><code>{html.escape(e)}</code></li>" for e in exts)
    return (
        f"<p><b>Repo:</b> {html.escape(repo)} · <b>Kaynak iş:</b> #{html.escape(str(source_wi))}</p>"
        f"<p><b>Ne gerekiyor:</b> {html.escape(reason)} Aşağıdaki PHP eklentilerinin uygulamanın imajında "
        "yüklü olduğu doğrulanmalı, değilse eklenmeli:</p>"
        f"<ul>{items}</ul><p><b>Dağıtım yerleri (FLODevOps production):</b></p>" + _deploy_rows(deps)
        + "<p><b>Bitti sayılması için:</b> eklenti tüm imajlarda var (ya da zaten vardı); kaynak işe not düşüldü — "
          "kod değişikliği ancak bundan sonra kuyruğa alınabilir.</p>"
    )


# ── İş açma ───────────────────────────────────────────────────────────────

def _existing(client, area: str, key: str) -> int | None:
    k = key.replace("'", "''")
    a = area.replace("'", "''")
    q = ("SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
         f"AND [System.AreaPath] UNDER '{a}' AND [System.Tags] CONTAINS '{TAG}' "
         f"AND [System.Description] CONTAINS '{KEY_PREFIX} {k}'")
    found = client.query_work_items(q, limit=1)
    return int(found[0]["id"]) if found else None


def _link(client, source_wi, devops_id: int, _l) -> None:
    """Kaynak işe Predecessor bağı (DevOps işi önce biter). Zaten bağlıysa Azure reddeder — yutulur."""
    try:
        client.update_work_item(int(source_wi), [{
            "op": "add", "path": "/relations/-",
            "value": {"rel": "System.LinkTypes.Dependency-Reverse",
                      "url": f"{client._base_api_url}/wit/workItems/{int(devops_id)}",
                      "attributes": {"comment": "DevOps altyapı isteği (Tempo)"}},
        }])
    except Exception as e:  # noqa: BLE001
        if "already" not in str(e).lower() and "TF201035" not in str(e):
            _l(f"  🛠️ DevOps isteği bağlanamadı (#{source_wi} → #{devops_id}): {e}")


def open_request(client, *, team: str, title: str, body_html: str, source_wi, key: str,
                 task_fields: dict | None = None, logger=None) -> dict:
    """DevOps alanında aktif sprintte Task açar (zaten varsa onu döner), kaynak işe
    Predecessor bağı + yorum ekler. Dönen: {id, created}."""
    _l = logger or log.info
    area = client.get_team_area_path(team) or ""
    if not area:
        raise ValueError(f"DevOps takımının alanı çözümlenemedi: {team}")
    prev = _existing(client, area, key)
    if prev:
        _l(f"  🛠️ DevOps isteği zaten var: #{prev} ({key})")
        _link(client, source_wi, prev, _l)
        return {"id": prev, "created": False}
    iteration = ""
    for it in client.list_iterations(team) or []:
        if ((it.get("attributes") or {}).get("timeFrame")) == "current":
            iteration = it.get("path") or ""
            break
    fields = {
        "System.Title": title[:250],
        "System.Description": body_html + f"<p><small>{KEY_PREFIX} {html.escape(key)}</small></p>"
                              "<p><small><i>Tempo — DevOps isteği</i></small></p>",
        "System.AreaPath": area,
        "System.Tags": TAG,
        **(task_fields or {}),
    }
    if iteration:
        fields["System.IterationPath"] = iteration
    wi = client.create_work_item("Task", fields)
    new_id = int(wi["id"])
    _link(client, source_wi, new_id, _l)
    _l(f"  🛠️ DevOps isteği açıldı: #{new_id} ({team}{' · ' + iteration.rsplit(chr(92), 1)[-1] if iteration else ''})")
    return {"id": new_id, "created": True}
