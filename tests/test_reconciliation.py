"""Our analytics must agree with Beancount's own query engine (BQL) on the same ledger.

The ledger is the source of truth; if these ever disagree, our code has drifted from it.
"""

from decimal import Decimal

import pytest

from fin import config, demo, ledger
from fin.analytics import allocation, cashflow, networth, spend
from fin.analytics.postings import expense_postings


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("recon") / "repo"
    demo.create_demo(root)
    main = root / "ledger" / "main.beancount"
    led = ledger.load(main)
    return main, led, config.load_settings(root, led)


def bql(main, query):
    return ledger.query(main, query)[1]


def test_expense_totals_per_account_match_bql(env):
    main, led, _ = env
    df = expense_postings(led)
    for ccy in ("CHF", "EUR", "USD"):
        want = dict(
            bql(
                main,
                f"SELECT account, sum(number) WHERE account ~ '^Expenses:' AND currency = '{ccy}' GROUP BY account",
            )
        )
        mine = df[df["orig_ccy"] == ccy].groupby("account")["orig_amount"].sum().to_dict()
        assert mine == want, ccy


def test_balances_of_cash_accounts_match_bql(env):
    main, led, settings = env
    held = allocation.holdings(led, settings.base_currency).set_index("account")
    for account, ccy in (
        ("Assets:UBS:Checking", "CHF"),
        ("Assets:IBKR:Cash:USD", "USD"),
        ("Assets:Wise:EUR", "EUR"),
    ):
        (want,) = bql(
            main, f"SELECT sum(number) WHERE account = '{account}' AND currency = '{ccy}'"
        )[0]
        assert held.loc[account, "units"] == want, account


def test_security_units_match_bql(env):
    main, led, settings = env
    held = allocation.holdings(led, settings.base_currency).set_index("account")
    for account, sym in (
        ("Assets:IBKR:VT", "VT"),
        ("Assets:Schwab:ACME", "ACME"),
        ("Assets:IBKR:BND", "BND"),
    ):
        (want,) = bql(
            main, f"SELECT sum(number) WHERE account = '{account}' AND currency = '{sym}'"
        )[0]
        assert held.loc[account, "units"] == want, account


def test_pension_and_card_balances_match_bql(env):
    main, led, settings = env
    nw = networth.net_worth(led, settings.base_currency)
    latest = nw[nw["date"] == nw["date"].max()].set_index("group")["value"]
    (pension,) = bql(
        main, "SELECT sum(number) WHERE account = 'Assets:Pension:BVG' AND currency = 'CHF'"
    )[0]
    (card,) = bql(
        main,
        "SELECT sum(number) WHERE account = 'Liabilities:Swisscard:MilesMore' AND currency = 'CHF'",
    )[0]
    (ubs_card,) = bql(
        main,
        "SELECT sum(number) WHERE account = 'Liabilities:UBS:CreditCard' AND currency = 'CHF'",
    )[0]
    (mortgage,) = bql(
        main, "SELECT sum(number) WHERE account = 'Liabilities:Mortgage:UBS' AND currency = 'CHF'"
    )[0]
    assert latest["Pension"] == pension and latest["Liabilities"] == card + ubs_card + mortgage


def test_salary_income_matches_bql(env):
    main, led, settings = env
    model = spend.build(led, settings)
    (want,) = bql(
        main,
        "SELECT -sum(number) WHERE account ~ '^Income:Employer:(Salary|Bonus)' AND currency = 'CHF'",
    )[0]
    assert cashflow.cashflow(led, model)["salary"].sum() == want.quantize(Decimal("0.01"))


def _chf(cell):
    """The CHF number in a BQL value cell (an inventory with one position, or empty)."""
    positions = list(cell.get_positions()) if cell is not None else []
    return positions[0].units.number if positions else Decimal(0)


def test_market_value_per_holding_matches_bql(env):
    main, led, settings = env
    mine = allocation.holdings(led, settings.base_currency).set_index("account")["value"]
    rows = bql(
        main,
        "SELECT account, convert(value(sum(position)), 'CHF') WHERE account ~ '^Assets:(IBKR|Schwab|UBS|Wise)' GROUP BY account",
    )
    theirs = {a: _chf(c) for a, c in rows if _chf(c)}
    assert set(mine.index) == set(theirs)
    for account, value in mine.items():
        assert abs(value - theirs[account]) <= Decimal("0.005"), account


@pytest.mark.parametrize("day", ["2025-06-30", "2026-03-31", "2026-09-30"])
def test_net_worth_on_past_dates_matches_bql(env, day):
    main, led, settings = env
    nw = networth.net_worth(led, settings.base_currency)
    ours = networth.total_by_date(nw).set_index("date")["value"]
    d = __import__("datetime").date.fromisoformat(day)
    (cell,) = bql(
        main,
        f"SELECT convert(value(sum(position), {day}), 'CHF', {day}) WHERE date <= {day} AND account ~ '^(Assets|Liabilities)'",
    )[0]
    assert abs(ours[d] - _chf(cell)) <= Decimal("0.01"), day
