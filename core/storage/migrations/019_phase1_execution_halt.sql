-- Migration 019: persistent Phase 1 execution halt state.
-- A halt survives process restarts so an unbalanced/unknown execution state
-- cannot be cleared accidentally by restarting the service.
CREATE TABLE IF NOT EXISTS execution_control (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    halted INTEGER NOT NULL DEFAULT 0 CHECK (halted IN (0, 1)),
    reason TEXT,
    halted_at TEXT,
    cleared_at TEXT,
    updated_at TEXT NOT NULL
);

INSERT OR IGNORE INTO execution_control (id, halted, updated_at)
VALUES (1, 0, datetime('now'));
