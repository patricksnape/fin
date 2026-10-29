from datetime import date
from decimal import Decimal

import pytest

from fin import config, demo, ledger
from fin.analytics import spend

SERAFE = "serafe|Expenses:Government:Serafe"


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    root = tmp_path_factory.mktemp("spend") / "repo"
    demo.create_demo(root)
    return root


def model(root, confirmed=frozenset()):
    led = ledger.load(root / "ledger" / "main.beancount")
    return spend.build(led, config.load_settings(root, led), confirmed=set(confirmed))


@pytest.fixture(scope="module")
def m(root):
    return model(root, {SERAFE})


def series(m):
    return {s.id: s for s in m.series}


def test_detects_expected_series(m):
    s = series(m)
    assert s["ubs hypothek|Expenses:Housing:MortgageInterest"].period == "monthly"
    assert s["netflix|Expenses:Subscriptions:Streaming"].typical_amount == Decimal("13.90")
    assert s[SERAFE].period == "yearly"
    # groceries vary too much to be a series
    assert not any("Groceries" in sid for sid in s)


def test_yearly_series_needs_confirmation(root):
    s = series(model(root))
    assert s[SERAFE].status == "proposed"
    assert not s[SERAFE].applies


def test_gym_lapsed_after_cancellation(m):
    gym = series(m)["fitnesspark|Expenses:Subscriptions:Gym"]
    assert gym.status == "lapsed" and gym.last_date == date(2026, 3, 9)


def test_price_increases_detected_variable_bills_ignored(m):
    s = series(m)
    assert [(c[1], c[2]) for c in s["spotify|Expenses:Subscriptions:Streaming"].price_changes] == [
        (Decimal("12.90"), Decimal("14.90"))
    ]
    assert len(s["health insurer ag|Expenses:Insurance:Health"].price_changes) == 1
    assert s["ewz|Expenses:Housing:Utilities"].price_changes == []


def test_major_purchases_flagged_rent_is_not(m):
    major = spend.major_purchases(m)
    assert set(major["payee"]) == {"Küchen Schmid AG", "IKEA", "Digitec"}
    rent = m.postings[m.postings["account"] == "Expenses:Housing:MortgageInterest"]
    assert not rent["major"].any() and (rent["cadence"] == "recurring").all()


def test_major_excluded_from_spend_but_taxes_and_payroll_are_too(m):
    march = spend.spend_by(m, "total")
    row = march[march["month"] == date(2026, 3, 1)]["amount"].iloc[0]
    assert Decimal(3000) < row < Decimal(7000)  # no CHF 24.5k car, no tax installment
    cats = set(spend.spend_by(m, "category")["key"])
    assert not any(c.startswith(("Expenses:Taxes", "Expenses:Social")) for c in cats)


def test_yearly_bill_spread_in_run_rate_but_not_cash(m):
    def serafe_rows(view):
        rows = spend.monthly_rows(m, view)
        return rows[rows["account"] == "Expenses:Government:Serafe"]

    rr = serafe_rows("run_rate")
    jan26_oct26 = rr[rr["month"] >= date(2026, 1, 1)]
    assert len(jan26_oct26) == 10 and set(jan26_oct26["amount"]) == {Decimal("27.92")}
    cash = serafe_rows("cash")
    assert list(cash[cash["month"] == date(2026, 1, 1)]["amount"]) == [Decimal("335.00")]
    assert not cash[cash["month"] == date(2026, 2, 1)]["amount"].any()


def test_foreign_currency_converted_to_chf(m):
    eur = m.postings[m.postings["orig_ccy"] == "EUR"]
    assert len(eur) > 0
    ratios = (eur["amount"] / eur["orig_amount"]).astype(float)
    assert ratios.between(0.8, 1.1).all()


def test_refund_nets_against_category(m):
    rows = spend.monthly_rows(m, "cash")
    june = rows[
        (rows["month"] == date(2026, 6, 1)) & (rows["account"] == "Expenses:Shopping:Clothing")
    ]
    assert (june["amount"] < 0).any()


def test_headline_matches_manual_average(m):
    total = spend.spend_by(m, "total")
    window = [date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)]
    expected = (total[total["month"].isin(window)]["amount"].sum() / 3).quantize(Decimal("0.01"))
    h = spend.headline(m)
    assert h["core_monthly_spend"] == expected
    assert h["major_this_year"] == Decimal("20100.00")
    assert abs(h["essential_baseline"] + h["discretionary"] - h["core_monthly_spend"]) <= Decimal(
        "0.01"
    )


def test_next_due_follows_the_calendar(m):
    s = series(m)
    assert s["ubs hypothek|Expenses:Housing:MortgageInterest"].next_expected == date(2026, 11, 1)
    assert s[SERAFE].next_expected == date(2027, 1, 20)


def test_fees_are_never_recurring_series(m):
    assert not any("Fees" in sid for sid in series(m))


def test_an_account_can_say_its_large_payments_are_not_major(tmp_path):
    from fin import account_admin

    root = tmp_path / "repo"
    demo.create_demo(root)
    before = spend.major_purchases(model(root))
    shop = before.iloc[0]["account"]
    account_admin.set_account(root, shop, {"major": False})
    assert "major: FALSE" in (root / "ledger" / "accounts.beancount").read_text()
    after = spend.major_purchases(model(root))
    assert shop not in set(after["account"]) and len(after) < len(before)
    account_admin.set_account(root, shop, {"major": None})  # removing the setting restores it
    assert len(spend.major_purchases(model(root))) == len(before)


def test_monthly_headline_averages_to_the_headline(m):
    rows = {r["month"]: r for r in spend.monthly_headline(m)}
    window = ["2026-07", "2026-08", "2026-09"]
    assert (sum(rows[w]["core"] for w in window) / 3).quantize(Decimal("0.01")) == spend.headline(
        m
    )["core_monthly_spend"]
