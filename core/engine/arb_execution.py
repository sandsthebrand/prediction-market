"""Execution primitives for Phase 1 cross-platform arbitrage.

The arb engine must never infer a hedge from requested size. This module
contains the small, deterministic state machine used by the production arb
path: concurrent leg submission, actual-fill accounting, cancellation of
resting remainder, and immediate flattening of any excess filled quantity.

It deliberately does not contain profitability assumptions. Profitability is
calculated from the executable prices and actual fills by the caller.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable

from execution.clients.base import BaseExecutionClient, OrderResult
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
    def requires_alert(self) -> bool:
        return self.outcome in {
            ArbOutcome.UNBALANCED_FLATTENED,
            ArbOutcome.FLATTEN_FAILED,
            ArbOutcome.SUBMISSION_ERROR,
        }


def _filled_size(result: OrderResult | None) -> float:
    """Return actual filled quantity, never requested quantity."""
    if result is None or result.filled_size is None:
        return 0.0
    try:
        return max(0.0, float(result.filled_size))
    except (TypeError, ValueError):
        return 0.0


def _valid_price(price: float | None) -> bool:
    if price is None:
        return False
    try:
        return 0.0 < float(price) < 1.0
    except (TypeError, ValueError):
        return False


def compute_worst_case_exposure(buy_leg: OrderLeg, sell_leg: OrderLeg) -> float | None:
    """Return the capital needed if both legs fill, or None for invalid input.

    For prediction-market YES contracts, buying costs size*price. A SELL is
    potentially translated to a complementary BUY by the Polymarket resolver,
    so its requested price is also treated as capital at risk rather than
    assuming a naked short is free.
    """
    if buy_leg.size <= 0 or sell_leg.size <= 0:
        return None
    if not _valid_price(buy_leg.limit_price) or not _valid_price(sell_leg.limit_price):
        return None
    return max(
        float(buy_leg.size) * float(buy_leg.limit_price),
        float(sell_leg.size) * float(sell_leg.limit_price),
    )


class ArbExecutionEngine:
    """Execute two arb legs concurrently and fail closed on imbalance."""

    def __init__(
        self,
        *,
        max_unhedged_exposure_usd: float,
        flatten_order_type: str = "MARKET",
        fill_tolerance: float = 1e-9,
        on_halt: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        if max_unhedged_exposure_usd <= 0:
            raise ValueError("max_unhedged_exposure_usd must be > 0")
        if flatten_order_type.upper() not in {"MARKET", "LIMIT"}:
            raise ValueError("flatten_order_type must be MARKET or LIMIT")
        self.max_unhedged_exposure_usd = max_unhedged_exposure_usd
        self.flatten_order_type = flatten_order_type.upper()
        self.fill_tolerance = fill_tolerance
        self._on_halt = on_halt

    async def execute(
        self,
        *,
        buy_client: BaseExecutionClient,
        sell_client: BaseExecutionClient,
        buy_leg: OrderLeg,
        sell_leg: OrderLeg,
        signal_id: str | None,
        strategy: str | None,
    ) -> ArbExecutionResult:
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
                f"worst-case exposure ${exposure:.4f} exceeds "
                f"unhedged cap ${self.max_unhedged_exposure_usd:.4f}",
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
        except Exception as exc:
            # A transport exception does not mean "no fill". The exchange may
            # have accepted the order before the response was lost. Halt and
            # reconcile rather than retrying into an unknown state.
            await self._halt(
                f"arb leg submission exception; exchange state unknown: {exc}"
            )
            return self._result(
                ArbOutcome.SUBMISSION_ERROR,
                buy_leg.size,
                started,
                f"submission exception: {exc}",
            )

        buy_filled = _filled_size(buy_result)
        sell_filled = _filled_size(sell_result)
        matched = min(buy_filled, sell_filled)
        imbalance = abs(buy_filled - sell_filled)

        # Always cancel resting remainder before deciding whether the cycle is
        # balanced. A timeout/pending order must never be left live while the
        # engine proceeds to another opportunity.
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

        flatten_attempts = [flatten]
        if flatten.succeeded:
            await self._halt(
                f"arb imbalance flattened; halt until reconciliation: "
                f"qty={excess:.8f}"
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
                flatten_attempts,
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
            flatten_attempts,
        )

    async def _cancel_remainder(
        self, client: BaseExecutionClient, result: OrderResult | None
    ) -> None:
        if result is None or result.order_id.startswith("FAILED-"):
            return
        if result.status in {"pending", "partially_filled"}:
            try:
                ok = await client.cancel_order(result.order_id)
                if not ok:
                    logger.error("Failed to cancel resting arb order %s", result.order_id)
            except Exception:
                logger.exception("Exception cancelling resting arb order %s", result.order_id)

    async def _flatten(
        self,
        client: BaseExecutionClient,
        original_leg: OrderLeg,
        excess_qty: float,
        signal_id: str | None,
        strategy: str | None,
    ) -> FlattenAttempt:
        from execution.enums import Side

        original_side = original_leg.side
        flatten_side = Side.SELL if original_side is Side.BUY else Side.BUY
        # A MARKET order is preferred. If the venue/client does not support it,
        # callers should fail closed rather than silently resting a limit order.
        flatten_leg = OrderLeg(
            market_id=original_leg.market_id,
            platform=original_leg.platform,
            side=flatten_side,
            size=excess_qty,
            limit_price=None if self.flatten_order_type == "MARKET" else (
                0.01 if flatten_side is Side.SELL else 0.99
            ),
            order_type=self.flatten_order_type,
        )
        try:
            result = await client.submit_order(
                flatten_leg, signal_id=signal_id, strategy=strategy
            )
        except Exception as exc:
            return FlattenAttempt(
                platform=original_leg.platform,
                market_id=original_leg.market_id,
                excess_qty=excess_qty,
                original_side=original_side.value,
                flatten_side=flatten_side.value,
                detail=f"flatten exception: {exc}",
            )

        filled = _filled_size(result)
        succeeded = filled + self.fill_tolerance >= excess_qty
        return FlattenAttempt(
            platform=original_leg.platform,
            market_id=original_leg.market_id,
            excess_qty=excess_qty,
            original_side=original_side.value,
            flatten_side=flatten_side.value,
            order_id=result.order_id,
            succeeded=succeeded,
            filled_size=filled,
            filled_price=result.filled_price,
            detail=(
                "fully flattened"
                if succeeded
                else f"partial/failed flatten: filled={filled:.8f}"
            ),
        )

    async def _halt(self, reason: str) -> None:
        if self._on_halt is not None:
            try:
                await self._on_halt(reason)
            except Exception:
                logger.exception("Execution halt callback failed: %s", reason)

    def _result(
        self,
        outcome: ArbOutcome,
        requested_size: float,
        started: float,
        detail: str,
        buy_result: OrderResult | None = None,
        sell_result: OrderResult | None = None,
        matched_qty: float = 0.0,
        imbalance_qty: float = 0.0,
        flatten_attempts: list[FlattenAttempt] | None = None,
    ) -> ArbExecutionResult:
        return ArbExecutionResult(
            outcome=outcome,
            requested_size=requested_size,
            buy_result=buy_result,
            sell_result=sell_result,
            matched_qty=matched_qty,
            imbalance_qty=imbalance_qty,
            flatten_attempts=flatten_attempts or [],
            latency_ms=int((time.monotonic() - started) * 1000),
            detail=detail,
        )
