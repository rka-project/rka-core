-- I3b: attribution-only revisions. Existing unknown originals remain NULL.
-- requires-table: journal, projects, project_deletion_authorizations

ALTER TABLE journal ADD COLUMN attribution_revision INTEGER NOT NULL DEFAULT 0
    CHECK (attribution_revision >= 0);
CREATE UNIQUE INDEX idx_journal_id_project ON journal(id, project_id);

CREATE TABLE journal_attribution_revisions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    journal_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    expected_revision INTEGER NOT NULL CHECK (expected_revision = revision - 1),
    request_id TEXT NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128),
    actor TEXT NOT NULL CHECK (actor IN ('brain', 'executor', 'pi', 'llm', 'web_ui', 'system')),
    actor_basis TEXT NOT NULL CHECK (actor_basis = 'caller_asserted'),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    before_source TEXT NOT NULL,
    before_verbatim_input TEXT,
    after_source TEXT NOT NULL CHECK (after_source IN ('brain', 'executor', 'pi', 'llm', 'web_ui')),
    after_verbatim_input TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (project_id, journal_id, revision),
    UNIQUE (project_id, journal_id, request_id),
    FOREIGN KEY (journal_id, project_id) REFERENCES journal(id, project_id) ON DELETE RESTRICT
);

CREATE TRIGGER trg_journal_attribution_no_update
BEFORE UPDATE ON journal_attribution_revisions
BEGIN
    SELECT RAISE(ABORT, 'journal attribution revisions are immutable');
END;

CREATE TRIGGER trg_journal_attribution_no_delete
BEFORE DELETE ON journal_attribution_revisions
WHEN NOT EXISTS (
    SELECT 1 FROM project_deletion_authorizations WHERE project_id = OLD.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'journal attribution requires project-authorized deletion');
END;

-- Ordinary field editors, including direct internal SQL, cannot overwrite
-- attribution without the matching immutable correction in this transaction.
CREATE TRIGGER trg_journal_attribution_guard
BEFORE UPDATE OF source, verbatim_input, attribution_revision ON journal
WHEN NEW.source IS NOT OLD.source
  OR NEW.verbatim_input IS NOT OLD.verbatim_input
  OR NEW.attribution_revision IS NOT OLD.attribution_revision
BEGIN
    SELECT CASE WHEN NEW.attribution_revision != OLD.attribution_revision + 1
      OR NOT EXISTS (
        SELECT 1 FROM journal_attribution_revisions AS revision
        WHERE revision.project_id = OLD.project_id AND revision.journal_id = OLD.id
          AND revision.revision = NEW.attribution_revision
          AND revision.before_source IS OLD.source
          AND revision.before_verbatim_input IS OLD.verbatim_input
          AND revision.after_source IS NEW.source
          AND revision.after_verbatim_input IS NEW.verbatim_input
      ) THEN RAISE(ABORT, 'use a revision-guarded journal attribution correction') END;
END;
