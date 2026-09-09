"""Execution client factory."""

import logging, os
from execution.clients.base import BaseExecutionClient

logger = logging.getLogger(__name__)


def _assert_phase1_live_preflight():
    required = {"PHASE1_FEES_VERIFIED": "true", "PHASE1_API_V2_VERIFIED": "true"}
    missing = [k for k, v in required.items() if os.getenv(k, "").lower() != v]
    if missing:
        raise RuntimeError(
            "Live execution blocked: set verified preflight flags: "
            + ", ".join(missing)
        )


def _make_execution_clients(
    db, execution_mode: str
) -> tuple[BaseExecutionClient, BaseExecutionClient]:
    if execution_mode == "live":
        _assert_phase1_live_preflight()
        from execution.clients.kalshi_v2 import KalshiExecutionClientV2
        from execution.clients.polymarket_v2 import PolymarketExecutionClientV2

        return PolymarketExecutionClientV2(db), KalshiExecutionClientV2(db)
    from execution.clients.paper import PaperExecutionClient

    return PaperExecutionClient(db, platform_label="polymarket"), PaperExecutionClient(
        db, platform_label="paper_kalshi"
    )


def _make_single_execution_client(db, execution_mode: str, platform: str):
    if execution_mode == "live":
        _assert_phase1_live_preflight()
        if platform == "polymarket":
            from execution.clients.polymarket_v2 import PolymarketExecutionClientV2

            return PolymarketExecutionClientV2(db)
        from execution.clients.kalshi_v2 import KalshiExecutionClientV2

        return KalshiExecutionClientV2(db)
    from execution.clients.paper import PaperExecutionClient

    return PaperExecutionClient(db, platform_label=f"paper_{platform}")
