from core.engine.arb_profitability import calculate_executable_arb


def test_profitable_edge_above_floor_is_allowed(monkeypatch):
    monkeypatch.setenv("PHASE1_MIN_NET_PROFIT", "0.50")
    result = calculate_executable_arb(
        buy_price=0.40,
        sell_price=0.50,
        quantity=10,
        buy_fee_rate=0.0,
        sell_fee_rate=0.0,
    )
    assert result is not None
    assert result.profitable
    assert result.net_profit == 1.0


def test_positive_edge_below_floor_is_rejected(monkeypatch):
    monkeypatch.setenv("PHASE1_MIN_NET_PROFIT", "0.50")
    result = calculate_executable_arb(
        buy_price=0.40,
        sell_price=0.405,
        quantity=10,
        buy_fee_rate=0.0,
        sell_fee_rate=0.0,
    )
    assert result is not None
    assert result.net_profit == 0.05
    assert not result.profitable


def test_explicit_floor_overrides_environment(monkeypatch):
    monkeypatch.setenv("PHASE1_MIN_NET_PROFIT", "0.50")
    result = calculate_executable_arb(
        buy_price=0.40,
        sell_price=0.405,
        quantity=10,
        buy_fee_rate=0.0,
        sell_fee_rate=0.0,
        min_net_profit=0.05,
    )
    assert result is not None
    assert result.profitable


def test_fees_can_turn_gross_edge_negative(monkeypatch):
    monkeypatch.setenv("PHASE1_MIN_NET_PROFIT", "0.50")
    result = calculate_executable_arb(
        buy_price=0.49,
        sell_price=0.51,
        quantity=100,
        buy_fee_rate=0.07,
        sell_fee_rate=0.07,
    )
    assert result is not None
    assert not result.profitable


def test_slippage_is_included(monkeypatch):
    monkeypatch.setenv("PHASE1_MIN_NET_PROFIT", "0.50")
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


def test_fee_exponent_changes_price_curve_not_rounding_precision():
    linear = calculate_executable_arb(
        buy_price=0.25,
        sell_price=0.50,
        quantity=10,
        buy_fee_rate=0.10,
        sell_fee_rate=0.0,
        buy_fee_exponent=1.0,
        buy_fee_decimals=4,
        min_net_profit=0.0,
    )
    squared = calculate_executable_arb(
        buy_price=0.25,
        sell_price=0.50,
        quantity=10,
        buy_fee_rate=0.10,
        sell_fee_rate=0.0,
        buy_fee_exponent=2.0,
        buy_fee_decimals=4,
        min_net_profit=0.0,
    )
    assert linear is not None and squared is not None
    assert linear.buy_fee > squared.buy_fee
    assert linear.buy_fee == 0.1875
    assert squared.buy_fee == 0.0352
