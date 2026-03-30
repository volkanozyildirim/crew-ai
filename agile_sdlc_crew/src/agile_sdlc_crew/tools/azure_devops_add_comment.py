from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient


class AddCommentInput(BaseModel):
    work_item_id: int = Field(..., description="Azure DevOps work item ID numarasi")
    comment_text: str = Field(..., description="Eklenecek yorum metni")


class AzureDevOpsAddCommentTool(BaseTool):
    name: str = "Azure DevOps Yorum Ekleme"
    description: str = (
        "Azure DevOps'taki bir work item'e yorum ekler. "
        "Analiz sonuclari, test raporlari veya durum guncellemelerini "
        "yorum olarak kaydetmek icin kullanilir."
    )
    args_schema: type[BaseModel] = AddCommentInput

    def _run(self, work_item_id: int, comment_text: str, **kwargs: Any) -> str:
        try:
            client = AzureDevOpsClient()
            data = client.add_comment(work_item_id, comment_text)

            return (
                f"Work item #{work_item_id}'e yorum basariyla eklendi.\n"
                f"Yorum ID: {data.get('id', 'Bilinmiyor')}"
            )

        except Exception as e:
            return f"Yorum eklenirken hata olustu: {e}"
