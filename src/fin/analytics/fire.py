"""FIRE: how far you are from financial independence, and how long it takes.

Method, in plain terms:
- Annual spend = your spend over the last 12 complete months (run-rate; excludes major purchases,
  taxes and payroll deductions), unless you set a `target-spend` in the ledger.
- FI number = annual spend / withdrawal rate (e.g. 3.5% means 28.6x your spend).
- Assets that count: cash and investments always; pension only once you reach the access age
  (or now, if you switch it on without a birth year); home equity only if you switch it on.
- Contributions are what you actually saved over those 12 months, not counting what your
  investments earned (the real-return assumption already covers that) or one-off major purchases,
  and split by where the money went: investable, pension, mortgage principal.
- Everything is in today's money (a real return), so the FI number does not drift with inflation.
"""

import math
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal

from beancount.core import convert, data

from fin import registry
from fin.agent import engine
from fin.analytics import allocation, fixed, spend, trends
from fin.analytics.valuation import Market

CENT = Decimal("0.01")
PCT = Decimal("0.0001")
HORIZON_MONTHS = 60 * 12
SPREAD = Decimal("0.02")  # the low and high scenarios are the real return -/+ 2 points
RATES = [Decimal(i) / 100 for i in range(5, 85, 5)]


@dataclass(frozen=True)
class Assumptions:
    withdrawal_rate: Decimal = Decimal("0.035")
    real_return: Decimal = Decimal("0.04")
    target_spend: Decimal | None = None  # annual; None means use your own last 12 months
    birth_year: int | None = None
    pension_access_age: int = 60
    target_age: int = 55

    @classmethod
    def from_ledger(cls, fire: dict[str, Decimal]) -> "Assumptions":
        base = cls()
        return replace(
            base,
            withdrawal_rate=fire.get("withdrawal-rate", base.withdrawal_rate),
            real_return=fire.get("real-return", base.real_return),
            target_spend=fire.get("target-spend"),
            birth_year=int(fire["birth-year"]) if "birth-year" in fire else None,
            pension_access_age=int(fire.get("pension-access-age", base.pension_access_age)),
            target_age=int(fire.get("target-age", base.target_age)),
        )


def _q(v) -> Decimal:
    return Decimal(v).quantize(CENT)


def _months_window(data_: engine.Data) -> list[date]:
    """Up to the last 12 complete months that have data."""
    last = trends.complete_through(data_)
    first = data_.cashflow["month"].min()
    months = [spend.add_months(last, -i) for i in range(11, -1, -1)]
    return [m for m in months if m >= first]


def _flows(data_: engine.Data, start: date, end: date) -> dict[str, Decimal]:
    """Money that went somewhere other than cash/investments in the window: pension contributions,
    mortgage principal repaid, homes and cars bought. Read straight from the ledger postings."""
    market = Market.of(data_.led.entries, data_.settings.base_currency)
    out = {"pension": Decimal(0), "principal": Decimal(0), "fixed_assets": Decimal(0)}
    reg = registry.of(data_.led)
    for e in data_.led.entries:
        if not isinstance(e, data.Transaction) or not start <= e.date <= end:
            continue
        for p in e.postings:
            if p.units.number is None or p.units.number <= 0:
                continue
            role = reg.role(p.account)
            if role == "pension":
                out["pension"] += market.convert(p.units, e.date)
            elif role == "mortgage":
                out["principal"] += market.convert(p.units, e.date)  # debt reduced
            elif role in ("property", "vehicle"):
                out["fixed_assets"] += market.convert(convert.get_weight(p), e.date)
    return out


def simulate(
    liquid: Decimal,
    c_liquid: Decimal,
    pension: Decimal,
    c_pension: Decimal,
    equity: Decimal,
    c_equity: Decimal,
    debt: Decimal,
    real_return: Decimal,
    include_pension: bool,
    pension_from_month: int,
    include_home: bool,
    months: int = HORIZON_MONTHS,
) -> list[Decimal]:
    """Counted assets at the end of each month, today first. Pension joins from `pension_from_month`;
    home equity grows only by mortgage principal repaid (the home itself is held flat in real terms)."""
    growth = Decimal(str((1 + float(real_return)) ** (1 / 12)))
    path = []
    for t in range(months + 1):
        counted = liquid
        if include_pension and t >= pension_from_month:
            counted += pension
        if include_home:
            counted += equity
        path.append(counted)
        liquid = liquid * growth + c_liquid
        pension = pension * growth + c_pension
        paid = min(c_equity, debt)
        debt -= paid
        equity += paid
    return path


def _first_reaching(path: list[Decimal], target: Decimal) -> int | None:
    return next((t for t, v in enumerate(path) if v >= target), None)


def _years(months: int | None) -> Decimal | None:
    return None if months is None else (Decimal(months) / 12).quantize(Decimal("0.1"))


def _date_after(start: date, months: int | None) -> date | None:
    return None if months is None else spend.add_months(start.replace(day=1), months)


def status(
    data_: engine.Data,
    a: Assumptions,
    include_pension: bool = False,
    include_home: bool = False,
) -> dict:
    months = _months_window(data_)
    as_of = data_.model.as_of
    base = data_.settings.base_currency
    if not months:
        return {"ready": False, "reason": "No complete month of data yet", "assumptions": a}
    start, end = months[0], spend.add_months(months[-1], 1) - timedelta(days=1)
    n = len(months)

    rows = spend.monthly_rows(data_.model, "run_rate")
    rows = rows[rows["in_spend"] & rows["month"].isin(months)]
    spend_year = sum(rows["amount"], Decimal(0)) / n * 12
    essential_year = sum(rows[rows["necessity"] == "essential"]["amount"], Decimal(0)) / n * 12
    annual_spend = a.target_spend if a.target_spend is not None else spend_year
    fi_number = annual_spend / a.withdrawal_rate
    lean_number = essential_year / a.withdrawal_rate

    cf = data_.cashflow[data_.cashflow["month"].isin(months)]
    total = lambda col: sum(cf[col], Decimal(0))
    returns = total("investment_income") + total("realized_gains")
    flows = _flows(data_, start, end)
    # Normalized: one-off purchases (major spending, a home or car bought) are neither ongoing saving
    # nor ongoing spending, so they are left out of both sides.
    saved = total("net") - returns + total("major") - flows["pension"] - flows["principal"]
    c_liquid, c_pension, c_home = saved / n, flows["pension"] / n, flows["principal"] / n

    held = allocation.holdings(data_.led, base)
    liquid = Decimal(held["value"].sum())
    nw_fixed = fixed.fixed_assets(data_.led, base)
    pension_now = sum((v for k, v in _group_values(data_).items() if k == "Pension"), Decimal(0))
    equity_now, debt_now = nw_fixed["home_equity"], nw_fixed["mortgage_balance"]

    if a.birth_year is not None:
        age_now = Decimal((as_of - date(a.birth_year, 7, 1)).days) / Decimal("365.25")
        pension_from = max(0, math.ceil((a.pension_access_age - float(age_now)) * 12))
    else:
        age_now, pension_from = None, 0
    counted_now = (
        liquid
        + (pension_now if include_pension and pension_from == 0 else 0)
        + (equity_now if include_home else 0)
    )

    def run(r: Decimal) -> list[Decimal]:
        return simulate(
            liquid,
            c_liquid,
            pension_now,
            c_pension,
            equity_now,
            c_home,
            debt_now,
            r,
            include_pension,
            pension_from,
            include_home,
        )

    paths = {
        "low": run(a.real_return - SPREAD),
        "base": run(a.real_return),
        "high": run(a.real_return + SPREAD),
    }
    reached = {k: _first_reaching(v, fi_number) for k, v in paths.items()}
    scenarios = [
        {
            "key": k,
            "label": label,
            "real_return": rr,
            "years_to_fi": _years(reached[k]),
            "fi_date": _date_after(as_of, reached[k]),
        }
        for k, label, rr in (
            ("low", "Cautious", a.real_return - SPREAD),
            ("base", "Base", a.real_return),
            ("high", "Optimistic", a.real_return + SPREAD),
        )
    ]
    latest = max((m for m in reached.values() if m is not None), default=None)
    shown = min(HORIZON_MONTHS, max(10 * 12, (latest or 20 * 12) + 24))
    projection = [
        {
            "date": _date_after(as_of, t),
            "low": _q(paths["low"][t]),
            "base": _q(paths["base"][t]),
            "high": _q(paths["high"][t]),
            "fi_number": _q(fi_number),
        }
        for t in range(0, shown + 1, 12)
    ]

    coast = None
    if age_now is not None:
        years_left = max(Decimal(0), Decimal(a.target_age) - age_now)
        need = fi_number / Decimal(str((1 + float(a.real_return)) ** float(years_left)))
        coast = {
            "target_age": a.target_age,
            "years_left": years_left.quantize(Decimal("0.1")),
            "number": _q(need),
            "reached": counted_now >= need,
            "progress": (counted_now / need).quantize(PCT) if need else None,
        }

    income_year = (
        (total("income") - total("taxes") - total("payroll_deductions") - returns) / n * 12
    )
    curve = []
    # money that is neither spent nor counted as saving: pension/principal when those do not count
    other_year = (0 if include_pension else c_pension * 12) + (0 if include_home else c_home * 12)
    if income_year > 0:
        for rate in RATES:
            spend_s = (1 - rate) * income_year - other_year
            if spend_s <= 0:
                continue
            path = simulate(
                counted_now, rate * income_year / 12, Decimal(0), Decimal(0), Decimal(0),
                Decimal(0), Decimal(0), a.real_return, False, 0, False,
            )  # fmt: skip
            years = _years(_first_reaching(path, spend_s / a.withdrawal_rate))
            curve.append({"rate": rate, "years_to_fi": years})
    counted_saving = (
        c_liquid + (c_pension if include_pension else 0) + (c_home if include_home else 0)
    )
    notes = [
        "Spend is your last 12 complete months, without major purchases, taxes or payroll deductions. Taxes in retirement are not included: add them by setting a target spend.",
        "Contributions leave out what your investments earned and one-off purchases (major spending, a home or car bought). Share compensation (RSUs, ESPP) counts as saving.",
        "Home equity grows only as you repay the mortgage; the home is held flat in today's money.",
    ]
    if a.birth_year is None:
        notes.append(
            "Set a birth year to count the pension from its access age and to see Coast FIRE."
        )
    return {
        "ready": True,
        "as_of": as_of,
        "assumptions": a,
        "basis": {
            "months_used": n,
            "annual_spend": _q(annual_spend),
            "spend_source": "target" if a.target_spend is not None else "last 12 complete months",
            "measured_annual_spend": _q(spend_year),
            "essential_annual_spend": _q(essential_year),
            "one_off_purchases_left_out": _q((total("major") + flows["fixed_assets"]) / n * 12),
        },
        "fi_number": _q(fi_number),
        "lean_number": _q(lean_number),
        "assets": {
            "investable": _q(liquid),
            "pension": _q(pension_now),
            "home_equity": _q(equity_now),
            "counted": _q(counted_now),
            "include_pension": include_pension,
            "include_home": include_home,
        },
        "progress": (counted_now / fi_number).quantize(PCT) if fi_number else None,
        "contributions": {
            "investable_monthly": _q(c_liquid),
            "pension_monthly": _q(c_pension),
            "mortgage_principal_monthly": _q(c_home),
            "savings_rate": (counted_saving * 12 / income_year).quantize(PCT)
            if income_year > 0
            else None,
        },
        "pension": {
            "access_age": a.pension_access_age,
            "months_until_access": pension_from if a.birth_year is not None else None,
            "counted_from": _date_after(as_of, pension_from) if a.birth_year is not None else None,
        },
        "scenarios": scenarios,
        "projection": projection,
        "coast": coast,
        "savings_curve": curve,
        "notes": notes,
    }


def _group_values(data_: engine.Data) -> dict[str, Decimal]:
    from fin.analytics import networth

    nw = networth.net_worth(data_.led, data_.settings.base_currency)
    latest = nw[nw["date"] == nw["date"].max()]
    return {r.group: r.value for r in latest.itertuples()}


def from_ledger(data_: engine.Data) -> Assumptions:
    return Assumptions.from_ledger(ledger_settings_read(data_).fire)


def ledger_settings_read(data_: engine.Data):
    from fin import ledger_settings

    return ledger_settings.read(data_.led)
