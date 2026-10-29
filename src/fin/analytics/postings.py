"""Flatten ledger expense postings into a DataFrame in base currency."""

from decimal import Decimal

import pandas as pd
from beancount.core import data

from fin.analytics.valuation import Market
from fin.ledger import Ledger

COLUMNS = [
    "date", "ref", "payee", "narration", "account", "amount", "orig_amount", "orig_ccy",
    "cadence", "necessity", "explicit_cadence", "explicit_necessity", "explicit_major", "tags",
]  # fmt: skip


def _flag(value) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value)


def expense_postings(led: Ledger, base: str = "CHF") -> pd.DataFrame:
    """One row per posting to Expenses:*, converted to `base` at the price on the posting date."""
    market = Market.of(led.entries, base)
    defaults = {e.account: e.meta or {} for e in led.entries if isinstance(e, data.Open)}
    rows = []
    for e in led.entries:
        if not isinstance(e, data.Transaction):
            continue
        for p in e.postings:
            if not p.account.startswith("Expenses:") or p.units.number is None:
                continue
            ccy, number = p.units.currency, p.units.number
            amount = number if ccy == base else market.convert(p.units, e.date)
            pm, tm, dm = p.meta or {}, e.meta or {}, defaults.get(p.account, {})
            rows.append(
                {
                    "date": e.date,
                    "ref": tm.get("ref"),
                    "payee": e.payee or "",
                    "narration": e.narration or "",
                    "account": p.account,
                    "amount": amount.quantize(Decimal("0.01")),
                    "orig_amount": number,
                    "orig_ccy": ccy,
                    "cadence": dm.get("cadence", "regular"),
                    "necessity": dm.get("necessity", "discretionary"),
                    "explicit_cadence": pm.get("cadence", tm.get("cadence")),
                    "explicit_necessity": pm.get("necessity", tm.get("necessity")),
                    # the posting's or transaction's own flag, else the account's (`major: FALSE` on
                    # an account of bills or aggregates, so its large payments are never "major")
                    "explicit_major": _flag(pm.get("major", tm.get("major", dm.get("major")))),
                    "tags": tuple(sorted(e.tags or ())),
                }
            )
    return pd.DataFrame(rows, columns=COLUMNS)
