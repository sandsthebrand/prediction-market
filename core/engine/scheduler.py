"""Phase 1 strategy scheduler.

P2-P5 directional/single-platform strategies are intentionally disabled while
Phase 1 cross-platform arbitrage is being validated. Resolution, mark-to-market
cleanup, reconciliation and invariant checks remain enabled.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from core.engine.scheduler_legacy import ScheduledStrategyRunner as _LegacyScheduledStrategyRunner

logger = logging.getLogger(__name__)


class ScheduledStrategyRunner(_LegacyScheduledStrategyRunner):
    """Run maintenance only; do not activate P2-P5 during Phase 1."""

    async def run_one_cycle(self) -> list:
        if self._circuit_breaker is not None and await self._circuit_breaker.should_halt():
            logger.warning("CIRCUIT_BREAKER halted — skipping scheduled maintenance")
            return []
        try:
            from core.engine.resolution import close_resolved_positions
            await close_resolved_positions(self.db)
        except Exception:
            logger.exception("resolution pass failed")
        try:
            from core.strategies.single_platform import mark_and_close_positions
            await mark_and_close_positions(
                self.db,
                holding_period_s=self._risk_config.strategy_holding_period_s,
                price_cache=self._price_cache,
            )
        except Exception:
            logger.exception("mark-to-market maintenance failed")
        self._cycle_count += 1
        if self._cycle_count % self._reconcile_every == 0:
            try:
                from core.engine.reconciliation import reconcile_internal_state
                await reconcile_internal_state(self.db)
            except Exception:
                logger.exception("reconciliation pass failed")
        try:
            from core.invariants import check_all_invariants
            await check_all_invariants(self.db, mode="warn", alert_manager=self._alert_manager)
        except Exception:
            logger.exception("invariant check failed")
        if os.getenv("PHASE1_ONLY", "true").lower() == "true":
            return []
        # Explicit operator opt-in is required to ever run the legacy P2-P5
        # path after Phase 1 validation.
        return await super().run_one_cycle()

    async def run(self, stop_event: asyncio.Event):
        logger.info("Phase 1 scheduler started: P2-P5 disabled")
        while not stop_event.is_set():
            try:
                t0 = time.time()
                trades = await self.run_one_cycle()
                self.total_trades += len(trades)
                logger.info("Phase 1 maintenance: %d trades in %.1fs", len(trades), time.time() - t0)
            except Exception:
                logger.exception("Phase 1 scheduler cycle failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
