"""Retrieval: heading-aware chunks, BM25 + dense fused with RRF, then a cross-encoder."""

import hashlib
import logging
import os
import re
import threading
from pathlib import Path

import numpy as np

import guardrails
import store

log = logging.getLogger(__name__)

EMBED_DIM = 1024
EMBED_MODEL = "BAAI/bge-m3"
RERANK_MODEL = "onnx-community/bge-reranker-v2-m3-ONNX"
# Quantized: 571 MB instead of 2.2 GB, which is what a small server can hold.
RERANK_FILE = "onnx/model_int8.onnx"


def cache_dir():
    """Where fastembed models live. Defaults to a persistent per-user cache,
    never the system temp dir: /tmp here is a 512M tmpfs and the BGE-M3 model
    alone is 2.3G, so fastembed's temp default can never hold it."""
    return os.environ.get("FASTEMBED_CACHE_PATH") or str(
        Path.home() / ".cache" / "fastembed"
    )

SHORT_DOC_CHARS = 1000
CHARS_PER_TOKEN = 4
MAX_CHARS = {"policy": 500 * CHARS_PER_TOKEN, "explanatory": 800 * CHARS_PER_TOKEN}
OVERLAP = 0.12
# Customer order data reaches the model through scoped tools, never through search.
# README files are directory notes for the builders, not customer knowledge.
SKIP_DOCS = {"orders.md", "README.md"}

STOPWORDS = frozenset(
    """a about after all also am an and any are as at be been but by can could did do does
    for from get got had has have how i if in into is it its just me my of on or our out so
    some than that the their them then there these they this to too up us was we were what
    when where which who why will with would you your please hi hello hey""".split()
)
WORD = re.compile(r"[a-z0-9]+")


class FakeEmbedder:
    """Deterministic hashed bag of words for tests. Not a real embedding model."""

    dim = EMBED_DIM

    def embed(self, texts):
        vectors = []
        for text in texts:
            vector = np.zeros(self.dim, dtype=np.float32)
            for word in WORD.findall(text.lower()):
                digest = hashlib.sha256(word.encode("utf-8")).digest()
                vector[int.from_bytes(digest[:4], "big") % self.dim] += 1.0
            norm = np.linalg.norm(vector)
            vectors.append((vector / norm if norm else vector).tolist())
        return vectors


class FastEmbedProvider:
    """BGE-M3 dense vectors through fastembed. Downloads the model on first use."""

    dim = EMBED_DIM

    def __init__(self):
        from fastembed import TextEmbedding
        from fastembed.common.model_description import ModelSource, PoolingType

        if EMBED_MODEL not in {
            m["model"] for m in TextEmbedding.list_supported_models()
        }:
            TextEmbedding.add_custom_model(
                model=EMBED_MODEL,
                pooling=PoolingType.CLS,
                normalization=True,
                sources=ModelSource(hf=EMBED_MODEL),
                dim=EMBED_DIM,
                model_file="onnx/model.onnx",
                additional_files=[
                    "onnx/model.onnx_data",
                    "onnx/Constant_7_attr__value",
                ],
            )
        self._model = TextEmbedding(
            EMBED_MODEL, cache_dir=cache_dir(), threads=2
        )

    def embed(self, texts):
        return [vector.tolist() for vector in self._model.embed(list(texts))]


class FastEmbedReranker:
    def __init__(self):
        from fastembed.common.model_description import ModelSource
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        if RERANK_MODEL not in {
            m["model"] for m in TextCrossEncoder.list_supported_models()
        }:
            TextCrossEncoder.add_custom_model(
                model=RERANK_MODEL,
                sources=ModelSource(hf=RERANK_MODEL),
                model_file=RERANK_FILE,
            )
        self._model = TextCrossEncoder(
            RERANK_MODEL, cache_dir=cache_dir(), threads=2
        )

    def rerank(self, query, documents):
        return [float(score) for score in self._model.rerank(query, list(documents))]


_providers = {}
_providers_lock = threading.Lock()


def _load_once(key, factory):
    """Load a model once per process. A failed load is logged and remembered, so
    retrieval runs keyword-only rather than pretending it has embeddings."""
    with _providers_lock:
        if key not in _providers:
            try:
                _providers[key] = factory()
            except Exception:
                log.exception("Could not load %s; retrieval continues without it", key)
                _providers[key] = None
        return _providers[key]


def get_embedder():
    """AGENT_EMBEDDER: 'fastembed' (default), 'fake' (tests) or 'off' (BM25 only)."""
    mode = os.environ.get("AGENT_EMBEDDER") or "fastembed"
    if mode == "off":
        return None
    if mode == "fake":
        return FakeEmbedder()
    return _load_once("embedder", FastEmbedProvider)


def get_reranker():
    if (os.environ.get("AGENT_EMBEDDER") or "fastembed") != "fastembed":
        return None
    return _load_once("reranker", FastEmbedReranker)


def _split_section(body, max_chars):
    """Split on paragraphs then sentences, carrying about 12% overlap forward."""
    if len(body) <= max_chars:
        return [body]
    units = []
    for paragraph in re.split(r"\n\s*\n", body):
        if len(paragraph) <= max_chars:
            units.append(paragraph)
        else:
            units.extend(re.split(r"(?<=[.!?])\s+", paragraph))

    overlap_chars = int(max_chars * OVERLAP)
    chunks, current = [], []
    for unit in units:
        if current and len("\n\n".join([*current, unit])) > max_chars:
            chunks.append("\n\n".join(current))
            tail = re.split(r"(?<=[.!?])\s+", current[-1])[-1]
            current = [tail] if len(tail) <= overlap_chars else []
        current.append(unit)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def chunk_document(doc_id, markdown):
    """Short documents stay whole. Longer ones split on headings, and every
    chunk carries its title and section so it makes sense alone."""
    kind_match = re.search(r"<!--\s*kind:\s*(\w+)\s*-->", markdown)
    kind = (
        kind_match.group(1)
        if kind_match and kind_match.group(1) in MAX_CHARS
        else "policy"
    )
    text = re.sub(r"<!--.*?-->", "", markdown, flags=re.S).strip()
    title_match = re.match(r"#\s+(.+)", text)
    title = title_match.group(1).strip() if title_match else doc_id
    body = text[title_match.end() :].strip() if title_match else text

    if len(body) <= SHORT_DOC_CHARS:
        sections = [("Overview", body)]
    else:
        parts = re.split(r"^#{2,3}\s+(.+)$", body, flags=re.M)
        sections = [("Overview", parts[0].strip())] if parts[0].strip() else []
        sections += [
            (parts[i].strip(), parts[i + 1].strip())
            for i in range(1, len(parts) - 1, 2)
            if parts[i + 1].strip()
        ]

    chunks, seen = [], set()
    for section, section_body in sections:
        for piece in _split_section(section_body, MAX_CHARS[kind]):
            chunk_text = f"{title}: {section}\n{piece}"
            if chunk_text not in seen:
                seen.add(chunk_text)
                chunks.append(
                    {
                        "doc_id": doc_id,
                        "title": title,
                        "section": section,
                        "chunk_index": len(chunks),
                        "text": chunk_text,
                    }
                )
    return chunks


def _chunk_directory(docs_dir):
    chunks = []
    for path in sorted(Path(docs_dir).glob("*.md")):
        if path.name not in SKIP_DOCS:
            chunks.extend(chunk_document(path.name, path.read_text(encoding="utf-8")))
    return chunks


def _screen_chunks(chunks):
    """Secret screening at ingest (S3): a document holding a secret-like
    value is never stored. Fails loudly — a bad doc must stop the ingest,
    not slip into the corpus where retrieval could quote it."""
    for chunk in chunks:
        trigger = guardrails.find_secret(chunk["text"])
        if trigger:
            raise ValueError(
                f"secret pattern ({trigger}) in {chunk['doc_id']}: "
                "refusing to ingest"
            )


def _store_chunks(db_path, agent, chunks, embedder):
    _screen_chunks(chunks)
    vectors = (
        embedder.embed([c["text"] for c in chunks]) if embedder and chunks else None
    )
    for index, chunk in enumerate(chunks):
        chunk["embedding"] = (
            np.asarray(vectors[index], dtype=np.float32).tobytes() if vectors else None
        )
    return store.replace_chunks(db_path, agent, chunks)


def ingest_docs(db_path, agent, docs_dir, embedder=None):
    return _store_chunks(db_path, agent, _chunk_directory(docs_dir), embedder)


def ensure_ingested(db_path, agent, docs_dir, embedder=None) -> bool:
    """Rebuild only when documents changed or stored vectors are missing."""
    docs_dir = Path(docs_dir)
    if not docs_dir.is_dir():
        raise FileNotFoundError(
            f"corpus directory missing for agent {agent!r}: {docs_dir}"
        )
    chunks = _chunk_directory(docs_dir)
    if not chunks:
        raise ValueError(
            f"corpus for agent {agent!r} yielded zero chunks from {docs_dir}"
        )
    stored = {row["text"] for row in store.list_chunks(db_path, agent)}
    stale = embedder is not None and store.count_stale_embeddings(
        db_path, agent, embedder.dim
    )
    if stored == {c["text"] for c in chunks} and not stale:
        return False
    _store_chunks(db_path, agent, chunks, embedder)
    return True


def _fts_query(query):
    terms = [t for t in WORD.findall(query.lower()) if t not in STOPWORDS]
    return " OR ".join(f'"{term}"' for term in terms)


def _dense_rank(rows, query, embedder, floor):
    usable = [
        r for r in rows if r["embedding"] and len(r["embedding"]) == embedder.dim * 4
    ]
    if not usable:
        return [], {}
    matrix = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in usable])
    vector = np.asarray(embedder.embed([query])[0], dtype=np.float32)
    scores = matrix @ vector
    cosine = {r["id"]: float(s) for r, s in zip(usable, scores) if s >= floor}
    return sorted(cosine, key=cosine.get, reverse=True), cosine


def retrieve(
    db_path, agent, query, top_k=6, embedder=None, reranker=None, settings=None
):
    """Returns (hits, mode). hits are best-first dicts with doc_id, title, section,
    text, score and rank. mode names the pipeline that actually ran."""
    settings = settings or {}
    floor = settings.get("cosine_floor", 0.28)
    candidates = settings.get("candidates", 20)
    rrf_k = settings.get("rrf_k", 60)

    rows = store.list_chunks(db_path, agent)
    by_id = {row["id"]: row for row in rows}
    dense, cosine = (
        _dense_rank(rows, query, embedder, floor) if embedder and rows else ([], {})
    )
    match = _fts_query(query)
    keyword = store.fts_search(db_path, agent, match, candidates) if match else []

    fused = {}
    for ranking in (dense[:candidates], keyword):
        for position, chunk_id in enumerate(ranking):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (rrf_k + position + 1)
    ordered = sorted(fused, key=fused.get, reverse=True)[:candidates]

    scores = fused
    mode = "hybrid" if embedder else "bm25-only"
    if reranker and ordered:
        rerank_scores = reranker.rerank(query, [by_id[c]["text"] for c in ordered])
        scores = dict(zip(ordered, rerank_scores))
        ordered = sorted(ordered, key=scores.get, reverse=True)
        mode += "+rerank"

    return [
        {
            "chunk_id": chunk_id,
            "doc_id": by_id[chunk_id]["doc_id"],
            "title": by_id[chunk_id]["title"],
            "section": by_id[chunk_id]["section"],
            "text": by_id[chunk_id]["text"],
            "score": float(scores[chunk_id]),
            "cosine": cosine.get(chunk_id),
            "rank": rank,
        }
        for rank, chunk_id in enumerate(ordered[:top_k], start=1)
    ], mode


def build_citations(hits):
    """Number the sources in code, so the model can only ever point at real ones."""
    refs = [
        {"n": i, "doc_id": h["doc_id"], "title": h["title"], "section": h["section"]}
        for i, h in enumerate(hits, start=1)
    ]
    blocks = [
        f"[{ref['n']}] {ref['title']}: {ref['section']}\n"
        + guardrails.wrap_untrusted(hit["text"], "kb", f"{hit['doc_id']}#{hit['rank']}")
        for ref, hit in zip(refs, hits)
    ]
    return {"refs": refs, "context": "\n\n".join(blocks)}


def filter_citations(answer, refs):
    """Drop markers with no matching source and list only the sources cited."""
    valid = {ref["n"] for ref in refs}
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", answer) if int(n) in valid}
    cleaned = re.sub(
        r"\[(\d+)\]", lambda m: m.group(0) if int(m.group(1)) in valid else "", answer
    )
    return cleaned, [ref for ref in refs if ref["n"] in cited]
