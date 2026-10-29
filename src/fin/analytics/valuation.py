"""Valuation with Beancount's own conversion; we add only what Beancount leaves silent.

Beancount returns the amount unconverted when no price is found. For net worth that would be a
quietly wrong number, so `Market` raises instead, and carries a lot with no price yet at cost.
"""

import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from beancount.core import convert, data, inventory, prices
from beancount.core.amount import Amount
from beancount.core.position import Position

_MARKETS: dict[tuple, tuple[list, "Market"]] = {}


@dataclass(frozen=True)
class Market:
    pmap: dict
    base: str

    @classmethod
    def of(cls, entries: list, base: str = "CHF") -> "Market":
        """Building the price map reads every entry, so a loaded ledger's is built once. The cache
        holds the entries list itself, so its id cannot be reused by another list meanwhile."""
        key = (id(entries), len(entries), base)
        hit = _MARKETS.get(key)
        if hit is None or hit[0] is not entries:
            if len(_MARKETS) >= 4:
                _MARKETS.pop(next(iter(_MARKETS)))
            hit = _MARKETS[key] = (entries, cls(prices.build_price_map(entries), base))
        return hit[1]

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
    """Per-account positions at the end of each date (what Beancount's balance_by_account gives),
    in one pass over the entries: the running balances are read off at each date in turn."""
    txns = sorted((e for e in entries if isinstance(e, data.Transaction)), key=lambda e: e.date)
    running: dict[str, inventory.Inventory] = defaultdict(inventory.Inventory)
    snapshots, i = {}, 0
    for d in sorted(set(dates)):
        while i < len(txns) and txns[i].date <= d:
            for posting in txns[i].postings:
                running[posting.account].add_position(posting)
            i += 1
        snapshots[d] = {account: list(inv.get_positions()) for account, inv in running.items()}
    return {d: snapshots[d] for d in dates}
