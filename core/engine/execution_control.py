"""Persistent execution halt / re-arm control for live-safe operation."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiosqlite

logger = logging.getLogger(__name__)


async def is_halted(db: aiosqlite.Connection) -> bool:
    cur = await db.execute("SELECT halted FROM execution_control WHERE id = 1")
    row = await cur.fetchone()
    return bool(row and row[0])


async def halt(db: aiosqlite.Connection, reason: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """UPDATE execution_control
           SET halted = 1, reason = ?, halted_at = ?, updated_at = ?
           WHERE id = 1""",
        (reason[:2000], now, now),
    )
    await db.execute(
        """INSERT INTO system_events
           (event_type, severity, component, detail, occurred_at)
           VALUES ('execution_halt', 'critical', 'phase1_arb', ?, ?)""",
        (reason[:4000], now),
    )
    await db.commit()
    logger.critical("PHASE1 EXECUTION HALTED: %s", reason)


async def halt_and_cancel(
    db: aiosqlite.Connection, clients: dict[str, object], reason: str
) -> None:
    """Persist the halt and make a best-effort cancellation of all open orders.

    The halt is committed first. Cancellation failures never clear the halt;
    reconciliation remains responsible for detecting any residual exchange
    state before trading can resume.
    """
    await halt(db, reason)
    for platform, client in clients.items():
        try:
            open_orders = await client.list_open_orders()
        except Exception as exc:
            logger.exception(
                "Could not enumerate %s open orders while halted: %s", platform, exc
            )
            continue
        for order in open_orders:
            order_id = (
                order.get("order_id") or order.get("orderID") or order.get("id")
            )
            if not order_id:
                logger.error("%s open order missing order id during halt", platform)
                continue
            try:
                cancelled = await client.cancel_order(str(order_id))
                if not cancelled:
                    logger.error(
                        "Failed to cancel %s open order %s during halt",
                        platform,
                        order_id,
                    )
            except Exception:
                logger.exception(
                    "Exception cancelling %s order %s during halt",
                    platform,
                    order_id,
                )


async def clear_halt(
    db: aiosqlite.Connection, reason: str = "operator re-armed"
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """UPDATE execution_control
           SET halted = 0, reason = NULL, cleared_at = ?, updated_at = ?
           WHERE id = 1""",
        (now, now),
    )
    await db.execute(
        """INSERT INTO system_events
           (event_type, severity, component, detail, occurred_at)
           VALUES ('execution_rearmed', 'warning', 'phase1_arb', ?, ?)""",
        (reason[:4000], now),
    )
    await db.commit()
    logger.warning("PHASE1 EXECUTION RE-ARMED: %s", reason)
