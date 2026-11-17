import base64
import os
import re

import requests


# Azure DevOps PR yorumlarında markdown ":/", ":(", ":D" gibi ifadeleri emojiye
# çevirir. Reviewer bir değeri tırnak içinde yazınca (PR #44918: "host olmayan
# canonical için ':/' ve '://en'") metin "'😕/'" olarak göründü ve anlam
# kayboldu. Kod dışındaki bu tür sözcükler satır içi koda alınır — kodda
# dönüşüm yapılmaz; URL'ler (harften sonra gelen ':/') etkilenmez.
_EMOTICON_TOKEN = re.compile(r"[^\s`|]*(?<![A-Za-z0-9\-|]):[/\\()DPpOo][^\s`|]*")
_TRAIL_PUNCT = ",.;!?)"


def _defuse_segment(seg: str) -> str:
    def wrap(m: re.Match) -> str:
        tok = m.group(0)
        core = tok.rstrip(_TRAIL_PUNCT) if tok.rstrip(_TRAIL_PUNCT) else tok
        return f"`{core}`{tok[len(core):]}"
    return _EMOTICON_TOKEN.sub(wrap, seg)


def defuse_emoticons(md: str) -> str:
    """Kod blokları ve satır içi kod hariç, emojiye dönüşecek sözcükleri koda al."""
    if not md or ":" not in md:
        return md or ""
    out = []
    for i, block in enumerate(re.split(r"(```.*?```)", md, flags=re.S)):
        if i % 2:
            out.append(block)
            continue
        parts = re.split(r"(`[^`]*`)", block)
        out.append("".join(p if j % 2 else _defuse_segment(p) for j, p in enumerate(parts)))
    return "".join(out)


_TS_PREFIX = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ?")
_LOG_NOISE = re.compile(
    r"^(##\[(section|command|group|endgroup)\]|={10,}|Task\s+:|Description\s+:|Version\s+:|"
    r"Author\s+:|Help\s+:|Generating script|Evaluating:|Expanded:|Result:)")
_LOG_ERROR = re.compile(
    r"(\.(go|php|py|js|ts|tsx|java|cs|rb)[:(]\d+|--- FAIL|^FAIL\b|panic:|Fatal error|"
    r"\b[Ee]rror\b|Exception|AssertionError|Failures?:|undefined|cannot |not found|exit code)")
_PKG_FAIL = re.compile(r"^FAIL\s+\S+\s+\[build failed\]$")


def extract_build_errors(log_text: str, max_lines: int = 80) -> str:
    """CI Task logundan geliştiriciye gereken kısım: zaman damgası ve şablon
    gürültüsü atılır, hata satırları (dosya:satır, FAIL, panic, Error…) bir satır
    bağlamla önce gelir, `FAIL pkg [build failed]` yığını tek satıra iner, sonra
    kuyruk. #248'de derleyici hataları logun BAŞINDAYDI, kuyrukta yalnız 60 paketlik
    FAIL listesi vardı."""
    lines = [_TS_PREFIX.sub("", ln.rstrip()) for ln in (log_text or "").splitlines()]
    lines = [ln for ln in lines if ln.strip() and not _LOG_NOISE.match(ln)]
    pkg_fail = [ln for ln in lines if _PKG_FAIL.match(ln)]
    body = [ln for ln in lines if not _PKG_FAIL.match(ln)]
    keep: list[int] = []
    for i, ln in enumerate(body):
        if _LOG_ERROR.search(ln):
            keep += [j for j in (i - 1, i, i + 1) if 0 <= j < len(body)]
    picked = [body[i] for i in dict.fromkeys(keep)][:max_lines]
    out = picked
    if pkg_fail:
        out.append(f"({len(pkg_fail)} paket 'build failed': {', '.join(l.split()[1] for l in pkg_fail[:5])}"
                   + (", …" if len(pkg_fail) > 5 else "") + ")")
    tail = [ln for ln in body[-15:] if ln not in picked]
    if tail:
        out += ["--- son satırlar ---", *tail]
    return "\n".join(out)


PR_DESCRIPTION_MAX = 4000  # Azure DevOps PR açıklaması sınırı


class AzureDevOpsClient:
    """Azure DevOps REST API icin paylasilan HTTP istemcisi."""

    API_VERSION = "7.1"

    def __init__(self):
        # Credentials store > env fallback. Dashboard "Kimlik Bilgileri > Is Yonetimi
        # > azure_devops" altindan yonetilir; env hala calismaya devam eder.
        from agile_sdlc_crew import credentials
        creds = credentials.get_all("work_item", "azure_devops") or {}

        self.org_url = (creds.get("org_url") or os.environ.get("AZURE_DEVOPS_ORG_URL", "")).rstrip("/")
        self.pat = creds.get("pat") or os.environ.get("AZURE_DEVOPS_PAT", "")
        self.project = creds.get("project") or os.environ.get("AZURE_DEVOPS_PROJECT", "")

        repo_projects_raw = creds.get("repo_projects") or os.environ.get("AZURE_DEVOPS_REPO_PROJECTS", "")
        self.repo_projects = (
            [p.strip() for p in repo_projects_raw.split(",") if p.strip()]
            if repo_projects_raw else [self.project]
        )
        self.team = (creds.get("team") or os.environ.get("AZURE_DEVOPS_TEAM", "")).strip()

        self._repo_project_cache: dict[str, str] = {}

        if not all([self.org_url, self.pat, self.project]):
            raise ValueError(
                "Azure DevOps kimlik bilgileri eksik. Dashboard 'Kimlik Bilgileri > "
                "Is Yonetimi > azure_devops' altindan girin veya AZURE_DEVOPS_ORG_URL/PAT/PROJECT "
                "env degiskenlerini ayarlayin."
            )

    @property
    def _headers(self) -> dict:
        token = base64.b64encode(f":{self.pat}".encode()).decode()
        return {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        }

    @property
    def _patch_headers(self) -> dict:
        token = base64.b64encode(f":{self.pat}".encode()).decode()
        return {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json-patch+json",
        }

    @property
    def _base_api_url(self) -> str:
        return f"{self.org_url}/{self.project}/_apis"

    def get_work_item(self, work_item_id: int) -> dict:
        url = f"{self._base_api_url}/wit/workitems/{work_item_id}"
        params = {"$expand": "all", "api-version": self.API_VERSION}
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def download_attachment(self, url: str, timeout: int = 30) -> bytes:
        """Azure DevOps attachment veya iç kaynak URL'sini PAT ile indir."""
        resp = requests.get(url, headers=self._headers, timeout=timeout, verify=False)
        resp.raise_for_status()
        return resp.content

    def update_work_item(self, work_item_id: int, operations: list[dict]) -> dict:
        url = f"{self._base_api_url}/wit/workitems/{work_item_id}"
        params = {"api-version": self.API_VERSION}
        resp = requests.patch(
            url, headers=self._patch_headers, json=operations, params=params, timeout=30
        )
        resp.raise_for_status()
        return resp.json()

    def get_work_item_comments(self, work_item_id: int) -> list[dict]:
        """Bir work item'in yorumlarini getirir."""
        url = f"{self._base_api_url}/wit/workitems/{work_item_id}/comments"
        params = {"api-version": "7.1-preview.4"}
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        comments = resp.json().get("comments", [])
        return [
            {
                "id": c.get("id"),
                "yazar": c.get("createdBy", {}).get("displayName", ""),
                "tarih": c.get("createdDate", ""),
                "metin": c.get("text", ""),
            }
            for c in comments
        ]

    def get_work_item_comments_all(self, work_item_id: int, max_items: int = 1000) -> tuple[list[dict], bool]:
        """Bütün yorum sayfaları (continuationToken ile), get_work_item_comments ile aynı satır biçimi.
        Döner: (yorumlar, tamam). max_items aşılırsa okuma durur ve tamam=False — işaret arayan çağıran
        'bilinmiyor' sayar (yorumu tekrar yazmaz)."""
        url = f"{self._base_api_url}/wit/workitems/{work_item_id}/comments"
        out: list[dict] = []
        token = None
        while True:
            params = {"api-version": "7.1-preview.4", "$top": 200}
            if token:
                params["continuationToken"] = token
            resp = requests.get(url, headers=self._headers, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json() or {}
            page = data.get("comments", []) or []
            out += [{"id": c.get("id"), "yazar": c.get("createdBy", {}).get("displayName", ""),
                     "tarih": c.get("createdDate", ""), "metin": c.get("text", "")}
                    for c in page]
            token = data.get("continuationToken")
            if not token:
                return out, True
            if not page or len(out) >= max_items:   # boş sayfa + belirteç: döngüye girme
                return out[:max_items], False

    def add_comment(self, work_item_id: int, text: str) -> dict:
        url = f"{self._base_api_url}/wit/workitems/{work_item_id}/comments"
        params = {"api-version": "7.1-preview.4"}
        resp = requests.post(
            url, headers=self._headers, json={"text": text}, params=params, timeout=30
        )
        resp.raise_for_status()
        return resp.json()

    def update_comment(self, work_item_id: int, comment_id: int, text: str) -> dict:
        """Mevcut WI yorumunun metnini degistirir (yanlis render edilen yorumu
        duzeltmek icin; yeni yorum eklemek yerine — duplikasyon olmasin)."""
        url = f"{self._base_api_url}/wit/workitems/{work_item_id}/comments/{comment_id}"
        params = {"api-version": "7.1-preview.4"}
        resp = requests.patch(
            url, headers=self._headers, json={"text": text}, params=params, timeout=30
        )
        resp.raise_for_status()
        return resp.json()

    # ── WI yasam dongusu (wi_lifecycle.py) ──

    def get_work_item_type_states(self, type_name: str) -> list[dict]:
        """Bir WI tipinin surecteki durumlari: [{name, category, color}].
        category ∈ Proposed | InProgress | Resolved | Completed | Removed."""
        from urllib.parse import quote
        url = f"{self._base_api_url}/wit/workitemtypes/{quote(type_name)}/states"
        params = {"api-version": "7.1"}
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("value", []) or []

    def set_work_item_state(self, work_item_id: int, state: str) -> dict:
        """System.State yaz (surec kurallari gecise izin vermiyorsa Azure 400 doner)."""
        return self.update_work_item(work_item_id, [
            {"op": "add", "path": "/fields/System.State", "value": state},
        ])

    def get_authenticated_user(self) -> dict:
        """PAT sahibi: {id, displayName, uniqueName}."""
        url = f"{self.org_url}/_apis/connectionData"
        params = {"api-version": "7.1-preview.1"}
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        u = resp.json().get("authenticatedUser", {}) or {}
        props = u.get("properties") or {}
        account = (props.get("Account") or {}).get("$value", "")
        return {
            "id": u.get("id", ""),
            "displayName": u.get("providerDisplayName", "") or u.get("customDisplayName", ""),
            "uniqueName": account,
        }

    def assign_work_item(self, work_item_id: int, identity: str) -> dict:
        """System.AssignedTo = identity (uniqueName / e-posta / displayName)."""
        return self.update_work_item(work_item_id, [
            {"op": "add", "path": "/fields/System.AssignedTo", "value": identity},
        ])

    # ── Alt is kayitlari (wi_children.py) ──

    def create_work_item(self, type_name: str, fields: dict, parent_id: int | None = None) -> dict:
        """Yeni WI (JSON-patch). parent_id verilirse Hierarchy-Reverse iliskisiyle
        parent'in altina baglanir. Doner: Azure WI JSON (id, fields, ...)."""
        from urllib.parse import quote
        ops = [{"op": "add", "path": f"/fields/{k}", "value": v} for k, v in fields.items()]
        if parent_id:
            ops.append({
                "op": "add", "path": "/relations/-",
                "value": {
                    "rel": "System.LinkTypes.Hierarchy-Reverse",
                    "url": f"{self._base_api_url}/wit/workItems/{int(parent_id)}",
                },
            })
        url = f"{self._base_api_url}/wit/workitems/${quote(type_name)}"
        params = {"api-version": self.API_VERSION}
        resp = requests.post(url, headers=self._patch_headers, json=ops, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_work_item_children(self, parent_id: int) -> list[dict]:
        """Parent'in Hierarchy-Forward cocuklari (id + fields: Title, State, Tags, WorkItemType)."""
        wi = self.get_work_item(int(parent_id))
        ids = []
        for rel in (wi or {}).get("relations", []) or []:
            if rel.get("rel") == "System.LinkTypes.Hierarchy-Forward":
                try:
                    ids.append(int(str(rel.get("url", "")).rstrip("/").rsplit("/", 1)[-1]))
                except ValueError:
                    continue
        if not ids:
            return []
        url = f"{self._base_api_url}/wit/workitemsbatch"
        body = {"ids": ids[:200], "fields": ["System.Id", "System.Title", "System.State",
                                             "System.Tags", "System.WorkItemType"]}
        resp = requests.post(url, headers={**self._headers, "Content-Type": "application/json"},
                             json=body, params={"api-version": self.API_VERSION}, timeout=30)
        resp.raise_for_status()
        return resp.json().get("value", []) or []

    # ── Git / Repo API'leri ──

    def _project_api_url(self, project: str) -> str:
        return f"{self.org_url}/{project}/_apis"

    def list_repositories(self) -> list[dict]:
        """Tum repo projelerindeki Git repolarini listeler."""
        all_repos = []
        for proj in self.repo_projects:
            url = f"{self._project_api_url(proj)}/git/repositories"
            params = {"api-version": self.API_VERSION}
            resp = requests.get(url, headers=self._headers, params=params, timeout=30)
            resp.raise_for_status()
            repos = resp.json().get("value", [])
            for repo in repos:
                repo["_project"] = proj
            all_repos.extend(repos)
        return all_repos

    def _find_repo_project(self, repo_id_or_name: str) -> str:
        """Repo ID veya adina gore hangi projede oldugunu bulur (cache'li)."""
        if repo_id_or_name in self._repo_project_cache:
            return self._repo_project_cache[repo_id_or_name]
        for proj in self.repo_projects:
            try:
                url = f"{self._project_api_url(proj)}/git/repositories/{repo_id_or_name}"
                params = {"api-version": self.API_VERSION}
                resp = requests.get(url, headers=self._headers, params=params, timeout=10)
                if resp.status_code == 200:
                    self._repo_project_cache[repo_id_or_name] = proj
                    return proj
            except Exception:
                continue
        fallback = self.repo_projects[0] if self.repo_projects else self.project
        self._repo_project_cache[repo_id_or_name] = fallback
        return fallback

    def _repo_project_strict(self, repo_id_or_name: str) -> str:
        """Reponun projesi — yalnız KESİN ve TEKİL bulunursa. _find_repo_project'in aksine hata yutmaz, yedek
        projeye düşmez ve onun önbelleğini ne okur ne yazar (o önbellekte yedek değer olabilir): bir kapının
        kanıtı yanlış projede aranıp "yok" okunmasın. Her yapılandırılmış proje sorulur: yalnız HTTP 200 + repo
        `id`'si olan JSON olumludur; 404 olumsuz; başka her yanıt (203 oturum sayfası, 401, 5xx, JSON değil)
        ve bağlantı hatası yükselir. Tek olumlu → o proje; birden çok → ValueError (aynı adlı repo iki projede
        — hangisi olduğu bilinmeden aranmaz); hiç yok → ValueError. Olumlu sonuç ayrı önbellekte tutulur."""
        found = getattr(self, "_repo_project_found", None)
        if found is None:
            found = self._repo_project_found = {}
        if repo_id_or_name in found:
            return found[repo_id_or_name]
        hits = []
        for proj in self.repo_projects:
            url = f"{self._project_api_url(proj)}/git/repositories/{repo_id_or_name}"
            resp = requests.get(url, headers=self._headers, params={"api-version": self.API_VERSION}, timeout=10)
            if resp.status_code == 404:
                continue
            if resp.status_code != 200:
                raise ValueError(f"'{repo_id_or_name}' reposu {proj} projesinde sorgulanamadı "
                                 f"(HTTP {resp.status_code}).")
            try:
                body = resp.json()
            except ValueError:
                body = None
            if not isinstance(body, dict) or not body.get("id"):
                raise ValueError(f"'{repo_id_or_name}' reposu {proj} projesinde sorgulanamadı (yanıt repo değil).")
            hits.append(proj)
        if len(hits) > 1:
            raise ValueError(f"'{repo_id_or_name}' adlı repo birden çok projede var ({', '.join(hits)}) — test "
                             "hattı tanımının hangi projede aranacağı belirsiz.")
        if not hits:
            raise ValueError(f"'{repo_id_or_name}' reposu Azure projelerinde bulunamadı "
                             f"({', '.join(self.repo_projects)})")
        found[repo_id_or_name] = hits[0]
        return hits[0]

    def _repo_api_url(self, repo_id_or_name: str, project: str | None = None) -> str:
        proj = project or self._find_repo_project(repo_id_or_name)
        return f"{self._project_api_url(proj)}/git/repositories/{repo_id_or_name}"

    def get_repository(self, repo_id_or_name: str) -> dict:
        """Tek bir reponun detaylarini getirir."""
        url = f"{self._repo_api_url(repo_id_or_name)}"
        params = {"api-version": self.API_VERSION}
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def list_branches(self, repo_id_or_name: str, project: str | None = None) -> list[dict]:
        """Bir repodaki branch'leri listeler."""
        url = f"{self._repo_api_url(repo_id_or_name, project)}/refs"
        params = {"filter": "heads/", "api-version": self.API_VERSION}
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("value", [])

    def get_file_bytes(self, repo_id_or_name: str, file_path: str, branch: str | None = None,
                       project: str | None = None) -> bytes | None:
        """Dosyanın HAM baytları (karakter kümesi tahmini / JSON çözümü yok); dosya yoksa None.
        Yazma yolundaki tek-satır denetimi çözülmüş metne değil bayta dayanır."""
        url = f"{self._repo_api_url(repo_id_or_name, project)}/items"
        params: dict = {
            "path": file_path,
            "$format": "octetStream",
            "download": "true",
            "api-version": self.API_VERSION,
        }
        if branch:
            params["versionDescriptor.version"] = branch
            params["versionDescriptor.versionType"] = "branch"
        headers = {**self._headers, "Accept": "application/octet-stream"}
        resp = requests.get(url, headers=headers, params=params, timeout=60)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content

    def get_items_in_path(
        self,
        repo_id_or_name: str,
        path: str = "/",
        branch: str | None = None,
        recursion_level: str = "oneLevel",
        project: str | None = None,
    ) -> list[dict]:
        """Bir repoda belirtilen dizindeki dosya/klasorleri listeler.
        project: repo AZURE_DEVOPS_REPO_PROJECTS disindaysa (ör. FLODevOps)."""
        url = f"{self._repo_api_url(repo_id_or_name, project)}/items"
        params: dict = {
            "scopePath": path,
            "recursionLevel": recursion_level,
            "api-version": self.API_VERSION,
        }
        if branch:
            params["versionDescriptor.version"] = branch
            params["versionDescriptor.versionType"] = "branch"
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        if not resp.text.strip():
            return []
        try:
            return resp.json().get("value", [])
        except ValueError:
            return []

    def get_file_content(
        self,
        repo_id_or_name: str,
        file_path: str,
        branch: str | None = None,
        project: str | None = None,
    ) -> str:
        """Bir repodaki dosyanin icerigini (text) dondurur."""
        url = f"{self._repo_api_url(repo_id_or_name, project)}/items"
        params: dict = {
            "path": file_path,
            "includeContent": "true",
            "api-version": self.API_VERSION,
        }
        if branch:
            params["versionDescriptor.version"] = branch
            params["versionDescriptor.versionType"] = "branch"
        resp = requests.get(url, headers=self._headers, params=params, timeout=60)
        resp.raise_for_status()
        # API text icerik dondururse direkt text olarak gelir
        if not resp.text.strip():
            return ""
        content_type = resp.headers.get("Content-Type", "")
        if "application/json" in content_type:
            try:
                data = resp.json()
                return data.get("content", resp.text)
            except ValueError:
                return resp.text
        return resp.text

    def get_recent_commits(
        self,
        repo_id_or_name: str,
        branch: str | None = None,
        top: int = 20,
    ) -> list[dict]:
        """Bir repodaki son commit'leri getirir."""
        url = f"{self._repo_api_url(repo_id_or_name)}/commits"
        params: dict = {"$top": top, "api-version": self.API_VERSION}
        if branch:
            params["searchCriteria.itemVersion.version"] = branch
            params["searchCriteria.itemVersion.versionType"] = "branch"
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("value", [])

    def search_code(self, search_text: str, repo_name: str | None = None) -> list[dict]:
        """Kod icinde arama yapar (Azure DevOps Search API). Tum repo projelerinde arar."""
        # Search API farkli subdomain kullanir: almsearch.dev.azure.com
        search_base = self.org_url.replace("dev.azure.com", "almsearch.dev.azure.com")
        all_results = []
        for proj in self.repo_projects:
            search_url = f"{search_base}/{proj}/_apis/search/codesearchresults"
            params = {"api-version": "7.1-preview.1"}
            body: dict = {
                "searchText": search_text,
                "$top": 25,
                "filters": {"Project": [proj]},
            }
            if repo_name:
                body["filters"]["Repository"] = [repo_name]
            try:
                resp = requests.post(
                    search_url, headers=self._headers, json=body, params=params, timeout=30
                )
                resp.raise_for_status()
                results = resp.json().get("results", [])
                for r in results:
                    r["_project"] = proj
                all_results.extend(results)
            except Exception:
                continue
        return all_results

    # ── Git Yazma API'leri ──

    def create_branch(
        self,
        repo_id_or_name: str,
        branch_name: str,
        source_branch: str = "main",
        project: str | None = None,
        source_object_id: str | None = None,
    ) -> dict:
        """Yeni bir branch olusturur. `source_object_id` verilirse dal TAM o commit'te açılır
        (kaynak dal adı yeniden aranmaz) — çağıranın doğruladığı commit sabitlenir."""
        source_ref = source_object_id
        if not source_ref:
            # Kaynak branch'in objectId'sini al
            branches = self.list_branches(repo_id_or_name, project=project)
            for b in branches:
                ref_name = b.get("name", "")
                if ref_name == f"refs/heads/{source_branch}":
                    source_ref = b.get("objectId")
                    break
        if not source_ref:
            raise ValueError(f"Kaynak branch '{source_branch}' bulunamadi.")

        url = f"{self._repo_api_url(repo_id_or_name, project)}/refs"
        params = {"api-version": self.API_VERSION}
        body = [
            {
                "name": f"refs/heads/{branch_name}",
                "oldObjectId": "0000000000000000000000000000000000000000",
                "newObjectId": source_ref,
            }
        ]
        resp = requests.post(
            url, headers=self._headers, json=body, params=params, timeout=30
        )
        resp.raise_for_status()
        return resp.json()

    def push_changes(
        self,
        repo_id_or_name: str,
        branch: str,
        changes: list[dict],
        commit_message: str,
        project: str | None = None,
        old_object_id: str | None = None,
    ) -> dict:
        """Branch'e dosya degisiklikleri push eder.

        changes formati:
        [
            {"changeType": "add"|"edit", "path": "/src/file.py", "content": "..."},
            {"changeType": "add"|"edit", "path": "/a", "content": "<base64>",
             "contentType": "base64encoded"},                    # opsiyonel; varsayılan rawtext
            {"changeType": "delete", "path": "/src/old.py"},   # icerik yok
            ...
        ]
        `old_object_id` verilirse push o commit üzerine kurulur (dal başı yeniden okunmaz): dal
        o arada ilerlediyse Azure push'u reddeder.
        """
        branch_ref = old_object_id
        if not branch_ref:
            # Branch'in son commit objectId'sini al
            branches = self.list_branches(repo_id_or_name, project=project)
            for b in branches:
                if b.get("name") == f"refs/heads/{branch}":
                    branch_ref = b.get("objectId")
                    break
        if not branch_ref:
            raise ValueError(f"Branch '{branch}' bulunamadi.")

        formatted_changes = []
        for change in changes:
            item = {
                "changeType": change["changeType"],
                "item": {"path": change["path"]},
            }
            if change["changeType"] != "delete":
                item["newContent"] = {
                    "content": change["content"],
                    "contentType": change.get("contentType", "rawtext"),
                }
            formatted_changes.append(item)

        url = f"{self._repo_api_url(repo_id_or_name, project)}/pushes"
        params = {"api-version": self.API_VERSION}
        body = {
            "refUpdates": [
                {"name": f"refs/heads/{branch}", "oldObjectId": branch_ref}
            ],
            "commits": [
                {"comment": commit_message, "changes": formatted_changes}
            ],
        }
        resp = requests.post(
            url, headers=self._headers, json=body, params=params, timeout=60
        )
        resp.raise_for_status()
        return resp.json()

    def create_pull_request(
        self,
        repo_id_or_name: str,
        source_branch: str,
        target_branch: str = "main",
        title: str = "",
        description: str = "",
        work_item_ids: list[int] | None = None,
        draft: bool = False,
        project: str | None = None,
    ) -> dict:
        """Pull request olusturur (`draft` → taslak: incelemeciye düşmez, build politikası koşar).

        Azure PR aciklamasi en cok 4000 karakter; asilirsa govdesiz bir 400 Bad Request doner —
        #268: 41 dosyalik arac PR'inin listesi siniri asti, PR acilmadi. Aciklama burada
        sigdirilir; yine hata olursa Azure'un yaniti mesaja eklenir."""
        if len(description or "") > PR_DESCRIPTION_MAX:
            note = "\n\n… (açıklama Azure'un 4000 karakter sınırına sığdırıldı — liste kısaltıldı)"
            description = description[:PR_DESCRIPTION_MAX - len(note)] + note
        url = f"{self._repo_api_url(repo_id_or_name, project)}/pullrequests"
        params = {"api-version": self.API_VERSION}
        body: dict = {
            "sourceRefName": f"refs/heads/{source_branch}",
            "targetRefName": f"refs/heads/{target_branch}",
            "title": title,
            "description": description,
        }
        if work_item_ids:
            body["workItemRefs"] = [{"id": str(wid)} for wid in work_item_ids]
        if draft:
            body["isDraft"] = True
        resp = requests.post(
            url, headers=self._headers, json=body, params=params, timeout=30
        )
        if not resp.ok:
            raise requests.HTTPError(f"{resp.status_code} PR oluşturulamadı: {(resp.text or '')[:400]}",
                                     response=resp)
        return resp.json()

    def find_pull_request(self, repo_id_or_name: str, source_branch: str, *,
                          project: str | None = None) -> dict | None:
        """O daldan açılmış en yeni PR (her durumda); yoksa None.
        {"id","status","url","closed_at","merge_commit","target"} — status Azure'un verdiği:
        active | completed | abandoned."""
        url = f"{self._repo_api_url(repo_id_or_name, project)}/pullrequests"
        params = {
            "searchCriteria.sourceRefName": f"refs/heads/{source_branch}",
            "searchCriteria.status": "all",
            "api-version": self.API_VERSION,
        }
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        vals = resp.json().get("value", [])
        if not vals:
            return None
        pr = max(vals, key=lambda v: int(v.get("pullRequestId") or 0))
        repo = pr.get("repository") or {}
        proj = (repo.get("project") or {}).get("name") \
            or project or self._find_repo_project(repo_id_or_name)
        repo_name = repo.get("name") or repo_id_or_name
        return {
            "id": pr.get("pullRequestId"),
            "status": pr.get("status") or "",
            "url": f"{self.org_url}/{proj}/_git/{repo_name}/pullrequest/{pr.get('pullRequestId')}",
            "closed_at": pr.get("closedDate") or "",
            "merge_commit": (pr.get("lastMergeCommit") or {}).get("commitId") or "",
            "target": (pr.get("targetRefName") or "").removeprefix("refs/heads/"),
            # PR'ı açan kimlik (PAT sahibiyle karşılaştırılır: başkasının PR'ı Tempo'nun sayılmaz)
            "created_by": (pr.get("createdBy") or {}).get("id") or "",
        }

    def delete_branch(self, repo_id_or_name: str, branch_name: str) -> bool:
        """Remote branch'i siler. Branch yoksa False doner (hata firlatmaz).

        Basarisiz bir kosunun biraktigi bayat feature branch'i temizlemek icin:
        create_branch mevcut branch'i SIFIRLAMAZ ('zaten mevcut' der), bu yuzden
        eski commit'ler yeni kosunun PR diff'ine karisir."""
        branches = self.list_branches(repo_id_or_name)
        target = f"refs/heads/{branch_name}"
        object_id = None
        for b in branches:
            if b.get("name") == target:
                object_id = b.get("objectId")
                break
        if not object_id:
            return False
        url = f"{self._repo_api_url(repo_id_or_name)}/refs"
        params = {"api-version": self.API_VERSION}
        body = [
            {
                "name": target,
                "oldObjectId": object_id,
                "newObjectId": "0000000000000000000000000000000000000000",
            }
        ]
        resp = requests.post(
            url, headers=self._headers, json=body, params=params, timeout=30
        )
        resp.raise_for_status()
        return True

    def abandon_pull_request(
        self,
        repo_id_or_name: str,
        pull_request_id: int,
        reason: str = "",
    ) -> dict:
        """PR'i abandoned durumuna alir (silmez — Azure DevOps'ta PR silinemez).

        Basarisiz/terk edilmis bir pipeline kosusunun biraktigi PR'i kapatmak icin
        kullanilir; ayni branch'te yeni kosu baslatildiginda bayat diff karismaz.
        reason verilirse abandon'dan ONCE PR'a gerekce yorumu birakilir."""
        if reason:
            try:
                self.add_pr_comment(repo_id_or_name, pull_request_id, reason)
            except Exception:
                pass  # yorum kritik degil, abandon'i engellemesin
        url = f"{self._repo_api_url(repo_id_or_name)}/pullrequests/{pull_request_id}"
        params = {"api-version": self.API_VERSION}
        resp = requests.patch(
            url, headers=self._headers, json={"status": "abandoned"},
            params=params, timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def add_pr_comment(
        self,
        repo_id_or_name: str,
        pull_request_id: int,
        content: str,
        file_path: str | None = None,
        line_number: int | None = None,
        end_line: int | None = None,
    ) -> dict:
        """PR'a yorum ekler. file_path verilirse dosya uzerinde inline yorum yapar.

        end_line verilirse yorum bir satir ARALIGINI kapsar. Bu, ```suggestion
        bloklari icin sarttir: Azure DevOps onerilen kodu thread'in kapsadigi
        satirlarin YERINE koyar, dolayisiyla aralik degistirilecek satirlarla
        birebir ortusmezse "Apply changes" yanlis yeri gunceller."""
        url = f"{self._repo_api_url(repo_id_or_name)}/pullrequests/{pull_request_id}/threads"
        params = {"api-version": self.API_VERSION}
        thread: dict = {
            "comments": [{"content": defuse_emoticons(content), "commentType": "text"}],
            "status": "active",
        }
        if file_path:
            thread["threadContext"] = {
                "filePath": file_path,
                "rightFileStart": {"line": line_number or 1, "offset": 1},
                "rightFileEnd": {"line": end_line or line_number or 1, "offset": 1},
            }
        resp = requests.post(
            url, headers=self._headers, json=thread, params=params, timeout=30
        )
        resp.raise_for_status()
        return resp.json()

    def set_pr_draft(self, repo_id_or_name: str, pull_request_id: int, draft: bool) -> dict:
        """Taslak PR'ı yayınla (draft=False) ya da taslağa çek."""
        url = f"{self._repo_api_url(repo_id_or_name)}/pullrequests/{pull_request_id}"
        resp = requests.patch(url, headers=self._headers, json={"isDraft": bool(draft)},
                              params={"api-version": self.API_VERSION}, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def update_pr_description(self, repo_id_or_name: str, pull_request_id: int, description: str) -> dict:
        """PR açıklamasını değiştir (4000 sınırına sığdırılır). Bilgi notları yorum yerine buraya
        yazılır — yorum, incelenmesi gereken bir durum içindir (kullanıcı kararı 2026-11-04)."""
        if len(description or "") > PR_DESCRIPTION_MAX:
            description = description[:PR_DESCRIPTION_MAX - 1] + "…"
        url = f"{self._repo_api_url(repo_id_or_name)}/pullrequests/{pull_request_id}"
        resp = requests.patch(url, headers=self._headers, json={"description": description},
                              params={"api-version": self.API_VERSION}, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def delete_own_pr_comments(self, repo_id_or_name: str, pull_request_id: int, markers: tuple[str, ...]) -> int:
        """Bu PAT'in (Tempo) PR'a yazdığı, `markers`'dan birini taşıyan yorumları sil — aynı PR'ı
        güncelleyen yeniden koşuda eski (artık geçersiz) inceleme yorumları kalmasın (#45729: iki
        koşunun 16 satır yorumu + özetleri). İnsanın yorumuna ASLA dokunmaz (yazar kimliği eşleşmeli)."""
        me = (self.get_authenticated_user() or {}).get("id")
        if not me:
            return 0
        base = f"{self._repo_api_url(repo_id_or_name)}/pullrequests/{pull_request_id}/threads"
        params = {"api-version": self.API_VERSION}
        resp = requests.get(base, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        n = 0
        for t in resp.json().get("value") or []:
            for c in t.get("comments") or []:
                if c.get("isDeleted") or c.get("commentType") == "system":
                    continue
                if (c.get("author") or {}).get("id") != me:
                    continue
                if not any(m in (c.get("content") or "") for m in markers):
                    continue
                r = requests.delete(f"{base}/{t['id']}/comments/{c['id']}", headers=self._headers,
                                    params=params, timeout=30)
                n += int(r.ok)
        return n

    def reply_to_pr_thread(
        self,
        repo_id_or_name: str,
        pull_request_id: int,
        thread_id: int,
        content: str,
    ) -> dict:
        """Mevcut bir PR thread'ine yanit yazar."""
        url = (
            f"{self._repo_api_url(repo_id_or_name)}/pullrequests/"
            f"{pull_request_id}/threads/{thread_id}/comments"
        )
        params = {"api-version": self.API_VERSION}
        body = {"content": defuse_emoticons(content), "commentType": "text"}
        resp = requests.post(url, headers=self._headers, json=body, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def update_pr_comment(self, repo_id_or_name: str, pull_request_id: int, thread_id: int,
                          comment_id: int, content: str) -> dict:
        """Var olan bir yorumun metnini değiştir (Tempo'nun tek özet yorumu yerinde güncellenir)."""
        url = (f"{self._repo_api_url(repo_id_or_name)}/pullrequests/{pull_request_id}"
               f"/threads/{thread_id}/comments/{comment_id}")
        resp = requests.patch(url, headers=self._headers, json={"content": defuse_emoticons(content)},
                              params={"api-version": self.API_VERSION}, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def set_pr_thread_status(self, repo_id_or_name: str, pull_request_id: int, thread_id: int,
                             status: str) -> dict:
        """Thread durumu: active | fixed | closed | wontFix | byDesign | pending."""
        url = f"{self._repo_api_url(repo_id_or_name)}/pullrequests/{pull_request_id}/threads/{thread_id}"
        resp = requests.patch(url, headers=self._headers, json={"status": status},
                              params={"api-version": self.API_VERSION}, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_commit_diff_paths(self, repo_id_or_name: str, base_commit: str, target_commit: str) -> list[str]:
        """İki commit arasında değişen dosya yolları (klasörler hariç) — artımlı inceleme için."""
        url = f"{self._repo_api_url(repo_id_or_name)}/diffs/commits"
        out, skip = [], 0
        while True:
            params = {"api-version": self.API_VERSION, "baseVersion": base_commit, "baseVersionType": "commit",
                      "targetVersion": target_commit, "targetVersionType": "commit", "$top": 500, "$skip": skip}
            resp = requests.get(url, headers=self._headers, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            ch = data.get("changes") or []
            for c in ch:
                item = c.get("item") or {}
                if item.get("isFolder") or item.get("gitObjectType") == "tree":
                    continue
                if item.get("path") and item["path"] not in out:
                    out.append(item["path"])
            if data.get("allChangesIncluded", True) or not ch:
                return out
            skip += len(ch)

    def get_pr_last_push(self, repo_id_or_name: str, pull_request_id: int) -> dict:
        """PR'ın son iterasyonu (= son push): {commit, date}. Yoksa boş dict."""
        url = f"{self._repo_api_url(repo_id_or_name)}/pullrequests/{pull_request_id}/iterations"
        resp = requests.get(url, headers=self._headers, params={"api-version": self.API_VERSION}, timeout=30)
        resp.raise_for_status()
        its = resp.json().get("value") or []
        if not its:
            return {}
        last = its[-1]
        return {"commit": ((last.get("sourceRefCommit") or {}).get("commitId") or ""),
                "date": last.get("createdDate") or ""}

    def resolve_pr_thread(
        self,
        repo_id_or_name: str,
        pull_request_id: int,
        thread_id: int,
    ) -> dict:
        """PR thread'ini 'fixed' olarak isaretler."""
        url = (
            f"{self._repo_api_url(repo_id_or_name)}/pullrequests/"
            f"{pull_request_id}/threads/{thread_id}"
        )
        params = {"api-version": self.API_VERSION}
        body = {"status": "fixed"}
        resp = requests.patch(url, headers=self._headers, json=body, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def find_active_pr_by_branch(
        self,
        repo_id_or_name: str,
        source_branch: str,
        target_branch: str = "main",
    ) -> dict | None:
        """Verilen kaynak branch icin aktif (acik) PR varsa bilgisini dondur.

        Yoksa None. SSL/transient hata olursa exception fırlatir."""
        url = f"{self._repo_api_url(repo_id_or_name)}/pullrequests"
        params = {
            "searchCriteria.sourceRefName": f"refs/heads/{source_branch}",
            "searchCriteria.targetRefName": f"refs/heads/{target_branch}",
            "searchCriteria.status": "active",
            "api-version": self.API_VERSION,
        }
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        prs = resp.json().get("value", [])
        return prs[0] if prs else None

    def get_pull_request(
        self,
        repo_id_or_name: str,
        pull_request_id: int,
    ) -> dict:
        """PR detaylarini getirir (baslik, branch, status, work items)."""
        url = f"{self._repo_api_url(repo_id_or_name)}/pullrequests/{pull_request_id}"
        params = {"api-version": self.API_VERSION}
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_pr_threads(
        self,
        repo_id_or_name: str,
        pull_request_id: int,
    ) -> list[dict]:
        """PR'daki tum yorum thread'lerini getirir.
        Her thread: id, status, comments[], threadContext (dosya/satir bilgisi)."""
        url = f"{self._repo_api_url(repo_id_or_name)}/pullrequests/{pull_request_id}/threads"
        params = {"api-version": self.API_VERSION}
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("value", [])

    def get_pr_comments_text(
        self,
        repo_id_or_name: str,
        pull_request_id: int,
    ) -> list[dict]:
        """PR yorumlarini okunabilir formatta dondurur.
        Returns: [{"author": str, "content": str, "file_path": str|None, "date": str}]"""
        threads = self.get_pr_threads(repo_id_or_name, pull_request_id)
        comments = []
        for thread in threads:
            # Sistem thread'lerini atla (merge, status update vb.)
            if thread.get("properties", {}).get("CodeReviewThreadType"):
                continue
            file_path = None
            ctx = thread.get("threadContext")
            if ctx:
                file_path = ctx.get("filePath")
            for comment in thread.get("comments", []):
                # Sistem yorumlarini atla
                if comment.get("commentType") == "system":
                    continue
                content = comment.get("content", "").strip()
                if not content:
                    continue
                author = comment.get("author", {}).get("displayName", "")
                date = comment.get("publishedDate", "")
                comments.append({
                    "author": author,
                    "content": content,
                    "file_path": file_path,
                    "date": date,
                })
        return comments

    def get_pull_request_changes(
        self,
        repo_id_or_name: str,
        pull_request_id: int,
    ) -> list[dict]:
        """PR'daki degisiklikleri (diff) getirir."""
        url = f"{self._repo_api_url(repo_id_or_name)}/pullrequests/{pull_request_id}/iterations"
        params = {"api-version": self.API_VERSION}
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        iterations = resp.json().get("value", [])
        if not iterations:
            return []
        last_iter = iterations[-1]["id"]
        changes_url = f"{self._repo_api_url(repo_id_or_name)}/pullrequests/{pull_request_id}/iterations/{last_iter}/changes"
        resp2 = requests.get(changes_url, headers=self._headers, params=params, timeout=30)
        resp2.raise_for_status()
        return resp2.json().get("changeEntries", [])

    # ── Build / CI API'leri (PR test gate) ──

    def get_pr_build(self, repo_id_or_name: str, pull_request_id: int) -> dict | None:
        """PR'in en yeni CI build'ini dondur (branchName=refs/pull/{id}/merge).
        None → bu repoda PR'i tetikleyen bir build pipeline'i yok demektir."""
        project = self._find_repo_project(repo_id_or_name)
        url = f"{self.org_url}/{project}/_apis/build/builds"
        params = {
            "api-version": self.API_VERSION,
            "branchName": f"refs/pull/{pull_request_id}/merge",
            "$top": 1,
            "queryOrder": "queueTimeDescending",
        }
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        vals = resp.json().get("value", [])
        if not vals:
            return None
        b = vals[0]
        return {
            "build_id": b.get("id"),
            "status": b.get("status"),       # notStarted|inProgress|completed|postponed|...
            "result": b.get("result"),       # succeeded|failed|partiallySucceeded|canceled|None
            "definition": (b.get("definition") or {}).get("name"),
            "url": ((b.get("_links") or {}).get("web") or {}).get("href"),
            "project": project,
            "queue_time": b.get("queueTime") or "",
            # PR build'i refs/pull/N/merge'i derler: bu, PR'ın lastMergeCommit'idir. Eşit değilse
            # build PR'ın daha eski bir hâli içindir (pr_review bunu "eski commit" diye işaretler).
            "source_version": b.get("sourceVersion") or "",
        }

    def _strict_json(self, url: str, params: dict | None = None) -> dict:
        """GET → JSON sözlüğü; 200 dışı her yanıt, JSON olmayan gövde ve bağlantı hatası yükselir (kapı kanıtı
        okunamadıysa 'yok' sayılmaz)."""
        resp = requests.get(url, headers=self._headers, params={"api-version": self.API_VERSION, **(params or {})},
                            timeout=30)
        if resp.status_code != 200:
            raise ValueError(f"Azure isteği başarısız (HTTP {resp.status_code}): {url}")
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ValueError(f"Azure yanıtı JSON değil: {url}")
        return body

    def get_test_pipeline(self, repo_id_or_name: str) -> dict | None:
        """Reponun `<repo>-test` build tanımı ve koştuğu test hattı dosyası — sürüm yükseltmede test hattının
        TEK kaynağı (dört ayrı `yml-files` reposu var; core'un Core-test'i EcomLojistik/yml-files'ı koşar).
        Tanım reponun projesinde (kesin çözüm) adıyla aranır, büyük/küçük harf duyarsız; hiç yoksa None, birden
        çok varsa ValueError. Dönüş: {id, name, project, yaml (process.type 2 değilse None; baştaki '/' atılır),
        repo_id, repo_name, branch (refs/heads/ olmadan), repo_project (repo GUID'i organizasyon genelinde
        çözülür)}. 200 dışı / JSON olmayan her yanıt yükselir."""
        project = self._repo_project_strict(repo_id_or_name)
        name = f"{repo_id_or_name}-test"
        body = self._strict_json(f"{self.org_url}/{project}/_apis/build/definitions", {"name": name})
        hits = [d for d in body.get("value") or [] if str(d.get("name") or "").lower() == name.lower()]
        if not hits:
            return None
        if len(hits) > 1:
            raise ValueError(f"'{name}' adlı birden çok build tanımı var ({project}: "
                             f"{', '.join(str(d.get('name')) + ' #' + str(d.get('id')) for d in hits)}) — "
                             "hangisinin test hattı olduğu belirsiz.")
        det = self._strict_json(f"{self.org_url}/{project}/_apis/build/definitions/{int(hits[0]['id'])}")
        proc = det.get("process") or {}
        yml = proc.get("yamlFilename") if proc.get("type") == 2 else None
        rep = det.get("repository") or {}
        repo_id = str(rep.get("id") or "")
        repo_project = ""
        if repo_id:
            rb = self._strict_json(f"{self.org_url}/_apis/git/repositories/{repo_id}")
            repo_project = str((rb.get("project") or {}).get("name") or "")
        return {"id": det.get("id") or hits[0].get("id"), "name": det.get("name") or hits[0].get("name"),
                "project": project, "yaml": (str(yml).lstrip("/") or None) if yml else None,
                "repo_id": repo_id, "repo_name": str(rep.get("name") or ""),
                "branch": str(rep.get("defaultBranch") or "").replace("refs/heads/", "", 1),
                "repo_project": repo_project}

    def list_definition_builds(self, project: str, definition_id: int, *, min_time: str = "",
                               top: int = 50) -> list[dict]:
        """Tanımın PR build'leri (reason=pullRequest), en yeni kuyruktan geriye; get_pr_build ile aynı biçim."""
        url = f"{self.org_url}/{project}/_apis/build/builds"
        params = {
            "api-version": self.API_VERSION,
            "definitions": int(definition_id),
            "reasonFilter": "pullRequest",
            "queryOrder": "queueTimeDescending",
            "$top": int(top),
        }
        if min_time:
            params["minTime"] = min_time
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        return [{
            "build_id": b.get("id"),
            "status": b.get("status"),
            "result": b.get("result"),
            "definition": (b.get("definition") or {}).get("name"),
            "url": ((b.get("_links") or {}).get("web") or {}).get("href"),
            "project": project,
            "queue_time": b.get("queueTime") or "",
            "source_version": b.get("sourceVersion") or "",
        } for b in resp.json().get("value", []) or []]

    def get_last_push_date(self, repo_id_or_name: str, branch: str, *, project: str | None = None) -> str:
        """Dala yapılan EN SON push'un zamanı (`value[0].date`). Pipeline repolarına doğrudan push yapılıyor:
        commit zamanı, önce yazılıp sonra push edilen değişiklikte erkene düşer — kanıt saati push zamanıdır.
        Push yoksa ya da `date` alanı yoksa "" (okunamaz → kapı kapalı). 200 dışı / JSON olmayan yanıt yükselir."""
        url = f"{self._repo_api_url(repo_id_or_name, project)}/pushes"
        resp = requests.get(url, headers=self._headers, timeout=30, params={
            "api-version": self.API_VERSION,
            "searchCriteria.refName": f"refs/heads/{branch}",
            "$top": 1,
        })
        if resp.status_code != 200:
            raise ValueError(f"Push listesi okunamadı (HTTP {resp.status_code}): {repo_id_or_name} {branch}")
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ValueError(f"Push listesi JSON değil: {repo_id_or_name} {branch}")
        vals = body.get("value") or []
        return str((vals[0] or {}).get("date") or "") if vals and isinstance(vals[0], dict) else ""

    def get_last_commit_date(self, repo_id_or_name: str, path: str, branch: str, *,
                             project: str | None = None) -> str:
        """Dosyaya dokunan en son commit'in zamanı (committer, yoksa author) — dalın first-parent çizgisinde.
        Commit yoksa "". firstParent şart: varsayılan (sadeleştirilmiş) geçmiş, "Merge (no fast-forward)" ile
        tamamlanan PR'da özellik dalındaki commit'i (push zamanı) döndürür, merge commit'ini değil."""
        url = f"{self._repo_api_url(repo_id_or_name, project)}/commits"
        params = {
            "api-version": self.API_VERSION,
            "searchCriteria.itemPath": path,
            "searchCriteria.itemVersion.version": branch,
            "searchCriteria.itemVersion.versionType": "branch",
            "searchCriteria.historyMode": "firstParent",
            "searchCriteria.$top": 1,
        }
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        vals = resp.json().get("value", []) or []
        if not vals:
            return ""
        c = vals[0]
        return str((c.get("committer") or {}).get("date") or (c.get("author") or {}).get("date") or "")

    def get_build_failure_summary(
        self, project: str, build_id: int, max_chars: int = 4000
    ) -> str:
        """Basarisiz build'in failed task'lari + test outcome ozeti + 'Run tests'
        log kuyrugu — developer'in testleri/kodu duzeltebilmesi icin context."""
        parts: list[str] = []
        # 1) timeline → failed task'lar (+ test log'u)
        try:
            url = f"{self.org_url}/{project}/_apis/build/builds/{build_id}/timeline"
            resp = requests.get(url, headers=self._headers,
                                params={"api-version": self.API_VERSION}, timeout=30)
            if resp.ok:
                for rec in resp.json().get("records", []):
                    if rec.get("result") != "failed" or rec.get("type") not in ("Task", "Job", "Phase"):
                        continue
                    nm = rec.get("name", "?")
                    issues = rec.get("issues", []) or []
                    errs = "; ".join(
                        i.get("message", "")[:300] for i in issues if i.get("type") == "error"
                    )[:1500]
                    parts.append(f"FAILED [{rec.get('type')}] {nm}" + (f": {errs}" if errs else ""))
                    # Yalnız TASK logu: Phase/Job logu ajanın hazırlık YAML'ı
                    # (#248: "Unit Tests" Phase'i dekoratör şablonunu verdi, asıl
                    # `undefined: webanalytics` satırları Task logundaydı).
                    log_url = (rec.get("log") or {}).get("url")
                    if log_url and rec.get("type") == "Task":
                        try:
                            lr = requests.get(log_url, headers=self._headers, timeout=30)
                            if lr.ok:
                                parts.append(f"--- {nm} log ---\n{extract_build_errors(lr.text)}")
                        except Exception:
                            pass
        except Exception:
            pass
        # 2) test outcome ozeti (varsa)
        try:
            tr = requests.get(
                f"{self.org_url}/{project}/_apis/test/ResultSummaryByBuild",
                headers=self._headers,
                params={"api-version": "7.1-preview.1", "buildId": build_id}, timeout=30,
            )
            if tr.ok:
                agg = tr.json().get("aggregatedResultsAnalysis", {})
                if agg.get("totalTests"):
                    parts.append(f"Test ozet: {agg.get('totalTests')} test, sonuc: {agg.get('resultsByOutcome', {})}")
        except Exception:
            pass
        out = "\n".join(parts).strip()
        return out[:max_chars]

    def get_build_issue_tasks(self, project: str, build_id: int) -> list[dict]:
        """failed / succeededWithIssues biten Task'lar: [{name, result}]."""
        url = f"{self.org_url}/{project}/_apis/build/builds/{build_id}/timeline"
        resp = requests.get(url, headers=self._headers,
                            params={"api-version": self.API_VERSION}, timeout=30)
        resp.raise_for_status()
        return [
            {"name": r.get("name", ""), "result": r.get("result", "")}
            for r in resp.json().get("records", [])
            if r.get("type") == "Task" and r.get("result") in ("failed", "succeededWithIssues")
        ]

    # ── Sprint / Iteration API'leri ──

    def list_teams(self) -> list[dict]:
        """Projedeki takimlari listeler."""
        url = f"{self.org_url}/_apis/projects/{self.project}/teams"
        params = {"api-version": self.API_VERSION}
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("value", [])

    def list_iterations(self, team: str = "") -> list[dict]:
        """Projedeki sprint/iteration'lari listeler."""
        team = team.strip() or self.team
        if team:
            url = f"{self.org_url}/{self.project}/{requests.utils.quote(team, safe='')}/_apis/work/teamsettings/iterations"
        else:
            url = f"{self.org_url}/{self.project}/_apis/work/teamsettings/iterations"
        params = {"api-version": self.API_VERSION}
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("value", [])

    def get_iteration_work_items(self, iteration_path: str) -> list[dict]:
        """Belirli bir sprint/iteration'daki work item'lari getirir."""
        safe_path = iteration_path.replace("'", "''")
        wiql = (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.IterationPath] = '{safe_path}' "
            "AND [System.WorkItemType] <> 'User Story' "
            "ORDER BY [Microsoft.VSTS.Common.Priority] ASC, "
            "[System.CreatedDate] DESC"
        )
        raw_items = self.query_work_items(wiql)
        result = []
        for item in raw_items:
            fields = item.get("fields", {})
            assigned = fields.get("System.AssignedTo")
            wi_url = item.get("_links", {}).get("html", {}).get("href", "")
            if not wi_url:
                wi_url = f"{self.org_url}/{self.project}/_workitems/edit/{item.get('id', '')}"
            # Story point: bu org Custom.StoryPoints kullaniyor; standart alanlara fallback
            sp = 0.0
            for sp_field in (
                "Custom.StoryPoints",
                "Microsoft.VSTS.Scheduling.StoryPoints",
                "Microsoft.VSTS.Scheduling.Effort",
            ):
                val = fields.get(sp_field)
                if val is not None:
                    try:
                        sp = float(val)
                        break
                    except (TypeError, ValueError):
                        continue
            result.append({
                "id": item.get("id"),
                "title": fields.get("System.Title", ""),
                "state": fields.get("System.State", ""),
                "type": fields.get("System.WorkItemType", ""),
                "assignedTo": assigned.get("displayName", "") if isinstance(assigned, dict) else "",
                "priority": fields.get("Microsoft.VSTS.Common.Priority", 4),
                "storyPoints": sp,
                "tags": fields.get("System.Tags", ""),
                "iterationPath": fields.get("System.IterationPath", ""),
                "areaPath": fields.get("System.AreaPath", ""),
                # Yaslandirma (aging.py): isin SU ANKI durumda ne kadar
                # bekledigi StateChangeDate'ten olculur; alan bos gelirse
                # ChangedDate/CreatedDate yedege duser.
                "stateChangeDate": fields.get("Microsoft.VSTS.Common.StateChangeDate", ""),
                "changedDate": fields.get("System.ChangedDate", ""),
                "createdDate": fields.get("System.CreatedDate", ""),
                "url": wi_url,
                # WI'ye bağlı PR'lar (ilişkilerden; elle ya da başkasının açtığı
                # PR'lar da). Board kartı PR'ı yalnız pipeline işinden okuyordu.
                "prs": self._pr_links(item.get("relations") or []),
            })
        return result

    _PR_REL = re.compile(r"PullRequestId/([0-9a-f-]+)%2F([0-9a-f-]+)%2F(\d+)", re.I)

    def _pr_links(self, relations: list) -> list[dict]:
        out: list[dict] = []
        for r in relations:
            m = self._PR_REL.search(r.get("url") or "")
            if m and not any(p["id"] == int(m.group(3)) for p in out):
                out.append({"id": int(m.group(3)),
                            "url": f"{self.org_url}/{m.group(1)}/_git/{m.group(2)}/pullrequest/{m.group(3)}"})
        return out

    def analytics_query(self, odata_query: str) -> list:
        """Azure DevOps Analytics OData sorgusu calistir (burndown/velocity icin).
        odata_query: 'v4.0-preview/' sonrasi gelen kisim, or.
        'WorkItemSnapshot?$apply=filter(...)/...'. Doner: 'value' listesi."""
        analytics = self.org_url.replace("://dev.azure.com", "://analytics.dev.azure.com")
        url = f"{analytics}/{self.project}/_odata/v4.0-preview/{odata_query}"
        resp = requests.get(url, headers=self._headers, timeout=60)
        resp.raise_for_status()
        return resp.json().get("value", [])

    def get_work_item_parents(self, work_item_ids: list) -> dict:
        """Verilen work item'larin parent'ini cozer.
        Doner: {child_id(int): {"parent_id", "parent_title", "parent_type"}}.
        Parent'i olmayan cocuk map'te yer almaz."""
        ids = [int(i) for i in work_item_ids if i]
        if not ids:
            return {}

        def _parent_from_item(item: dict):
            f = item.get("fields", {})
            pid = f.get("System.Parent")
            if pid:
                return int(pid)
            for rel in item.get("relations", []) or []:
                if rel.get("rel") == "System.LinkTypes.Hierarchy-Reverse":
                    m = re.search(r"/workItems/(\d+)", rel.get("url", ""), re.IGNORECASE)
                    if m:
                        return int(m.group(1))
            return None

        # 1) cocuklarin parent id'leri
        child_to_parent: dict[int, int] = {}
        for i in range(0, len(ids), 200):
            batch = ",".join(str(x) for x in ids[i : i + 200])
            wiql = f"SELECT [System.Id] FROM WorkItems WHERE [System.Id] IN ({batch})"
            for item in self.query_work_items(wiql):
                pid = _parent_from_item(item)
                if pid:
                    child_to_parent[int(item.get("id"))] = pid

        # 2) parent baslik/tip
        parent_ids = sorted(set(child_to_parent.values()))
        parent_info: dict[int, dict] = {}
        for i in range(0, len(parent_ids), 200):
            batch = ",".join(str(x) for x in parent_ids[i : i + 200])
            wiql = f"SELECT [System.Id] FROM WorkItems WHERE [System.Id] IN ({batch})"
            for item in self.query_work_items(wiql):
                f = item.get("fields", {})
                parent_info[int(item.get("id"))] = {
                    "title": f.get("System.Title", ""),
                    "type": f.get("System.WorkItemType", ""),
                }

        out = {}
        for cid, pid in child_to_parent.items():
            info = parent_info.get(pid, {})
            out[cid] = {
                "parent_id": pid,
                "parent_title": info.get("title", ""),
                "parent_type": info.get("type", ""),
            }
        return out

    def query_work_items(self, wiql: str, limit: int = 0) -> list[dict]:
        url = f"{self._base_api_url}/wit/wiql"
        params = {"api-version": self.API_VERSION}
        resp = requests.post(
            url, headers=self._headers, json={"query": wiql}, params=params, timeout=30
        )
        resp.raise_for_status()
        result = resp.json()

        ids = [item["id"] for item in result.get("workItems", [])]
        if limit and limit > 0:
            ids = ids[:limit]
        if not ids:
            return []

        items = []
        for i in range(0, len(ids), 200):
            batch_ids = ",".join(str(wid) for wid in ids[i : i + 200])
            batch_url = f"{self._base_api_url}/wit/workitems"
            batch_params = {
                "ids": batch_ids,
                "$expand": "all",
                "api-version": self.API_VERSION,
            }
            batch_resp = requests.get(
                batch_url, headers=self._headers, params=batch_params, timeout=30
            )
            batch_resp.raise_for_status()
            items.extend(batch_resp.json().get("value", []))

        return items

    def get_team_area_path(self, team: str = "") -> str:
        """Takimin area path'ini dondurur.

        teamfieldvalues.values[] icinde leaf segment'i takim adina esit olan
        area tercih edilir (ornek: takim 'E-commerce Logistic Operations' ->
        'Boards Management\\E-commerce Logistic Operations'). Eslesme yoksa
        defaultValue'ya duser. defaultValue bazi takimlarda yanlis (test) area
        olabildigi icin values[] once denenir. Bulunamaz/404/hata -> bos string."""
        team = (team or "").strip() or self.team
        if not team:
            return ""
        url = (
            f"{self.org_url}/{self.project}/"
            f"{requests.utils.quote(team, safe='')}/_apis/work/teamsettings/teamfieldvalues"
        )
        params = {"api-version": self.API_VERSION}
        try:
            resp = requests.get(url, headers=self._headers, params=params, timeout=30)
            if resp.status_code == 404:
                return ""
            resp.raise_for_status()
        except requests.RequestException:
            return ""
        data = resp.json()
        # Takim adiyla eslesen area (leaf == team) — en isabetli kapsam
        for v in (data.get("values") or []):
            val = str(v.get("value", "")).strip()
            if val and val.split("\\")[-1].strip().lower() == team.lower():
                return val
        return (data.get("defaultValue") or "").strip()

    def query_done_work_items(
        self, area_path: str = "", states: list[str] | None = None, limit: int = 0,
    ) -> list[dict]:
        """Tamamlanmis (done) work item'lari ChangedDate DESC (guncelden eskiye) dondurur.
        area_path verilirse o alanin altina filtreler. Donen item'lar fields + relations icerir."""
        states = states or ["Done", "Closed", "Resolved"]
        states_sql = ", ".join("'" + s.replace("'", "''") + "'" for s in states)
        wiql = (
            "SELECT [System.Id] FROM WorkItems "
            "WHERE [System.TeamProject] = @project "
            f"AND [System.State] IN ({states_sql}) "
        )
        if area_path:
            safe_area = area_path.replace("'", "''")
            wiql += f"AND [System.AreaPath] UNDER '{safe_area}' "
        wiql += "ORDER BY [System.ChangedDate] DESC"
        return self.query_work_items(wiql, limit=limit)
