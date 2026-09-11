"""
Internal and exchange-state reconciliation for the trading system.

The exchange reconciliation path is deliberately fail-closed: if an
exchange cannot provide a trustworthy view, or if an exchange-side order/fill
cannot be mapped to local state, execution is halted rather than guessed.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

import aiosqlite

from core.config import get_config

logger = logging.getLogger(__name__)


async def reconcile_exchange_state(
    db: aiosqlite.Connection,
    clients: dict[str, object],
    *,
    fill_lookback_s: int = 24 * 60 * 60,
) -> dict[str, int | bool]:
    """Reconcile local orders against authenticated exchange state.

    ``clients`` is keyed by platform label and each client must implement
    ``list_open_orders`` and ``list_recent_fills``. A platform failure or an
    unexplained remote order/fill immediately persists a halt.

    We also re-query every locally pending order. This closes the crash window
    where an order was accepted by the exchange but the process died before
    the local fill update was committed.
    """
    summary: dict[str, int | bool] = {
        "platforms_checked": 0,
        "open_orders_checked": 0,
        "fills_checked": 0,
        "pending_orders_checked": 0,
        "local_pending_recovered": 0,
        "unknown_remote_orders": 0,
        "unknown_remote_fills": 0,
        "exchange_errors": 0,
        "clean": True,
    }

    cutoff = int(time.time()) - fill_lookback_s
    try:
        cursor = await db.execute(
            """
            SELECT id, platform, submitted_at, status
            FROM orders
            WHERE status = 'pending'
              AND submitted_at IS NOT NULL
              AND submitted_at != ''
            """
        )
        pending_rows = await cursor.fetchall()
    except Exception as exc:
        await _halt_exchange(db, f"exchange reconciliation DB read failed: {exc}")
        summary["exchange_errors"] = 1
        summary["clean"] = False
        return summary

    for platform, client in clients.items():
        summary["platforms_checked"] += 1
        try:
            open_orders = await client.list_open_orders()
            fills = await client.list_recent_fills(cutoff)
        except NotImplementedError as exc:
            await _halt_exchange(
                db, f"{platform} exchange reconciliation unsupported: {exc}"
            )
            summary["exchange_errors"] += 1
            summary["clean"] = False
            continue
        except Exception as exc:
            logger.exception("Exchange reconciliation failed for %s", platform)
            await _halt_exchange(db, f"{platform} exchange reconciliation failed: {exc}")
            summary["exchange_errors"] += 1
            summary["clean"] = False
            continue

        open_ids = {
            str(_remote_order_id(order))
            for order in open_orders
            if _remote_order_id(order)
        }
        summary["open_orders_checked"] += len(open_ids)
        fill_order_ids = {
            str(_remote_fill_order_id(fill))
            for fill in fills
            if _remote_fill_order_id(fill)
        }
        summary["fills_checked"] += len(fills)

        try:
            cursor = await db.execute(
                """
                SELECT id, status, submitted_at
                FROM orders
                WHERE platform = ?
                """,
                (platform,),
            )
            local_rows = await cursor.fetchall()
        except Exception as exc:
            await _halt_exchange(db, f"{platform} local order query failed: {exc}")
            summary["exchange_errors"] += 1
            summary["clean"] = False
            continue

        local_ids = {str(row[0]) for row in local_rows if row[0]}
        local_pending_ids = {
            str(row[0]) for row in local_rows if row[0] and row[1] == "pending"
        }

        unknown_orders = open_ids - local_ids
        if unknown_orders:
            summary["unknown_remote_orders"] += len(unknown_orders)
            summary["clean"] = False
            await _halt_exchange(
                db,
                f"{platform} has remote open orders absent from local DB: "
                + ", ".join(sorted(unknown_orders)[:20]),
            )

        unknown_fills = fill_order_ids - local_ids
        if unknown_fills:
            summary["unknown_remote_fills"] += len(unknown_fills)
            summary["clean"] = False
            await _halt_exchange(
                db,
                f"{platform} has remote fills absent from local DB: "
                + ", ".join(sorted(unknown_fills)[:20]),
            )

        # A pending local order is not allowed to remain ambiguous. If it is
        # absent from the open set, ask the exchange for its terminal state.
        for order_id in local_pending_ids:
            summary["pending_orders_checked"] += 1
            if order_id in open_ids:
                continue
            try:
                remote = await client.get_order_status(order_id)
            except Exception as exc:
                await _halt_exchange(
                    db, f"{platform} status lookup failed for {order_id}: {exc}"
                )
                summary["exchange_errors"] += 1
                summary["clean"] = False
                continue
            if not remote:
                await _halt_exchange(
                    db,
                    f"{platform} local pending order {order_id} is absent from "
                    "open orders and cannot be retrieved",
                )
                summary["exchange_errors"] += 1
                summary["clean"] = False
                continue
            if await _reconcile_terminal_order(db, platform, order_id, remote):
                summary["local_pending_recovered"] += 1

    # If any pending rows exist on a platform for which no client was supplied,
    # the caller has not established complete exchange coverage.
    pending_platforms = {str(row[1]) for row in pending_rows if row[1]}
    missing_clients = pending_platforms - set(clients)
    if missing_clients:
        await _halt_exchange(
            db,
            "pending orders have no exchange reconciliation client: "
            + ", ".join(sorted(missing_clients)),
        )
        summary["exchange_errors"] += len(missing_clients)
        summary["clean"] = False

    return summary


async def _reconcile_terminal_order(
    db: aiosqlite.Connection,
    platform: str,
    order_id: str,
    remote: dict,
) -> bool:
    """Persist a terminal remote fill when the local DB missed the update."""
    status = str(remote.get("status", "")).lower()
    matched = _number(
        remote.get("size_matched")
        if platform == "polymarket"
        else remote.get("fill_count_fp", remote.get("fill_count", 0))
    )
    terminal = status in {
        "matched",
        "unmatched",
        "executed",
        "filled",
        "canceled",
        "cancelled",
    }
    if not terminal:
        return False
    if matched <= 0:
        await db.execute(
            "UPDATE orders SET status = 'failed', updated_at = ? WHERE id = ?",
            (int(time.time()), order_id),
        )
        await db.commit()
        return True

    if platform == "polymarket":
        price = _number(remote.get("price"))
        fee = None
        fee_verified = 0
    else:
        price = _number(remote.get("yes_price_dollars"))
        fee = _number(remote.get("taker_fees_dollars", 0)) + _number(
            remote.get("maker_fees_dollars", 0)
        )
        fee_verified = 1

    local_requested = await db.execute_fetchone(
        "SELECT requested_size FROM orders WHERE id = ?", (order_id,)
    )
    requested = _number(local_requested[0]) if local_requested else 0.0
    new_status = "filled" if matched >= requested else "partially_filled"
    await db.execute(
        """
        UPDATE orders SET
            filled_price = ?, filled_size = ?, fee_paid = ?, fee_verified = ?,
            status = ?, filled_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            price,
            matched,
            fee,
            fee_verified,
            new_status,
            int(time.time()),
            int(time.time()),
            order_id,
        ),
    )
    await db.commit()
    return True


def _remote_order_id(order: dict):
    return order.get("order_id") or order.get("orderID") or order.get("id")


def _remote_fill_order_id(fill: dict):
    return fill.get("order_id") or fill.get("orderID") or fill.get("orderId")


def _number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


async def _halt_exchange(db: aiosqlite.Connection, reason: str) -> None:
    from core.engine.execution_control import halt

    try:
        await halt(db, reason)
    except Exception:
        logger.exception("Failed to persist exchange reconciliation halt")


async def reconcile_internal_state(db: aiosqlite.Connection) -> dict[str, int]:
    """Run all internal reconciliation checks.

    Returns a summary dict with the number of discrepancies found per
    check_type. Each discrepancy is also written to `reconciliation_log`
    with status='discrepancy' so it can be reviewed.
    """
    summary: dict[str, int] = {
        "orphaned_positions": 0,
        "stuck_pending_orders": 0,
        "unbalanced_arb_pairs": 0,
        "closed_without_outcomes": 0,
        "signals_without_orders": 0,
    }

    summary["orphaned_positions"] = await _check_orphaned_positions(db)
    summary["stuck_pending_orders"] = await _check_stuck_pending_orders(db)
    summary["unbalanced_arb_pairs"] = await _check_unbalanced_arb_pairs(db)
    summary["closed_without_outcomes"] = await _check_closed_without_outcomes(db)
    summary["signals_without_orders"] = await _check_signals_without_orders(db)

    try:
        await db.commit()
    except Exception as e:
        logger.warning("reconciliation commit failed: %s", e)

    total = sum(summary.values())
    if total:
        logger.warning("RECONCILIATION found %d discrepancies: %s", total, summary)
        try:
            from core.alerting import Severity, get_alert_manager

            get_alert_manager().send_nowait(
                title="Reconciliation discrepancies detected",
                message=f"{total} discrepancies: {summary}",
                severity=Severity.WARNING,
                component="reconciliation",
            )
        except Exception as _alert_err:
            logger.error("Failed to send reconciliation alert: %s", _alert_err)
    else:
        logger.info("RECONCILIATION clean: no discrepancies")

    return summary


async def _check_orphaned_positions(db: aiosqlite.Connection) -> int:
    """Open positions whose backing order(s) are failed or missing.

    If an order is 'failed' or 'cancelled' but a position is still marked
    'open' for the same signal_id + market_id, that's state drift.
    """
    cursor = await db.execute("""
        WITH latest_order AS (
            SELECT signal_id, market_id, status,
                   ROW_NUMBER() OVER (
                       PARTITION BY signal_id, market_id
                       ORDER BY submitted_at DESC
                   ) AS rn
            FROM orders
        )
        SELECT p.id, p.signal_id, p.market_id, lo.status AS order_status
        FROM positions p
        LEFT JOIN latest_order lo
               ON lo.signal_id = p.signal_id
              AND lo.market_id = p.market_id
              AND lo.rn = 1
        WHERE p.status = 'open'
          AND (lo.status IS NULL OR lo.status IN ('failed', 'cancelled'))
        """)
    rows = await cursor.fetchall()
    count = 0
    for pos_id, signal_id, market_id, order_status in rows:
        order_status_label = (
            "<no_matching_order>" if order_status is None else order_status
        )
        detail = (
            f"position_id={pos_id} signal_id={signal_id} "
            f"market_id={market_id} order_status={order_status_label}"
        )
        if await _is_recently_logged(db, "orphaned_position", detail):
            continue
        await _log_discrepancy(
            db,
            platform="internal",
            check_type="orphaned_position",
            local_value=1.0,
            exchange_value=0.0,
            discrepancy=1.0,
            status="discrepancy",
            detail=detail,
        )
        count += 1
    return count


async def _check_stuck_pending_orders(db: aiosqlite.Connection) -> int:
    """Orders in 'pending' state past the stuck threshold.

    `submitted_at` is stored as TEXT but arb_engine/base client write it
    as str(int(time.time())) — a 10-digit decimal string. Text comparison
    of equal-length decimal strings is lexicographically equivalent to
    numeric comparison, so we compare directly to allow SQLite to use the
    idx_orders_submitted_at index instead of computing CAST on every row.
    """
    threshold_s = get_config().risk_controls.reconcile_stuck_pending_threshold_s
    cutoff_str = str(int(time.time()) - threshold_s)
    cursor = await db.execute(
        """
        SELECT id, platform, signal_id, market_id, submitted_at
        FROM orders
        WHERE status = 'pending'
          AND submitted_at IS NOT NULL
          AND submitted_at != ''
          AND submitted_at < ?
        """,
        (cutoff_str,),
    )
    rows = await cursor.fetchall()
    count = 0
    for order_id, platform, signal_id, market_id, submitted_at in rows:
        try:
            age_s = int(time.time()) - int(submitted_at)
        except (TypeError, ValueError):
            age_s = -1
        detail = f"order_id={order_id}"
        action_taken = (
            f"order_id={order_id} signal_id={signal_id} "
            f"market_id={market_id} age_s={age_s}"
        )
        if await _is_recently_logged(db, "stuck_pending_order", detail):
            continue
        await _log_discrepancy(
            db,
            platform=platform or "unknown",
            check_type="stuck_pending_order",
            local_value=float(age_s),
            exchange_value=None,
            discrepancy=float(age_s),
            status="discrepancy",
            detail=detail,
            action_taken=action_taken,
        )
        count += 1
    return count


async def _check_unbalanced_arb_pairs(db: aiosqlite.Connection) -> int:
    """Arb signals where only one leg filled but no position was written."""
    cutoff_30d_str = str(int(time.time()) - 30 * 86400)
    cursor = await db.execute(
        """
        WITH multi_leg_signals AS (
            SELECT signal_id FROM orders
            WHERE submitted_at > ?
            GROUP BY signal_id
            HAVING COUNT(*) >= 2
        )
        SELECT o.signal_id,
               SUM(CASE WHEN o.status IN ('filled','partially_filled') THEN 1 ELSE 0 END) AS filled_count,
               COUNT(*) AS leg_count,
               GROUP_CONCAT(
                   CASE WHEN o.status IN ('filled','partially_filled') THEN o.platform END
               ) AS filled_platforms
        FROM orders o
        JOIN multi_leg_signals mls ON o.signal_id = mls.signal_id
        WHERE o.submitted_at > ?
        GROUP BY o.signal_id
        HAVING filled_count = 1
          AND NOT EXISTS (SELECT 1 FROM positions p WHERE p.signal_id = o.signal_id)
        """,
        (cutoff_30d_str, cutoff_30d_str),
    )
    rows = await cursor.fetchall()
    count = 0
    for signal_id, filled_count, leg_count, filled_platforms in rows:
        platform_detail = (
            f" filled_platform={filled_platforms}" if filled_platforms else ""
        )
        detail = (
            f"signal_id={signal_id} filled_legs={filled_count} "
            f"total_legs={leg_count}{platform_detail} — one side open without hedge"
        )
        if await _is_recently_logged(db, "unbalanced_arb_pair", detail):
            continue
        await _log_discrepancy(
            db,
            platform="cross_platform",
            check_type="unbalanced_arb_pair",
            local_value=float(filled_count),
            exchange_value=float(leg_count),
            discrepancy=float(leg_count - filled_count),
            status="discrepancy",
            detail=detail,
        )
        count += 1
    return count


async def _is_recently_logged(
    db: aiosqlite.Connection,
    check_type: str,
    detail: str,
    window_s: int = 3600,
) -> bool:
    """Return True if the same discrepancy was recently logged."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_s)).isoformat()
    cursor = await db.execute(
        "SELECT COUNT(*) FROM reconciliation_log "
        "WHERE check_type = ? AND detail = ? AND checked_at >= ?",
        (check_type, detail, cutoff),
    )
    row = await cursor.fetchone()
    return bool(row and row[0] > 0)


async def _log_discrepancy(
    db: aiosqlite.Connection,
    *,
    platform: str,
    check_type: str,
    local_value: float,
    exchange_value: float | None,
    discrepancy: float | None,
    status: str,
    detail: str,
    action_taken: str | None = None,
) -> None:
    """Insert one row into reconciliation_log."""
    try:
        await db.execute(
            """
            INSERT INTO reconciliation_log (
                platform, check_type, local_value, exchange_value,
                discrepancy, status, detail, action_taken, checked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                platform,
                check_type,
                local_value,
                exchange_value,
                discrepancy,
                status,
                detail,
                action_taken,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    except Exception as e:
        logger.error(
            "Failed to write reconciliation_log row: check_type=%s err=%s",
            check_type,
            e,
        )


async def _check_signals_without_orders(db: aiosqlite.Connection) -> int:
    """Signals that were fired but generated no orders."""
    cutoff_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    grace_period = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    cursor = await db.execute(
        """
        SELECT s.id, s.strategy, s.fired_at
        FROM signals s
        WHERE s.fired_at >= ?
          AND s.fired_at <= ?
          AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.signal_id = s.id)
        """,
        (cutoff_30d, grace_period),
    )
    rows = await cursor.fetchall()
    count = 0
    for signal_id, strategy, fired_at in rows:
        detail = f"signal_id={signal_id} strategy={strategy} fired_at={fired_at}"
        if await _is_recently_logged(db, "signal_without_orders", detail):
            continue
        await _log_discrepancy(
            db,
            platform="internal",
            check_type="signal_without_orders",
            local_value=1.0,
            exchange_value=0.0,
            discrepancy=1.0,
            status="discrepancy",
            detail=detail,
        )
        count += 1
    return count


async def _check_closed_without_outcomes(db: aiosqlite.Connection) -> int:
    """Closed positions with no corresponding trade_outcomes row."""
    cutoff_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    cursor = await db.execute(
        """
        SELECT p.id, p.signal_id, p.market_id, p.updated_at
        FROM positions p
        WHERE p.status = 'closed'
          AND p.updated_at >= ?
          AND p.signal_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM trade_outcomes t WHERE t.signal_id = p.signal_id
          )
        """,
        (cutoff_30d,),
    )
    rows = await cursor.fetchall()
    count = 0
    for pos_id, signal_id, market_id, updated_at in rows:
        detail = (
            f"position_id={pos_id} signal_id={signal_id} "
            f"market_id={market_id} closed_at={updated_at}"
        )
        if await _is_recently_logged(db, "closed_without_outcome", detail):
            continue
        await _log_discrepancy(
            db,
            platform="internal",
            check_type="closed_without_outcome",
            local_value=1.0,
            exchange_value=0.0,
            discrepancy=1.0,
            status="discrepancy",
            detail=detail,
        )
        count += 1
    return count
