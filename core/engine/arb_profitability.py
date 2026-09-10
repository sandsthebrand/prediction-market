"""Executable profitability calculations for Phase 1 arbitrage.

There is intentionally no fixed dollar-profit floor. The gate asks whether an
opportunity is positive after executable price, fee curve, slippage and other
known costs. A small positive opportunity can therefore trade if execution
risk is acceptable.
"""

from __future__ import annotations

import math
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


def _quadratic_fee(
    quantity: float,
    price: float,
    rate: float,
    fee_exponent: float = 1.0,
    rounding_decimals: int = 4,
) -> float:
    """Calculate a prediction-market fee conservatively.

    Polymarket V2 exposes ``fd.e`` as the exponent applied to the price term
    ``price * (1 - price)``. It is not a decimal-place rounding exponent.
    Fees are rounded upward to the configured precision so pre-trade P&L does
    not overstate the opportunity.
    """
    if fee_exponent < 0 or fee_exponent > 8:
        raise ValueError("fee_exponent must be between 0 and 8")
    if rounding_decimals < 0 or rounding_decimals > 8:
        raise ValueError("rounding_decimals must be between 0 and 8")
    raw = quantity * rate * (price * (1.0 - price)) ** fee_exponent
    scale = 10**rounding_decimals
    return math.ceil(raw * scale) / scale


def calculate_executable_arb(
    *,
    buy_price: float,
    sell_price: float,
    quantity: float,
    buy_fee_rate: float,
    sell_fee_rate: float,
    slippage_bps: float = 0.0,
    extra_cost: float = 0.0,
    buy_fee_exponent: float = 1.0,
    sell_fee_exponent: float = 1.0,
    buy_fee_decimals: int = 4,
    sell_fee_decimals: int = 4,
) -> ExecutableArb | None:
    """Calculate conservative pre-trade net profit for a matched pair."""
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
    buy_fee = _quadratic_fee(
        quantity,
        effective_buy,
        buy_fee_rate,
        buy_fee_exponent,
        buy_fee_decimals,
    )
    sell_fee = _quadratic_fee(
        quantity,
        effective_sell,
        sell_fee_rate,
        sell_fee_exponent,
        sell_fee_decimals,
    )
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
