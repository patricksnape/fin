"""Daily exchange rates into the base currency, so money held or spent in another currency can be
valued (analytics raise rather than guess when a rate is missing).

Rates are the European Central Bank's reference rates, one per business day, fetched from the
public Frankfurter service. The request names only currencies and dates. They are appended to
`ledger/prices.beancount` as `price` directives; the analytics use the latest rate on or before
each day, so weekends and holidays fall back to the previous business day.
"""

import json
import re
import urllib.request
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from beancount.core import data

from fin import config, gitops, ledger

URL = "https://api.frankfurter.dev/v1/{start}..{end}?base={ccy}&symbols={base}"
LEAD = timedelta(days=7)  # rates from a little before the first use, so day one has one too
# the currencies the ECB publishes reference rates for; anything else (a share, an ETF) is priced
# by its own source, e.g. the broker's report
ECB = {
    "AUD",
    "BGN",
    "BRL",
    "CAD",
    "CHF",
    "CNY",
    "CZK",
    "DKK",
    "EUR",
    "GBP",
    "HKD",
    "HUF",
    "IDR",
    "ILS",
    "INR",
    "ISK",
    "JPY",
    "KRW",
    "MXN",
    "MYR",
    "NOK",
    "NZD",
    "PHP",
    "PLN",
    "RON",
    "SEK",
    "SGD",
    "THB",
    "TRY",
    "USD",
    "ZAR",
}
_PRICE = re.compile(r"^(\d{4}-\d\d-\d\d) price (\S+)\s+\S+ (\S+)")


def needed(entries, base: str) -> dict[str, date]:
    """Each foreign currency (one the ECB publishes) the ledger uses, with its first day."""
    first: dict[str, date] = {}
    for e in entries:
        if not isinstance(e, data.Transaction):
            continue
        for p in e.postings:
            for amount in (p.units, p.price):
                if amount and amount.currency != base and amount.currency in ECB:
                    first[amount.currency] = min(first.get(amount.currency, e.date), e.date)
    return first


def _have(path: Path, base: str) -> dict[str, tuple[date, date]]:
    """The first and last day already recorded for each currency."""
    span: dict[str, tuple[date, date]] = {}
    for line in path.read_text().splitlines() if path.exists() else []:
        m = _PRICE.match(line)
        if m and m.group(3) == base:
            d, ccy = date.fromisoformat(m.group(1)), m.group(2)
            lo, hi = span.get(ccy, (d, d))
            span[ccy] = (min(lo, d), max(hi, d))
    return span


def _missing(first: date, have: tuple[date, date] | None, today: date) -> list[tuple[date, date]]:
    """The date ranges still to fetch: before what is recorded (a source with older history was
    added) and after it."""
    want = first - LEAD
    if have is None:
        return [(want, today)] if want <= today else []
    out = []
    if first < have[0]:  # the first use has no rate on or before it
        out.append((want, have[0] - timedelta(days=1)))
    if have[1] < today:
        out.append((have[1] + timedelta(days=1), today))
    return out


def fetch_rates(ccy: str, base: str, start: date, end: date) -> dict[date, Decimal]:
    url = URL.format(start=start, end=end, ccy=ccy, base=base)
    with urllib.request.urlopen(url, timeout=30) as r:
        body = json.loads(r.read(), parse_float=Decimal)
    return {date.fromisoformat(d): v[base] for d, v in body.get("rates", {}).items() if base in v}


def update(root: Path, today: date | None = None, fetch=None) -> int:
    """Add the rates still missing up to `today` (the file stays sorted); returns how many."""
    today, fetch = today or date.today(), fetch or fetch_rates
    path = config.main_ledger(root).parent / "prices.beancount"
    with gitops.write_lock(root):
        led = ledger.load(config.main_ledger(root))
        base = config.load_settings(root, led).base_currency
        have = _have(path, base)
        lines = []
        for ccy, first in sorted(needed(led.entries, base).items()):
            for start, end in _missing(first, have.get(ccy), today):
                rates = fetch(ccy, base, start, end)
                lines += [
                    f"{d} price {ccy}  {r} {base}" for d, r in rates.items() if start <= d <= end
                ]
        if not lines:
            return 0
        before = path.read_text() if path.exists() else ""
        old = before.splitlines()
        notes = [ln for ln in old if not _PRICE.match(ln)]
        path.write_text(
            "\n".join([*notes, *sorted([ln for ln in old if _PRICE.match(ln)] + lines)]) + "\n"
        )
        errors = ledger.load(config.main_ledger(root)).errors
        if errors:
            path.write_text(before)
            raise ValueError("; ".join(ledger.format_errors(errors)[:3]))
        gitops.commit_all(root, f"prices: {len(lines)} exchange rate(s)")
    return len(lines)
