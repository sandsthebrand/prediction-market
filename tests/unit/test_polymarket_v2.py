"""Targeted tests for Polymarket V2 fill accounting."""

import asyncio

import pytest

from execution.clients.polymarket_v2 import PolymarketExecutionClientV2
from execution.models import OrderLeg
from execution.enums import Side


class FakeClob:
    def __init__(self, order):
        self.order = order

    def get_order(self, order_id):
        return self.order


def make_leg(size):
    return OrderLeg(
        market_id="poly-market",
        platform="polymarket",
        side=Side.BUY,
        size=size,
        limit_price=0.40,
        order_type="LIMIT",
    )


async def _noop(*args, **kwargs):
    return None


@pytest.mark.asyncio
async def test_confirmed_fill_survives_fee_verification_failure(monkeypatch):
    client = PolymarketExecutionClientV2(None)
    client._client = FakeClob(
        {"status": "MATCHED", "size_matched": "10", "price": "0.41"}
    )
    client.update_order_fill = _noop
    client.write_fill_event = _noop

    async def fee_failure(*args, **kwargs):
        raise RuntimeError("fee endpoint unavailable")

    async def no_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(client, "_estimate_fee", fee_failure)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    result = await client._poll("poly-order-1", make_leg(10), 0.0, max_polls=1)

    assert result.status == "filled"
    assert result.filled_size == 10
    assert result.filled_price == pytest.approx(0.41)
    assert result.fee_paid is None
    assert result.fee_verified is False
    assert "fee unverified" in (result.error_message or "")


@pytest.mark.asyncio
async def test_confirmed_partial_fill_survives_fee_verification_failure(monkeypatch):
    client = PolymarketExecutionClientV2(None)
    client._client = FakeClob(
        {"status": "CANCELLED", "size_matched": "4", "price": "0.37"}
    )
    client.update_order_fill = _noop
    client.write_fill_event = _noop

    async def fee_failure(*args, **kwargs):
        raise RuntimeError("fee metadata temporarily missing")

    async def no_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(client, "_estimate_fee", fee_failure)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    result = await client._poll("poly-order-2", make_leg(10), 0.0, max_polls=1)

    assert result.status == "partially_filled"
    assert result.filled_size == 4
    assert result.fee_paid is None
    assert result.fee_verified is False
    assert result.order_id == "poly-order-2"
