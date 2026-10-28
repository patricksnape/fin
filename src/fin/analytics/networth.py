"""Net worth over time by group, valued at market in the base currency."""

from datetime import date
from decimal import Decimal

import pandas as pd

from fin import ledger, registry
from fin.analytics.valuation import Market, balances_at, checkpoints
from fin.ledger import Ledger

GROUPS = registry.GROUPS


def valued_balances(inv: dict, market: Market, d: date, reg: registry.Registry) -> list[dict]:
    rows = []
    for account, positions in inv.items():
        if not account.startswith(("Assets:", "Liabilities:")):
            continue
        for pos in positions:
            rows.append(
                {
                    "account": account,
                    "group": reg.group(account),
                    "commodity": pos.units.currency,
                    "units": pos.units.number,
                    "cost": pos.cost,
                    "value": market.value(pos, d),
                }
            )
    return rows


def net_worth(led: Ledger, base: str = "CHF", as_of: date | None = None) -> pd.DataFrame:
    """Long rows: date, group, value. Liabilities are negative; the series ends at as_of."""
    as_of = as_of or ledger.valuation_date(led)
    dates = checkpoints(led.entries, as_of)
    market = Market.of(led.entries, base)
    reg = registry.of(led)
    out = []
    for d, inv in balances_at(led.entries, dates).items():
        totals = dict.fromkeys(GROUPS, Decimal(0))
        for r in valued_balances(inv, market, d, reg):
            totals[r["group"]] += r["value"]
        out += [
            {"date": d, "group": g, "value": v.quantize(Decimal("0.01"))} for g, v in totals.items()
        ]
    return pd.DataFrame(out, columns=["date", "group", "value"])


def total_by_date(df: pd.DataFrame) -> pd.DataFrame:
    return df.groupby("date", as_index=False)["value"].sum()
