from decimal import Decimal as D

from xmatch.pricing.engine import FeeModel, kalshi_books, polymarket_books, price_direction

NOFEE = FeeModel(None, D("0.000001"), True)
POLY_FEE = FeeModel(D("0.05"), D("0.00001"), False)


def books(k_yes_ask, k_no_ask, p_yes_ask, p_no_ask, size=D(100)):
    # Kalshi gives bids: YES ask x  <=>  NO bid 1-x
    k = kalshi_books(yes_bids=[(1 - k_no_ask, size)], no_bids=[(1 - k_yes_ask, size)])
    p = polymarket_books([(p_yes_ask, size)], [(p_no_ask, size)])
    return k, p


def test_reference_example_from_writeup():
    # Kalshi YES 47c + Polymarket NO 40c = 87c for a $1 payout: 13c gross per contract
    k, p = books(D("0.47"), D("0.55"), D("0.62"), D("0.40"))
    q = price_direction(1, k, p, NOFEE, NOFEE, D("0.02"), D(1000))
    assert q.k_ask == D("0.47") and q.p_ask == D("0.40") and q.top_cost == D("0.87")
    assert q.contracts == 100 and q.cost == D("87.00")
    assert round(q.net_margin, 4) == round(D("13") / D("87"), 4)


def test_no_arb_when_legs_sum_over_a_dollar():
    k, p = books(D("0.55"), D("0.52"), D("0.50"), D("0.52"))   # 1.07 and 1.02
    for d in (1, 2):
        q = price_direction(d, k, p, NOFEE, NOFEE, D("0.0"), D(1000))
        assert q.contracts == 0 and q.net_margin < 0


def test_direction_two_uses_poly_yes_and_kalshi_no():
    k, p = books(D("0.60"), D("0.35"), D("0.50"), D("0.52"))
    q = price_direction(2, k, p, NOFEE, NOFEE, D("0.02"), D(1000))
    assert (q.k_ask, q.p_ask) == (D("0.35"), D("0.50")) and q.contracts == 100


def test_fees_reduce_margin_and_can_kill_the_arb():
    k, p = books(D("0.49"), D("0.55"), D("0.60"), D("0.49"))   # 98c gross
    assert price_direction(1, k, p, NOFEE, NOFEE, D("0.01"), D(1000)).contracts == 100
    q = price_direction(1, k, p, NOFEE, POLY_FEE, D("0.01"), D(1000))  # fee 0.05*.49*.51 = 1.2c
    assert q.contracts == 0 and q.top_cost > D("0.99")


def test_walks_depth_and_stops_when_marginal_contract_fails():
    k = kalshi_books(yes_bids=[], no_bids=[(D("0.60"), D(10)), (D("0.55"), D(50))])  # YES asks 0.40 x10, 0.45 x50
    p = polymarket_books([], [(D("0.50"), D(30)), (D("0.58"), D(100))])                # NO asks 0.50 x30, 0.58 x100
    q = price_direction(1, k, p, NOFEE, NOFEE, D("0.02"), D(10_000))
    # 10 @ 0.40+0.50=0.90, 20 @ 0.45+0.50=0.95; next 0.45+0.58=1.03 fails
    assert q.contracts == 30 and q.cost == D("10") * D("0.90") + D("20") * D("0.95")
    assert (q.k_levels, q.p_levels) == (2, 1)


def test_budget_caps_size():
    k, p = books(D("0.40"), D("0.55"), D("0.60"), D("0.40"), size=D(10_000))
    q = price_direction(1, k, p, NOFEE, NOFEE, D("0.02"), D(80))
    assert q.contracts == 100 and q.cost <= 80


def test_fractional_crumbs_are_skipped_not_fatal():
    k = kalshi_books(yes_bids=[], no_bids=[(D("0.60"), D("0.5")), (D("0.58"), D(20))])
    p = polymarket_books([], [(D("0.40"), D(20))])
    q = price_direction(1, k, p, NOFEE, NOFEE, D("0.0"), D(1000))
    assert q.contracts == 20 and q.k_ask == D("0.40")


def test_empty_book():
    k = kalshi_books([], [])
    p = polymarket_books([(D("0.5"), D(1))], [(D("0.5"), D(1))])
    q = price_direction(1, k, p, NOFEE, NOFEE, D("0.02"), D(100))
    assert q.contracts == 0 and q.top_cost is None and q.net_margin is None


def test_fee_rounding():
    kal = FeeModel(D("0.07"), D("0.000001"), True)
    assert kal.fee(D(1), D("0.5")) == D("0.017500")
    assert kal.fee(D(3), D("0.33")) == D("0.046431")      # exact, nothing to round
    assert kal.fee(D(3), D("0.333")) == D("0.046644")     # 0.04664331 rounded UP
    assert POLY_FEE.fee(D(1), D("0.5")) == D("0.01250")
