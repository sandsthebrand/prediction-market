"""Executable profitability calculations for Phase 1 arbitrage.

This module intentionally has no fixed dollar-profit floor. A $0.15 edge can
be worth taking and a $15 edge can be rejected if execution costs/risk make it
negative. The decision is based on executable prices, fees, slippage and size.

Venue fee schedules are configuration inputs. They must be verified against
current venue documentation before live trading; the code never silently
assumes that a hard-coded fee is authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutableArb:
    buy_price: float
    sell_price: float
    quantity: float
    buy_fee: float
    sell_fee: float
    slippage_cost: float
    gross_profit: float
    net_profit: float
    net_edge_per_contract: float
    profitable: bool


def _price_ok(price: float) -> bool:
    return 0.0 < price < 1.0


def calculate_executable_arb(
    *,
    buy_price: float,
    sell_price: float,
    quantity: float,
    buy_fee_rate: float,
    sell_fee_rate: float,
    slippage_bps: float = 0.0,
    extra_cost: float = 0.0,
) -> ExecutableArb | None:
    """Calculate net executable profit for a matched pair.

    Fees are applied to the notional of each leg. Slippage is modeled as an
    adverse movement on both legs from the observed executable prices. This is
    deliberately conservative and is used for pre-trade estimation; realized
    P&L must use actual fill prices and actual fee records.
    """
    if (
        not _price_ok(buy_price)
        or not _price_ok(sell_price)
        or quantity <= 0
        or buy_fee_rate < 0
        or sell_fee_rate < 0
        or slippage_bps < 0
        or extra_cost < 0
    ):
        return None

    slip = slippage_bps / 10000.0
    effective_buy = min(0.999999, buy_price * (1.0 + slip))
    effective_sell = max(0.000001, sell_price * (1.0 - slip))
    gross = (effective_sell - effective_buy) * quantity
    buy_fee = effective_buy * quantity * buy_fee_rate
    sell_fee = effective_sell * quantity * sell_fee_rate
    net = gross - buy_fee - sell_fee - extra_cost

    return ExecutableArb(
        buy_price=buy_price,
        sell_price=sell_price,
        quantity=quantity,
        buy_fee=buy_fee,
        sell_fee=sell_fee,
        slippage_cost=(effective_buy - buy_price) * quantity
        + (sell_price - effective_sell) * quantity,
        gross_profit=(sell_price - buy_price) * quantity,
        net_profit=net,
        net_edge_per_contract=net / quantity,
        profitable=net > 0,
    )
