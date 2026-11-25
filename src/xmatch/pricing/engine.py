"""Arbitrage math (PRD section 8). Pure functions: books in, quotes out. No I/O.

A hedge buys one leg on each platform whose payouts sum to exactly $1 in every outcome when the pair is truly
equivalent:
  direction 1 = YES on Kalshi + NO on Polymarket
  direction 2 = YES on Polymarket + NO on Kalshi
Prices are asks, because asks are what a buyer pays. Size comes from walking both ask ladders together. A contract
is added only while its own marginal all-in cost (both asks plus both fees) still meets the minimum margin, and
the budget allows it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, ROUND_FLOOR, Decimal

ONE = Decimal(1)
Level = tuple[Decimal, Decimal]  # (price in dollars, size in contracts)


@dataclass(frozen=True)
class Books:
    """Ask ladders for both sides of one market, best (lowest) price first."""
    yes_asks: list[Level]
    no_asks: list[Level]


def sort_asks(levels: list[Level]) -> list[Level]:
    return sorted(((p, s) for p, s in levels if s > 0 and Decimal(0) < p < ONE), key=lambda x: x[0])


def kalshi_books(yes_bids: list[Level], no_bids: list[Level]) -> Books:
    """Kalshi returns bids only. A NO bid at x is a YES ask at 1-x, and vice versa (Kalshi orderbook docs)."""
    return Books(yes_asks=sort_asks([(ONE - p, s) for p, s in no_bids]),
                 no_asks=sort_asks([(ONE - p, s) for p, s in yes_bids]))


def polymarket_books(yes_token_asks: list[Level], no_token_asks: list[Level]) -> Books:
    """Polymarket has a book per outcome token. Index 0 = YES, index 1 = NO (market-details docs)."""
    return Books(yes_asks=sort_asks(yes_token_asks), no_asks=sort_asks(no_token_asks))


@dataclass(frozen=True)
class FeeModel:
    """Taker fee per contract = rate x p x (1 - p) on both platforms (Polymarket fees docs; Kalshi series fee_type
    'quadratic*' with fee_multiplier). `rate` None means the rate is unverified."""
    rate: Decimal | None
    round_to: Decimal          # Kalshi: up to $0.000001 (fee_rounding docs); Polymarket: 5 dp
    round_up: bool

    def fee(self, contracts: Decimal, price: Decimal) -> Decimal:
        if not self.rate:
            return Decimal(0)
        raw = contracts * self.rate * price * (ONE - price)
        return raw.quantize(self.round_to, rounding=ROUND_CEILING if self.round_up else ROUND_HALF_UP)


@dataclass
class Quote:
    direction: int
    k_ask: Decimal | None
    p_ask: Decimal | None
    top_cost: Decimal | None     # all-in cost of one contract at top of book
    contracts: Decimal
    k_cost: Decimal
    p_cost: Decimal
    fees: Decimal
    k_levels: int
    p_levels: int

    @property
    def cost(self) -> Decimal:
        return self.k_cost + self.p_cost + self.fees

    @property
    def net_margin(self) -> Decimal | None:
        """Return per dollar staked after fees: (payout - cost) / cost, where payout = $1 x contracts."""
        if self.contracts <= 0 or self.cost <= 0:
            if self.top_cost:
                return (ONE - self.top_cost) / self.top_cost
            return None
        return (self.contracts - self.cost) / self.cost


def price_direction(direction: int, kalshi: Books, poly: Books, k_fee: FeeModel, p_fee: FeeModel,
                    min_net_margin: Decimal, budget_usd: Decimal) -> Quote:
    k_leg = kalshi.yes_asks if direction == 1 else kalshi.no_asks
    p_leg = poly.no_asks if direction == 1 else poly.yes_asks
    if not k_leg or not p_leg:
        return Quote(direction, k_leg[0][0] if k_leg else None, p_leg[0][0] if p_leg else None, None,
                     Decimal(0), Decimal(0), Decimal(0), Decimal(0), 0, 0)
    one = Decimal(1)
    top_cost = k_leg[0][0] + p_leg[0][0] + k_fee.fee(one, k_leg[0][0]) + p_fee.fee(one, p_leg[0][0])
    max_unit_cost = ONE / (ONE + min_net_margin)   # margin >= m  <=>  cost per contract <= 1 / (1 + m)
    i = j = 0
    k_left, p_left = k_leg[0][1], p_leg[0][1]
    contracts = k_cost = p_cost = fees = Decimal(0)
    used_k = used_p = 0
    while i < len(k_leg) and j < len(p_leg):
        if k_left < 1:   # fractional crumb left at this level: whole contracts only, move to the next level
            i += 1
            k_left = k_leg[i][1] if i < len(k_leg) else Decimal(0)
            continue
        if p_left < 1:
            j += 1
            p_left = p_leg[j][1] if j < len(p_leg) else Decimal(0)
            continue
        kp, pp = k_leg[i][0], p_leg[j][0]
        unit = kp + pp + k_fee.fee(one, kp) + p_fee.fee(one, pp)
        if unit > max_unit_cost:
            break
        room = ((budget_usd - (k_cost + p_cost + fees)) / unit).to_integral_value(ROUND_FLOOR)
        q = min(k_left, p_left, room).to_integral_value(ROUND_FLOOR)
        if q <= 0:
            break
        contracts += q
        k_cost += q * kp
        p_cost += q * pp
        fees += k_fee.fee(q, kp) + p_fee.fee(q, pp)
        used_k, used_p = i + 1, j + 1
        k_left -= q
        p_left -= q
        if k_left <= 0:
            i += 1
            k_left = k_leg[i][1] if i < len(k_leg) else Decimal(0)
        if p_left <= 0:
            j += 1
            p_left = p_leg[j][1] if j < len(p_leg) else Decimal(0)
        if q < 1 or room <= q:
            break
    return Quote(direction, k_leg[0][0], p_leg[0][0], top_cost, contracts, k_cost, p_cost, fees, used_k, used_p)
