-- A failed backfill can leave a verified, coherent subset of current vectors.
-- Preserve failure history and distinguish that subset from complete readiness.
-- No canonical record, vector or embedding metadata is changed by this migration.
-- requires-table: embedding_index_state
ALTER TABLE embedding_index_state RENAME TO embedding_index_state_before_partial;

CREATE TABLE embedding_index_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    generation INTEGER NOT NULL CHECK (generation >= 1),
    space_signature TEXT NOT NULL CHECK (length(trim(space_signature)) > 0),
    model_name TEXT NOT NULL CHECK (length(trim(model_name)) > 0),
    dimensions INTEGER NOT NULL CHECK (dimensions > 0),
    status TEXT NOT NULL CHECK (status IN ('ready', 'partial', 'reindexing', 'failed')),
    last_error TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

INSERT INTO embedding_index_state
SELECT * FROM embedding_index_state_before_partial;
DROP TABLE embedding_index_state_before_partial;
