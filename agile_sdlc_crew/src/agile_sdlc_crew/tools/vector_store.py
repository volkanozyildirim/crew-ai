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
    ".git", ".idea", ".vscode", "__pycache__",
    "dist", "build", ".next", "storage", "cache", "logs",
}
# vendor / node_modules — varsayilan olarak SKIP, ama allowlist ile secili
# alt paketleri index'e dahil edebiliyoruz (3rd-party framework kodunu okumak icin).
VENDOR_ROOTS = {"vendor", "node_modules"}

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

# Embedding configuration delegated to agile_sdlc_crew.embed package
from agile_sdlc_crew.embed import (  # noqa: E402
    KNOWN_EMBED_DIMS,
    embed_text as _registry_embed,
    get_api_key as get_embed_api_key,
    get_base_url as get_embed_base_url,
    get_dim as get_embed_dim,
    get_model as get_embed_model,
    get_provider as get_embed_provider,
    save_config as save_embed_config,
)

# Geriye uyumluluk shim'leri
EMBED_MODEL = get_embed_model()
EMBED_DIM = get_embed_dim()


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
    """Embedding registry uzerinden vector uret.

    Provider/model/base_url/api_key degerleri embed/resolver.py tarafindan
    config'ten okunur. 500 ve baglanti hatalarinda exponential backoff retry."""
    import time as _time
    provider = get_embed_provider()
    model = get_embed_model()
    base_url = get_embed_base_url()
    api_key = get_embed_api_key()

    last_err = None
    for attempt in range(retries + 1):
        try:
            return _registry_embed(
                provider=provider,
                text=text[:MAX_CHUNK_CHARS],
                model=model,
                base_url=base_url,
                api_key=api_key,
            )
        except requests.HTTPError as e:
            last_err = e
            if e.response is not None and e.response.status_code == 500 and attempt < retries:
                _time.sleep(1.0 * (2 ** attempt))
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
        """Lazy init — direct LanceDBStorage, LLM YOK.

        Mevcut tablo dim'i ile config'in dim'i uyusmuyorsa tabloyu drop eder.
        Aksi halde LanceDB eski semayi korur, sorgu/ekleme hata verir."""
        if self._storage is None:
            from crewai.memory.storage.lancedb_storage import LanceDBStorage
            self._reset_table_if_dim_mismatch(get_embed_dim())
            self._storage = LanceDBStorage(
                path=self._db_path,
                vector_dim=get_embed_dim(),
            )
        return self._storage

    def _reset_table_if_dim_mismatch(self, expected_dim: int) -> None:
        """Diskteki LanceDB tablosunun vector dim'ini config ile karsilastir,
        uyusmazsa tabloyu drop et — yeni dim ile temiz baslangic.

        Bilinen senaryo: Embedding modeli degistirildi (orn. 384 → 1024).
        Eski tabloyu yeni vector_dim parametresiyle acmak LanceDB'de etkisiz;
        tablo silmeden yeni embed'ler eklenmiyor."""
        try:
            import lancedb
        except ImportError:
            return
        if not Path(self._db_path).exists():
            return
        try:
            db = lancedb.connect(self._db_path)
            tables_resp = db.list_tables()
            # lancedb yeni surumlerde ListTablesResponse objesi donduruyor
            # (.tables attr); eski surumlerde direkt list. Iki durumu da destekle.
            tnames = getattr(tables_resp, "tables", None) or list(tables_resp)
            for tname in tnames:
                t = db.open_table(tname)
                for field in t.schema:
                    if field.name == "vector" and hasattr(field.type, "list_size"):
                        actual = field.type.list_size
                        if actual != expected_dim:
                            log.warning(
                                f"  Vector DB dim uyusmazligi: tablo '{tname}' "
                                f"dim={actual}, config dim={expected_dim} — tablo siliniyor"
                            )
                            db.drop_table(tname)
        except Exception as e:
            log.warning(f"  Vector DB dim kontrol hatasi: {e}")

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

    def index_repo(self, repo_name: str, repo_path, vendor_allowlist: set[str] | None = None):
        """Tum repo'yu embed et. Dikkat: buyuk repolarda uzun surer ve gereksiz olabilir.
        Targeted embed icin index_plan_files() kullan.

        vendor_allowlist verilirse listedeki vendor paketleri de index'e dahil edilir
        (default: vendor/ tamamen skip)."""
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
        if vendor_allowlist:
            log.info(f"  Vendor allowlist: {len(vendor_allowlist)} paket")
        chunk_count, failed = self._index_files(
            repo_name, repo_path,
            files=[f for f in repo_path.rglob("*") if f.is_file()],
            scope=scope,
            vendor_allowlist=vendor_allowlist,
        )
        self._indexed_repos.add(repo_name)
        log.info(f"  Repo indekslendi: {repo_name} ({chunk_count} chunk, {failed} hata)")

    def index_plan_files(
        self,
        repo_name: str,
        repo_path,
        plan_file_paths: list[str],
        vendor_allowlist: set[str] | None = None,
    ):
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
        chunk_count, failed = self._index_files(
            repo_name, repo_path, files, scope, vendor_allowlist=vendor_allowlist,
        )
        self._indexed_repos.add(repo_name)
        log.info(f"  Hedef embed tamam: {chunk_count} chunk, {failed} hata")

    def _index_files(
        self,
        repo_name: str,
        repo_path: Path,
        files: list,
        scope: str,
        vendor_allowlist: set[str] | None = None,
    ) -> tuple[int, int]:
        """Ortak dosya→chunk→embed loop'u. (chunk_count, failed, skipped) raporlar.
        DEDUP: scope'ta ayni (file_path, hash) zaten varsa chunk yeniden embed edilmez.

        vendor_allowlist: vendor/X/Y veya node_modules/Y formatinda relative path
        prefix listesi. Verilirse vendor altindan SADECE bu path'lere uyan dosyalar
        index'e dahil edilir. None/bos ise vendor tamamen skip.
        """
        vendor_allowlist = vendor_allowlist or set()
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

        # Vendor paket basina chunk limiti — tek bir buyuk paket repo budget'ini
        # tuketmesin diye. Default 300 chunk/paket.
        MAX_VENDOR_CHUNKS_PER_PACKAGE = 300
        vendor_pkg_count: dict[str, int] = {}

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
            # Vendor / node_modules — allowlist filter
            try:
                rel_parts = fpath.relative_to(repo_path).parts
            except ValueError:
                rel_parts = fpath.parts
            if rel_parts and rel_parts[0] in VENDOR_ROOTS:
                if not vendor_allowlist:
                    continue  # vendor disabled
                rel_str = "/".join(rel_parts)
                # Allowlist match: dosya yolu allowlist prefix'lerinden biriyle basliyor mu?
                pkg_key = None
                for allow in vendor_allowlist:
                    if rel_str == allow or rel_str.startswith(allow.rstrip("/") + "/"):
                        pkg_key = allow
                        break
                if not pkg_key:
                    continue
                # Per-package cap
                if vendor_pkg_count.get(pkg_key, 0) >= MAX_VENDOR_CHUNKS_PER_PACKAGE:
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
                    # Vendor paket sayacini guncelle (allowlist match'in olduysa)
                    if rel_parts and rel_parts[0] in VENDOR_ROOTS:
                        rel_str = "/".join(rel_parts)
                        for allow in vendor_allowlist:
                            if rel_str == allow or rel_str.startswith(allow.rstrip("/") + "/"):
                                vendor_pkg_count[allow] = vendor_pkg_count.get(allow, 0) + 1
                                break
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
