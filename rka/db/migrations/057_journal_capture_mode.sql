-- Existing originals and sources remain untouched and unclassified.
-- requires-table: journal, journal_attribution_revisions
ALTER TABLE journal ADD COLUMN capture_mode TEXT NOT NULL DEFAULT 'unknown'
    CHECK (capture_mode IN ('unknown', 'raw_capture', 'agent_restatement'));
ALTER TABLE journal_attribution_revisions ADD COLUMN before_capture_mode TEXT NOT NULL
    DEFAULT 'unknown' CHECK (before_capture_mode IN ('unknown', 'raw_capture', 'agent_restatement'));
ALTER TABLE journal_attribution_revisions ADD COLUMN after_capture_mode TEXT NOT NULL
    DEFAULT 'unknown' CHECK (after_capture_mode IN ('unknown', 'raw_capture', 'agent_restatement'));

DROP TRIGGER trg_journal_attribution_guard;
CREATE TRIGGER trg_journal_attribution_guard
BEFORE UPDATE OF source, verbatim_input, attribution_revision, capture_mode ON journal
WHEN NEW.source IS NOT OLD.source
  OR NEW.verbatim_input IS NOT OLD.verbatim_input
  OR NEW.attribution_revision IS NOT OLD.attribution_revision
  OR NEW.capture_mode IS NOT OLD.capture_mode
BEGIN
    SELECT CASE WHEN NEW.attribution_revision != OLD.attribution_revision + 1
      OR NOT EXISTS (
        SELECT 1 FROM journal_attribution_revisions AS revision
        WHERE revision.project_id = OLD.project_id AND revision.journal_id = OLD.id
          AND revision.revision = NEW.attribution_revision
          AND revision.before_source IS OLD.source
          AND revision.before_verbatim_input IS OLD.verbatim_input
          AND revision.before_capture_mode IS OLD.capture_mode
          AND revision.after_source IS NEW.source
          AND revision.after_verbatim_input IS NEW.verbatim_input
          AND revision.after_capture_mode IS NEW.capture_mode
      ) THEN RAISE(ABORT, 'use a revision-guarded journal attribution correction') END;
END;

CREATE TRIGGER trg_journal_capture_insert
BEFORE INSERT ON journal
WHEN (NEW.capture_mode = 'raw_capture'
   OR (NEW.capture_mode = 'agent_restatement' AND NEW.source = 'pi'))
 AND length(trim(coalesce(NEW.verbatim_input, ''))) = 0
BEGIN
    SELECT RAISE(ABORT, 'explicit capture mode requires original text');
END;

CREATE TRIGGER trg_journal_capture_update
BEFORE UPDATE OF capture_mode, source, verbatim_input ON journal
WHEN (NEW.capture_mode = 'raw_capture'
   OR (NEW.capture_mode = 'agent_restatement' AND NEW.source = 'pi'))
 AND length(trim(coalesce(NEW.verbatim_input, ''))) = 0
BEGIN
    SELECT RAISE(ABORT, 'explicit capture mode requires original text');
END;
