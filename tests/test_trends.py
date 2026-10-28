from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fin import demo, ledger, scaffold
from fin.analytics import networth, trends
from fin.api.app import create_app
from fin.api.context import Context


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    root = tmp_path_factory.mktemp("trends") / "repo"
    demo.create_demo(root)
    return root


@pytest.fixture(scope="module")
def data(root):
    return Context(root).data()


def test_annual_rows_cover_each_year_with_partial_years_marked(data):
    a = trends.annual(data).set_index("year")
    assert list(a.index) == [2024, 2025, 2026]
    assert list(a["months"]) == [
        2,
        12,
        9,
    ]  # complete months only: Nov-Dec 2024, a full 2025, Jan-Sep 2026
    assert list(a["partial"]) == [True, False, True]


def test_annual_totals_agree_with_the_monthly_cashflow(data):
    a = trends.annual(data).set_index("year")
    cf = data.cashflow
    for year in (2025, 2026):
        part = cf[[m.year == year for m in cf["month"]]]
        assert a.loc[year, "income"] == part["income"].sum().quantize(Decimal("0.01"))
        assert a.loc[year, "spend"] == part["spend"].sum().quantize(Decimal("0.01"))
        assert a.loc[year, "net"] == part["net"].sum().quantize(Decimal("0.01"))
    assert a.loc[2025, "avg_monthly_spend"] == (a.loc[2025, "spend"] / 12).quantize(Decimal("0.01"))


def test_growth_splits_into_saving_and_the_rest_and_adds_up(data):
    a = trends.annual(data)
    for r in a.itertuples():
        assert r.from_saving + r.from_markets_and_revaluation == r.net_worth_change
    # year-end values chain: each change is measured from the previous year-end
    ends = list(a["net_worth_end"])
    assert a["net_worth_change"].iloc[1] == ends[1] - ends[0]


def test_annual_salary_matches_bql(root, data):
    a = trends.annual(data).set_index("year")
    main = root / "ledger" / "main.beancount"
    (want,) = ledger.query(
        main,
        "SELECT -sum(number) WHERE account ~ '^Income:Employer:(Salary|Bonus)' AND currency = 'CHF' AND year = 2025",
    )[1][0]
    assert a.loc[2025, "salary"] == want.quantize(Decimal("0.01"))


def test_overlay_gives_twelve_slots_per_year(data):
    o = trends.overlay(data)
    assert set(o) == {2024, 2025, 2026}
    assert all(v is not None for v in o[2025]) and [
        i for i, v in enumerate(o[2024]) if v is not None
    ] == [10, 11]
    assert [i for i, v in enumerate(o[2026]) if v is not None] == list(
        range(9)
    )  # October is still in progress
    monthly = trends.monthly_spend(data).set_index("month")["spend"]
    assert o[2025][3] == monthly[date(2025, 4, 1)]


def test_rolling_windows_need_twelve_months(data):
    r = trends.rolling(data).set_index("month")
    assert r["spend_12m"].isna().sum() == 11 and r["spend_12m"].notna().all() is not None
    assert r.index[r["spend_12m"].notna()][0] == date(2025, 10, 1)  # Nov 2024 + 11 months
    monthly = trends.monthly_spend(data).set_index("month")["spend"]
    window = [m for m in monthly.index if date(2024, 12, 1) <= m <= date(2025, 11, 1)]
    assert r.loc[date(2025, 11, 1), "spend_12m"] == (sum(monthly[m] for m in window) / 12).quantize(
        Decimal("0.01")
    )
    assert Decimal(0) < r["savings_rate_12m"].dropna().iloc[-1] < Decimal(1)
    totals = networth.total_by_date(networth.net_worth(data.led)).set_index("date")["value"]
    assert r.loc[date(2025, 6, 1), "net_worth"] == totals[date(2025, 6, 30)]


def test_categories_by_year_use_complete_months(data):
    c = trends.categories_by_year(data)
    by_year = {r["year"]: r for r in c["rows"]}
    assert (
        by_year[2025]["months"] == 12
        and by_year[2026]["months"] == 9
        and by_year[2024]["months"] == 2
    )
    monthly = trends.monthly_spend(data)
    for year in (2024, 2025, 2026):
        want = sum(
            v for m, v in zip(monthly["month"], monthly["spend"], strict=True) if m.year == year
        )
        assert by_year[year]["total"] == Decimal(want).quantize(Decimal("0.01"))
    assert c["keys"][0] == "Expenses:Food" or c["keys"][0].startswith("Expenses:")


def _tiny(tmp_path, ledger_text, accounts):
    root = tmp_path / "tiny"
    scaffold.init_data(root)
    (root / "ledger" / "accounts.beancount").write_text(accounts)
    (root / "ledger" / "manual" / "all.beancount").write_text(ledger_text)
    return Context(root).data()


ACCOUNTS = """2020-01-01 open Assets:Bank CHF
2020-01-01 open Income:Job CHF
2020-01-01 open Expenses:Food CHF
  cadence: "regular"
  necessity: "essential"
2020-01-01 open Assets:Property:Home
2020-01-01 open Equity:Opening-Balances
"""


def test_with_nothing_but_cash_all_growth_is_saving(tmp_path):
    month = lambda m, pay, food: (
        f'2025-{m:02d}-25 * "Job" "Pay"\n  ref: "t:p{m}"\n  Income:Job  -{pay} CHF\n  Assets:Bank  {pay} CHF\n\n'
        f'2025-{m:02d}-10 * "Coop" "Food"\n  ref: "t:f{m}"\n  Expenses:Food  {food} CHF\n  Assets:Bank  -{food} CHF\n'
    )
    text = (
        '2024-12-31 * "Open" "Open"\n  ref: "t:o"\n  Assets:Bank  1000 CHF\n  Equity:Opening-Balances\n\n'
        + "\n".join(month(m, 5000, 1000) for m in range(1, 13))
    )
    a = trends.annual(_tiny(tmp_path, text, ACCOUNTS)).iloc[0]
    assert (a["income"], a["spend"], a["net"]) == (Decimal(60000), Decimal(12000), Decimal(48000))
    assert (
        a["net_worth_change"] == a["from_saving"] == Decimal(48000)
        and a["from_markets_and_revaluation"] == 0
    )
    assert a["savings_rate"] == Decimal("0.8000")


def test_a_revaluation_shows_up_as_markets_not_saving(tmp_path):
    text = (
        '2024-12-31 * "Open" "Open"\n  ref: "t:o"\n  Assets:Bank  1000 CHF\n  Assets:Property:Home  1 HOME {500000 CHF}\n  Equity:Opening-Balances\n\n'
        '2025-03-01 * "Job" "Pay"\n  ref: "t:p"\n  Income:Job  -5000 CHF\n  Assets:Bank  5000 CHF\n\n'
        '2025-03-02 * "Coop" "Food"\n  ref: "t:f"\n  Expenses:Food  1000 CHF\n  Assets:Bank  -1000 CHF\n\n'
        "2025-06-30 price HOME 550000 CHF\n"
    )
    accounts = ACCOUNTS.replace(
        "2020-01-01 open Assets:Property:Home", "2020-01-01 open Assets:Property:Home"
    )
    (tmp_path / "x").mkdir()
    a = trends.annual(_tiny(tmp_path, text + "2020-01-01 price HOME 500000 CHF\n", accounts)).iloc[
        0
    ]
    assert a["from_saving"] == Decimal(4000) and a["from_markets_and_revaluation"] == Decimal(50000)
    assert a["net_worth_change"] == Decimal(54000)


def test_api(root):
    body = TestClient(create_app(root, backend=object())).get("/api/trends").json()
    assert [r["year"] for r in body["annual"]] == [2024, 2025, 2026] and body[
        "through"
    ] == "2026-09-01"
    assert set(body["overlay"]) == {"2024", "2025", "2026"} and len(body["overlay"]["2025"]) == 12
    assert len(body["rolling"]) == 23 and body["categories"]["keys"][0].startswith("Expenses:")
    assert Path(root).exists()
