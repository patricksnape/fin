"""Valuation with Beancount's own conversion; we add only what Beancount leaves silent.

Beancount returns the amount unconverted when no price is found. For net worth that would be a
quietly wrong number, so `Market` raises instead, and carries a lot with no price yet at cost.
"""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from beancount.core import convert, data, prices
from beancount.core.amount import Amount
from beancount.core.position import Position
from beancount.ops import summarize


@dataclass(frozen=True)
class Market:
    pmap: dict
    base: str

    @classmethod
    def of(cls, entries: list, base: str = "CHF") -> "Market":
        return cls(prices.build_price_map(entries), base)

    def price(self, ccy: str, quote: str, d: date) -> Decimal | None:
        """Latest price of `ccy` in `quote` on or before `d`."""
        return prices.get_price(self.pmap, (ccy, quote), d)[1]

    def convert(self, amount: Amount, d: date) -> Decimal:
        """An amount in the base currency, at the price on or before `d`."""
        out = convert.convert_amount(amount, self.base, self.pmap, d)
        if out.currency != self.base:
            raise ValueError(f"No {amount.currency}/{self.base} price on or before {d}")
        return out.number

    def value(self, pos: Position, d: date) -> Decimal:
        """Market value of a position in the base currency. A lot held at cost is priced in its
        cost currency (Beancount hops through it); with no price yet it is carried at cost."""
        out = convert.convert_position(pos, self.base, self.pmap, d)
        if out.currency != self.base:
            out = convert.convert_amount(convert.get_cost(pos), self.base, self.pmap, d)
        if out.currency != self.base:
            raise ValueError(f"No {pos.units.currency}/{self.base} price on or before {d}")
        return out.number


def month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def checkpoints(entries: list, as_of: date) -> list[date]:
    """Month-ends from the first transaction's month through as_of (as_of closes the series)."""
    first = min(e.date for e in entries if isinstance(e, data.Transaction))
    out, d = [], month_end(first)
    while d < as_of:
        out.append(d)
        d = month_end(date(d.year + (d.month == 12), d.month % 12 + 1, 1))
    return [*out, as_of]


def balances_at(entries: list, dates: list[date]) -> dict[date, dict[str, list[Position]]]:
    """Per-account positions at the end of each date (Beancount's own balance_by_account)."""
    out = {}
    for d in dates:
        balances, _ = summarize.balance_by_account(entries, d + timedelta(days=1))
        out[d] = {account: list(inv.get_positions()) for account, inv in balances.items()}
    return out
