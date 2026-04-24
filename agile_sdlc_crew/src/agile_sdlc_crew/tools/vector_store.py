"""LanceDB vector store — repo kodu semantic search + job gecmisi.

DOGRUDAN LanceDBStorage + Ollama embedder kullanir. CrewAI Memory wrapper
LLM cagrilari (consolidation, field resolution) yaptigi icin kullanmiyoruz —
bizim field'larimizi zaten explicit veriyoruz, merge ihtiyacimiz yok.

Scope'lar:
- /sdlc/repo-summaries → her repo'nun REPO_SUMMARY.md ozeti
- /sdlc/repos/{repo}/code → kod chunk'lari
- /sdlc/jobs/{work_item_id}/{step} → tamamlanan step ciktilari
"""

import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import requests

log = logging.getLogger("pipeline")

# Kod dosyasi uzantilari
CODE_EXTENSIONS = {
    ".php", ".go", ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".cs",
    ".rb", ".rs", ".vue", ".sql", ".sh", ".yaml", ".yml", ".json",
}

SKIP_DIRS = {
    ".git", "node_modules", "vendor", ".idea", ".vscode", "__pycache__",
    "dist", "build", ".next", "storage", "cache", "logs",
}

# Test dosyalari agent icin kod okuma degerine katki saglamiyor,
# embed etmiyoruz (Ollama 500 hatalarinin cogunlugu test dosyalarindan geliyor)
SKIP_FILE_SUFFIXES = (
    "_test.go", ".test.ts", ".test.js", ".test.tsx", ".test.jsx",
    ".spec.ts", ".spec.js", ".spec.tsx", ".spec.jsx",
    "Test.php", "_test.py",
)

MAX_FILE_SIZE = 50_000

# Chunk ayarlari — nomic-embed-text context 2048 token. Kod/JSON gibi yogun
# icerikte 1 token ≈ 2 char olabiliyor, 3500 char ≈ 1750 token guvenli sinir.
# Onceden 6000 idi, bazi test dosyalari 500 hatasi veriyordu.
CHUNK_LINES = 150
CHUNK_OVERLAP = 15
MAX_CHUNK_CHARS = 3500
MAX_CHUNKS_PER_REPO = 5000

# Ollama embedding
# mxbai-embed-large: nomic-embed-text'e gore cok daha iyi semantic ayirt etme
# (nomic'te alakasiz metinler bile 0.65 skor aliyordu, mxbai'de alakali 0.72 vs alakasiz 0.42)
EMBED_MODEL = os.environ.get("CREW_EMBED_MODEL", "mxbai-embed-large")
EMBED_DIM = 1024  # mxbai-embed-large 1024 boyutlu, nomic 768


def _chunk_file(content: str, file_path: str) -> list[dict]:
    """Dosya icerigini chunk'lara bol. Hem satir hem karakter limit'ine uy."""
    lines = content.split("\n")
    if len(lines) <= CHUNK_LINES and len(content) <= MAX_CHUNK_CHARS:
        return [{"content": content, "start": 1, "end": len(lines)}]

    chunks = []
    i = 0
    while i < len(lines):
        end = min(i + CHUNK_LINES, len(lines))
        while end > i + 10:
            chunk_text = "\n".join(lines[i:end])
            if len(chunk_text) <= MAX_CHUNK_CHARS:
                break
            end = i + (end - i) // 2
        chunk_text = "\n".join(lines[i:end])
        if len(chunk_text) > MAX_CHUNK_CHARS:
            chunk_text = chunk_text[:MAX_CHUNK_CHARS]
        chunks.append({"content": chunk_text, "start": i + 1, "end": end})
        next_i = end - CHUNK_OVERLAP
        if next_i <= i:
            next_i = end
        i = next_i
    return chunks


def _content_hash(content: str) -> str:
    return hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()[:12]


def _extract_focused_sections(md_content: str, repo_name: str) -> str:
    """REPO_SUMMARY.md'den sadece ayirt edici bolumleri cikar."""
    result = [f"Repository: {repo_name}"]
    sections = {}
    current = None
    for line in md_content.split("\n"):
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current:
            sections[current].append(line)

    for key in ("Ozet", "README", "Domain Bilesenleri"):
        if key in sections:
            body = "\n".join(sections[key]).strip()
            if body:
                result.append(f"\n## {key}\n{body}")

    return "\n".join(result)[:2500]


def _embed_text(text: str, retries: int = 4) -> list[float]:
    """Ollama mxbai-embed-large ile embedding uret. 500 hatasinda retry.
    LLM cagrisi YOK — sadece HTTP POST.
    Ollama model yuklerken veya concurrent istek limitinde 500 verebilir,
    4 retry + exponential backoff (1s, 2s, 4s, 8s) ile cogu gecici hatadan kurtulur."""
    import time as _time
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                f"{base_url}/api/embeddings",
                json={"model": EMBED_MODEL, "prompt": text[:MAX_CHUNK_CHARS]},
                timeout=90,
            )
            resp.raise_for_status()
            return resp.json()["embedding"]
        except requests.HTTPError as e:
            last_err = e
            if e.response.status_code == 500 and attempt < retries:
                _time.sleep(1.0 * (2 ** attempt))  # backoff: 1s, 2s, 4s, 8s
                continue
            raise
        except Exception as e:
            last_err = e
            if attempt < retries:
                _time.sleep(1.0 * (2 ** attempt))
                continue
            raise
    raise last_err


class VectorStore:
    """LanceDB uzerinde repo kodu ve job gecmisi. LLM kullanmadan direkt embed."""

    def __init__(self, db_path: str | None = None):
        self._db_path = str(Path(
            db_path or os.environ.get("CREW_VECTOR_DB", "~/.crew_repos/.vectordb")
        ).expanduser())
        self._storage = None
        self._indexed_repos: set[str] = set()

    @property
    def storage(self):
        """Lazy init — direct LanceDBStorage, LLM YOK."""
        if self._storage is None:
            from crewai.memory.storage.lancedb_storage import LanceDBStorage
            self._storage = LanceDBStorage(
                path=self._db_path,
                vector_dim=EMBED_DIM,
            )
        return self._storage

    # Uyumluluk icin — bazi kodlar self._memory kontrol ediyor olabilir
    @property
    def _memory(self):
        """Uyumluluk kontrollari icin — storage aktifse True gibi davran."""
        return self.storage if self._storage or True else None

    def _save_record(self, content: str, scope: str, categories: list, metadata: dict, importance: float = 0.5):
        """Tek bir kayit embed et ve LanceDB'ye yaz."""
        from crewai.memory.types import MemoryRecord
        embedding = _embed_text(content)
        record = MemoryRecord(
            content=content,
            scope=scope,
            categories=categories,
            metadata=metadata,
            importance=importance,
            embedding=embedding,
        )
        self.storage.save([record])

    def _search(self, query: str, scope_prefix: str, limit: int = 10, min_score: float = 0.0) -> list:
        """Vector search — query'yi embed edip LanceDB'de ara."""
        query_emb = _embed_text(query)
        results = self.storage.search(
            query_embedding=query_emb,
            scope_prefix=scope_prefix,
            limit=limit,
            min_score=min_score,
        )
        return results  # list of (MemoryRecord, score)

    # ── Repo Summary ──────────────────────────────────

    def index_repo_summary(self, repo_name: str, repo_path):
        """REPO_SUMMARY.md'yi vector DB'ye embed et."""
        repo_path = Path(repo_path)
        summary_file = repo_path / "REPO_SUMMARY.md"
        if not summary_file.exists():
            return

        scope = "/repo-summaries"
        # Zaten var mi kontrol et (basit liste)
        try:
            info = self.storage.get_scope_info(scope)
            if info and info.record_count > 0:
                existing = self.storage.list_records(scope, limit=200)
                for r in existing:
                    if r.metadata.get("repo") == repo_name:
                        return  # zaten var
        except Exception:
            pass

        try:
            content = summary_file.read_text(encoding="utf-8", errors="replace")
            focused = _extract_focused_sections(content, repo_name)
            self._save_record(
                content=focused,
                scope=scope,
                categories=["repo-summary"],
                metadata={"repo": repo_name, "type": "summary"},
                importance=0.9,
            )
        except Exception as e:
            log.warning(f"  Summary index hatasi ({repo_name}): {e}")

    def find_relevant_repos(self, query: str, limit: int = 5) -> list[dict]:
        """REPO_SUMMARY'ler uzerinden semantic arama."""
        try:
            results = self._search(query, "/repo-summaries", limit=limit)
            out = []
            for record, score in results:
                out.append({
                    "repo": record.metadata.get("repo", "?"),
                    "score": round(score, 3),
                    "summary_excerpt": record.content[:400],
                })
            return out
        except Exception as e:
            log.warning(f"  find_relevant_repos hatasi: {e}")
            return []

    # ── Repo Kodu ──────────────────────────────────

    def index_repo(self, repo_name: str, repo_path):
        """Tum repo'yu embed et. Dikkat: buyuk repolarda uzun surer ve gereksiz olabilir.
        Targeted embed icin index_plan_files() kullan."""
        repo_path = Path(repo_path)
        if not repo_path.exists():
            return

        self.index_repo_summary(repo_name, repo_path)

        if repo_name in self._indexed_repos:
            return

        scope = f"/repos/{repo_name}/code"
        try:
            info = self.storage.get_scope_info(scope)
            if info and info.record_count > 0:
                log.info(f"  Vector index mevcut: {repo_name} ({info.record_count} chunk)")
                self._indexed_repos.add(repo_name)
                return
        except Exception:
            pass

        log.info(f"  Tum repo indeksleniyor: {repo_name}")
        chunk_count, failed = self._index_files(
            repo_name, repo_path,
            files=[f for f in repo_path.rglob("*") if f.is_file()],
            scope=scope,
        )
        self._indexed_repos.add(repo_name)
        log.info(f"  Repo indekslendi: {repo_name} ({chunk_count} chunk, {failed} hata)")

    def index_plan_files(self, repo_name: str, repo_path, plan_file_paths: list[str]):
        """HEDEF ODAKLI embed: plan'daki dosyalarin parent dizinlerindeki kodlari embed et.
        Cok daha hizli, agent yine de semantic arama yapabilir ama dar kapsamda."""
        repo_path = Path(repo_path)
        if not repo_path.exists() or not plan_file_paths:
            return

        self.index_repo_summary(repo_name, repo_path)

        # Plan'daki dosyalarin parent dizinlerini cikar
        target_dirs: set[Path] = set()
        for fp in plan_file_paths:
            clean = fp.lstrip("/")
            p = repo_path / clean
            # Dosyanin dizini + bir ust dizin (siblinglar icin)
            if p.parent.exists() and p.parent != repo_path:
                target_dirs.add(p.parent)
                if p.parent.parent != repo_path and p.parent.parent.exists():
                    target_dirs.add(p.parent.parent)

        if not target_dirs:
            log.info(f"  Plan'da gecerli dosya dizini yok, embed atlaniyor")
            return

        scope = f"/repos/{repo_name}/code"
        # Hedef dizinlerdeki dosyalari topla — SADECE plan dosyalarinin
        # dogrudan komşuları (rglob yerine iterdir, max 20 dosya).
        # Onceki 104 dosya × chunk × embed = 10dk+ bloke ediyordu.
        MAX_PLAN_FILES = 20
        files = []
        for d in target_dirs:
            try:
                for f in sorted(d.iterdir()):
                    if f.is_file() and len(files) < MAX_PLAN_FILES:
                        files.append(f)
            except Exception:
                continue

        log.info(
            f"  Hedef odakli embed: {repo_name} "
            f"({len(target_dirs)} dizin, {len(files)} dosya aday, max {MAX_PLAN_FILES})"
        )
        chunk_count, failed = self._index_files(repo_name, repo_path, files, scope)
        self._indexed_repos.add(repo_name)
        log.info(f"  Hedef embed tamam: {chunk_count} chunk, {failed} hata")

    def _index_files(self, repo_name: str, repo_path: Path, files: list, scope: str) -> tuple[int, int]:
        """Ortak dosya→chunk→embed loop'u. (chunk_count, failed, skipped) raporlar.
        DEDUP: scope'ta ayni (file_path, hash) zaten varsa chunk yeniden embed edilmez."""
        chunk_count = 0
        failed = 0
        skipped = 0

        # Mevcut chunk hash'lerini topla — tekrar embed etmeyelim
        existing_hashes: set[tuple[str, str]] = set()
        try:
            info = self.storage.get_scope_info(scope)
            if info and info.record_count > 0:
                # Scope buyuk olabilir, generous limit
                existing = self.storage.list_records(scope, limit=10_000)
                for r in existing:
                    fp = r.metadata.get("file_path", "")
                    h = r.metadata.get("hash", "")
                    if fp and h:
                        existing_hashes.add((fp, h))
                if existing_hashes:
                    log.info(f"  Dedup: {len(existing_hashes)} mevcut chunk scope'ta var")
        except Exception as e:
            log.debug(f"  Dedup icin list_records atlandi: {e}")

        file_idx = 0
        total_files = len(files)
        for fpath in sorted(files):
            if chunk_count >= MAX_CHUNKS_PER_REPO:
                break
            if not fpath.is_file():
                continue
            if fpath.suffix.lower() not in CODE_EXTENSIONS:
                continue
            if any(skip in fpath.parts for skip in SKIP_DIRS):
                continue
            if fpath.name.endswith(SKIP_FILE_SUFFIXES):
                continue
            if fpath.stat().st_size > MAX_FILE_SIZE:
                continue

            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if not content.strip():
                continue

            file_idx += 1
            rel_path = str(fpath.relative_to(repo_path))
            log.info(f"  Embed [{file_idx}/{total_files}]: {rel_path}")
            chunks = _chunk_file(content, rel_path)

            ext_map = {
                ".php": "php", ".go": "go", ".py": "python", ".js": "javascript",
                ".ts": "typescript", ".jsx": "react", ".tsx": "react",
                ".java": "java", ".cs": "csharp", ".rb": "ruby", ".rs": "rust",
            }
            lang = ext_map.get(fpath.suffix.lower(), "other")
            parent_dir = str(fpath.parent.relative_to(repo_path))
            if parent_dir == ".":
                parent_dir = "root"

            for chunk in chunks:
                if chunk_count >= MAX_CHUNKS_PER_REPO:
                    break
                chunk_hash = _content_hash(chunk["content"])
                fp_key = f"/{rel_path}"
                # Dedup: ayni dosya + ayni hash daha once embed edildiyse atla
                if (fp_key, chunk_hash) in existing_hashes:
                    skipped += 1
                    continue
                try:
                    self._save_record(
                        content=f"/{rel_path}:{chunk['start']}-{chunk['end']}\n{chunk['content']}",
                        scope=scope,
                        categories=[lang, parent_dir],
                        metadata={
                            "file_path": fp_key,
                            "start_line": chunk["start"],
                            "end_line": chunk["end"],
                            "repo": repo_name,
                            "hash": chunk_hash,
                        },
                        importance=0.5,
                    )
                    chunk_count += 1
                    existing_hashes.add((fp_key, chunk_hash))
                except Exception as e:
                    failed += 1
                    if failed <= 3:
                        log.warning(f"  Chunk index hatasi ({rel_path}): {e}")

        if skipped:
            log.info(f"  Dedup: {skipped} chunk zaten var, atlandi")
        return chunk_count, failed

    def search_code(self, repo_name: str, query: str, limit: int = 10) -> list[dict]:
        """Semantic kod arama."""
        scope = f"/repos/{repo_name}/code"
        try:
            results = self._search(query, scope, limit=limit)
            out = []
            for record, score in results:
                out.append({
                    "file_path": record.metadata.get("file_path", "?"),
                    "lines": f"{record.metadata.get('start_line', '?')}-{record.metadata.get('end_line', '?')}",
                    "score": round(score, 3),
                    "content": record.content[:500],
                    "repo": repo_name,
                })
            return out
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
            self._save_record(
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
            results = self._search(query, "/jobs", limit=limit)
            out = []
            for record, score in results:
                out.append({
                    "work_item_id": record.metadata.get("work_item_id", "?"),
                    "step": record.metadata.get("step", "?"),
                    "score": round(score, 3),
                    "content": record.content[:300],
                })
            return out
        except Exception as e:
            log.warning(f"  Similar jobs arama hatasi: {e}")
            return []

    def close(self):
        """Pending writes'i bitir."""
        pass  # LanceDBStorage kendisi sync
