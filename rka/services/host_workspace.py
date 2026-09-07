"""Bounded host-side scanning shared by MCP scan and bootstrap adapters."""

from rka.infra.file_access import FileAccessError, ScanBudget
from rka.services.classify import (
    classify_extension,
    detect_capabilities,
    detect_content_hint,
    extension_to_target,
    extract_pdf_preview,
    hash_file,
    hint_to_type,
    safe_read_text,
)


def scan_host_files(policy, root, *, ignores, max_bytes, skip_files=()):
    if max_bytes < 1:
        raise ValueError("max_file_size_mb must be positive")
    files = []
    budget = ScanBudget()
    caps = detect_capabilities()
    skipped = set(skip_files)
    for path in policy.walk(root, ignores=ignores, budget=budget):
        relative = str(path.relative_to(root))
        if relative in skipped:
            continue
        try:
            with policy.snapshot(path, max_bytes=max_bytes, budget=budget) as snapshot:
                ext = path.suffix.lower()
                category = classify_extension(ext)
                target = extension_to_target(ext)
                preview = None
                content_hint = "general"
                proposed_type = "finding"
                if category.value not in ("pdf", "data", "unknown"):
                    preview = safe_read_text(snapshot, capabilities=caps, max_chars=500)
                    if preview:
                        hint = detect_content_hint(preview)
                        content_hint = hint.value
                        proposed_type = hint_to_type(hint)
                elif category.value == "pdf":
                    preview = extract_pdf_preview(snapshot, caps)
                files.append(
                    {
                        "relative_path": relative,
                        "filename": path.name,
                        "extension": ext,
                        "size_bytes": snapshot.stat().st_size,
                        "file_hash": hash_file(snapshot),
                        "content_preview": preview[:500] if preview else None,
                        "category": category.value,
                        "content_hint": content_hint,
                        "ingestion_target": target.value,
                        "proposed_type": proposed_type,
                        "proposed_tags": [],
                    }
                )
        except FileAccessError as exc:
            if exc.code == "file_access_busy":
                raise
            if budget.stopped:
                break
            # A raced/unreadable entry cannot authorize a different file.
            budget.skipped += 1
            continue
    return files, budget


def validate_host_manifest(manifest, scanned_files):
    """A server reply can annotate an existing file, never nominate a new path."""
    trusted = {item["relative_path"]: item for item in scanned_files}
    returned = manifest.get("files", [])
    if not isinstance(returned, list) or len(returned) > len(trusted):
        raise FileAccessError("Server manifest exceeds the locally scanned file set")
    seen = set()
    for item in returned:
        relative = item.get("relative_path") if isinstance(item, dict) else None
        if not isinstance(relative, str) or relative not in trusted or relative in seen:
            raise FileAccessError("Server manifest names an unscanned or repeated file")
        seen.add(relative)
        for field in ("filename", "category", "ingestion_target", "file_hash"):
            if item.get(field) != trusted[relative][field]:
                raise FileAccessError("Server manifest changed a locally scanned file descriptor")


def read_host_content(policy, root, scanned, budget):
    from rka.services.classify import extract_pdf_metadata_raw

    path = policy.relative_file(root, scanned["relative_path"])
    caps = detect_capabilities()
    with policy.snapshot(path, budget=budget) as snapshot:
        if hash_file(snapshot) != scanned["file_hash"]:
            raise FileAccessError(
                "File changed since scan; rescan before ingestion",
                code="file_changed",
                status_code=409,
            )
        if scanned["category"] == "pdf":
            metadata = extract_pdf_metadata_raw(snapshot) or {"title": path.stem}
            return "", "pdf_metadata", metadata
        content_type = (
            "bibtex"
            if scanned["category"] == "bibtex"
            else "code"
            if scanned["category"] == "code"
            else "text"
        )
        return safe_read_text(snapshot, capabilities=caps) or "", content_type, {}
