"""Execution client factory."""
import logging
from execution.clients.base import BaseExecutionClient

logger = logging.getLogger(__name__)


def _make_execution_clients(db, execution_mode: str) -> tuple[BaseExecutionClient, BaseExecutionClient]:
    if execution_mode == "live":
        from execution.clients.kalshi_v2 import KalshiExecutionClientV2
        from execution.clients.polymarket_v2 import PolymarketExecutionClientV2
        return PolymarketExecutionClientV2(db), KalshiExecutionClientV2(db)
    from execution.clients.paper import PaperExecutionClient
    poly = PaperExecutionClient(db, platform_label="polymarket")
    kalshi = PaperExecutionClient(db, platform_label="paper_kalshi")
    logger.info("Execution clients: %s", "SHADOW" if execution_mode == "shadow" else "PAPER")
    return poly, kalshi


def _make_single_execution_client(db, execution_mode: str, platform: str):
    if execution_mode == "live":
        if platform == "polymarket":
            from execution.clients.polymarket_v2 import PolymarketExecutionClientV2
            return PolymarketExecutionClientV2(db)
        from execution.clients.kalshi_v2 import KalshiExecutionClientV2
        return KalshiExecutionClientV2(db)
    from execution.clients.paper import PaperExecutionClient
    return PaperExecutionClient(db, platform_label=f"paper_{platform}")
