-- Migration 020: distinguish verified fees from confirmed fills.
-- A confirmed exchange fill must remain a fill even when fee metadata is
-- temporarily unavailable. Reconciliation can resolve the fee later.
ALTER TABLE orders ADD COLUMN fee_verified INTEGER NOT NULL DEFAULT 1
    CHECK (fee_verified IN (0, 1));

CREATE INDEX IF NOT EXISTS idx_orders_fee_verified ON orders(fee_verified);
