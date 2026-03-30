import json
from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient


class GetWorkItemInput(BaseModel):
    work_item_id: int = Field(..., description="Azure DevOps work item ID numarasi")


class AzureDevOpsGetWorkItemTool(BaseTool):
    name: str = "Azure DevOps Work Item Okuma"
    description: str = (
        "Azure DevOps'tan bir work item'in detaylarini okur. "
        "Work item ID'si verilerek baslik, aciklama, durum, atanan kisi, "
        "kabul kriterleri gibi tum bilgileri getirir."
    )
    args_schema: type[BaseModel] = GetWorkItemInput

    def _run(self, work_item_id: int, **kwargs: Any) -> str:
        try:
            client = AzureDevOpsClient()
            data = client.get_work_item(work_item_id)

            fields = data.get("fields", {})
            result = {
                "id": data.get("id"),
                "baslik": fields.get("System.Title", ""),
                "aciklama": fields.get("System.Description", ""),
                "durum": fields.get("System.State", ""),
                "tip": fields.get("System.WorkItemType", ""),
                "atanan_kisi": fields.get("System.AssignedTo", {}).get("displayName", "Atanmamis"),
                "oncelik": fields.get("Microsoft.VSTS.Common.Priority", ""),
                "kabul_kriterleri": fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", ""),
                "etiketler": fields.get("System.Tags", ""),
                "olusturulma_tarihi": fields.get("System.CreatedDate", ""),
                "son_guncelleme": fields.get("System.ChangedDate", ""),
                "alan_yolu": fields.get("System.AreaPath", ""),
                "iterasyon_yolu": fields.get("System.IterationPath", ""),
            }

            return json.dumps(result, ensure_ascii=False, indent=2)

        except Exception as e:
            return f"Work item okunurken hata olustu: {e}"
