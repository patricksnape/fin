"""Monthly cashflow: income by kind, expenses by group, net and savings rate (base currency)."""

from collections import defaultdict
from datetime import date
from decimal import Decimal

import pandas as pd
from beancount.core import data

from fin.analytics.spend import SpendModel, month_start
from fin.analytics.valuation import Market
from fin.ledger import Ledger

INCOME_KINDS = ["salary", "equity_comp", "investment_income", "realized_gains", "other_income"]
EXPENSE_GROUPS = ["spend", "major", "taxes", "payroll_deductions"]
RSU_WITHHOLDING = "Expenses:Taxes:RSU-Withholding"


def income_kind(account: str) -> str:
    if account.startswith(("Income:Employer:Salary", "Income:Employer:Bonus")):
        return "salary"
    if account.startswith(("Income:Employer:RSU", "Income:Employer:ESPP")):
        return "equity_comp"
    if account.startswith("Income:Investments:Dividends"):
        return "investment_income"
    if account.startswith("Income:Investments:Gains"):
        return "realized_gains"
    return "other_income"


def expense_group(row) -> str:
    if row.major:
        return "major"
    if row.account.startswith("Expenses:Taxes"):
        return "taxes"
    if row.account.startswith("Expenses:Social"):
        return "payroll_deductions"
    return "spend"


def _rate(num: Decimal, den: Decimal) -> Decimal | None:
    return (num / den).quantize(Decimal("0.0001")) if den > 0 else None


def cashflow(led: Ledger, model: SpendModel, base: str = "CHF") -> pd.DataFrame:
    """One row per month (payment/booking month, cash view). `savings_rate` is net / all income;
    `savings_rate_excl_equity` leaves out equity compensation and its withheld tax."""
    market = Market.of(led.entries, base)
    totals: dict[date, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for e in led.entries:
        if not isinstance(e, data.Transaction):
            continue
        for p in e.postings:
            if p.account.startswith("Income:") and p.units.number is not None:
                amount = -market.convert(p.units, e.date)
                totals[month_start(e.date)][income_kind(p.account)] += amount
    for r in model.postings.itertuples():
        m = month_start(r.date)
        totals[m][expense_group(r)] += r.amount
        if r.account == RSU_WITHHOLDING:
            totals[m]["rsu_withholding"] += r.amount
    rows = []
    for m in sorted(totals):
        t = totals[m]
        income = sum((t[k] for k in INCOME_KINDS), Decimal(0))
        expenses = sum((t[k] for k in EXPENSE_GROUPS), Decimal(0))
        net = income - expenses
        cash_income = income - t["equity_comp"] - t["realized_gains"]
        rows.append(
            {
                "month": m,
                **{k: t[k].quantize(Decimal("0.01")) for k in INCOME_KINDS},
                "income": income.quantize(Decimal("0.01")),
                **{k: t[k].quantize(Decimal("0.01")) for k in EXPENSE_GROUPS},
                "expenses": expenses.quantize(Decimal("0.01")),
                "net": net.quantize(Decimal("0.01")),
                "savings_rate": _rate(net, income),
                "savings_rate_excl_equity": _rate(
                    net - t["equity_comp"] + t["rsu_withholding"] - t["realized_gains"], cash_income
                ),
            }
        )
    return pd.DataFrame(rows)
