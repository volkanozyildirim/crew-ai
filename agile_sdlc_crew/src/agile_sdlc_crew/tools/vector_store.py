"""LanceDB vector store — repo kodu semantic search + job gecmisi.

CrewAI Memory sinifini kullanarak:
- /repos/{repo_name}/code → dosya chunk'lari
- /jobs/{work_item_id}/{step} → step ciktilari
"""

import hashlib
import logging
import os
from pathlib import Path

log = logging.getLogger("pipeline")

# Kod dosyasi uzantilari
CODE_EXTENSIONS = {
    ".php", ".go", ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".cs",
    ".rb", ".rs", ".vue", ".sql", ".sh", ".yaml", ".yml", ".json",
}

# Atlanacak dizinler
SKIP_DIRS = {
    ".git", "node_modules", "vendor", ".idea", ".vscode", "__pycache__",
    "dist", "build", ".next", "storage", "cache", "logs",
}

# Max dosya boyutu (byte)
MAX_FILE_SIZE = 50_000

# Chunk ayarlari
CHUNK_LINES = 200
CHUNK_OVERLAP = 20
MAX_CHUNKS_PER_REPO = 5000


def _chunk_file(content: str, file_path: str) -> list[dict]:
    """Dosya icerigini chunk'lara bol."""
    lines = content.split("\n")
    if len(lines) <= CHUNK_LINES:
        return [{"content": content, "start": 1, "end": len(lines)}]

    chunks = []
    i = 0
    while i < len(lines):
        end = min(i + CHUNK_LINES, len(lines))
        chunk_text = "\n".join(lines[i:end])
        chunks.append({"content": chunk_text, "start": i + 1, "end": end})
        i += CHUNK_LINES - CHUNK_OVERLAP
    return chunks


def _content_hash(content: str) -> str:
    return hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()[:12]


def _extract_focused_sections(md_content: str, repo_name: str) -> str:
    """REPO_SUMMARY.md'den sadece ayirt edici bolumleri cikar.
    Dependencies ve generic ust-seviye dizinleri atla — semantic matching'i bozuyorlar."""
    # Repo adi prefix (her zaman dahil)
    result = [f"Repository: {repo_name}"]

    # Bolumleri parse et
    sections = {}
    current = None
    for line in md_content.split("\n"):
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current:
            sections[current].append(line)

    # Tut: Ozet, README, Domain Bilesenleri
    # Atla: Ust Seviye Dizinler, Onemli Bagimliliklar (semantic gurultu)
    for key in ("Ozet", "README", "Domain Bilesenleri"):
        if key in sections:
            body = "\n".join(sections[key]).strip()
            if body:
                result.append(f"\n## {key}\n{body}")

    out = "\n".join(result)
    return out[:2500]  # max 2500 char — yeterince sinyal, minimal gurultu


class VectorStore:
    """LanceDB uzerinde repo kodu ve job gecmisi icin vector store."""

    def __init__(self, db_path: str | None = None):
        self._db_path = str(Path(
            db_path or os.environ.get("CREW_VECTOR_DB", "~/.crew_repos/.vectordb")
        ).expanduser())
        self._memory = None
        self._indexed_repos: set[str] = set()

    @property
    def memory(self):
        """Lazy init — Ollama local embedding + litellm proxy LLM."""
        if self._memory is None:
            from crewai.memory import Memory
            from crewai import LLM

            # LLM: litellm proxy
            base_url = os.environ.get("LITELLM_BASE_URL", "")
            api_key = os.environ.get("LITELLM_API_KEY", "")
            model = os.environ.get("LITELLM_MODEL", "o4-mini")
            if base_url and not model.startswith("openai/"):
                model = f"openai/{model}"
            llm = LLM(model=model, base_url=base_url, api_key=api_key, max_tokens=1024)

            # Embedder: Ollama local (nomic-embed-text, 768 boyut, ucretsiz)
            embedder_config = {
                "provider": "ollama",
                "config": {
                    "model": "nomic-embed-text",
                    "url": "http://localhost:11434",
                },
            }

            self._memory = Memory(
                storage=self._db_path,
                llm=llm,
                embedder=embedder_config,
                root_scope="/sdlc",
                read_only=False,
            )
        return self._memory

    # ── Repo Kodu ──────────────────────────────────

    def index_repo_summary(self, repo_name: str, repo_path: str | Path):
        """REPO_SUMMARY.md'nin en ayirt edici kisimlarini vector DB'ye embed et.
        Odak: repo adi + README + Domain Bilesenleri (Controller/Service/Widget/Command).
        Uzun dependency listeleri ve generic dizinler embed edilmiyor — gurultuyu azaltmak icin."""
        repo_path = Path(repo_path)
        summary_file = repo_path / "REPO_SUMMARY.md"
        if not summary_file.exists():
            return

        scope = "/repo-summaries"

        # Bu repo zaten index'lenmis mi?
        try:
            existing = self.memory.recall(
                query=repo_name,
                scope=scope,
                limit=3,
                depth="shallow",
            )
            for m in existing:
                if m.record.metadata.get("repo") == repo_name:
                    return  # zaten var
        except Exception:
            pass

        try:
            content = summary_file.read_text(encoding="utf-8", errors="replace")
            # Sadece en ayirt edici kisimlari al: Ozet + README + Domain Bilesenleri
            focused = _extract_focused_sections(content, repo_name)
            self.memory.remember(
                content=focused,
                scope=scope,
                categories=["repo-summary"],
                metadata={"repo": repo_name, "type": "summary"},
                importance=0.9,
            )
        except Exception as e:
            log.warning(f"  Summary index hatasi ({repo_name}): {e}")

    def find_relevant_repos(self, query: str, limit: int = 5) -> list[dict]:
        """Bir sorgu icin hangi repo(lar)da aranmali? Summary'ler uzerinden semantic arama.
        Returns: [{repo: name, score: float, summary_excerpt: str}]"""
        try:
            matches = self.memory.recall(
                query=query,
                scope="/repo-summaries",
                limit=limit,
                depth="shallow",
            )
            results = []
            for m in matches:
                repo = m.record.metadata.get("repo", "?")
                results.append({
                    "repo": repo,
                    "score": round(m.score, 3),
                    "summary_excerpt": m.record.content[:400],
                })
            return results
        except Exception as e:
            log.warning(f"  find_relevant_repos hatasi: {e}")
            return []

    def index_repo(self, repo_name: str, repo_path: str | Path):
        """Repo dosyalarini chunk'layip embed et. Idempotent — zaten index'lenmis dosyalari atlar.
        REPO_SUMMARY.md de ayri scope'ta embed edilir."""
        repo_path = Path(repo_path)
        if not repo_path.exists():
            return

        # REPO_SUMMARY'yi embed et (hafif islem, chunk'lardan onceliki)
        self.index_repo_summary(repo_name, repo_path)

        # Zaten bu session'da index'lendi mi?
        if repo_name in self._indexed_repos:
            return

        scope = f"/repos/{repo_name}/code"

        # Mevcut index'te kac kayit var?
        try:
            info = self.memory.info(scope)
            if info and info.record_count > 0:
                log.info(f"  Vector index mevcut: {repo_name} ({info.record_count} chunk)")
                self._indexed_repos.add(repo_name)
                return
        except Exception:
            pass

        log.info(f"  Repo indeksleniyor: {repo_name}")
        chunk_count = 0

        for fpath in sorted(repo_path.rglob("*")):
            if chunk_count >= MAX_CHUNKS_PER_REPO:
                break

            # Filtrele
            if not fpath.is_file():
                continue
            if fpath.suffix.lower() not in CODE_EXTENSIONS:
                continue
            if any(skip in fpath.parts for skip in SKIP_DIRS):
                continue
            if fpath.stat().st_size > MAX_FILE_SIZE:
                continue

            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            if not content.strip():
                continue

            rel_path = str(fpath.relative_to(repo_path))
            chunks = _chunk_file(content, rel_path)

            # Dil tespiti
            ext_map = {
                ".php": "php", ".go": "go", ".py": "python", ".js": "javascript",
                ".ts": "typescript", ".jsx": "react", ".tsx": "react",
                ".java": "java", ".cs": "csharp", ".rb": "ruby", ".rs": "rust",
            }
            lang = ext_map.get(fpath.suffix.lower(), "other")

            # Parent dizin (kategori olarak)
            parent_dir = str(fpath.parent.relative_to(repo_path))
            if parent_dir == ".":
                parent_dir = "root"

            for chunk in chunks:
                if chunk_count >= MAX_CHUNKS_PER_REPO:
                    break
                try:
                    self.memory.remember(
                        content=f"/{rel_path}:{chunk['start']}-{chunk['end']}\n{chunk['content']}",
                        scope=scope,
                        categories=[lang, parent_dir],
                        metadata={
                            "file_path": f"/{rel_path}",
                            "start_line": chunk["start"],
                            "end_line": chunk["end"],
                            "repo": repo_name,
                            "hash": _content_hash(chunk["content"]),
                        },
                        importance=0.5,
                    )
                    chunk_count += 1
                except Exception as e:
                    log.warning(f"  Chunk index hatasi ({rel_path}): {e}")
                    break

        self._indexed_repos.add(repo_name)
        log.info(f"  Repo indekslendi: {repo_name} ({chunk_count} chunk)")

    def search_code(self, repo_name: str, query: str, limit: int = 10) -> list[dict]:
        """Semantic kod arama. Sonuclari dict listesi olarak dondurur."""
        scope = f"/repos/{repo_name}/code"
        try:
            matches = self.memory.recall(
                query=query,
                scope=scope,
                limit=limit,
                depth="shallow",
            )
            results = []
            for m in matches:
                results.append({
                    "file_path": m.record.metadata.get("file_path", "?"),
                    "lines": f"{m.record.metadata.get('start_line', '?')}-{m.record.metadata.get('end_line', '?')}",
                    "score": round(m.score, 3),
                    "content": m.record.content[:500],
                    "repo": repo_name,
                })
            return results
        except Exception as e:
            log.warning(f"  Semantic search hatasi: {e}")
            return []

    # ── Job Gecmisi ────────────────────────────────

    def save_step_output(self, work_item_id: str, step_key: str, output: str, metadata: dict | None = None):
        """Tamamlanan step ciktisini embed et."""
        if not output or len(output.strip()) < 20:
            return
        scope = f"/jobs/{work_item_id}/{step_key}"
        meta = {"work_item_id": work_item_id, "step": step_key}
        if metadata:
            meta.update(metadata)
        try:
            self.memory.remember(
                content=output[:5000],
                scope=scope,
                categories=[step_key],
                metadata=meta,
                importance=0.7,
            )
        except Exception as e:
            log.warning(f"  Step output vector kayit hatasi: {e}")

    def find_similar_jobs(self, query: str, limit: int = 5) -> list[dict]:
        """Benzer onceki is ciktılarini bul."""
        try:
            matches = self.memory.recall(
                query=query,
                scope="/jobs",
                limit=limit,
                depth="shallow",
            )
            results = []
            for m in matches:
                results.append({
                    "work_item_id": m.record.metadata.get("work_item_id", "?"),
                    "step": m.record.metadata.get("step", "?"),
                    "score": round(m.score, 3),
                    "content": m.record.content[:300],
                })
            return results
        except Exception as e:
            log.warning(f"  Similar jobs arama hatasi: {e}")
            return []

    def close(self):
        """Bekleyen yazmalari bitir ve kapat."""
        if self._memory:
            try:
                self._memory.close()
            except Exception:
                pass
