"""Current holdings and allocation by asset class and currency."""

from collections import defaultdict
from datetime import date
from decimal import Decimal

import pandas as pd
from beancount.core import data

from fin import ledger, registry
from fin.analytics.networth import valued_balances
from fin.analytics.valuation import Market, balances_at
from fin.ledger import Ledger

CASH_CCYS = {"CHF", "USD", "EUR"}


def commodity_meta(led: Ledger) -> dict[str, dict]:
    return {e.currency: e.meta or {} for e in led.entries if isinstance(e, data.Commodity)}


def holdings(
    led: Ledger, base: str = "CHF", as_of: date | None = None, include_pension: bool = False
) -> pd.DataFrame:
    """One row per (account, commodity, lot currency): units, average cost, price, value,
    unrealized gain in the lot currency, asset class and weight of the allocation total."""
    as_of = as_of or ledger.valuation_date(led)
    market = Market.of(led.entries, base)
    meta = commodity_meta(led)
    inv = balances_at(led.entries, [as_of])[as_of]
    agg: dict[tuple, dict] = defaultdict(
        lambda: {"units": Decimal(0), "cost_total": Decimal(0), "value": Decimal(0)}
    )
    for r in valued_balances(inv, market, as_of, registry.of(led)):
        # allocation is about what you can invest: homes, cars, debts and (by default) pension are separate
        if r["group"] in ("Other", "Liabilities", "Property", "Vehicles") or (
            r["group"] == "Pension" and not include_pension
        ):
            continue
        quote = r["cost"].currency if r["cost"] else r["commodity"]
        a = agg[(r["account"], r["commodity"], quote, r["group"])]
        a["units"] += r["units"]
        a["value"] += r["value"]
        if r["cost"]:
            a["cost_total"] += r["units"] * r["cost"].number
    rows = []
    for (account, commodity, quote, group), a in agg.items():
        if commodity in CASH_CCYS:
            asset_class, name = "cash", commodity
        elif group == "Pension":
            asset_class, name = "pension", commodity
        else:
            asset_class = meta.get(commodity, {}).get("asset_class", "other")
            name = meta.get(commodity, {}).get("name", commodity)
        price = market.price(commodity, quote, as_of) if commodity not in CASH_CCYS else None
        has_cost = a["cost_total"] != 0
        rows.append(
            {
                "account": account,
                "commodity": commodity,
                "name": name,
                "asset_class": asset_class,
                "currency": quote,
                "units": a["units"],
                "avg_cost": (a["cost_total"] / a["units"]).quantize(Decimal("0.0001"))
                if has_cost
                else None,
                "price": price,
                "value": a["value"].quantize(Decimal("0.01")),
                "unrealized": (a["units"] * price - a["cost_total"]).quantize(Decimal("0.01"))
                if has_cost and price
                else None,
            }
        )
    df = pd.DataFrame(rows)
    total = df["value"].sum()
    df["weight"] = [(v / total).quantize(Decimal("0.0001")) for v in df["value"]]
    return df.sort_values("value", ascending=False).reset_index(drop=True)


def allocation(
    df: pd.DataFrame, by: str = "asset_class", targets: dict[str, Decimal] | None = None
) -> pd.DataFrame:
    """Weights by `by` (asset_class | currency | commodity); with targets adds target and drift."""
    out = df.groupby(by, as_index=False).agg(value=("value", "sum"), weight=("weight", "sum"))
    out = out.sort_values("value", ascending=False).reset_index(drop=True)
    if targets:
        out["target"] = [targets.get(k, Decimal(0)) for k in out[by]]
        out["drift"] = out["weight"] - out["target"]
    return out


def concentration(df: pd.DataFrame, commodity: str) -> Decimal:
    """Share of the allocation total held in one commodity (e.g. employer stock)."""
    return df[df["commodity"] == commodity]["weight"].sum()
