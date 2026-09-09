from core.engine.arb_profitability import calculate_executable_arb


def test_profitable_small_edge_is_allowed():
    result = calculate_executable_arb(
        buy_price=0.40,
        sell_price=0.50,
        quantity=1,
        buy_fee_rate=0.0,
        sell_fee_rate=0.0,
    )
    assert result is not None
    assert result.profitable
    assert result.net_profit > 0


def test_fees_can_turn_gross_edge_negative():
    result = calculate_executable_arb(
        buy_price=0.49,
        sell_price=0.51,
        quantity=100,
        buy_fee_rate=0.07,
        sell_fee_rate=0.07,
    )
    assert result is not None
    assert not result.profitable


def test_slippage_is_included():
    no_slip = calculate_executable_arb(
        buy_price=0.40,
        sell_price=0.45,
        quantity=10,
        buy_fee_rate=0.0,
        sell_fee_rate=0.0,
        slippage_bps=0,
    )
    slip = calculate_executable_arb(
        buy_price=0.40,
        sell_price=0.45,
        quantity=10,
        buy_fee_rate=0.0,
        sell_fee_rate=0.0,
        slippage_bps=100,
    )
    assert no_slip and slip
    assert slip.net_profit < no_slip.net_profit
