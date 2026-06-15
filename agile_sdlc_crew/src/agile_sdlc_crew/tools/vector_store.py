"""LanceDB vector store — repo kodu semantic search + job gecmisi.

DOGRUDAN LanceDBStorage + Ollama embedder kullanir. CrewAI Memory wrapper
LLM cagrilari (consolidation, field resolution) yaptigi icin kullanmiyoruz —
bizim field'larimizi zaten explicit veriyoruz, merge ihtiyacimiz yok.

Scope'lar:
- /sdlc/repo-summaries → her repo'nun REPO_SUMMARY.md ozeti
- /sdlc/repos/{repo}/code → kod chunk'lari
- /sdlc/jobs/{work_item_id}/{step} → tamamlanan step ciktilari
- /repo-decisions → basarili her is icin WI icerigi + degisen dosya yollari/route'lari → repo eslesmesi (1 kayit/WI)
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


def _extract_routes(text: str) -> list[str]:
    """Metinden route/endpoint ve dosya-adı token'larini cikar (repo-decision indeksi icin).
    flow.py'deki repo-tespit regex desenleriyle tutarli."""
    import re as _re
    routes: set[str] = set()
    for m in _re.finditer(r'/api/[\w/]+', text):
        routes.add(m.group(0))
    for m in _re.finditer(r'\b(\w+\.(?:php|py|ts|tsx|js|jsx|go|cs|java|vue))\b', text):
        routes.add(m.group(1))
    return sorted(routes)


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

    for key in ("Ozet", "README", "Domain Bilesenleri", "DB Tablolari & Migrationlar"):
        if key in sections:
            body = "\n".join(sections[key]).strip()
            if body:
                result.append(f"\n## {key}\n{body}")

    # BM25 indeksi tum metni token'lar — uzun olabilir. Embedding kendi
    # MAX_CHUNK_CHARS'i ile kesiliyor; burada erken cap koymaya gerek yok.
    # Buyuk monolithlerde (orn. orkestra ~14KB) tablo + model listesi sigsin.
    return "\n".join(result)[:20000]


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
        # Hybrid search (BM25 + vector) — CREW_HYBRID_SEARCH ile gate'li
        self._hybrid_enabled = os.environ.get("CREW_HYBRID_SEARCH", "1") != "0"
        self._hybrid = None  # lazy init

    @property
    def hybrid(self):
        """Lazy HybridSearcher. CREW_HYBRID_SEARCH=0 ile None doner (saf vector path)."""
        if not self._hybrid_enabled:
            return None
        if self._hybrid is None:
            try:
                from agile_sdlc_crew.tools.bm25_search import HybridSearcher
                self._hybrid = HybridSearcher(storage_factory=lambda: self.storage)
            except Exception as e:
                log.warning(f"  HybridSearcher import/init hatasi: {e} — saf vector kullanilacak")
                self._hybrid_enabled = False
                return None
        return self._hybrid

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
        # BM25 index'i bu scope icin invalidate et — sonraki search rebuild
        if self.hybrid is not None:
            try:
                self.hybrid.invalidate(scope)
            except Exception:
                pass

    def _search(self, query: str, scope_prefix: str, limit: int = 10, min_score: float = 0.0) -> list:
        """Vector + opsiyonel BM25 hybrid search.

        CREW_HYBRID_SEARCH=1 (default) ile:
          1. Vector'den CREW_BM25_VECTOR_CANDIDATES (default 50) aday al
          2. BM25 sonuclariyla RRF (CREW_RRF_K=60) ile fuse
          3. Top `limit` dondur
        CREW_HYBRID_SEARCH=0 ile: byte-identical eski vector path.
        """
        query_emb = _embed_text(query)
        if self.hybrid is None:
            return self.storage.search(
                query_embedding=query_emb,
                scope_prefix=scope_prefix,
                limit=limit,
                min_score=min_score,
            )
        # Hybrid: vector'den over-fetch et
        vec_k = int(os.environ.get("CREW_BM25_VECTOR_CANDIDATES", "50"))
        vector_results = self.storage.search(
            query_embedding=query_emb,
            scope_prefix=scope_prefix,
            limit=max(vec_k, limit),
            min_score=min_score,
        )
        try:
            return self.hybrid.search(
                scope_prefix=scope_prefix,
                query=query,
                vector_results=vector_results,
                limit=limit,
            )
        except Exception as e:
            log.warning(f"  Hybrid search fallback: {e}")
            return vector_results[:limit]

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

    def existing_repo_decision_wis(self) -> set[str]:
        """/repo-decisions indeksinde halihazirda bulunan work_item_id kumesi (tek sorgu)."""
        out: set[str] = set()
        try:
            info = self.storage.get_scope_info("/repo-decisions")
            if info and info.record_count > 0:
                for r in self.storage.list_records("/repo-decisions", limit=100_000):
                    wi = r.metadata.get("work_item_id")
                    if wi:
                        out.add(str(wi))
        except Exception as e:
            log.debug(f"  existing_repo_decision_wis atlandi: {e}")
        return out

    def index_repo_decision(self, work_item_id: str, repo: str, pr_id: str, plan: dict, wi_content: str, skip_dedup_check: bool = False):
        """Basarili bir isin 'icerik+dosya yollari -> repo' kaydini /repo-decisions
        scope'una yaz. Idempotent: ayni work_item_id zaten varsa atlar."""
        if not repo or not work_item_id:
            return
        scope = "/repo-decisions"
        wi = str(work_item_id)
        if not skip_dedup_check:
            # Idempotency: ayni WI zaten indekste mi? (index_repo_summary deseni)
            try:
                info = self.storage.get_scope_info(scope)
                if info and info.record_count > 0:
                    for r in self.storage.list_records(scope, limit=10_000):
                        if r.metadata.get("work_item_id") == wi:
                            return
            except Exception as e:
                log.debug(f"  Repo-decision dedup kontrolu atlandi: {e}")
        changes = plan.get("changes", []) if isinstance(plan, dict) else []
        file_paths = [c.get("file_path", "") for c in changes if c.get("file_path")]
        routes = _extract_routes(f"{wi_content or ''} " + " ".join(file_paths))
        content = (
            f"WI #{wi}\n{(wi_content or '')[:2000]}\n"
            f"Degisen dosyalar: {', '.join(file_paths)}\n"
            f"Route/endpoint: {', '.join(routes)}"
        )
        try:
            self._save_record(
                content=content[:5000],
                scope=scope,
                categories=["repo-decision"],
                metadata={
                    "work_item_id": wi,
                    "repo": repo,
                    "pr_id": str(pr_id or ""),
                    "file_paths": file_paths[:50],
                    "routes": routes[:50],
                },
                importance=0.8,
            )
        except Exception as e:
            log.warning(f"  Repo-decision indeks hatasi (WI#{wi}): {e}")

    def suggest_repo_from_history(
        self, query: str, path_hints: list[str] | None = None,
        limit: int = 3, exclude_wi: str | None = None,
        known_repos: list[str] | None = None,
    ) -> list[dict]:
        """Gecmis basarili islerden repo onerisi. Sonuclari repo'ya gore gruplar:
        repo_score = max(tekil_skorlar) + 0.05*(n-1), 1.0'da sinirli.
        Donen: [{repo, score, supporting_wis, file_paths_evidence}] (skora gore sirali).

        known_repos=None -> filtre yok; known_repos=[] -> hicbir repo gecmez (hepsi elenir)."""
        try:
            q = query
            if path_hints:
                q = q + " " + " ".join(path_hints)
            results = self._search(q, "/repo-decisions", limit=max(limit * 5, 15))
        except Exception as e:
            log.warning(f"  suggest_repo_from_history arama hatasi: {e}")
            return []
        by_repo: dict[str, dict] = {}
        ex = str(exclude_wi) if exclude_wi is not None else None
        for record, score in results:
            repo = record.metadata.get("repo", "")
            wi = record.metadata.get("work_item_id", "")
            if not repo:
                continue
            if not wi:
                continue
            if ex and wi == ex:
                continue
            if known_repos is not None and repo not in known_repos:
                continue
            entry = by_repo.setdefault(
                repo, {"scores": [], "supporting_wis": [], "file_paths_evidence": []}
            )
            entry["scores"].append(score)
            entry["supporting_wis"].append(wi)
            entry["file_paths_evidence"].extend(record.metadata.get("file_paths", [])[:3])
        out = []
        for repo, entry in by_repo.items():
            n = len(entry["scores"])
            repo_score = min(1.0, max(entry["scores"]) + 0.05 * (n - 1))
            out.append({
                "repo": repo,
                "score": round(repo_score, 3),
                "supporting_wis": entry["supporting_wis"][:5],
                "file_paths_evidence": list(dict.fromkeys(entry["file_paths_evidence"]))[:8],
            })
        out.sort(key=lambda x: x["score"], reverse=True)
        return out[:limit]

    def backfill_repo_decisions(self, db, limit: int = 1000) -> int:
        """DB'deki basarili islerden /repo-decisions indeksini geri-doldur.
        Idempotent (index_repo_decision zaten var olani atlar). Doldurulan sayi doner."""
        from agile_sdlc_crew.main import _parse_architect_output
        try:
            jobs = db.list_successful_jobs_for_backfill(limit)
        except Exception as e:
            log.warning(f"  Backfill: is listesi alinamadi: {e}")
            return 0
        done = 0
        for j in jobs:
            wi = str(j.get("work_item_id") or "")
            repo = j.get("repo_name") or ""
            pr_id = j.get("pr_id") or ""
            if not wi or not repo:
                continue
            td = db.get_cached_step_output("technical_design_task", wi)
            if not td:
                continue
            try:
                plan = _parse_architect_output(td)
            except Exception:
                continue
            wi_content = (
                db.get_cached_step_output("requirements_analysis_task", wi)
                or plan.get("summary", "")
            )
            try:
                self.index_repo_decision(wi, repo, pr_id, plan, wi_content)
                done += 1
            except Exception as exc:
                log.warning(f"  Backfill: WI#{wi} indekslenemedi: {exc}")
                continue
        log.info(f"  📚 Repo-decision backfill: {done} is islendi")
        return done

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
