from pathlib import Path
import tempfile

import numpy as np
import pytest

import rag
import store

DOCS = Path(__file__).parent.parent / "data" / "parcelco"


class ReverseReranker:
    """Scores later documents higher, so tests can see the order change."""

    def rerank(self, query, documents):
        return [float(i) for i in range(len(documents))]


def test_fake_embedder_shape_and_determinism():
    embedder = rag.FakeEmbedder()
    first, second = embedder.embed(["refund policy"]), embedder.embed(["refund policy"])
    assert first == second and len(first[0]) == 1024
    assert abs(np.linalg.norm(first[0]) - 1) < 1e-6


def test_short_document_stays_whole():
    chunks = rag.chunk_document("a.md", "# Title\n\n## One\nFirst.\n\n## Two\nSecond.")
    assert len(chunks) == 1 and chunks[0]["text"].startswith("Title: Overview")


def test_long_document_splits_on_headings_with_context():
    body = "\n\n".join(f"## Part {i}\n" + "Some policy words here. " * 30 for i in range(3))
    chunks = rag.chunk_document("a.md", f"# Doc\n\n{body}")
    assert [c["section"] for c in chunks] == ["Part 0", "Part 1", "Part 2"]
    assert all(c["text"].startswith(f"Doc: {c['section']}\n") for c in chunks)


def test_oversized_section_respects_limit_and_overlaps():
    sentences = " ".join(f"Sentence number {i} is here." for i in range(400))
    chunks = rag.chunk_document("a.md", f"# Doc\n\n## Big\n{sentences}\n\n## Other\n{'x ' * 600}")
    big = [c for c in chunks if c["section"] == "Big"]
    assert len(big) > 1
    assert all(len(c["text"]) <= rag.MAX_CHARS["policy"] + 20 for c in big)
    first_tail = big[0]["text"].rsplit("\n\n", 1)[-1].strip()
    assert first_tail in big[1]["text"]


def test_explanatory_kind_gets_larger_chunks():
    sentences = " ".join(f"Sentence number {i} is here." for i in range(100))
    text = f"# Doc\n\n## Big\n{sentences}\n\n## Other\n{'x ' * 600}"
    assert len(rag.chunk_document("a.md", "<!-- kind: explanatory -->\n" + text)) < len(rag.chunk_document("a.md", text))


def test_ingest_skips_order_data(db):
    rag.ingest_docs(db, "parcelco", DOCS, rag.FakeEmbedder())
    chunks = store.list_chunks(db, "parcelco")
    assert chunks and "orders.md" not in {c["doc_id"] for c in chunks}
    assert not any("ShipFast" in c["text"] for c in chunks)


def test_ingest_skips_readme_docs(db):
    # README files are builder notes, not customer knowledge: they never
    # reach the corpus (the Scoopy README holds the [VERIFY] list and the
    # rejected-label rule, which belongs in the prompt, not in retrieval).
    docs = Path(__file__).parent.parent / "data" / "scoopy"
    rag.ingest_docs(db, "scoopy", docs, rag.FakeEmbedder())
    doc_ids = {c["doc_id"] for c in store.list_chunks(db, "scoopy")}
    assert "README.md" not in doc_ids
    assert doc_ids == {
        "booking-billing.md",
        "contact.md",
        "faq.md",
        "pricing.md",
        "services.md",
        "visit-process.md",
    }


def test_ingest_screens_secrets_loudly(db, tmp_path):
    # S3: a document holding a secret-like value stops the ingest instead
    # of slipping into the corpus where retrieval could quote it.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "ok.md").write_text("# Ok\n\nNothing secret here.")
    (docs / "bad.md").write_text("# Bad\n\nMy key is sk-abcdefghijklmnopqrstuv.")
    with pytest.raises(ValueError, match="secret pattern"):
        rag.ingest_docs(db, "parcelco", docs, rag.FakeEmbedder())
    assert store.list_chunks(db, "parcelco") == []


def test_ingest_screens_card_numbers(db, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "bad.md").write_text("# Bad\n\nMy card is 4111111111111111.")
    with pytest.raises(ValueError, match="card-number"):
        rag.ingest_docs(db, "parcelco", docs, rag.FakeEmbedder())


def test_ensure_ingested_only_rebuilds_when_needed(db):
    embedder = rag.FakeEmbedder()
    assert rag.ensure_ingested(db, "parcelco", DOCS, embedder) is True
    assert rag.ensure_ingested(db, "parcelco", DOCS, embedder) is False


def test_ensure_ingested_fills_missing_vectors(db):
    rag.ingest_docs(db, "parcelco", DOCS, None)
    assert rag.ensure_ingested(db, "parcelco", DOCS, rag.FakeEmbedder()) is True
    assert store.count_stale_embeddings(db, "parcelco", 1024) == 0


def test_ensure_ingested_fails_loudly_on_missing_or_empty_corpus(db, tmp_path):
    with pytest.raises(FileNotFoundError, match="corpus directory missing"):
        rag.ensure_ingested(db, "scoopy", tmp_path / "does-not-exist", rag.FakeEmbedder())
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="zero chunks"):
        rag.ensure_ingested(db, "scoopy", empty, rag.FakeEmbedder())


def test_hybrid_retrieval_finds_policy(db):
    embedder = rag.FakeEmbedder()
    rag.ingest_docs(db, "parcelco", DOCS, embedder)
    hits, mode = rag.retrieve(db, "parcelco", "maximum refund amount staff approval", 5, embedder)
    assert mode == "hybrid"
    assert hits[0]["doc_id"] == "refunds.md"
    assert [h["rank"] for h in hits] == list(range(1, len(hits) + 1))
    assert {"doc_id", "title", "text", "score", "rank"} <= hits[0].keys()


def test_keyword_only_when_no_embedder(db):
    rag.ingest_docs(db, "parcelco", DOCS, None)
    hits, mode = rag.retrieve(db, "parcelco", "prepaid return label", 5)
    assert mode == "bm25-only" and hits[0]["doc_id"] == "returns.md"


def test_dense_hits_under_the_floor_are_dropped(db):
    embedder = rag.FakeEmbedder()
    rag.ingest_docs(db, "parcelco", DOCS, embedder)
    rows = store.list_chunks(db, "parcelco")
    dense, cosine = rag._dense_rank(rows, "zebra quantum", embedder, 0.99)
    assert dense == [] and cosine == {}


def test_off_topic_question_finds_nothing(db):
    rag.ingest_docs(db, "parcelco", DOCS, None)
    assert rag.retrieve(db, "parcelco", "what is the capital of france")[0] == []


def test_reranker_controls_final_order(db):
    embedder = rag.FakeEmbedder()
    rag.ingest_docs(db, "parcelco", DOCS, embedder)
    plain, _ = rag.retrieve(db, "parcelco", "refund policy delivery", 20, embedder)
    ranked, mode = rag.retrieve(db, "parcelco", "refund policy delivery", 20, embedder, ReverseReranker())
    assert mode == "hybrid+rerank"
    assert [h["chunk_id"] for h in ranked] == [h["chunk_id"] for h in plain][::-1]


def test_agents_are_isolated(db):
    rag.ingest_docs(db, "parcelco", DOCS, None)
    assert rag.retrieve(db, "other", "refund policy")[0] == []


def test_citations_are_built_from_metadata():
    hits = [{"doc_id": "a.md", "title": "A", "section": "S", "text": "body", "rank": 1}]
    built = rag.build_citations(hits)
    assert built["refs"] == [{"n": 1, "doc_id": "a.md", "title": "A", "section": "S"}]
    assert built["context"].startswith("[1] A: S\n<untrusted-data")


def test_invented_citations_are_removed():
    refs = [{"n": 1, "doc_id": "a", "title": "A", "section": "S"}, {"n": 2, "doc_id": "b", "title": "B", "section": "S"}]
    answer, cited = rag.filter_citations("Yes [1]. Also [7].", refs)
    assert answer == "Yes [1]. Also ."
    assert [r["n"] for r in cited] == [1]


def test_default_cache_dir_is_not_the_system_temp_dir(monkeypatch):
    # /tmp here is a 512M tmpfs; the BGE-M3 model alone is 2.3G, so the
    # fastembed temp default can never hold it.
    monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
    assert not rag.cache_dir().startswith(tempfile.gettempdir())
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", "/data/models")
    assert rag.cache_dir() == "/data/models"
