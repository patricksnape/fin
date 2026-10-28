"""Every balance sheet account by institution, and each account's ledger with a running balance.

Balances are the ledger's own (Beancount's balance_by_account); values are at market in the base
currency, like net worth, so the institutions add up to net worth.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal

import pandas as pd
from beancount.core import convert, data

from fin import registry
from fin.agent import engine
from fin.analytics import networth
from fin.analytics.valuation import Market, balances_at, checkpoints
from fin.ledger import valuation_date

CENT = Decimal("0.01")
SERIES_POINTS = 25  # about two years of month-ends


def short_name(account: str) -> str:
    parts = account.split(":")
    return ":".join(parts[2:]) or parts[1]


def _q(v) -> Decimal:
    return Decimal(v).quantize(CENT)


def accounts(data_: engine.Data) -> dict:
    """Institutions with their accounts. An account appears once it has any posting."""
    led, base = data_.led, data_.settings.base_currency
    reg = registry.of(led)
    market = Market.of(led.entries, base)
    as_of = valuation_date(led)
    dates = checkpoints(led.entries, as_of)
    snaps = balances_at(led.entries, dates)

    values: dict[str, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for d, inv in snaps.items():
        for row in networth.valued_balances(inv, market, d, reg):
            values[row["account"]][d] += row["value"]

    activity: dict[str, dict] = defaultdict(
        lambda: {"transactions": 0, "last": None, "checked": None}
    )
    for e in led.entries:
        if isinstance(e, data.Transaction):
            for acct in {
                p.account for p in e.postings if p.account.startswith(("Assets:", "Liabilities:"))
            }:
                a = activity[acct]
                a["transactions"] += 1
                a["last"] = max(a["last"] or e.date, e.date)
        elif isinstance(e, data.Balance) and e.account.startswith(("Assets:", "Liabilities:")):
            a = activity[e.account]
            a["checked"] = max(a["checked"] or e.date, e.date)

    now = snaps[as_of]
    groups: dict[str, list[dict]] = defaultdict(list)
    for acct, info in sorted(activity.items()):
        if not info["transactions"]:
            continue
        held: dict[str, dict] = {}
        for p in now.get(
            acct, []
        ):  # one line per commodity; lots of the same shares are one holding
            h = held.setdefault(
                p.units.currency,
                {
                    "commodity": p.units.currency,
                    "units": Decimal(0),
                    "value": Decimal(0),
                    "lots": 0,
                },
            )
            h["units"] += p.units.number
            h["value"] += market.value(p, as_of)
            h["lots"] += 1
        positions = list(held.values())
        raw = sum((h["value"] for h in positions), Decimal(0))  # round once, after adding
        for h in positions:
            h["value"] = _q(h["value"])
        # every month-end, so an account that was empty at each one still shows a flat line
        series = [{"date": d, "value": _q(values[acct].get(d, Decimal(0)))} for d in dates][
            -SERIES_POINTS:
        ]
        groups[reg.institution(acct)].append(
            {
                "account": acct,
                "name": short_name(acct),
                "kind": "liability" if acct.startswith("Liabilities:") else "asset",
                "group": reg.group(acct),
                "value": _q(raw),
                "raw": raw,
                "positions": positions,
                "series": series,
                "last_activity": info["last"],
                "transactions": info["transactions"],
                "last_checked": info["checked"],
            }
        )
    institutions = [
        {"name": name, "value": _q(sum((a["raw"] for a in accts), Decimal(0))), "accounts": accts}
        for name, accts in groups.items()
    ]
    institutions.sort(key=lambda i: -i["value"])
    total = sum((a["raw"] for i in institutions for a in i["accounts"]), Decimal(0))
    assets = sum(
        (a["raw"] for i in institutions for a in i["accounts"] if a["kind"] == "asset"), Decimal(0)
    )
    for i in institutions:
        for a in i["accounts"]:
            del a["raw"]
    return {
        "as_of": as_of,
        "institutions": institutions,
        "assets": _q(assets),
        "liabilities": _q(total - assets),
        "total": _q(total),
    }


def account_ledger(data_: engine.Data, account: str) -> dict:
    """Every posting to `account` in order, each with the balance after it, in the account's own
    units (CHF, EUR, shares...). Raises KeyError for an account with no postings."""
    led, base = data_.led, data_.settings.base_currency
    market = Market.of(led.entries, base)
    table = data_.transactions.set_index("ref")
    running: dict[str, Decimal] = defaultdict(Decimal)
    rows = []
    for e in led.entries:
        if not isinstance(e, data.Transaction) or not any(p.account == account for p in e.postings):
            continue
        ref = (e.meta or {}).get("ref")
        info = table.loc[ref] if ref in table.index else None
        for p in e.postings:
            if p.account != account or p.units.number is None:
                continue
            running[p.units.currency] += p.units.number
            others = [q for q in e.postings if q.account != account and q.units.number is not None]
            biggest = max(
                others,
                key=lambda q: abs(market.convert(convert.get_weight(q), e.date)),
                default=None,
            )
            rows.append(
                {
                    "date": e.date,
                    "ref": ref,
                    "payee": e.payee or "",
                    "narration": e.narration or "",
                    "units": p.units.number,
                    "commodity": p.units.currency,
                    "value": _q(market.convert(convert.get_weight(p), e.date)),
                    "balance": running[p.units.currency],
                    "counterparty": biggest.account if biggest else "",
                    "kind": info["kind"] if info is not None else None,
                    "needs_review": bool(info["needs_review"]) if info is not None else False,
                }
            )
    if not rows:
        raise KeyError(account)
    return {"account": account, "rows": rows, "balances": dict(running)}


def month_view(ledger_: dict, month: str) -> dict:
    """Restrict a ledger to one month (YYYY-MM), with the balance carried in from before it."""
    rows = ledger_["rows"]
    inside = [r for r in rows if f"{r['date'].year}-{r['date'].month:02d}" == month]
    before: dict[str, Decimal] = {}
    for r in rows:
        if f"{r['date'].year}-{r['date'].month:02d}" >= month:
            break
        before[r["commodity"]] = r["balance"]
    return {**ledger_, "rows": inside, "balance_before": before}


def months_with_activity(ledger_: dict) -> list[dict]:
    counts = (
        pd.Series([f"{r['date'].year}-{r['date'].month:02d}" for r in ledger_["rows"]])
        .value_counts()
        .sort_index()
    )
    return [{"month": m, "count": int(c)} for m, c in counts.items()]
