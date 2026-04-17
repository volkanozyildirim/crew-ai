import json
from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient


class UpdateWorkItemInput(BaseModel):
    work_item_id: int = Field(..., description="Azure DevOps work item ID numarasi")
    field_updates: str = Field(
        ...,
        description=(
            'Guncellenecek alanlar JSON formatinda. Ornek: '
            '[{"path": "/fields/System.State", "value": "Active"}]'
        ),
    )


class AzureDevOpsUpdateWorkItemTool(BaseTool):
    name: str = "update_work_item"
    description: str = (
        "Azure DevOps'taki bir work item'in alanlarini gunceller. "
        "Durum, atama, oncelik gibi alanlari degistirebilir. "
        'field_updates parametresi JSON array formatinda olmalidir: '
        '[{"path": "/fields/System.State", "value": "Active"}]'
    )
    args_schema: type[BaseModel] = UpdateWorkItemInput

    def _run(self, work_item_id: int, field_updates: str, **kwargs: Any) -> str:
        try:
            updates = json.loads(field_updates)
            operations = []
            for update in updates:
                operations.append({
                    "op": "replace",
                    "path": update["path"],
                    "value": update["value"],
                })

            client = AzureDevOpsClient()
            data = client.update_work_item(work_item_id, operations)

            fields = data.get("fields", {})
            return (
                f"Work item #{work_item_id} basariyla guncellendi.\n"
                f"Yeni durum: {fields.get('System.State', 'Bilinmiyor')}\n"
                f"Baslik: {fields.get('System.Title', '')}"
            )

        except json.JSONDecodeError:
            return "Hata: field_updates gecerli bir JSON formati degil."
        except Exception as e:
            return f"Work item guncellenirken hata olustu: {e}"
