"""BM25 lexical search + hybrid (vector + lexical) fusion.

VectorStore'a paralel calisan BM25 katmani. Cosine similarity teknik
terimleri (camelCase function adlari, endpoint isimleri, dosya yollari)
zayif temsil ediyor — BM25 tam kelime eslesmesinde bunu cozer.

Kullanim:
    hs = HybridSearcher(storage_factory=lambda: vector_store.storage)
    fused = hs.search(scope_prefix, query, vector_results, limit=10)

Env knobs (vector_store.py'de okunur, buraya parametre olarak gecer):
    CREW_HYBRID_SEARCH       (master toggle, default 1)
    CREW_BM25_VECTOR_CANDIDATES  (vector over-fetch, default 50)
    CREW_BM25_LEXICAL_CANDIDATES (BM25 top-k, default 50)
    CREW_RRF_K               (RRF dampening, default 60)
    CREW_FUSION              (rrf veya weighted, default rrf)
    CREW_BM25_WEIGHT         (sadece weighted modunda, default 0.5)
    CREW_BM25_CACHE_DIR      (pickle cache dir, default ~/.crew_repos/.vectordb/bm25)
"""

from __future__ import annotations

import hashlib
import logging
import os
import pickle
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import bm25s

log = logging.getLogger("pipeline")


# ── Tokenizer ──────────────────────────────────────────────

# Identifier split: hyphen, slash, dot, underscore, colon, parens, brackets
_SPLIT_RE = re.compile(r"[\s/._\-:()\[\]{},;\"']+")
# camelCase / PascalCase decomposer: insert space before uppercase preceded by lowercase
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# Tek-karakter ve sadece-rakam token'lari ele
_NOISE_RE = re.compile(r"^[0-9]+$|^.{0,1}$")


def tokenize(text: str) -> list[str]:
    """Code-aware tokenizer.

    Adimlar:
      1. Splittal (whitespace, /._-:()[]{}).
      2. Her parca icin:
         - orijinal lowercase compound'u koru ("getOrderDetails" → "getorderdetails")
         - camelCase parcalarini da emit ("getOrderDetails" → "get", "order", "details")
      3. >=6 karakterli identifier'lar icin uzun-dan-kisaya prefix'ler emit:
         "getorderdetails" → "getorderdet", "getorder", "getord"
         (BM25Okapi prefix match yapmiyor; query "getorder" bu prefix token'a eslesir)
      4. Dedupe + lowercase + noise-removal.

    Test ornekleri:
        tokenize("flo-dashboard/src/getOrderDetails.php")
        # → ['flo', 'dashboard', 'src', 'getorderdetails', 'get', 'order',
        #    'details', 'getorderdet', 'getorder', 'getord', 'php']
    """
    if not text:
        return []

    out: list[str] = []
    seen: set[str] = set()

    def _emit(tok: str) -> None:
        tok = tok.lower().strip()
        if not tok or _NOISE_RE.match(tok) or tok in seen:
            return
        seen.add(tok)
        out.append(tok)

    for part in _SPLIT_RE.split(text):
        if not part:
            continue
        # camelCase parcalarini ayri ayri al
        camel_parts = [p for p in _CAMEL_RE.split(part) if p]
        if len(camel_parts) > 1:
            # PascalCase/camelCase: hem orijinal compound hem parcalar
            _emit(part)  # original (lowercase compound oluyor)
            for cp in camel_parts:
                _emit(cp)
        else:
            _emit(part)

    # Prefix emit — >=8 char identifier-like tokenlar icin tum prefix'ler
    # (6 char minimum, query "getorder" → "getorderdetails" prefix'i eslesir)
    # Cost: ~3-5 ekstra token / dosya yolu. Index size'i ~%30 buyutur, kabul.
    prefix_candidates = [t for t in list(out) if len(t) >= 8 and t.isalnum()]
    for tok in prefix_candidates:
        # 6'dan baslayip orijinal uzunluga kadar 1'er artar
        for length in range(6, len(tok)):
            _emit(tok[:length])

    return out


# ── Fusion ─────────────────────────────────────────────────


def fuse_rrf(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    """Reciprocal Rank Fusion.

    Her ranking listesindeki dokumanlar icin:
        score(d) = sum_over_systems(1 / (k + rank_in_system))

    Rank 1-indexed. Sonuc score'lar 0-1 araliginda kalir.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


def fuse_weighted(
    vector_scores: dict[str, float],
    bm25_scores: dict[str, float],
    bm25_weight: float = 0.5,
) -> dict[str, float]:
    """Weighted sum fusion (min-max normalize + weighted combine).

    Kullanim: CREW_FUSION=weighted ile aktif.
    """
    def _norm(d: dict[str, float]) -> dict[str, float]:
        if not d:
            return {}
        vals = list(d.values())
        lo, hi = min(vals), max(vals)
        rng = hi - lo
        if rng <= 1e-9:
            return {k: 1.0 for k in d}
        return {k: (v - lo) / rng for k, v in d.items()}

    vn = _norm(vector_scores)
    bn = _norm(bm25_scores)
    all_ids = set(vn) | set(bn)
    w_vec = 1.0 - bm25_weight
    w_bm25 = bm25_weight
    return {
        i: w_vec * vn.get(i, 0.0) + w_bm25 * bn.get(i, 0.0)
        for i in all_ids
    }


# ── Index storage ──────────────────────────────────────────


@dataclass
class _ScopeIndex:
    bm25: Any  # bm25s.BM25 instance
    record_ids: list[str]
    record_lookup: dict[str, Any]  # id -> MemoryRecord
    tokenized_corpus: list[list[str]] = field(default_factory=list)
    built_at: float = 0.0


# ── HybridSearcher ─────────────────────────────────────────


class HybridSearcher:
    """Vector + BM25 hybrid retrieval over LanceDB-backed records.

    Per-scope lazy in-memory BM25 index + optional pickle warm-cache.
    Fuses with vector results via RRF (default) or weighted sum.
    """

    def __init__(
        self,
        storage_factory: Callable[[], Any],
        cache_dir: str | None = None,
    ):
        self._storage_factory = storage_factory
        cache_root = cache_dir or os.environ.get(
            "CREW_BM25_CACHE_DIR",
            os.path.expanduser("~/.crew_repos/.vectordb/bm25"),
        )
        self._cache_dir = Path(cache_root)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        # In-memory cache: scope_prefix → _ScopeIndex
        self._cache: dict[str, _ScopeIndex] = {}
        # Public knobs
        self._lex_k = int(os.environ.get("CREW_BM25_LEXICAL_CANDIDATES", "50"))
        self._rrf_k = int(os.environ.get("CREW_RRF_K", "60"))
        self._fusion_mode = os.environ.get("CREW_FUSION", "rrf").lower()
        self._bm25_weight = float(os.environ.get("CREW_BM25_WEIGHT", "0.5"))

    # ── Cache management ──────────────────────────────────

    def invalidate(self, scope: str) -> None:
        """Bu scope (veya prefix'i) icin in-memory ve disk cache'i sil."""
        to_drop = [k for k in self._cache if k.startswith(scope) or scope.startswith(k)]
        for k in to_drop:
            self._cache.pop(k, None)
        # Pickle dosyalarini sil (scope hash'leri uzerinden)
        try:
            for f in self._cache_dir.glob("*.pkl"):
                # Yedek bilgi: filename = hash(scope), dosyada saved scope yazili
                # Cabuk yontem: dosya tarihiyle kararsiz, hepsini sil
                f.unlink(missing_ok=True)
        except Exception:
            pass

    def _cache_path(self, scope_prefix: str) -> Path:
        h = hashlib.md5(scope_prefix.encode("utf-8")).hexdigest()[:16]
        return self._cache_dir / f"{h}.pkl"

    # ── Index build ───────────────────────────────────────

    def _build_index(self, scope_prefix: str) -> _ScopeIndex | None:
        """LanceDB'den scope altindaki tum kayitlari oku, BM25 indeksini olustur."""
        t0 = time.time()
        try:
            storage = self._storage_factory()
        except Exception as e:
            log.warning(f"  BM25 storage erisilemedi: {e}")
            return None

        records: list[Any] = []
        try:
            # CrewAI LanceDBStorage list_records — scope_prefix ile baslayanlari getirir
            # 10K limit: 5K chunks/repo + jobs sigar
            records = storage.list_records(scope_prefix, limit=10_000)
        except Exception as e:
            log.warning(f"  BM25 list_records hatasi ({scope_prefix}): {e}")
            return None

        if not records:
            return None

        # Tokenize her record: content + metadata.file_path + metadata.repo
        record_ids: list[str] = []
        record_lookup: dict[str, Any] = {}
        corpus_tokens: list[list[str]] = []

        for rec in records:
            rid = getattr(rec, "id", None) or _fallback_record_id(rec)
            text_parts = [rec.content or ""]
            md = rec.metadata or {}
            for key in ("file_path", "repo", "step", "work_item_id"):
                v = md.get(key)
                if v:
                    text_parts.append(str(v))
            tokens = tokenize(" ".join(text_parts))
            if not tokens:
                continue
            record_ids.append(rid)
            record_lookup[rid] = rec
            corpus_tokens.append(tokens)

        if not corpus_tokens:
            return None

        try:
            bm25 = bm25s.BM25()
            bm25.index(corpus_tokens)
        except Exception as e:
            log.warning(f"  BM25 index build hatasi ({scope_prefix}): {e}")
            return None

        idx = _ScopeIndex(
            bm25=bm25,
            record_ids=record_ids,
            record_lookup=record_lookup,
            tokenized_corpus=corpus_tokens,
            built_at=time.time(),
        )
        elapsed = time.time() - t0
        log.info(
            f"  BM25 index olusturuldu: {scope_prefix} "
            f"({len(record_ids)} doc, {elapsed:.2f}s)"
        )
        return idx

    def _save_cache(self, scope_prefix: str, idx: _ScopeIndex) -> None:
        """Pickle warm-cache (sonraki sunucu acilislarinda yeniden build'i atla)."""
        try:
            path = self._cache_path(scope_prefix)
            # bm25s.BM25 picklable; record_lookup'taki MemoryRecord da pydantic
            with path.open("wb") as f:
                pickle.dump(
                    {
                        "scope": scope_prefix,
                        "record_ids": idx.record_ids,
                        "tokenized_corpus": idx.tokenized_corpus,
                        "built_at": idx.built_at,
                    },
                    f,
                )
        except Exception as e:
            log.debug(f"  BM25 pickle cache yazilamadi ({scope_prefix}): {e}")

    def _load_cache(self, scope_prefix: str) -> _ScopeIndex | None:
        """Pickle'dan warm-load + LanceDB'den record_lookup yenile."""
        path = self._cache_path(scope_prefix)
        if not path.exists():
            return None
        try:
            with path.open("rb") as f:
                data = pickle.load(f)
            if data.get("scope") != scope_prefix:
                return None
            # Cache eskidi mi? LanceDB son save'inden once mi pickle yazildi mi?
            # Simdilik basit: pickle var → kullan, _save_record invalidate edecek
            tokenized = data["tokenized_corpus"]
            bm25 = bm25s.BM25()
            bm25.index(tokenized)
            # Record lookup'i LanceDB'den hizlica yenile
            try:
                storage = self._storage_factory()
                records = storage.list_records(scope_prefix, limit=10_000)
                lookup = {
                    (getattr(r, "id", None) or _fallback_record_id(r)): r for r in records
                }
            except Exception:
                lookup = {}

            # ID'ler eslesmeyebilir (record silinmis vb.), eslesmeyenlerin yerine None
            ids = data["record_ids"]
            if not all(rid in lookup for rid in ids):
                # Major mismatch — rebuild
                return None

            return _ScopeIndex(
                bm25=bm25,
                record_ids=ids,
                record_lookup=lookup,
                tokenized_corpus=tokenized,
                built_at=data["built_at"],
            )
        except Exception as e:
            log.debug(f"  BM25 pickle cache okunamadi ({scope_prefix}): {e}")
            return None

    def _get_or_build_index(self, scope_prefix: str) -> _ScopeIndex | None:
        if scope_prefix in self._cache:
            return self._cache[scope_prefix]
        idx = self._load_cache(scope_prefix)
        if idx is None:
            idx = self._build_index(scope_prefix)
            if idx is not None:
                self._save_cache(scope_prefix, idx)
        if idx is not None:
            self._cache[scope_prefix] = idx
        return idx

    # ── Public search ─────────────────────────────────────

    def search(
        self,
        scope_prefix: str,
        query: str,
        vector_results: list[tuple[Any, float]],
        limit: int = 10,
    ) -> list[tuple[Any, float]]:
        """Vector top-N ile BM25 top-N'i fuse edip top-`limit` dondurur.

        vector_results: [(MemoryRecord, cosine_score)] — VectorStore._search vector path.
        Geri donus: ayni format, ama score artik fused score (RRF veya weighted).
        Cache miss veya bm25 sorgusu basarisizsa vector_results'i degistirmeden dondurur.
        """
        idx = self._get_or_build_index(scope_prefix)
        if idx is None or not idx.record_ids:
            return vector_results[:limit]

        # BM25 sorgusu
        try:
            q_tokens = tokenize(query)
            if not q_tokens:
                return vector_results[:limit]
            # bm25s retrieve API: tek query → list, ya da liste of liste
            # `BM25.retrieve(query, k=N)` returns (doc_ids_array, scores_array)
            results, scores = idx.bm25.retrieve(
                [q_tokens], k=min(self._lex_k, len(idx.record_ids)), corpus=None
            )
            bm25_top = list(zip(results[0], scores[0]))  # [(doc_idx, score), ...]
        except Exception as e:
            log.warning(f"  BM25 retrieve hatasi ({scope_prefix}): {e}")
            return vector_results[:limit]

        # Ranking listeleri
        vector_ranking: list[str] = []
        vector_score_map: dict[str, float] = {}
        for rec, sc in vector_results:
            rid = getattr(rec, "id", None) or _fallback_record_id(rec)
            vector_ranking.append(rid)
            vector_score_map[rid] = float(sc)

        bm25_ranking: list[str] = []
        bm25_score_map: dict[str, float] = {}
        for doc_idx, sc in bm25_top:
            if 0 <= int(doc_idx) < len(idx.record_ids):
                rid = idx.record_ids[int(doc_idx)]
                bm25_ranking.append(rid)
                bm25_score_map[rid] = float(sc)

        # Fuse
        if self._fusion_mode == "weighted":
            fused = fuse_weighted(vector_score_map, bm25_score_map, self._bm25_weight)
        else:
            fused = fuse_rrf([vector_ranking, bm25_ranking], k=self._rrf_k)

        # Sort + truncate
        sorted_ids = sorted(fused.items(), key=lambda x: -x[1])[:limit]

        # MemoryRecord'a remap — once vector results'tan, sonra idx.record_lookup'tan
        out: list[tuple[Any, float]] = []
        rec_map: dict[str, Any] = {rid: rec for rec, _ in zip([r for r, _ in vector_results], vector_results) for _ in [0]}
        # Daha basit:
        rec_map = {}
        for rec, _ in vector_results:
            rid = getattr(rec, "id", None) or _fallback_record_id(rec)
            rec_map[rid] = rec
        for rid, fused_score in sorted_ids:
            rec = rec_map.get(rid) or idx.record_lookup.get(rid)
            if rec is not None:
                out.append((rec, fused_score))
        return out


# ── Helpers ────────────────────────────────────────────────


def _fallback_record_id(record: Any) -> str:
    """MemoryRecord.id yoksa stabil hash uret."""
    try:
        scope = record.scope or ""
        fp = (record.metadata or {}).get("file_path", "")
        h = (record.metadata or {}).get("hash", "")
        head = (record.content or "")[:80]
        raw = f"{scope}::{fp}::{h}::{head}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()
    except Exception:
        return hashlib.md5(str(record).encode("utf-8")).hexdigest()
