-- Reflagging (including legacy direct propagation) reopens review. Existing
-- reviewed rows are preserved; the immutable history remains in audit_log.
CREATE TRIGGER claims_reopen_staleness_review
AFTER UPDATE OF staleness ON claims
WHEN NEW.staleness IN ('yellow', 'red')
BEGIN
    UPDATE claims SET staleness_reviewed_at = NULL, staleness_verdict = NULL,
        staleness_resolution = NULL, staleness_resolution_journal_id = NULL,
        staleness_resolved_by = NULL WHERE id = NEW.id AND project_id = NEW.project_id;
END;

CREATE TRIGGER clusters_reopen_staleness_review
AFTER UPDATE OF staleness ON evidence_clusters
WHEN NEW.staleness IN ('yellow', 'red')
BEGIN
    UPDATE evidence_clusters SET staleness_reviewed_at = NULL, staleness_verdict = NULL,
        staleness_resolution = NULL, staleness_resolution_journal_id = NULL,
        staleness_resolved_by = NULL WHERE id = NEW.id AND project_id = NEW.project_id;
END;

CREATE TRIGGER claims_reopen_structural_review
AFTER UPDATE OF stale ON claims WHEN NEW.stale = 1
BEGIN
    UPDATE claims SET staleness_reviewed_at = NULL, staleness_verdict = NULL,
        staleness_resolution = NULL, staleness_resolution_journal_id = NULL,
        staleness_resolved_by = NULL WHERE id = NEW.id AND project_id = NEW.project_id;
END;

CREATE TRIGGER clusters_reopen_structural_review
AFTER UPDATE OF needs_reprocessing ON evidence_clusters WHEN NEW.needs_reprocessing = 1
BEGIN
    UPDATE evidence_clusters SET staleness_reviewed_at = NULL, staleness_verdict = NULL,
        staleness_resolution = NULL, staleness_resolution_journal_id = NULL,
        staleness_resolved_by = NULL WHERE id = NEW.id AND project_id = NEW.project_id;
END;
