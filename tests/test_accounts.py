from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from fin import demo, ledger, scaffold
from fin.analytics import accounts, networth
from fin.api.app import create_app
from fin.api.context import Context

D = Decimal


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    root = tmp_path_factory.mktemp("accounts") / "repo"
    demo.create_demo(root)
    return root


@pytest.fixture(scope="module")
def data(root):
    return Context(root).data()


@pytest.fixture(scope="module")
def overview(data):
    return accounts.accounts(data)


def bql(root, query):
    return ledger.query(root / "ledger" / "main.beancount", query)[1]


def find(overview, account):
    return next(
        a for i in overview["institutions"] for a in i["accounts"] if a["account"] == account
    )


def test_institutions_group_the_accounts(overview):
    names = {i["name"] for i in overview["institutions"]}
    assert names == {
        "UBS",
        "Wise",
        "IBKR",
        "Schwab",
        "Swisscard",
        "Pension",
        "Property",
        "Vehicle",
        "Receivable",
    }
    ibkr = next(i for i in overview["institutions"] if i["name"] == "IBKR")
    assert {a["name"] for a in ibkr["accounts"]} == {"BND", "Cash:USD", "VT"}
    values = [i["value"] for i in overview["institutions"]]
    assert values == sorted(values, reverse=True)  # biggest first, debts last


def test_the_institutions_add_up_to_net_worth(data, overview):
    nw = networth.total_by_date(networth.net_worth(data.led)).set_index("date")["value"]
    assert abs(overview["total"] - nw.iloc[-1]) <= D("0.05")
    assert overview["total"] == sum((i["value"] for i in overview["institutions"]), D(0)).quantize(
        D("0.01")
    ) or abs(overview["total"] - sum(i["value"] for i in overview["institutions"])) <= D("0.05")
    assert abs(overview["assets"] + overview["liabilities"] - overview["total"]) <= D("0.01")
    assert overview["liabilities"] < D(-676000)  # the mortgage and the card


def test_native_balances_match_bql(root, overview):
    mine = {
        (a["account"], p["commodity"]): p["units"]
        for i in overview["institutions"]
        for a in i["accounts"]
        for p in a["positions"]
    }
    rows = bql(
        root,
        "SELECT account, currency, sum(number) WHERE account ~ '^(Assets|Liabilities):' GROUP BY account, currency",
    )
    theirs = {(acct, ccy): total for acct, ccy, total in rows if total != 0}
    assert mine == theirs


def test_values_match_bql(root, overview):
    rows = bql(
        root,
        "SELECT account, convert(value(sum(position)), 'CHF') WHERE account ~ '^(Assets|Liabilities):' GROUP BY account",
    )
    for acct, cell in rows:
        positions = list(cell.get_positions()) if cell is not None else []
        want = positions[0].units.number if positions else D(0)
        assert abs(find(overview, acct)["value"] - want) <= D("0.005"), acct


def test_lots_of_the_same_shares_are_one_holding(overview):
    vt = find(overview, "Assets:IBKR:VT")
    assert (
        len(vt["positions"]) == 1
        and vt["positions"][0]["units"] == D(534)
        and vt["positions"][0]["lots"] > 20
    )


def test_trend_series_ends_at_the_current_value(overview):
    for i in overview["institutions"]:
        for a in i["accounts"]:
            assert (
                1 <= len(a["series"]) <= accounts.SERIES_POINTS
                and a["series"][-1]["value"] == a["value"]
            ), a["account"]
    home = find(overview, "Assets:Property:Home")
    assert home["series"][0]["value"] == D("1080000.00") and home["series"][-1]["value"] == D(
        "1140000.00"
    )


def test_activity_counts_match_bql(root, overview):
    for acct in ("Assets:UBS:Checking", "Assets:Wise:EUR", "Liabilities:Swisscard:MilesMore"):
        ids = {r[0] for r in bql(root, f"SELECT id WHERE account = '{acct}'")}
        assert find(overview, acct)["transactions"] == len(ids), acct
    assert find(overview, "Assets:UBS:Checking")["last_activity"].isoformat() == "2026-10-14"
    assert (
        find(overview, "Assets:UBS:Checking")["last_checked"] is None
    )  # importers add balance assertions later


# ---- one account's ledger ---------------------------------------------------------------------


def test_the_running_balance_is_the_cumulative_sum(data):
    led = accounts.account_ledger(data, "Assets:UBS:Checking")
    total = D(0)
    for r in led["rows"]:
        total += r["units"]
        assert r["balance"] == total
    assert led["balances"] == {"CHF": total}
    assert [r["date"] for r in led["rows"]] == sorted(r["date"] for r in led["rows"])


def test_ledger_agrees_with_bql(root, data):
    for acct, ccy in (
        ("Assets:UBS:Checking", "CHF"),
        ("Assets:Wise:EUR", "EUR"),
        ("Assets:IBKR:Cash:USD", "USD"),
    ):
        led = accounts.account_ledger(data, acct)
        (bal,) = bql(root, f"SELECT sum(number) WHERE account = '{acct}' AND currency = '{ccy}'")[0]
        count = len(bql(root, f"SELECT date WHERE account = '{acct}'"))
        assert led["rows"][-1]["balance"] == bal and len(led["rows"]) == count, acct


def test_a_month_carries_in_what_came_before(data):
    led = accounts.account_ledger(data, "Assets:UBS:Checking")
    month = accounts.month_view(led, "2026-03")
    inside = month["rows"]
    assert inside and all(r["date"].month == 3 and r["date"].year == 2026 for r in inside)
    assert month["balance_before"]["CHF"] + sum(r["units"] for r in inside) == inside[-1]["balance"]
    first = accounts.month_view(led, "2024-10")
    assert first["balance_before"] == {}  # nothing before the first month
    assert next(m["month"] for m in accounts.months_with_activity(led)) == "2024-10"


def test_each_row_says_what_the_other_side_was(data):
    rows = accounts.account_ledger(data, "Assets:UBS:Checking")["rows"]
    deposit = next(r for r in rows if r["narration"] == "Deposit")
    assert deposit["counterparty"] == "Assets:IBKR:Cash:USD" and deposit["kind"] == "transfer"
    pay = next(r for r in rows if r["narration"].startswith("Payslip"))
    assert (
        pay["counterparty"] == "Income:Employer:Salary"
        and pay["kind"] == "income"
        and pay["units"] > 5000
    )
    review = accounts.account_ledger(data, "Liabilities:Swisscard:MilesMore")["rows"]
    assert sum(r["needs_review"] for r in review) == 10


def test_securities_are_in_shares_with_a_value(data):
    rows = accounts.account_ledger(data, "Assets:IBKR:VT")["rows"]
    assert {r["commodity"] for r in rows} == {"VT"} and rows[-1]["balance"] == D(534)
    sale = next(r for r in rows if r["units"] == D(-20))
    assert sale["value"] < 0 and sale["balance"] == rows[rows.index(sale) - 1]["balance"] - 20


def test_an_account_without_postings_is_an_error(data):
    with pytest.raises(KeyError):
        accounts.account_ledger(data, "Assets:Nope")


# ---- the API ------------------------------------------------------------------------------------


@pytest.fixture
def client(root):
    return TestClient(create_app(root, backend=object()))


def test_api_overview_and_ledger(client):
    body = client.get("/api/accounts").json()
    assert body["total"] == "733099.92" and body["institutions"][0]["name"] == "Property"
    led = client.get("/api/accounts/ledger?account=Assets:Wise:EUR&month=2026-08").json()
    assert led["balance_before"] == {"EUR": "228.60"} and led["balances"] == {"EUR": "303.98"}
    assert led["info"]["name"] == "EUR" and led["matching"] == len(led["rows"]) == 6
    assert (
        led["rows"][0]["narration"] == "Convert CHF to EUR"
        and led["rows"][0]["counterparty"] == "Assets:Wise:CHF"
    )
    assert {m["month"] for m in led["months"]} >= {"2026-08"}
    assert (
        len(client.get("/api/accounts/ledger?account=Assets:UBS:Checking&limit=5").json()["rows"])
        == 5
    )
    assert client.get("/api/accounts/ledger?account=Assets:Nope").status_code == 404
    assert (
        client.get("/api/accounts/ledger?account=Assets:UBS:Checking&month=bad").status_code == 400
    )


def test_transactions_can_be_filtered_by_account_or_institution(client):
    inst = client.get("/api/transactions?month=all&account=Assets:Wise").json()
    one = client.get("/api/transactions?month=all&account=Assets:Wise:EUR").json()
    assert inst["matching"] > one["matching"] > 0
    assert all(
        "Wise" in r["account"] or any(p["account"].startswith("Assets:Wise") for p in r["postings"])
        for r in inst["rows"]
    )
    both = client.get("/api/transactions?month=2026-08&account=Assets:Wise:EUR&kind=expense").json()
    assert both["matching"] > 0 and all(
        r["kind"] == "expense" and r["date"].startswith("2026-08") for r in both["rows"]
    )


def test_an_account_filter_does_not_match_a_longer_name(tmp_path):
    root = tmp_path / "r"
    scaffold.init_data(root)
    (root / "ledger" / "accounts.beancount").write_text(
        "2020-01-01 open Assets:Bank CHF\n2020-01-01 open Assets:Bank2 CHF\n2020-01-01 open Income:Job CHF\n2020-01-01 open Equity:Opening-Balances\n"
    )
    (root / "ledger" / "manual" / "t.beancount").write_text(
        '2025-01-05 * "Job" "pay one"\n  ref: "t:1"\n  Income:Job  -100 CHF\n  Assets:Bank  100 CHF\n\n'
        '2025-01-06 * "Job" "pay two"\n  ref: "t:2"\n  Income:Job  -200 CHF\n  Assets:Bank2  200 CHF\n'
    )
    c = TestClient(create_app(root, backend=object()))
    rows = c.get("/api/transactions?month=all&account=Assets:Bank").json()["rows"]
    assert [r["narration"] for r in rows] == ["pay one"]
