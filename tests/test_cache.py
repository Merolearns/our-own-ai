import cache
import rag
import store

P = cache.partition_key("parcelco", "en", "public", 1, "v1")


def lookup(db, text, partition=P, **kw):
    return cache.lookup(db, partition, text, kw.pop("route", "faq"), **kw)


def test_exact_hit_ignores_case_and_spacing(db):
    entry_id = cache.put(db, "parcelco", P, "What is your policy?", "answer", [], 60)
    entry, status, _ = lookup(db, "  what IS your   policy? ")
    assert entry["id"] == entry_id and status == "exact" and entry["hits"] == 0


def test_miss(db):
    assert lookup(db, "anything")[1] == "miss"


def test_partition_isolation(db):
    cache.put(db, "parcelco", P, "q", "a", [], 60)
    for other in (
        cache.partition_key("scoopy", "en", "public", 1, "v1"),
        cache.partition_key("parcelco", "en", "tenant2", 1, "v1"),
        cache.partition_key("parcelco", "en", "public", 2, "v1"),
        cache.partition_key("parcelco", "en", "public", 1, "v2"),
    ):
        assert lookup(db, "q", other)[1] == "miss"


def test_expired_entry_misses(db):
    import sqlite3

    cache.put(db, "parcelco", P, "q", "a", [], 60)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE cache_entries SET expires_at = '2020-01-01T00:00:00+00:00'")
    assert lookup(db, "q")[1] == "miss"


def test_non_faq_route_never_reads(db):
    cache.put(db, "parcelco", P, "q", "a", [], 60)
    assert lookup(db, "q", route="account")[1] == "miss"


def test_semantic_hit_above_threshold(db):
    embedder = rag.FakeEmbedder()
    cache.put(db, "parcelco", P, "refund policy details", "a", [], 60, embedder)
    entry, status, similarity = lookup(db, "details refund policy", allow_semantic=True, embedder=embedder)
    assert entry and status == "semantic" and similarity >= 0.93


def test_semantic_below_threshold_misses(db):
    embedder = rag.FakeEmbedder()
    cache.put(db, "parcelco", P, "refund policy details", "a", [], 60, embedder)
    entry, status, similarity = lookup(db, "refund policy for damaged parcels", allow_semantic=True, embedder=embedder)
    assert entry is None and status == "miss" and similarity < 0.93


def test_semantic_off_means_exact_only(db):
    embedder = rag.FakeEmbedder()
    cache.put(db, "parcelco", P, "refund policy details", "a", [], 60, embedder)
    assert lookup(db, "details refund policy", allow_semantic=False, embedder=embedder)[1] == "miss"


def test_semantic_respects_partition(db):
    embedder = rag.FakeEmbedder()
    cache.put(db, "parcelco", P, "refund policy details", "a", [], 60, embedder)
    other = cache.partition_key("parcelco", "en", "t2", 1, "v1")
    assert lookup(db, "details refund policy", other, allow_semantic=True, embedder=embedder)[1] == "miss"


def test_vectors_stored_as_float32(db):
    embedder = rag.FakeEmbedder()
    cache.put(db, "parcelco", P, "q", "a", [], 60, embedder)
    assert len(store.cache_list_partition(db, P)[0]["embedding"]) == 1024 * 4


def test_cacheable_rules():
    assert cache.cacheable("faq", [], True, False) is True
    assert cache.cacheable("account", [], True, False) is False
    assert cache.cacheable("faq", [{"name": "issue_refund"}], True, False) is False
    assert cache.cacheable("faq", [], False, False) is False
    assert cache.cacheable("faq", [], True, True) is False
