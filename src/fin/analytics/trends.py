"""Trends across years: annual summaries, seasonality, rolling averages, where net worth growth came from.

Everything is derived from the ledger through the same cashflow, spend and net worth functions the
other pages use, so the numbers agree. Averages use complete months only; sums include the month in
progress ("so far").
"""

from decimal import Decimal

import pandas as pd

from fin import ledger
from fin.agent import engine
from fin.analytics import networth, rsu, spend

CENT = Decimal("0.01")
PCT = Decimal("0.0001")
SUM_COLUMNS = [
    "income", "salary", "equity_comp", "investment_income", "realized_gains",
    "spend", "major", "taxes", "payroll_deductions", "expenses", "net",
]  # fmt: skip


def _q(v) -> Decimal:
    return Decimal(v).quantize(CENT)


def _rate(num: Decimal, den: Decimal) -> Decimal | None:
    return (num / den).quantize(PCT) if den > 0 else None


def complete_through(data: engine.Data):
    """Last month that is finished (the month in progress is left out of averages)."""
    return engine.last_complete_month(data.model.as_of)


def net_worth_totals(data: engine.Data) -> pd.Series:
    nw = networth.net_worth(data.led, data.settings.base_currency)
    return networth.total_by_date(nw).set_index("date")["value"]


def annual(data: engine.Data) -> pd.DataFrame:
    """One row per calendar year: totals, savings rate, average monthly spend (complete months),
    year-end net worth, and the change split into what you saved and what the markets and your
    own revaluations did."""
    cf = data.cashflow.copy()
    cf["year"] = [m.year for m in cf["month"]]
    effect = rsu.yearly_effect(
        data.led.entries, data.root, sorted({m.year for m in cf["month"]}),
        ledger.valuation_date(data.led), data.settings.base_currency,
    )  # fmt: skip
    lcm = complete_through(data)
    totals = net_worth_totals(data)
    rows, previous = [], totals.iloc[0]
    for year, g in cf.groupby("year"):
        done = g[g["month"] <= lcm]
        sums = {c: sum((v for v in g[c]), Decimal(0)) for c in SUM_COLUMNS}
        year_end = totals[[d for d in totals.index if d.year == year]].iloc[-1]
        change = year_end - previous
        spend_done = sum((v for v in done["spend"]), Decimal(0))
        # the growth in four parts that add up to saved + markets: cash saving (without share
        # compensation and without gains already made on sales), share compensation (vests net of the
        # shares kept for tax), what the market and exchange rate did to employer shares, and the rest
        comp = sum((v for v in g["equity_comp"]), Decimal(0)) - sum(
            (v for v in g["rsu_withholding"]), Decimal(0)
        )
        market = (change - sums["net"]) + sums["realized_gains"]
        rows.append(
            {
                "year": int(year),
                "months": len(done),
                "partial": bool(len(g) > len(done)) or len(done) < 12,
                **{c: _q(v) for c, v in sums.items()},
                "savings_rate": _rate(sums["net"], sums["income"]),
                "avg_monthly_spend": _q(spend_done / len(done)) if len(done) else None,
                "net_worth_end": _q(year_end),
                "net_worth_change": _q(change),
                "from_saving": _q(sums["net"]),
                "from_markets_and_revaluation": _q(change - sums["net"]),
                "from_cash_saving": _q(sums["net"] - comp - sums["realized_gains"]),
                "from_share_comp": _q(comp),
                "from_employer_stock": _q(effect.get(int(year), Decimal(0))),
                "from_other_markets": _q(market - effect.get(int(year), Decimal(0))),
            }
        )
        previous = year_end
    return pd.DataFrame(rows)


def monthly_spend(data: engine.Data) -> pd.DataFrame:
    """Run-rate spend per month through the last complete month: month, amount."""
    total = spend.spend_by(data.model, "total", "run_rate")
    total = total[total["month"] <= complete_through(data)]
    return total.rename(columns={"amount": "spend"})[["month", "spend"]].reset_index(drop=True)


def overlay(data: engine.Data) -> dict[int, list[Decimal | None]]:
    """For each year, 12 monthly spend values (None where there is no data): lines to compare years."""
    out: dict[int, list[Decimal | None]] = {}
    for r in monthly_spend(data).itertuples():
        out.setdefault(r.month.year, [None] * 12)[r.month.month - 1] = r.spend
    return out


def rolling(data: engine.Data) -> pd.DataFrame:
    """Per complete month: 12-month average spend, 12-month savings rate, month-end net worth."""
    lcm = complete_through(data)
    spend_m = monthly_spend(data).set_index("month")["spend"]
    cf = data.cashflow[data.cashflow["month"] <= lcm].set_index("month")
    totals = net_worth_totals(data)
    by_month = {(d.year, d.month): v for d, v in totals.items()}
    rows = []
    months = list(cf.index)
    for i, m in enumerate(months):
        window = months[max(0, i - 11) : i + 1]
        full = len(window) == 12
        income = sum((cf.loc[w, "income"] for w in window), Decimal(0))
        net = sum((cf.loc[w, "net"] for w in window), Decimal(0))
        rows.append(
            {
                "month": m,
                "spend_12m": _q(sum((spend_m.get(w, Decimal(0)) for w in window), Decimal(0)) / 12)
                if full
                else None,
                "savings_rate_12m": _rate(net, income) if full else None,
                "net_worth": _q(by_month[(m.year, m.month)])
                if (m.year, m.month) in by_month
                else None,
            }
        )
    return pd.DataFrame(rows)


def categories_by_year(data: engine.Data) -> dict:
    """Spend per category per year: keys (largest first) and one row per year with the number of
    complete months it covers, so partial years are not mistaken for lower spending."""
    rows = spend.monthly_rows(data.model, "run_rate")
    rows = rows[rows["in_spend"] & (rows["month"] <= complete_through(data))]
    rows = rows.assign(year=[m.year for m in rows["month"]])
    piv = rows.pivot_table(
        index="year", columns="category", values="amount", aggfunc="sum", fill_value=Decimal(0)
    )
    keys = sorted(piv.columns, key=lambda k: -piv[k].sum())
    months = rows.groupby("year")["month"].nunique()
    return {
        "keys": keys,
        "rows": [
            {
                "year": int(y),
                "months": int(months[y]),
                "total": _q(piv.loc[y].sum()),
                **{k: _q(piv.loc[y, k]) for k in keys},
            }
            for y in piv.index
        ],
    }
