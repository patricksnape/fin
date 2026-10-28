"""Homes, vehicles and the mortgage: what they are worth by your own valuation, and the equity."""

from datetime import date
from decimal import Decimal

from beancount.core import convert, prices

from fin import registry
from fin.analytics.allocation import commodity_meta
from fin.analytics.valuation import Market, balances_at
from fin.ledger import Ledger, valuation_date

STALE_DAYS = 400  # a valuation older than about a year is worth refreshing


def fixed_assets(led: Ledger, base: str = "CHF", as_of: date | None = None) -> dict:
    """Fixed assets valued at the latest `price` directive for their commodity, mortgages as debts.
    Values are your own estimates: update them by adding a price line to the ledger."""
    as_of = as_of or valuation_date(led)
    market = Market.of(led.entries, base)
    meta = commodity_meta(led)
    inventory = balances_at(led.entries, [as_of])[as_of]
    reg = registry.of(led)
    assets, mortgages = [], []
    for account, positions in sorted(inventory.items()):
        for pos in positions:
            if reg.is_role(account, "property", "vehicle"):
                value = market.value(pos, as_of)
                cost = market.convert(convert.get_cost(pos), as_of)
                quote = pos.cost.currency if pos.cost else base
                valued_on = prices.get_price(market.pmap, (pos.units.currency, quote), as_of)[0]
                assets.append(
                    {
                        "account": account,
                        "kind": reg.role(account),
                        "commodity": pos.units.currency,
                        "name": meta.get(pos.units.currency, {}).get("name", pos.units.currency),
                        "value": value,
                        "cost": cost,
                        "unrealized": value - cost,
                        "valued_on": valued_on,
                        "stale": valued_on is not None and (as_of - valued_on).days > STALE_DAYS,
                    }
                )
            elif reg.is_role(account, "mortgage"):
                mortgages.append({"account": account, "balance": -market.convert(pos.units, as_of)})
    property_value = sum((a["value"] for a in assets if a["kind"] == "property"), Decimal(0))
    debt = sum((m["balance"] for m in mortgages), Decimal(0))
    return {
        "assets": assets,
        "mortgages": mortgages,
        "property_value": property_value,
        "mortgage_balance": debt,
        "home_equity": property_value - debt,
        "loan_to_value": (debt / property_value).quantize(Decimal("0.0001"))
        if property_value
        else None,
    }
