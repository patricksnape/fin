"""Things that lose value, like cars: price quotes made from how old they are.

A commodity whose `commodity` directive carries a `depreciation` yearly rate (declining balance,
0.12 = loses 12% of its remaining value a year) is priced at each month-end from its purchase cost
and date, down to an optional `depreciation-floor` share of the cost (default 10%). The quotes are
made when the ledger is loaded and never written to the files, so changing the rate changes the
whole history. A commodity with a quote of your own (an appraisal) is left alone.
"""

import calendar
from datetime import date
from decimal import Decimal

from beancount.core import data
from beancount.core.amount import Amount

FLOOR = Decimal("0.1")
CENT = Decimal("0.01")


def _purchase(entries: list, commodity: str) -> tuple[date, Decimal, str] | None:
    """When the first lot was bought, and its cost per unit."""
    for e in entries:  # sorted by date
        if isinstance(e, data.Transaction):
            for p in e.postings:
                if p.units.currency == commodity and p.cost and p.units.number > 0:
                    return e.date, p.cost.number, p.cost.currency
    return None


def _month_ends(first: date, last: date) -> list[date]:
    out, y, m = [], first.year, first.month
    while True:
        d = date(y, m, calendar.monthrange(y, m)[1])
        if d >= last:
            return [*out, last]
        if d > first:
            out.append(d)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def with_prices(entries: list) -> list:
    rates = {
        c.currency: c.meta
        for c in entries
        if isinstance(c, data.Commodity) and c.meta and "depreciation" in c.meta
    }
    txn_dates = [e.date for e in entries if isinstance(e, data.Transaction)]
    if not rates or not txn_dates:
        return entries
    last = max(txn_dates)
    quoted = {e.currency for e in entries if isinstance(e, data.Price)}
    made = []
    for commodity, meta in rates.items():
        bought = _purchase(entries, commodity)
        if commodity in quoted or bought is None:
            continue
        day, cost, quote = bought
        rate = Decimal(str(meta["depreciation"]))
        floor = cost * Decimal(str(meta.get("depreciation-floor", FLOOR)))
        for d in [day, *_month_ends(day, last)]:
            years = (d - day).days / 365.25
            value = max(cost * Decimal(str((1 - float(rate)) ** years)), floor)
            made.append(
                data.Price(
                    {"filename": "<depreciation>", "lineno": 0},
                    d,
                    commodity,
                    Amount(value.quantize(CENT), quote),
                )
            )
    return sorted([*entries, *made], key=data.entry_sortkey) if made else entries
