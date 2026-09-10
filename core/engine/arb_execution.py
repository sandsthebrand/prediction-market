"""Concurrent, fill-safe Phase 1 arbitrage execution."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum

from execution.clients.base import OrderResult
from execution.models import OrderLeg

logger = logging.getLogger(__name__)


class ArbOutcome(str, Enum):
    REJECTED_PRE_TRADE = "rejected_pre_trade"
    BOTH_FILLED = "both_filled"
    NO_FILL = "no_fill"
    UNBALANCED_FLATTENED = "unbalanced_flattened"
    FLATTEN_FAILED = "flatten_failed"
    SUBMISSION_ERROR = "submission_error"


@dataclass
class FlattenAttempt:
    platform: str
    market_id: str
    excess_qty: float
    original_side: str
    flatten_side: str
    order_id: str | None = None
    succeeded: bool = False
    filled_size: float = 0.0
    filled_price: float | None = None
    detail: str = ""


@dataclass
class ArbExecutionResult:
    outcome: ArbOutcome
    requested_size: float
    buy_result: OrderResult | None = None
    sell_result: OrderResult | None = None
    matched_qty: float = 0.0
    imbalance_qty: float = 0.0
    flatten_attempts: list[FlattenAttempt] = field(default_factory=list)
    latency_ms: int = 0
    detail: str = ""

    @property
    def requires_alert(self):
        return self.outcome in {
            ArbOutcome.UNBALANCED_FLATTENED,
            ArbOutcome.FLATTEN_FAILED,
            ArbOutcome.SUBMISSION_ERROR,
        }


def _filled_size(result):
    if result is None or result.filled_size is None:
        return 0.0
    try:
        return max(0.0, float(result.filled_size))
    except (TypeError, ValueError):
        return 0.0


def _valid_price(price):
    try:
        return 0.0 < float(price) < 1.0
    except (TypeError, ValueError):
        return False


def compute_worst_case_exposure(buy_leg, sell_leg):
    if (
        buy_leg.size <= 0
        or sell_leg.size <= 0
        or not _valid_price(buy_leg.limit_price)
        or not _valid_price(sell_leg.limit_price)
    ):
        return None
    return max(
        float(buy_leg.size) * float(buy_leg.limit_price),
        float(sell_leg.size) * float(sell_leg.limit_price),
    )


class ArbExecutionEngine:
    def __init__(
        self,
        *,
        max_unhedged_exposure_usd,
        flatten_order_type="LIMIT",
        fill_tolerance=1e-9,
        on_halt: Callable[[str], Awaitable[None]] | None = None,
    ):
        if max_unhedged_exposure_usd <= 0:
            raise ValueError("max_unhedged_exposure_usd must be > 0")
        if flatten_order_type.upper() != "LIMIT":
            raise ValueError("Phase1 flatten must use aggressive LIMIT orders")
        self.max_unhedged_exposure_usd = max_unhedged_exposure_usd
        self.flatten_order_type = "LIMIT"
        self.fill_tolerance = fill_tolerance
        self._on_halt = on_halt

    async def execute(
        self, *, buy_client, sell_client, buy_leg, sell_leg, signal_id, strategy
    ):
        started = time.monotonic()
        exposure = compute_worst_case_exposure(buy_leg, sell_leg)
        if exposure is None:
            return self._result(
                ArbOutcome.REJECTED_PRE_TRADE,
                buy_leg.size,
                started,
                "invalid size or limit price",
            )
        if exposure > self.max_unhedged_exposure_usd:
            return self._result(
                ArbOutcome.REJECTED_PRE_TRADE,
                buy_leg.size,
                started,
                (
                    f"worst-case exposure ${exposure:.4f} exceeds unhedged cap "
                    f"${self.max_unhedged_exposure_usd:.4f}"
                ),
            )
        try:
            buy_result, sell_result = await asyncio.gather(
                buy_client.submit_order(
                    buy_leg, signal_id=signal_id, strategy=strategy
                ),
                sell_client.submit_order(
                    sell_leg, signal_id=signal_id, strategy=strategy
                ),
                return_exceptions=False,
            )
        except Exception as exc:  # noqa: BLE001 - unknown exchange state must halt
            await self._halt(
                f"arb leg submission exception; exchange state unknown: {exc}"
            )
            return self._result(
                ArbOutcome.SUBMISSION_ERROR,
                buy_leg.size,
                started,
                f"submission exception: {exc}",
            )
        buy_filled, sell_filled = _filled_size(buy_result), _filled_size(sell_result)
        matched = min(buy_filled, sell_filled)
        imbalance = abs(buy_filled - sell_filled)
        await self._cancel_remainder(buy_client, buy_result)
        await self._cancel_remainder(sell_client, sell_result)
        if matched <= self.fill_tolerance and imbalance <= self.fill_tolerance:
            return self._result(
                ArbOutcome.NO_FILL,
                buy_leg.size,
                started,
                "neither leg filled",
                buy_result,
                sell_result,
            )
        if imbalance <= self.fill_tolerance:
            return self._result(
                ArbOutcome.BOTH_FILLED,
                buy_leg.size,
                started,
                f"matched quantity={matched:.8f}",
                buy_result,
                sell_result,
                matched,
                0.0,
            )
        if buy_filled > sell_filled:
            excess = buy_filled - sell_filled
            flatten = await self._flatten(
                buy_client, buy_leg, excess, signal_id, strategy
            )
        else:
            excess = sell_filled - buy_filled
            flatten = await self._flatten(
                sell_client, sell_leg, excess, signal_id, strategy
            )
        attempts = [flatten]
        if flatten.succeeded:
            await self._halt(
                f"arb imbalance flattened; halt until reconciliation: qty={excess:.8f}"
            )
            return self._result(
                ArbOutcome.UNBALANCED_FLATTENED,
                buy_leg.size,
                started,
                f"imbalance={excess:.8f} flattened; trading halted",
                buy_result,
                sell_result,
                matched,
                imbalance,
                attempts,
            )
        await self._halt(
            f"arb flatten failed; residual exposure may remain: qty={excess:.8f}"
        )
        return self._result(
            ArbOutcome.FLATTEN_FAILED,
            buy_leg.size,
            started,
            f"flatten failed; residual quantity={excess:.8f}",
            buy_result,
            sell_result,
            matched,
            imbalance,
            attempts,
        )

    async def _cancel_remainder(self, client, result):
        if result is None or result.order_id.startswith("FAILED-"):
            return
        if result.status in {"pending", "partially_filled"}:
            try:
                if not await client.cancel_order(result.order_id):
                    logger.error(
                        "Failed to cancel resting arb order %s", result.order_id
                    )
            except Exception:  # noqa: BLE001 - cancellation failure is safety-critical
                logger.exception(
                    "Exception cancelling resting arb order %s", result.order_id
                )

    async def _flatten(self, client, original_leg, excess_qty, signal_id, strategy):
        from execution.enums import Side

        original_side = original_leg.side
        flatten_side = Side.SELL if original_side is Side.BUY else Side.BUY
        flatten_leg = OrderLeg(
            market_id=original_leg.market_id,
            platform=original_leg.platform,
            side=flatten_side,
            size=excess_qty,
            limit_price=0.01 if flatten_side is Side.SELL else 0.99,
            order_type="LIMIT",
        )
        try:
            result = await client.submit_order(
                flatten_leg, signal_id=signal_id, strategy=strategy
            )
        except Exception as exc:  # noqa: BLE001 - flatten state is uncertain
            return FlattenAttempt(
                original_leg.platform,
                original_leg.market_id,
                excess_qty,
                original_side.value,
                flatten_side.value,
                detail=f"flatten exception: {exc}",
            )
        filled = _filled_size(result)
        ok = filled + self.fill_tolerance >= excess_qty
        return FlattenAttempt(
            original_leg.platform,
            original_leg.market_id,
            excess_qty,
            original_side.value,
            flatten_side.value,
            result.order_id,
            ok,
            filled,
            result.filled_price,
            "fully flattened" if ok else f"partial/failed flatten: filled={filled:.8f}",
        )

    async def _halt(self, reason):
        if self._on_halt:
            try:
                await self._on_halt(reason)
            except Exception:  # noqa: BLE001 - halt callback must not crash executor
                logger.exception("Execution halt callback failed: %s", reason)

    def _result(
        self,
        outcome,
        requested_size,
        started,
        detail,
        buy_result=None,
        sell_result=None,
        matched_qty=0.0,
        imbalance_qty=0.0,
        flatten_attempts=None,
    ):
        return ArbExecutionResult(
            outcome,
            requested_size,
            buy_result,
            sell_result,
            matched_qty,
            imbalance_qty,
            flatten_attempts or [],
            int((time.monotonic() - started) * 1000),
            detail,
        )
