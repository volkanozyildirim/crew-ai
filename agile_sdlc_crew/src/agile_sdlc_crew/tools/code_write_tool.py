import os
from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, Field


class CodeWriteInput(BaseModel):
    file_path: str = Field(..., description="Yazilacak dosyanin yolu")
    content: str = Field(..., description="Dosyaya yazilacak icerik")
    mode: str = Field(
        default="w",
        description="Yazma modu: 'w' ustune yaz, 'a' sonuna ekle",
    )


class CodeWriteTool(BaseTool):
    name: str = "Kod Yazma Araci"
    description: str = (
        "Belirtilen dosya yoluna kod veya icerik yazar. "
        "Dosya yoksa olusturur, varsa belirtilen moda gore gunceller. "
        "Ust dizinler otomatik olusturulur."
    )
    args_schema: type[BaseModel] = CodeWriteInput

    def _run(self, file_path: str, content: str, mode: str = "w", **kwargs: Any) -> str:
        try:
            parent_dir = os.path.dirname(file_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)

            with open(file_path, mode, encoding="utf-8") as f:
                f.write(content)

            return f"Dosya basariyla yazildi: {file_path} ({len(content)} karakter)"

        except Exception as e:
            return f"Dosya yazilirken hata olustu: {e}"
