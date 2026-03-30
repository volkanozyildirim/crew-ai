"""Azure DevOps kod arama araci."""

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient


class AzureDevOpsSearchCodeInput(BaseModel):
    """Kod arama araci icin giris schemasi."""

    search_text: str = Field(
        ...,
        description="Aranacak metin. Sinif adi, fonksiyon adi, degisken adi, "
        "hata mesaji veya herhangi bir kod parcasi olabilir.",
    )
    repo_name: str = Field(
        default="",
        description="Aramayı daraltmak icin repo adi. "
        "Bos birakilirsa tum repolarda arar.",
    )


class AzureDevOpsSearchCodeTool(BaseTool):
    name: str = "Azure DevOps Kod Arama"
    description: str = (
        "Azure DevOps repolarinda kod arar. Sinif adlari, fonksiyonlar, "
        "API endpoint'leri, konfigurasyonlar, hata mesajlari ve "
        "diger kod parcalarini bulur. Repolar arasi baglantilari "
        "ve bagimliliklari kesfetmek icin kullanin."
    )
    args_schema: type[BaseModel] = AzureDevOpsSearchCodeInput

    def _run(self, search_text: str, repo_name: str = "") -> str:
        try:
            client = AzureDevOpsClient()
            repo_arg = repo_name if repo_name else None
            results = client.search_code(search_text, repo_name=repo_arg)

            if not results:
                scope = f"'{repo_name}' reposunda" if repo_name else "tum repolarda"
                return f"'{search_text}' icin {scope} sonuc bulunamadi."

            lines = [f"## Kod Arama Sonuclari: '{search_text}'\n"]
            lines.append(f"**{len(results)} sonuc bulundu**\n")

            for i, r in enumerate(results[:20], 1):
                repo = r.get("repository", {}).get("name", "?")
                fpath = r.get("path", "?")
                fname = r.get("fileName", "?")
                matches = r.get("matches", {})

                lines.append(f"### {i}. {repo}/{fpath}")
                lines.append(f"- **Dosya:** {fname}")
                lines.append(f"- **Repo:** {repo}")

                # Eslesme satirlarini goster
                content_matches = matches.get("content", [])
                if content_matches:
                    for m in content_matches[:3]:
                        char_offset = m.get("charOffset", 0)
                        length = m.get("length", 0)
                        lines.append(f"- **Eslesme pozisyonu:** karakter {char_offset}, uzunluk {length}")

                lines.append("")

            return "\n".join(lines)

        except Exception as e:
            return f"Kod aranirken hata: {e}"
