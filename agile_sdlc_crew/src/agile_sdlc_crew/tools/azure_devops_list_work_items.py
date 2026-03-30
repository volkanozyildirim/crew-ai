import json
from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient


class ListWorkItemsInput(BaseModel):
    wiql_query: str = Field(
        ...,
        description=(
            "WIQL (Work Item Query Language) sorgusu. Ornek: "
            "\"SELECT [System.Id], [System.Title], [System.State] "
            "FROM WorkItems WHERE [System.State] = 'Active'\""
        ),
    )


class AzureDevOpsListWorkItemsTool(BaseTool):
    name: str = "Azure DevOps Work Item Listeleme"
    description: str = (
        "WIQL sorgusu kullanarak Azure DevOps'tan work item listesi getirir. "
        "Aktif, bekleyen veya belirli kriterlere uyan is kalemlerini listelemek "
        "icin kullanilir."
    )
    args_schema: type[BaseModel] = ListWorkItemsInput

    def _run(self, wiql_query: str, **kwargs: Any) -> str:
        try:
            client = AzureDevOpsClient()
            items = client.query_work_items(wiql_query)

            if not items:
                return "Sorgu sonucu bos: Kriterlere uyan work item bulunamadi."

            results = []
            for item in items:
                fields = item.get("fields", {})
                results.append({
                    "id": item.get("id"),
                    "baslik": fields.get("System.Title", ""),
                    "durum": fields.get("System.State", ""),
                    "tip": fields.get("System.WorkItemType", ""),
                    "atanan_kisi": fields.get("System.AssignedTo", {}).get("displayName", "Atanmamis"),
                    "oncelik": fields.get("Microsoft.VSTS.Common.Priority", ""),
                })

            return json.dumps(results, ensure_ascii=False, indent=2)

        except Exception as e:
            return f"Work item listesi alinirken hata olustu: {e}"
