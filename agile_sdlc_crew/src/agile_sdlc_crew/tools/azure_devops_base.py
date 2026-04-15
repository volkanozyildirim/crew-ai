import base64
import os

import requests


class AzureDevOpsClient:
    """Azure DevOps REST API icin paylasilan HTTP istemcisi."""

    API_VERSION = "7.1"

    def __init__(self):
        self.org_url = os.environ.get("AZURE_DEVOPS_ORG_URL", "").rstrip("/")
        self.pat = os.environ.get("AZURE_DEVOPS_PAT", "")
        self.project = os.environ.get("AZURE_DEVOPS_PROJECT", "")
        # Repolar farkli projelerde olabilir
        repo_projects = os.environ.get("AZURE_DEVOPS_REPO_PROJECTS", "")
        self.repo_projects = [p.strip() for p in repo_projects.split(",") if p.strip()] if repo_projects else [self.project]

        self._repo_project_cache: dict[str, str] = {}

        if not all([self.org_url, self.pat, self.project]):
            raise ValueError(
                "AZURE_DEVOPS_ORG_URL, AZURE_DEVOPS_PAT ve AZURE_DEVOPS_PROJECT "
                "ortam degiskenleri ayarlanmalidir."
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

    def update_work_item(self, work_item_id: int, operations: list[dict]) -> dict:
        url = f"{self._base_api_url}/wit/workitems/{work_item_id}"
        params = {"api-version": self.API_VERSION}
        resp = requests.patch(
            url, headers=self._patch_headers, json=operations, params=params, timeout=30
        )
        resp.raise_for_status()
        return resp.json()

    def add_comment(self, work_item_id: int, text: str) -> dict:
        url = f"{self._base_api_url}/wit/workitems/{work_item_id}/comments"
        params = {"api-version": "7.1-preview.4"}
        resp = requests.post(
            url, headers=self._headers, json={"text": text}, params=params, timeout=30
        )
        resp.raise_for_status()
        return resp.json()

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

    def list_branches(self, repo_id_or_name: str) -> list[dict]:
        """Bir repodaki branch'leri listeler."""
        url = f"{self._repo_api_url(repo_id_or_name)}/refs"
        params = {"filter": "heads/", "api-version": self.API_VERSION}
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("value", [])

    def get_items_in_path(
        self,
        repo_id_or_name: str,
        path: str = "/",
        branch: str | None = None,
        recursion_level: str = "oneLevel",
    ) -> list[dict]:
        """Bir repoda belirtilen dizindeki dosya/klasorleri listeler."""
        url = f"{self._repo_api_url(repo_id_or_name)}/items"
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
    ) -> str:
        """Bir repodaki dosyanin icerigini (text) dondurur."""
        url = f"{self._repo_api_url(repo_id_or_name)}/items"
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
    ) -> dict:
        """Yeni bir branch olusturur."""
        # Kaynak branch'in objectId'sini al
        branches = self.list_branches(repo_id_or_name)
        source_ref = None
        for b in branches:
            ref_name = b.get("name", "")
            if ref_name == f"refs/heads/{source_branch}":
                source_ref = b.get("objectId")
                break
        if not source_ref:
            raise ValueError(f"Kaynak branch '{source_branch}' bulunamadi.")

        url = f"{self._repo_api_url(repo_id_or_name)}/refs"
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
    ) -> dict:
        """Branch'e dosya degisiklikleri push eder.

        changes formati:
        [
            {"changeType": "add"|"edit", "path": "/src/file.py", "content": "..."},
            ...
        ]
        """
        # Branch'in son commit objectId'sini al
        branches = self.list_branches(repo_id_or_name)
        branch_ref = None
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
                "newContent": {
                    "content": change["content"],
                    "contentType": "rawtext",
                },
            }
            formatted_changes.append(item)

        url = f"{self._repo_api_url(repo_id_or_name)}/pushes"
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
    ) -> dict:
        """Pull request olusturur."""
        url = f"{self._repo_api_url(repo_id_or_name)}/pullrequests"
        params = {"api-version": self.API_VERSION}
        body: dict = {
            "sourceRefName": f"refs/heads/{source_branch}",
            "targetRefName": f"refs/heads/{target_branch}",
            "title": title,
            "description": description,
        }
        if work_item_ids:
            body["workItemRefs"] = [{"id": str(wid)} for wid in work_item_ids]
        resp = requests.post(
            url, headers=self._headers, json=body, params=params, timeout=30
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
    ) -> dict:
        """PR'a yorum ekler. file_path verilirse dosya uzerinde inline yorum yapar."""
        url = f"{self._repo_api_url(repo_id_or_name)}/pullrequests/{pull_request_id}/threads"
        params = {"api-version": self.API_VERSION}
        thread: dict = {
            "comments": [{"content": content, "commentType": "text"}],
            "status": "active",
        }
        if file_path:
            thread["threadContext"] = {
                "filePath": file_path,
                "rightFileStart": {"line": line_number or 1, "offset": 1},
                "rightFileEnd": {"line": line_number or 1, "offset": 1},
            }
        resp = requests.post(
            url, headers=self._headers, json=thread, params=params, timeout=30
        )
        resp.raise_for_status()
        return resp.json()

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
        team = team.strip() or os.environ.get("AZURE_DEVOPS_TEAM", "").strip()
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
            result.append({
                "id": item.get("id"),
                "title": fields.get("System.Title", ""),
                "state": fields.get("System.State", ""),
                "type": fields.get("System.WorkItemType", ""),
                "assignedTo": assigned.get("displayName", "") if isinstance(assigned, dict) else "",
                "priority": fields.get("Microsoft.VSTS.Common.Priority", 4),
                "tags": fields.get("System.Tags", ""),
                "iterationPath": fields.get("System.IterationPath", ""),
                "areaPath": fields.get("System.AreaPath", ""),
                "url": wi_url,
            })
        return result

    def query_work_items(self, wiql: str) -> list[dict]:
        url = f"{self._base_api_url}/wit/wiql"
        params = {"api-version": self.API_VERSION}
        resp = requests.post(
            url, headers=self._headers, json={"query": wiql}, params=params, timeout=30
        )
        resp.raise_for_status()
        result = resp.json()

        ids = [item["id"] for item in result.get("workItems", [])]
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
