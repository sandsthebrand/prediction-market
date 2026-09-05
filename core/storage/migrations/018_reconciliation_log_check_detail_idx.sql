-- Add composite index on reconciliation_log(check_type, detail, checked_at) for the
-- _is_recently_logged dedup query in core/engine/reconciliation.py.
-- The function queries WHERE check_type = ? AND detail = ? AND checked_at >= ?
-- on every reconciliation cycle; a covered composite index eliminates the
-- sequential scan that otherwise grows with the table.
CREATE INDEX IF NOT EXISTS idx_reconciliation_log_check_detail ON reconciliation_log(check_type, detail, checked_at);
