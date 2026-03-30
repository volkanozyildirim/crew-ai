from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, Field


class CodeReadInput(BaseModel):
    file_path: str = Field(..., description="Okunacak dosyanin yolu")


class CodeReadTool(BaseTool):
    name: str = "Kod Okuma Araci"
    description: str = (
        "Belirtilen dosya yolundaki kodu veya icerigi okur. "
        "Mevcut kodu incelemek, analiz etmek veya referans almak icin kullanilir."
    )
    args_schema: type[BaseModel] = CodeReadInput

    def _run(self, file_path: str, **kwargs: Any) -> str:
        try:
            with open(file_path, encoding="utf-8") as f:
                content = f.read()

            if not content:
                return f"Dosya bos: {file_path}"

            return f"--- {file_path} ---\n{content}"

        except FileNotFoundError:
            return f"Dosya bulunamadi: {file_path}"
        except Exception as e:
            return f"Dosya okunurken hata olustu: {e}"
