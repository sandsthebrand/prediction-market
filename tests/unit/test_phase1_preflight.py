"""Focused Phase 1 preflight and market-fee tests."""

from types import SimpleNamespace

import pytest

from execution.clients.kalshi_v2 import KalshiExecutionClientV2
from execution.factory import _make_execution_clients


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self, responses):
        self.responses = responses
        self.paths = []

    async def get(self, url, headers=None):
        path = url.split("/trade-api/v2", 1)[-1]
        self.paths.append(path)
        return FakeResponse(self.responses[path])


@pytest.mark.asyncio
async def test_kalshi_fee_uses_event_override_and_series_multiplier(monkeypatch):
    client = KalshiExecutionClientV2(None, api_key="k", rsa_key_path="")
    client._private_key = object()
    client._fee_cache.clear()
    fake = FakeHttp(
        {
            "/markets/MKT": {"market": {"event_ticker": "EVT"}},
            "/events/EVT": {
                "event": {
                    "series_ticker": "SER",
                    "fee_type_override": "quadratic",
                    "fee_multiplier_override": 0.5,
                }
            },
            "/series/SER": {
                "series": {"fee_type": "quadratic", "fee_multiplier": 1.0}
            },
        }
    )
    client.http_client = fake
    monkeypatch.setenv("KALSHI_QUADRATIC_BASE_RATE", "0.07")

    rate = await client.get_pretrade_fee_rate(SimpleNamespace(market_id="MKT"))

    assert rate == pytest.approx(0.035)
    assert fake.paths == ["/markets/MKT", "/events/EVT", "/series/SER"]


@pytest.mark.asyncio
async def test_kalshi_fee_rejects_flat_schedule(monkeypatch):
    client = KalshiExecutionClientV2(None, api_key="k", rsa_key_path="")
    client._private_key = object()
    client.http_client = FakeHttp(
        {
            "/markets/MKT": {"market": {"event_ticker": "EVT"}},
            "/events/EVT": {"event": {"series_ticker": "SER"}},
            "/series/SER": {
                "series": {"fee_type": "flat", "fee_multiplier": 1.0}
            },
        }
    )

    with pytest.raises(ValueError, match="unsupported Kalshi fee type"):
        await client.get_pretrade_fee_rate(SimpleNamespace(market_id="MKT"))


def test_live_execution_is_blocked_without_verified_preflight(monkeypatch):
    monkeypatch.delenv("PHASE1_FEES_VERIFIED", raising=False)
    monkeypatch.delenv("PHASE1_API_V2_VERIFIED", raising=False)
    with pytest.raises(RuntimeError, match="Live execution blocked"):
        _make_execution_clients(None, "live")
