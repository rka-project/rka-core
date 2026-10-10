-- Database-local retry ledger. 060 is reserved by the independent index work.
-- requires-table: projects, project_deletion_authorizations
CREATE TABLE journal_write_receipts (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    request_id TEXT NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128),
    -- Deliberately no journal FK: deleting a note must not free its retry key.
    journal_id TEXT NOT NULL,
    hash_version TEXT NOT NULL CHECK (hash_version = 'journal-create-v1'),
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    verbatim_input_sha256 TEXT CHECK (verbatim_input_sha256 IS NULL OR length(verbatim_input_sha256) = 64),
    actor TEXT NOT NULL CHECK (actor IN ('brain', 'executor', 'pi', 'llm', 'web_ui', 'system')),
    entry_json TEXT NOT NULL CHECK (json_valid(entry_json)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (project_id, request_id)
);

CREATE TRIGGER trg_journal_write_receipt_no_update
BEFORE UPDATE ON journal_write_receipts
BEGIN
    SELECT RAISE(ABORT, 'journal write receipts are immutable');
END;

CREATE TRIGGER trg_journal_write_receipt_no_delete
BEFORE DELETE ON journal_write_receipts
WHEN NOT EXISTS (
    SELECT 1 FROM project_deletion_authorizations WHERE project_id = OLD.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'journal write receipts require project-authorized deletion');
END;
