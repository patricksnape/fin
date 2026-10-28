from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from beancount.core import data

from fin import config, demo, ledger
from fin.analytics import allocation, cashflow, networth, spend


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("wealth") / "repo"
    demo.create_demo(root)
    led = ledger.load(root / "ledger" / "main.beancount")
    settings = config.load_settings(root, led)
    return led, settings, spend.build(led, settings)


def nw_at(env, d=None):
    led, _, _ = env
    df = networth.net_worth(led)
    d = d or df["date"].max()
    return {r.group: r.value for r in df[df["date"] == d].itertuples()}


def test_series_runs_from_opening_to_as_of(env):
    led, _, _ = env
    df = networth.net_worth(led)
    dates = sorted(df["date"].unique())
    assert dates[0] == date(2024, 10, 31)
    assert dates[-1] == ledger.as_of(led)
    assert dates == sorted(dates) and len(dates) == 25


def test_opening_net_worth_is_the_opening_balances(env):
    now = nw_at(env, date(2024, 10, 31))
    assert now["Cash"] == Decimal("40000.00") and now["Pension"] == Decimal("80000.00")
    assert now["Investments"] > 0 and now["Liabilities"] == Decimal("-700000.00")  # the mortgage
    assert now["Property"] == Decimal("1080000.00")  # the home at the valuation on file


def test_pension_grows_by_payslip_contribution(env):
    assert nw_at(env)["Pension"] == Decimal(80000 + 600 * 23)


def test_liabilities_match_card_plus_mortgage(env):
    led, _, _ = env
    owed = sum(
        p.units.number
        for e in led.entries
        if isinstance(e, data.Transaction)
        for p in e.postings
        if p.account.startswith("Liabilities:")
    )
    assert nw_at(env)["Liabilities"] == owed.quantize(Decimal("0.01"))


def test_espp_receivable_cleared_at_end(env):
    assert nw_at(env)["Other"] == 0


def test_holdings_total_matches_cash_plus_investments(env):
    led, _, _ = env
    h = allocation.holdings(led)
    now = nw_at(env)
    assert abs(h["value"].sum() - (now["Cash"] + now["Investments"])) <= Decimal("0.05")
    assert abs(h["weight"].sum() - 1) <= Decimal("0.001")


def test_unrealized_gain_in_lot_currency(env):
    led, _, _ = env
    h = allocation.holdings(led).set_index("commodity")
    assert h.loc["VT", "unrealized"] > 0 and h.loc["ACME", "unrealized"] < 0
    assert pd.isna(h.loc["CHF", "unrealized"])


def test_allocation_drift_against_targets(env):
    led, settings, _ = env
    a = allocation.allocation(allocation.holdings(led), "asset_class", settings.targets).set_index(
        "asset_class"
    )
    assert a.loc["equity", "target"] == Decimal("0.75")
    assert a.loc["equity", "drift"] == a.loc["equity", "weight"] - Decimal("0.75")
    assert set(a.index) == {"equity", "bonds", "cash"}


def test_currency_exposure_and_concentration(env):
    led, _, _ = env
    h = allocation.holdings(led)
    by_ccy = allocation.allocation(h, "currency").set_index("currency")
    assert by_ccy.loc["USD", "weight"] > by_ccy.loc["EUR", "weight"]
    assert Decimal("0.05") < allocation.concentration(h, "ACME") < Decimal("0.3")


@pytest.fixture(scope="module")
def cf(env):
    led, _, model = env
    return cashflow.cashflow(led, model).set_index("month")


def test_bonus_in_march_salary(cf):
    assert cf.loc[date(2025, 3, 1), "salary"] == Decimal("19000.00")
    assert cf.loc[date(2025, 4, 1), "salary"] == Decimal("11000.00")


def test_equity_comp_in_vest_months_only(cf):
    assert cf.loc[date(2024, 11, 1), "equity_comp"] > 0
    assert cf.loc[date(2024, 12, 1), "equity_comp"] == 0


def test_major_and_tax_groups(cf):
    assert cf.loc[date(2026, 3, 1), "major"] == Decimal("18000.00")
    assert cf.loc[date(2025, 9, 1), "taxes"] >= 6000
    assert cf.loc[date(2026, 3, 1), "net"] < 0


def test_net_is_income_minus_expenses(cf):
    for r in cf.itertuples():
        assert r.net == r.income - r.expenses


def test_savings_rate_excl_equity_matches_when_no_equity(cf):
    r = cf.loc[date(2026, 4, 1)]
    assert r["equity_comp"] == 0 and r["savings_rate"] == r["savings_rate_excl_equity"]
    assert Decimal("0.3") < r["savings_rate"] < Decimal("0.7")
