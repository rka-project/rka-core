-- Explicit lifecycle dependencies, never inferred from citations or prose.
CREATE TABLE directive_dependencies (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    directive_id TEXT NOT NULL REFERENCES journal(id),
    decision_id TEXT NOT NULL REFERENCES decisions(id),
    declared_by TEXT NOT NULL CHECK (declared_by IN ('brain', 'executor', 'pi')),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    created_at TEXT NOT NULL,
    UNIQUE(project_id, directive_id, decision_id)
);
CREATE INDEX idx_directive_dependencies_decision ON directive_dependencies(project_id, decision_id);
