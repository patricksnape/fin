"""Spend classification and monthly spend views (cash vs run-rate).

Classification, first hit wins: explicit meta on the posting/transaction, then a detected
recurring series, then the expense account's `open` defaults. A posting is `major` when it is
>= major_threshold (or in major_accounts) unless it is recurring; majors are excluded from spend.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pandas as pd

from fin import ledger
from fin.analytics import recurring
from fin.analytics.postings import expense_postings
from fin.config import Settings
from fin.ledger import Ledger


def month_start(d: date) -> date:
    return d.replace(day=1)


def add_months(d: date, n: int) -> date:
    idx = d.year * 12 + d.month - 1 + n
    return date(idx // 12, idx % 12 + 1, 1)


@dataclass
class SpendModel:
    postings: pd.DataFrame  # classified, one row per expense posting
    series: list[recurring.Series]
    as_of: date
    settings: Settings


def build(
    led: Ledger,
    settings: Settings,
    confirmed: set[str] = frozenset(),
    rejected: set[str] = frozenset(),
    as_of: date | None = None,
) -> SpendModel:
    df = expense_postings(led, settings.base_currency)
    as_of = as_of or ledger.as_of(led)
    prefixes = tuple(settings.non_spend_prefixes)
    series = recurring.detect(
        df,
        as_of,
        confirmed,
        rejected,
        exclude_prefixes=prefixes + tuple(settings.no_recurring_prefixes),
    )
    by_key = {s.id: s for s in series if s.applies}

    keys = [
        f"{recurring.normalize_payee(p)}|{a}"
        for p, a in zip(df["payee"], df["account"], strict=True)
    ]
    df = df.assign(series_id=[k if k in by_key else None for k in keys])
    df["non_spend"] = df["account"].str.startswith(prefixes)
    cadence = df["cadence"].mask(df["series_id"].notna(), "recurring")
    df["cadence"] = df["explicit_cadence"].where(df["explicit_cadence"].notna(), cadence)
    df["necessity"] = df["explicit_necessity"].fillna(df["necessity"])

    def is_major(row) -> bool:
        if row.explicit_major is not None and not pd.isna(row.explicit_major):
            return bool(row.explicit_major)
        if row.non_spend or row.cadence == "recurring":
            return False
        return row.amount >= settings.major_threshold or row.account in settings.major_accounts

    df["major"] = [is_major(r) for r in df.itertuples()]
    df.loc[df["major"], "cadence"] = "one_off"
    df["in_spend"] = ~df["non_spend"] & ~df["major"]
    df["category"] = df["account"].map(lambda a: ":".join(a.split(":")[:2]))
    return SpendModel(
        df.drop(columns=["explicit_cadence", "explicit_necessity", "explicit_major"]),
        series,
        as_of,
        settings,
    )


def monthly_rows(model: SpendModel, view: str = "run_rate") -> pd.DataFrame:
    """Postings placed into months. `run_rate` spreads quarterly/yearly recurring payments evenly
    over the months they cover (amount / months); `cash` keeps the payment month."""
    df, last = model.postings, month_start(model.as_of)
    by_id = {s.id: s for s in model.series}
    out = []
    for row in df.to_dict("records"):
        start = month_start(row["date"])
        months = 1
        if view == "run_rate" and pd.notna(row["series_id"]):
            months = recurring.PERIOD_MONTHS[by_id[row["series_id"]].period]
        share = (row["amount"] / months).quantize(Decimal("0.01")) if months > 1 else row["amount"]
        for i in range(months):
            m = add_months(start, i)
            if m <= last:
                out.append({**row, "month": m, "amount": share})
    # a lapse-free yearly series with no payment covering the current month still costs its share
    if view == "run_rate":
        for s in model.series:
            months = recurring.PERIOD_MONTHS[s.period]
            if (
                months > 1
                and s.status == "active"
                and add_months(month_start(s.last_date), months) <= last
            ):
                covered = add_months(month_start(s.last_date), months)
                share = (s.typical_amount / months).quantize(Decimal("0.01"))
                proto = df[df["series_id"] == s.id].iloc[-1].to_dict()
                m = covered
                while m <= last:
                    out.append({**proto, "month": m, "amount": share})
                    m = add_months(m, 1)
    cols = list(df.columns) + ["month"]
    return pd.DataFrame(out, columns=cols)


def spend_by(model: SpendModel, by: str = "total", view: str = "run_rate") -> pd.DataFrame:
    """Monthly spend (excludes taxes/payroll deductions and major purchases) grouped by
    `by` in total | cadence | necessity | category. Returns long rows: month, key, amount."""
    rows = monthly_rows(model, view)
    rows = rows[rows["in_spend"]]
    if by == "total":
        rows = rows.assign(key="total")
    else:
        rows = rows.assign(key=rows[by])
    out = rows.groupby(["month", "key"], as_index=False)["amount"].sum()
    return out.sort_values(["month", "key"]).reset_index(drop=True)


def major_purchases(model: SpendModel) -> pd.DataFrame:
    df = model.postings
    return (
        df[df["major"]][["date", "payee", "narration", "account", "amount"]]
        .sort_values("date")
        .reset_index(drop=True)
    )


def headline(model: SpendModel) -> dict[str, Decimal]:
    """Average monthly figures over the last 3 complete months (run-rate view)."""
    last = month_start(model.as_of)
    window = [add_months(last, -i) for i in (1, 2, 3)]
    rows = monthly_rows(model)
    rows = rows[rows["in_spend"] & rows["month"].isin(window)]

    def avg(sel) -> Decimal:
        return (
            (rows[sel]["amount"].sum() / 3).quantize(Decimal("0.01")) if len(rows) else Decimal(0)
        )

    ess_cad = rows["cadence"].isin(["recurring", "regular"])
    return {
        "core_monthly_spend": avg(rows["amount"].notna()),
        "essential_baseline": avg((rows["necessity"] == "essential") & ess_cad),
        "discretionary": avg(rows["necessity"] == "discretionary"),
        "major_this_year": sum(
            (
                r.amount
                for r in major_purchases(model).itertuples()
                if r.date.year == model.as_of.year
            ),
            Decimal(0),
        ),
    }
