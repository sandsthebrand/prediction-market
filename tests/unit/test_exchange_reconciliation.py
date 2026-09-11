import pytest
import aiosqlite

from core.engine.execution_control import is_halted
from core.engine.reconciliation import reconcile_exchange_state


class FakeClient:
    def __init__(self, open_orders=None, fills=None, order_status=None, error=None):
        self.open_orders = open_orders or []
        self.fills = fills or []
        self.order_status = order_status
        self.error = error

    async def list_open_orders(self):
        if self.error:
            raise self.error
        return self.open_orders

    async def list_recent_fills(self, since=None):
        if self.error:
            raise self.error
        return self.fills

    async def get_order_status(self, order_id):
        if self.error:
            raise self.error
        return self.order_status


async def make_db():
    db = await aiosqlite.connect(":memory:")
    await db.executescript(
        """
        CREATE TABLE execution_control (
            id INTEGER PRIMARY KEY,
            halted INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            halted_at TEXT,
            cleared_at TEXT,
            updated_at TEXT
        );
        INSERT INTO execution_control(id, halted) VALUES (1, 0);
        CREATE TABLE system_events (
            event_type TEXT,
            severity TEXT,
            component TEXT,
            detail TEXT,
            occurred_at TEXT
        );
        CREATE TABLE orders (
            id TEXT PRIMARY KEY,
            platform TEXT,
            status TEXT,
            submitted_at TEXT,
            requested_size REAL,
            filled_price REAL,
            filled_size REAL,
            fee_paid REAL,
            fee_verified INTEGER,
            filled_at INTEGER,
            updated_at INTEGER
        );
        """
    )
    await db.commit()
    return db


@pytest.mark.asyncio
async def test_clean_exchange_state_does_not_halt():
    db = await make_db()
    result = await reconcile_exchange_state(
        db,
        {"polymarket": FakeClient(), "kalshi": FakeClient()},
    )
    assert result["clean"] is True
    assert await is_halted(db) is False
    await db.close()


@pytest.mark.asyncio
async def test_unknown_remote_order_halts():
    db = await make_db()
    result = await reconcile_exchange_state(
        db,
        {
            "polymarket": FakeClient(open_orders=[{"order_id": "remote-1"}]),
            "kalshi": FakeClient(),
        },
    )
    assert result["unknown_remote_orders"] == 1
    assert result["clean"] is False
    assert await is_halted(db) is True
    await db.close()


@pytest.mark.asyncio
async def test_unknown_remote_fill_halts():
    db = await make_db()
    result = await reconcile_exchange_state(
        db,
        {
            "polymarket": FakeClient(fills=[{"order_id": "remote-fill-1"}]),
            "kalshi": FakeClient(),
        },
    )
    assert result["unknown_remote_fills"] == 1
    assert result["clean"] is False
    assert await is_halted(db) is True
    await db.close()


@pytest.mark.asyncio
async def test_pending_local_order_is_recovered_from_terminal_exchange_state():
    db = await make_db()
    await db.execute(
        "INSERT INTO orders(id, platform, status, submitted_at, requested_size) VALUES (?, ?, ?, ?, ?)",
        ("known-1", "kalshi", "pending", "1778000000", 10.0),
    )
    await db.commit()
    result = await reconcile_exchange_state(
        db,
        {
            "polymarket": FakeClient(),
            "kalshi": FakeClient(
                order_status={
                    "status": "executed",
                    "fill_count_fp": 10,
                    "yes_price_dollars": 0.42,
                    "taker_fees_dollars": 0.01,
                    "maker_fees_dollars": 0,
                }
            ),
        },
    )
    row = await db.execute_fetchone(
        "SELECT status, filled_size, fee_verified FROM orders WHERE id = ?", ("known-1",)
    )
    assert result["local_pending_recovered"] == 1
    assert row == ("filled", 10.0, 1)
    assert await is_halted(db) is False
    await db.close()


@pytest.mark.asyncio
async def test_exchange_api_failure_halts():
    db = await make_db()
    result = await reconcile_exchange_state(
        db,
        {
            "polymarket": FakeClient(error=RuntimeError("venue unavailable")),
            "kalshi": FakeClient(),
        },
    )
    assert result["exchange_errors"] >= 1
    assert result["clean"] is False
    assert await is_halted(db) is True
    await db.close()
