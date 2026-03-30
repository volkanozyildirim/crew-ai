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

    def list_repositories(self) -> list[dict]:
        """Projedeki tum Git repolarini listeler."""
        url = f"{self._base_api_url}/git/repositories"
        params = {"api-version": self.API_VERSION}
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("value", [])

    def get_repository(self, repo_id_or_name: str) -> dict:
        """Tek bir reponun detaylarini getirir."""
        url = f"{self._base_api_url}/git/repositories/{repo_id_or_name}"
        params = {"api-version": self.API_VERSION}
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def list_branches(self, repo_id_or_name: str) -> list[dict]:
        """Bir repodaki branch'leri listeler."""
        url = f"{self._base_api_url}/git/repositories/{repo_id_or_name}/refs"
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
        url = f"{self._base_api_url}/git/repositories/{repo_id_or_name}/items"
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
        return resp.json().get("value", [])

    def get_file_content(
        self,
        repo_id_or_name: str,
        file_path: str,
        branch: str | None = None,
    ) -> str:
        """Bir repodaki dosyanin icerigini (text) dondurur."""
        url = f"{self._base_api_url}/git/repositories/{repo_id_or_name}/items"
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
        content_type = resp.headers.get("Content-Type", "")
        if "application/json" in content_type:
            data = resp.json()
            return data.get("content", resp.text)
        return resp.text

    def get_recent_commits(
        self,
        repo_id_or_name: str,
        branch: str | None = None,
        top: int = 20,
    ) -> list[dict]:
        """Bir repodaki son commit'leri getirir."""
        url = f"{self._base_api_url}/git/repositories/{repo_id_or_name}/commits"
        params: dict = {"$top": top, "api-version": self.API_VERSION}
        if branch:
            params["searchCriteria.itemVersion.version"] = branch
            params["searchCriteria.itemVersion.versionType"] = "branch"
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("value", [])

    def search_code(self, search_text: str, repo_name: str | None = None) -> list[dict]:
        """Kod icinde arama yapar (Azure DevOps Search API)."""
        # Search API farkli base URL kullanir
        search_url = f"{self.org_url}/{self.project}/_apis/search/codesearchresults"
        params = {"api-version": "7.1-preview.1"}
        body: dict = {
            "searchText": search_text,
            "$top": 25,
            "filters": {"Project": [self.project]},
        }
        if repo_name:
            body["filters"]["Repository"] = [repo_name]
        resp = requests.post(
            search_url, headers=self._headers, json=body, params=params, timeout=30
        )
        resp.raise_for_status()
        return resp.json().get("results", [])

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
