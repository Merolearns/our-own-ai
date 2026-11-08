"""Answer cache: exact match, plus semantic match for public FAQ questions only."""

import hashlib
import json
import re

import numpy as np

import store


def partition_key(agent, locale, tenant, doc_version, prompt_version) -> str:
    """Matches are only allowed inside one partition, and a new prompt or doc
    version starts a new one, which invalidates old answers for free."""
    return f"{agent}|{locale}|{tenant}|{doc_version}|{prompt_version}"


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def query_hash(partition: str, text: str) -> str:
    return hashlib.sha256(f"{partition}|{normalize(text)}".encode("utf-8")).hexdigest()


def cacheable(route, tool_calls, validated, multi_turn) -> bool:
    """A cached replay must never skip a tool, leak personal data or pin a bad answer."""
    return route == "faq" and not tool_calls and validated and not multi_turn


def _unit_blob(vector) -> bytes:
    array = np.asarray(vector, dtype=np.float32)
    norm = np.linalg.norm(array)
    return (array / norm if norm else array).tobytes()


def lookup(
    db_path, partition, text, route, allow_semantic=False, embedder=None, threshold=0.93
):
    """Returns (entry, status, similarity); status is exact, semantic or miss."""
    if route != "faq":
        return None, "miss", None
    entry = store.cache_find_exact(db_path, partition, query_hash(partition, text))
    if entry:
        store.cache_record_hit(db_path, entry["id"])
        return entry, "exact", 1.0
    if not (allow_semantic and embedder):
        return None, "miss", None
    entries = [
        e
        for e in store.cache_list_partition(db_path, partition)
        if len(e["embedding"]) == embedder.dim * 4
    ]
    if not entries:
        return None, "miss", None
    query = np.frombuffer(_unit_blob(embedder.embed([text])[0]), dtype=np.float32)
    scores = (
        np.stack([np.frombuffer(e["embedding"], dtype=np.float32) for e in entries])
        @ query
    )
    best = int(np.argmax(scores))
    if float(scores[best]) < threshold:
        return None, "miss", float(scores[best])
    store.cache_record_hit(db_path, entries[best]["id"])
    return entries[best], "semantic", float(scores[best])


def put(
    db_path, agent, partition, text, answer, sources, ttl_seconds, embedder=None
) -> int:
    embedding = _unit_blob(embedder.embed([text])[0]) if embedder else None
    return store.cache_insert(
        db_path,
        {
            "agent_id": agent,
            "partition_key": partition,
            "query_text": text,
            "query_hash": query_hash(partition, text),
            "embedding": embedding,
            "answer_text": answer,
            "sources_json": json.dumps(sources),
            "ttl_seconds": ttl_seconds,
        },
    )
