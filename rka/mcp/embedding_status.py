"""Shared, read-only presentation of the public embedding capability.

Optional index fields are supplied by newer backends. Missing fields must not
be interpreted as complete coverage, nor should discovery failures imply FTS.
"""


def embedding_warning(embedding: dict) -> str:
    if embedding.get("available") is False:
        return "⚠ FTS-only — embeddings unavailable; semantic recall is degraded."
    if embedding.get("available") is not True:
        return "⚠ Embedding status unknown; semantic coverage is unverified."
    status = embedding.get("index_status")
    if status == "partial":
        return (
            "⚠ Partial semantic coverage — verified vectors are searchable; "
            "records without vectors remain keyword-searchable. Backfill still needs attention."
        )
    if status in ("reindexing", "failed", "unavailable"):
        return f"⚠ Embedding index is {status}; complete semantic coverage is not confirmed."
    if embedding.get("warning"):
        return f"⚠ {embedding['warning']}"
    if status != "ready":
        return "⚠ Embedding index coverage is unknown; this backend did not confirm a ready index."
    return ""


def embedding_summary(embedding: dict) -> str:
    available = embedding.get("available")
    state = "✓ available" if available is True else (
        f"✗ unavailable ({embedding.get('reason_unavailable') or 'unknown'})"
        if available is False else "unknown"
    )
    return (
        f"embedding: {state}; search: {embedding.get('search_mode') or 'unknown'}; "
        f"index: {embedding.get('index_status') or 'unknown'}"
    )
