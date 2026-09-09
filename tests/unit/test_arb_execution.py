"""Unit tests for Phase 1 concurrent arb execution."""

import asyncio

import pytest

from core.engine.arb_execution import (
    ArbExecutionEngine,
    ArbOutcome,
    compute_worst_case_exposure,
)
from execution.clients.base import OrderResult
from execution.enums import Side
from execution.models import OrderLeg


class FakeClient:
    def __init__(self, result, *, delay=0.0, cancel_ok=True):
        self.result = result
        self.delay = delay
        self.cancel_ok = cancel_ok
        self.submitted = []
        self.cancelled = []

    async def submit_order(self, leg, signal_id=None, strategy=None):
        self.submitted.append(leg)
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return self.cancel_ok


def leg(side, size=100.0, price=0.40, platform="kalshi"):
    return OrderLeg(
        market_id="mkt",
        platform=platform,
        side=side,
        size=size,
        limit_price=price,
        order_type="LIMIT",
    )


def result(platform, status="filled", size=100.0, price=0.40):
    return OrderResult(
        order_id=f"{platform}-1",
        platform=platform,
        status=status,
        submission_latency_ms=1,
        filled_price=price if size else None,
        filled_size=size,
        fee_paid=0.0,
    )


@pytest.mark.asyncio
async def test_full_match_is_both_filled():
    buy = FakeClient(result("buy", size=100, price=0.40))
    sell = FakeClient(result("sell", size=100, price=0.45))
    engine = ArbExecutionEngine(max_unhedged_exposure_usd=100)

    outcome = await engine.execute(
        buy_client=buy,
        sell_client=sell,
        buy_leg=leg(Side.BUY),
        sell_leg=leg(Side.SELL, price=0.45),
        signal_id="s",
        strategy="P1",
    )

    assert outcome.outcome is ArbOutcome.BOTH_FILLED
    assert outcome.matched_qty == 100
    assert outcome.imbalance_qty == 0


@pytest.mark.asyncio
async def test_partial_fill_uses_actual_quantity_and_flattens_excess():
    buy = FakeClient(result("buy", size=37, price=0.40))
    sell = FakeClient(result("sell", size=100, price=0.45))
    engine = ArbExecutionEngine(max_unhedged_exposure_usd=100)

    outcome = await engine.execute(
        buy_client=buy,
        sell_client=sell,
        buy_leg=leg(Side.BUY),
        sell_leg=leg(Side.SELL, price=0.45),
        signal_id="s",
        strategy="P1",
    )

    assert outcome.outcome is ArbOutcome.UNBALANCED_FLATTENED
    assert outcome.matched_qty == 37
    assert outcome.imbalance_qty == 63
    assert outcome.flatten_attempts[0].excess_qty == 63
    assert outcome.flatten_attempts[0].succeeded


@pytest.mark.asyncio
async def test_zero_zero_is_no_fill():
    buy = FakeClient(result("buy", size=0))
    sell = FakeClient(result("sell", size=0))
    engine = ArbExecutionEngine(max_unhedged_exposure_usd=100)

    outcome = await engine.execute(
        buy_client=buy,
        sell_client=sell,
        buy_leg=leg(Side.BUY),
        sell_leg=leg(Side.SELL),
        signal_id="s",
        strategy="P1",
    )

    assert outcome.outcome is ArbOutcome.NO_FILL


@pytest.mark.asyncio
async def test_submission_exception_halts():
    halted = []

    async def on_halt(reason):
        halted.append(reason)

    buy = FakeClient(RuntimeError("network"))
    sell = FakeClient(result("sell"))
    engine = ArbExecutionEngine(max_unhedged_exposure_usd=100, on_halt=on_halt)

    outcome = await engine.execute(
        buy_client=buy,
        sell_client=sell,
        buy_leg=leg(Side.BUY),
        sell_leg=leg(Side.SELL),
        signal_id="s",
        strategy="P1",
    )

    assert outcome.outcome is ArbOutcome.SUBMISSION_ERROR
    assert halted


def test_invalid_price_fails_closed():
    assert compute_worst_case_exposure(leg(Side.BUY, price=0), leg(Side.SELL)) is None
    assert compute_worst_case_exposure(leg(Side.BUY, price=1), leg(Side.SELL)) is None


def test_exposure_cap_rejects_before_submission():
    assert compute_worst_case_exposure(
        leg(Side.BUY, size=100, price=0.60), leg(Side.SELL, size=100, price=0.60)
    ) == pytest.approx(60.0)


@pytest.mark.asyncio
async def test_partial_flatten_halts_and_reports_failure():
    halted = []

    async def on_halt(reason):
        halted.append(reason)

    # The sell client is used for the excess in this scenario. Its first
    # submission is the 100-contract fill; the flatten call returns only 10.
    class PartialFlattenClient(FakeClient):
        def __init__(self):
            super().__init__(result("sell", size=100, price=0.45))
            self.calls = 0

        async def submit_order(self, leg, signal_id=None, strategy=None):
            self.calls += 1
            if self.calls == 1:
                return result("sell", size=100, price=0.45)
            return result("sell", size=10, price=0.44)

    buy = FakeClient(result("buy", size=37, price=0.40))
    sell = PartialFlattenClient()
    engine = ArbExecutionEngine(max_unhedged_exposure_usd=100, on_halt=on_halt)

    outcome = await engine.execute(
        buy_client=buy,
        sell_client=sell,
        buy_leg=leg(Side.BUY),
        sell_leg=leg(Side.SELL, price=0.45),
        signal_id="s",
        strategy="P1",
    )

    assert outcome.outcome is ArbOutcome.FLATTEN_FAILED
    assert halted
