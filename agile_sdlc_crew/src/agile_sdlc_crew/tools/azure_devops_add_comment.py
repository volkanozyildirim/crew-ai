from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient


class AddCommentInput(BaseModel):
    work_item_id: int = Field(..., description="Azure DevOps work item ID numarasi")
    comment_text: str = Field(
        ...,
        description=(
            "Markdown formatinda yorum metni. "
            "Basliklar icin ## kullan, listeler icin - kullan, "
            "kod icin ``` kullan."
        ),
    )


class AzureDevOpsAddCommentTool(BaseTool):
    name: str = "add_wi_comment"
    description: str = (
        "Work item'a MARKDOWN formatinda yorum ekler. "
        "Yorum icerigi HTML'e cevrilir. Baslik, liste, kod blogu desteklenir."
    )
    args_schema: type[BaseModel] = AddCommentInput

    def _run(self, work_item_id: int, comment_text: str, **kwargs: Any) -> str:
        try:
            client = AzureDevOpsClient()
            # Markdown'i basit HTML'e cevir (Azure DevOps HTML yorum destekler)
            html = _markdown_to_html(comment_text)
            data = client.add_comment(work_item_id, html)
            return f"Work item #{work_item_id}'e yorum eklendi (ID: {data.get('id', '?')})."
        except Exception as e:
            return f"HATA: Yorum eklenemedi: {e}"


def _markdown_to_html(md: str) -> str:
    """Basit markdown -> HTML donusumu."""
    import re
    lines = md.split("\n")
    html_lines = []
    in_code = False
    in_list = False

    for line in lines:
        # Code block
        if line.strip().startswith("```"):
            if in_code:
                html_lines.append("</code></pre>")
                in_code = False
            else:
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                html_lines.append("<pre><code>")
                in_code = True
            continue

        if in_code:
            html_lines.append(line)
            continue

        stripped = line.strip()

        # Headers
        if stripped.startswith("### "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<h3>{stripped[4:]}</h3>")
        elif stripped.startswith("## "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<h2>{stripped[3:]}</h2>")
        elif stripped.startswith("# "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<h1>{stripped[2:]}</h1>")
        # List items
        elif stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            # Inline code
            item = re.sub(r"`([^`]+)`", r"<code>\1</code>", stripped[2:])
            # Bold
            item = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", item)
            html_lines.append(f"<li>{item}</li>")
        # Empty line
        elif not stripped:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append("<br/>")
        # Regular text
        else:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            text = re.sub(r"`([^`]+)`", r"<code>\1</code>", stripped)
            text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
            html_lines.append(f"<p>{text}</p>")

    if in_list:
        html_lines.append("</ul>")
    if in_code:
        html_lines.append("</code></pre>")

    return "\n".join(html_lines)
