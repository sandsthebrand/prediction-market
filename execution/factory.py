"""Execution client factory."""
import logging

from execution.clients.base import BaseExecutionClient

logger = logging.getLogger(__name__)


def _make_execution_clients(db, execution_mode: str) -> tuple[BaseExecutionClient, BaseExecutionClient]:
    if execution_mode == "live":
        from execution.clients.kalshi import KalshiExecutionClient
        from execution.clients.polymarket_v2 import PolymarketExecutionClientV2
        poly_client = PolymarketExecutionClientV2(db)
        kalshi_client = KalshiExecutionClient(db)
        logger.info("Execution clients: LIVE (Polymarket CLOB V2 + Kalshi)")
    else:
        from execution.clients.paper import PaperExecutionClient
        poly_client = PaperExecutionClient(db, platform_label="polymarket")
        kalshi_client = PaperExecutionClient(db, platform_label="paper_kalshi")
        label = "SHADOW (paper clients, no real orders)" if execution_mode == "shadow" else "PAPER (simulated)"
        logger.info("Execution clients: %s", label)
    return poly_client, kalshi_client


def _make_single_execution_client(db, execution_mode: str, platform: str):
    if execution_mode == "live":
        if platform == "polymarket":
            from execution.clients.polymarket_v2 import PolymarketExecutionClientV2
            return PolymarketExecutionClientV2(db)
        from execution.clients.kalshi import KalshiExecutionClient
        return KalshiExecutionClient(db)
    from execution.clients.paper import PaperExecutionClient
    return PaperExecutionClient(db, platform_label=f"paper_{platform}")
