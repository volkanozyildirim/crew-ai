"""Agent'lara enjekte edilecek domain knowledge — FLO stack (PHP/Butterfly, Go/Gin, Next.js)."""

from pathlib import Path

_KNOWLEDGE_DIR = Path(__file__).parent


def load_knowledge(name: str) -> str:
    """Knowledge dosyasini oku, icerigini dondur.

    Args:
        name: Dosya adi (.md olmadan). Orn: 'backend_tech_design'

    Returns:
        Dosyanin icerigi veya boş string.
    """
    path = _KNOWLEDGE_DIR / f"{name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    return ""


def knowledge_for_repo_type(repo_type: str) -> str:
    """Repo tipine göre uygun knowledge'ı döndür.

    repo_type: 'php', 'go', 'nextjs', 'python' vb.
    """
    backend = load_knowledge("backend_tech_design")
    frontend = load_knowledge("frontend_nextjs")

    if repo_type in ("php", "go", "butterfly", "gin", "laravel"):
        return backend
    if repo_type in ("nextjs", "react", "vue", "javascript", "typescript"):
        return frontend
    # Bilinmiyorsa backend daha yaygin olduğu için onu döndür
    return backend


def detect_repo_type(repo_name: str, repos_base_dir: str | None = None) -> str:
    """Repo'nun local dizininden tipini tespit et."""
    import os
    from pathlib import Path

    base = Path(repos_base_dir or os.environ.get("CREW_REPOS_DIR", "~/.crew_repos")).expanduser()
    repo_dir = base / repo_name

    if not repo_dir.exists():
        return "unknown"

    if (repo_dir / "composer.json").exists():
        return "php"
    if (repo_dir / "go.mod").exists():
        return "go"
    if (repo_dir / "package.json").exists():
        try:
            import json
            pj = json.loads((repo_dir / "package.json").read_text(encoding="utf-8", errors="replace"))
            deps = {**pj.get("dependencies", {}), **pj.get("devDependencies", {})}
            if "next" in deps:
                return "nextjs"
            if "react" in deps:
                return "react"
            if "vue" in deps:
                return "vue"
            return "javascript"
        except Exception:
            return "javascript"
    if (repo_dir / "requirements.txt").exists() or (repo_dir / "pyproject.toml").exists():
        return "python"

    return "unknown"
