"""Azure DevOps repo icerigi gozden gecirme araci."""

import json

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient


class AzureDevOpsBrowseRepoInput(BaseModel):
    """Repo gozden gecirme araci icin giris schemasi."""

    repo_name: str = Field(
        ...,
        description="Gozden gecirilecek reponun adi. "
        "Once 'Azure DevOps Repo Listeleme' araci ile repo listesini alin.",
    )
    path: str = Field(
        default="/",
        description="Gozden gecirilecek dizin yolu. Ornek: '/', '/src', '/src/api'",
    )
    branch: str = Field(
        default="",
        description="Branch adi. Bos birakilirsa varsayilan dal kullanilir.",
    )
    include_file_content: bool = Field(
        default=False,
        description="True ise dosya iceriklerini de getirir (sadece kucuk dosyalar icin).",
    )


class AzureDevOpsBrowseRepoTool(BaseTool):
    name: str = "Azure DevOps Repo Icerik Gozden Gecirme"
    description: str = (
        "Azure DevOps'taki bir Git reposunun dizin yapisini ve dosyalarini gozden gecirir. "
        "Belirtilen dizindeki dosya/klasorleri listeler, branch bilgisi ve dosya iceriklerini okur. "
        "Reponun yapisini, kullanilan teknolojileri ve mimari yapiyi anlamak icin kullanin."
    )
    args_schema: type[BaseModel] = AzureDevOpsBrowseRepoInput

    def _run(
        self,
        repo_name: str,
        path: str = "/",
        branch: str = "",
        include_file_content: bool = False,
    ) -> str:
        try:
            client = AzureDevOpsClient()
            branch_arg = branch if branch else None

            # Dizin icerigini getir
            items = client.get_items_in_path(
                repo_name, path=path, branch=branch_arg, recursion_level="oneLevel"
            )

            if not items:
                return f"'{repo_name}' reposunda '{path}' yolunda icerik bulunamadi."

            lines = [f"## {repo_name} - {path}\n"]

            folders = []
            files = []
            for item in items:
                item_path = item.get("path", "")
                is_folder = item.get("isFolder", False)
                if item_path == path:
                    continue  # kendisini atla
                if is_folder:
                    folders.append(item_path)
                else:
                    files.append(item_path)

            if folders:
                lines.append("### Klasorler:")
                for f in sorted(folders):
                    lines.append(f"  - {f}/")
                lines.append("")

            if files:
                lines.append("### Dosyalar:")
                for f in sorted(files):
                    lines.append(f"  - {f}")
                lines.append("")

            # Onemli dosyalari otomatik oku (config/proje dosyalari)
            config_patterns = [
                "package.json", "pom.xml", "build.gradle", "Cargo.toml",
                "requirements.txt", "setup.py", "pyproject.toml",
                "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
                ".csproj", "appsettings.json", "Startup.cs", "Program.cs",
                "go.mod", "Makefile", "README.md",
                "tsconfig.json", "angular.json", "next.config.js",
                "webpack.config.js", "vite.config.ts",
                ".env.example", "launchSettings.json",
            ]

            auto_read_files = []
            for f in files:
                fname = f.split("/")[-1]
                if fname in config_patterns or fname.endswith((".csproj", ".sln")):
                    auto_read_files.append(f)

            if include_file_content:
                auto_read_files = files  # Tum dosyalari oku

            if auto_read_files:
                lines.append("### Proje Konfigurasyonu / Onemli Dosyalar:")
                for fpath in auto_read_files[:10]:  # Max 10 dosya
                    try:
                        content = client.get_file_content(
                            repo_name, fpath, branch=branch_arg
                        )
                        # Cok buyuk dosyalari kirp
                        if len(content) > 3000:
                            content = content[:3000] + "\n... (kirpildi)"
                        lines.append(f"\n#### {fpath}")
                        lines.append(f"```\n{content}\n```")
                    except Exception:
                        lines.append(f"\n#### {fpath} (okunamadi)")

            # Branch bilgisi
            try:
                branches = client.list_branches(repo_name)
                if branches:
                    branch_names = [
                        b.get("name", "").replace("refs/heads/", "")
                        for b in branches
                    ]
                    lines.append(f"\n### Branch'ler ({len(branch_names)}):")
                    for b in branch_names[:20]:
                        lines.append(f"  - {b}")
            except Exception:
                pass

            # Son commit'ler
            try:
                commits = client.get_recent_commits(
                    repo_name, branch=branch_arg, top=5
                )
                if commits:
                    lines.append("\n### Son Commit'ler:")
                    for c in commits:
                        author = c.get("author", {}).get("name", "?")
                        date = c.get("author", {}).get("date", "?")[:10]
                        comment = c.get("comment", "?")[:80]
                        lines.append(f"  - [{date}] {author}: {comment}")
            except Exception:
                pass

            return "\n".join(lines)

        except Exception as e:
            return f"Repo gozden gecirilirken hata: {e}"
