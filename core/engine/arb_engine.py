"""Phase 1 hardened arbitrage engine."""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone

from core.engine.arb_engine_legacy import ArbitrageEngine as _LegacyArbitrageEngine
from core.engine.arb_execution import ArbExecutionEngine, ArbOutcome
from core.engine.arb_profitability import calculate_executable_arb
from core.engine.execution_control import halt, is_halted
from core.engine.fire_state import _RiskLeg, _RiskSignal
from execution.enums import Side
from execution.models import OrderLeg

logger = logging.getLogger(__name__)


class ArbitrageEngine(_LegacyArbitrageEngine):
    """Drop-in replacement with hardened Phase 1 cross-platform execution."""

    async def _execute_arb_trade(self, match, p_price, k_price, spread, pair_id):
        from core.signals.risk import get_portfolio_value, run_all_checks

        if await is_halted(self.db) or not (0 < p_price < 1 and 0 < k_price < 1):
            return None
        strategy = "P1_cross_market_arb"
        signal_id = f"sig_{uuid.uuid4().hex[:12]}"
        violation_id = f"viol_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        if p_price < k_price:
            buy_platform, sell_platform = "polymarket", "kalshi"
            buy_id, sell_id = match["poly_id"], match["kalshi_id"]
            buy_price, sell_price = p_price, k_price
            buy_client, sell_client = self._poly_client, self._kalshi_client
        else:
            buy_platform, sell_platform = "kalshi", "polymarket"
            buy_id, sell_id = match["kalshi_id"], match["poly_id"]
            buy_price, sell_price = k_price, p_price
            buy_client, sell_client = self._kalshi_client, self._poly_client
        if self._circuit_breaker is not None and await self._circuit_breaker.should_halt():
            return None
        bankroll = await get_portfolio_value(self.db, self._risk_config.starting_capital)
        max_notional = bankroll * self._risk_config.max_position_pct
        if bankroll <= 0 or max_notional <= 0:
            return None
        size = round(max_notional / (buy_price + sell_price), 1)
        if size <= 0:
            return None
        executable = calculate_executable_arb(
            buy_price=buy_price, sell_price=sell_price, quantity=size,
            buy_fee_rate=self._fee_rate(buy_platform, buy_price),
            sell_fee_rate=self._fee_rate(sell_platform, sell_price),
            slippage_bps=self._risk_config.slippage_bps,
        )
        if executable is None or not executable.profitable or executable.net_edge_per_contract < self._risk_config.min_edge:
            return None
        try:
            await self.db.execute("""INSERT OR IGNORE INTO market_pairs
                (id, market_id_a, market_id_b, pair_type, similarity_score, match_method, active, created_at, updated_at)
                VALUES (?, ?, ?, 'cross_platform', ?, 'inverted_index', 1, ?, ?)""",
                (pair_id, buy_id, sell_id, match.get("similarity", 0.0), now, now))
            await self.db.execute("""INSERT OR IGNORE INTO violations
                (id, pair_id, violation_type, price_a_at_detect, price_b_at_detect, raw_spread, net_spread,
                 fee_estimate_a, fee_estimate_b, status, detected_at, updated_at)
                VALUES (?, ?, 'cross_platform', ?, ?, ?, ?, ?, ?, 'detected', ?, ?)""",
                (violation_id, pair_id, buy_price, sell_price, spread, executable.net_edge_per_contract,
                 executable.buy_fee, executable.sell_fee, now, now))
            await self.db.commit()
        except Exception:
            logger.exception("Phase1 opportunity persistence failed; no order sent")
            return None
        risk_signal = _RiskSignal(
            legs=[_RiskLeg(market_id=buy_id, limit_price=buy_price, size=size, side="BUY"),
                  _RiskLeg(market_id=sell_id, limit_price=sell_price, size=size, side="SELL")],
            edge=executable.net_edge_per_contract, strategy=strategy, violation_id=violation_id)
        all_passed, checks = await run_all_checks(risk_signal, self._risk_config, self.db, portfolio_value=bankroll)
        if not all_passed:
            failed = [r.check_type for r in checks if not r.passed]
            await self.db.execute("UPDATE violations SET status='risk_rejected', rejection_reason=?, updated_at=? WHERE id=?",
                                  (", ".join(failed), now, violation_id))
            await self.db.commit()
            return None
        try:
            await self.db.execute("""INSERT OR IGNORE INTO signals
                (id, violation_id, strategy, signal_type, market_id_a, market_id_b, target_price_a, target_price_b,
                 model_edge, kelly_fraction, position_size_a, position_size_b, total_capital_at_risk, status, fired_at, updated_at)
                VALUES (?, ?, ?, 'arb_pair', ?, ?, ?, ?, ?, 0, ?, ?, ?, 'fired', ?, ?)""",
                (signal_id, violation_id, strategy, buy_id, sell_id, buy_price, sell_price,
                 executable.net_edge_per_contract, size, size, size * (buy_price + sell_price), now, now))
            await self.db.commit()
        except Exception:
            logger.exception("Phase1 signal persistence failed; no order sent")
            return None
        buy_leg = OrderLeg(market_id=buy_id, platform=buy_platform, side=Side.BUY, size=size, limit_price=buy_price, order_type="LIMIT")
        sell_leg = OrderLeg(market_id=sell_id, platform=sell_platform, side=Side.SELL, size=size, limit_price=sell_price, order_type="LIMIT")
        max_unhedged = min(bankroll * self._risk_config.max_position_pct, bankroll * self._risk_config.max_portfolio_exposure_pct)
        execution = await ArbExecutionEngine(max_unhedged_exposure_usd=max_unhedged, on_halt=lambda reason: halt(self.db, reason)).execute(
            buy_client=buy_client, sell_client=sell_client, buy_leg=buy_leg, sell_leg=sell_leg,
            signal_id=signal_id, strategy=strategy)
        if execution.outcome is not ArbOutcome.BOTH_FILLED:
            await self._record_execution_failure(violation_id, execution.outcome.value, execution.detail)
            return None
        buy_result, sell_result = execution.buy_result, execution.sell_result
        if not buy_result or not sell_result or buy_result.filled_price is None or sell_result.filled_price is None:
            return None
        matched_qty = execution.matched_qty
        actual_fees = (buy_result.fee_paid or 0.0) + (sell_result.fee_paid or 0.0)
        actual_pnl = round((sell_result.filled_price - buy_result.filled_price) * matched_qty - actual_fees, 6)
        try:
            pos_id = f"pos_{uuid.uuid4().hex[:12]}"
            await self.db.execute("""INSERT INTO positions
                (id, signal_id, market_id, strategy, side, book, entry_price, entry_size, exit_price, exit_size,
                 realized_pnl, fees_paid, pnl_model, status, opened_at, closed_at, updated_at)
                VALUES (?, ?, ?, ?, 'BUY', 'YES', ?, ?, ?, ?, ?, ?, 'realistic', 'closed', ?, ?, ?)""",
                (pos_id, signal_id, buy_id, strategy, buy_result.filled_price, matched_qty,
                 sell_result.filled_price, matched_qty, actual_pnl, actual_fees, now, now, now))
            await self.db.execute("""INSERT INTO trade_outcomes
                (id, signal_id, strategy, violation_id, market_id_a, market_id_b, predicted_edge, predicted_pnl,
                 actual_pnl, fees_total, edge_captured_pct, signal_to_fill_ms, holding_period_ms, spread_at_signal,
                 resolved_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (f"trade_{uuid.uuid4().hex[:12]}", signal_id, strategy, violation_id, buy_id, sell_id,
                 executable.net_edge_per_contract, executable.net_profit, actual_pnl, actual_fees,
                 (actual_pnl / executable.net_profit) * 100 if executable.net_profit > 0 else 0,
                 execution.latency_ms, execution.latency_ms, spread, now, now))
            await self.db.execute("UPDATE violations SET status='executed', closed_at=?, updated_at=? WHERE id=?", (now, now, violation_id))
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            logger.exception("Phase1 financial truth write failed for signal=%s", signal_id)
            await halt(self.db, f"DB persistence failure after filled arb signal={signal_id}")
            return None
        if self._circuit_breaker is not None:
            await self._circuit_breaker.record_order_result(success=actual_pnl > 0)
        return {"strategy": strategy, "pair_id": pair_id, "spread": spread, "actual_pnl": actual_pnl,
                "fees": actual_fees, "requested_size": size, "matched_size": matched_qty,
                "theoretical_net_profit": executable.net_profit, "execution_latency_ms": execution.latency_ms}

    @staticmethod
    def _fee_rate(platform: str, price: float) -> float:
        return float(os.getenv("POLYMARKET_FEE_RATE" if platform == "polymarket" else "KALSHI_FEE_RATE",
                              "0.05" if platform == "polymarket" else "0.07"))

    async def _record_execution_failure(self, violation_id: str, outcome: str, detail: str) -> None:
        try:
            await self.db.execute("UPDATE violations SET status=?, rejection_reason=?, updated_at=? WHERE id=?",
                                  (outcome, detail[:2000], datetime.now(timezone.utc).isoformat(), violation_id))
            await self.db.commit()
        except Exception:
            logger.exception("Could not record Phase1 execution outcome")
