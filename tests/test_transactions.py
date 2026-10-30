from collections import Counter
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from fin import config, demo, ledger
from fin.analytics import spend
from fin.analytics.transactions import transactions
from fin.api.app import create_app


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    root = tmp_path_factory.mktemp("txns") / "repo"
    demo.create_demo(root)
    return root


@pytest.fixture(scope="module")
def table(root):
    led = ledger.load(root / "ledger" / "main.beancount")
    return transactions(led, spend.build(led, config.load_settings(root, led)))


def row(table, narration, **eq):
    sel = table[table["narration"].str.contains(narration)]
    for k, v in eq.items():
        sel = sel[sel[k] == v]
    return sel.iloc[0]


def test_one_row_per_transaction_matching_bql(root, table):
    main = root / "ledger" / "main.beancount"
    per_month = {
        f"{y}-{m:02d}": n
        for y, m, n in ledger.query(
            main,
            "SELECT year, month, count(*) FROM entries WHERE type = 'transaction' GROUP BY year, month",
        )[1]
    }
    mine = Counter(f"{d.year}-{d.month:02d}" for d in table["date"])
    assert dict(mine) == per_month and len(table) == sum(per_month.values())


def test_kinds_are_what_a_person_would_call_them(table):
    assert row(table, "Mortgage payment")["kind"] == "expense"
    assert row(table, "Payslip")["kind"] == "income" and row(table, "Payslip")["amount"] == Decimal(
        "11000.00"
    )
    assert (
        row(table, "Convert CHF to EUR")["kind"] == "transfer"
    )  # a conversion that costs a fee is not an expense
    assert row(table, "Top up")["kind"] == "transfer"
    buy = row(table, "Buy ")
    assert (
        buy["kind"] == "investment" and buy["category"] == "VT" and buy["amount"] > 1000
    )  # not the CHF 0.88 fee
    for what in ("vest 40 ACME", "ESPP purchase", "VT dividend"):
        assert row(table, what)["kind"] == "income", what
    assert row(table, "Sale of")["kind"] == "investment"
    assert set(table["kind"]) == {"expense", "income", "transfer", "investment", "asset", "opening"}


def test_signs_and_currency_conversion(table):
    renovation = row(table, "Kitchen renovation")
    assert renovation["amount"] == Decimal("-18000.00") and renovation["major"]
    car = row(table, "Car purchase")  # an asset bought, not money spent
    assert (
        car["kind"] == "asset" and car["category"] == "CAR" and car["amount"] == Decimal("24500.00")
    )
    refund = row(table, "Refund")
    assert refund["amount"] > 0  # a refund is money back
    hotel = table[(table["category"] == "Expenses:Travel:Hotels")].iloc[0]
    assert (
        hotel["orig_ccy"] == "EUR"
        and 0.8 < float(hotel["amount"]) / -float(hotel["orig_amount"]) < 1.1
    )


def test_labels_and_flags_match_the_spend_model(root, table):
    led = ledger.load(root / "ledger" / "main.beancount")
    model = spend.build(led, config.load_settings(root, led))
    exp = table[table["kind"] == "expense"].set_index("ref")
    post = model.postings.groupby("ref").agg(total=("amount", "sum"))
    assert (exp["amount"] + post.loc[exp.index, "total"]).abs().max() <= Decimal(
        "0.005"
    )  # same amounts, opposite sign
    assert row(table, "Kitchen renovation")["major"] and not row(table, "Mortgage payment")["major"]
    assert row(table, "Mortgage payment")["recurring"]
    assert row(table, "Subscription", payee="Netflix")["cadence"] == "recurring"


def test_the_review_queue_is_the_uncategorized_ones(table):
    review = table[table["needs_review"]]
    assert len(review) == 10 and set(review["category"]) == {"Expenses:Uncategorized"}
    assert all("review" in tags for tags in review["tags"])


def test_every_row_can_be_traced_to_its_file(table):
    assert (
        table["ref"].notna().all() and table["ref"].is_unique
    )  # every transaction can be addressed by its ref
    assert all(f.startswith("ledger/") for f in table["file"])
    assert all(isinstance(p, list) and len(p) >= 2 for p in table["postings"])


@pytest.fixture
def client(root):
    return TestClient(create_app(root, backend=object()))


def test_api_defaults_to_the_latest_month(client):
    r = client.get("/api/transactions").json()
    assert r["month"] == "2026-10" and r["summary"]["count"] == r["matching"] == len(r["rows"])
    assert {x["date"][:7] for x in r["rows"]} == {"2026-10"}
    assert [m["month"] for m in r["months"]][:2] == ["2024-10", "2024-11"] and sum(
        m["count"] for m in r["months"]
    ) == 896
    dates = [x["date"] for x in r["rows"]]
    assert dates == sorted(dates, reverse=True)


def test_api_month_summary_ignores_filters_but_matching_does_not(client):
    base = client.get("/api/transactions?month=2026-03").json()
    filtered = client.get("/api/transactions?month=2026-03&kind=expense&q=zalando").json()
    assert filtered["summary"] == base["summary"] and 0 < filtered["matching"] < base["matching"]
    assert all(
        x["kind"] == "expense" and "zalando" in (x["payee"] + x["narration"]).lower()
        for x in filtered["rows"]
    )
    expenses = sum(Decimal(x["amount"]) for x in base["rows"] if x["kind"] == "expense")
    assert (
        Decimal(base["summary"]["expenses"]) == expenses
        and Decimal(base["summary"]["expenses"]) < -18000
    )  # the car is in March


def test_api_filters(client):
    review = client.get("/api/transactions?month=all&review=true").json()
    assert review["matching"] == 10 == review["review_total"] and len(review["rows"]) == 10
    assert Decimal(review["summary"]["needs_review_spend"]) > 0
    major = client.get("/api/transactions?month=all&flag=major").json()
    assert {x["payee"] for x in major["rows"]} == {"Küchen Schmid AG", "IKEA", "Digitec"}
    rec = client.get("/api/transactions?month=2026-05&flag=recurring").json()
    assert rec["matching"] > 0 and all(x["recurring"] for x in rec["rows"])
    cat = client.get("/api/transactions?month=all&category=Expenses:Food:Groceries").json()
    assert cat["matching"] > 100
    capped = client.get("/api/transactions?month=all&limit=5").json()
    assert len(capped["rows"]) == 5 and capped["truncated"] and capped["matching"] == 896


def test_api_rejects_bad_input(client):
    for bad in ("month=2026-13", "month=march", "kind=bogus", "flag=x"):
        assert client.get(f"/api/transactions?{bad}").status_code == 400, bad


def test_api_breaks_expenses_down_by_group_or_category(client):
    top = client.get("/api/transactions?month=2026-05&kind=expense").json()
    groups = {c["category"]: c for c in top["by_category"]}
    assert all(len(k.split(":")) == 2 for k in groups)  # Expenses:Food, one level
    spent = -sum(Decimal(x["amount"]) for x in top["rows"])
    assert sum(Decimal(c["amount"]) for c in groups.values()) == spent
    assert sum(c["count"] for c in groups.values()) == top["matching"]
    amounts = [Decimal(c["amount"]) for c in top["by_category"]]
    assert amounts == sorted(amounts, reverse=True)
    deep = client.get("/api/transactions?month=2026-05&kind=expense&depth=3").json()["by_category"]
    assert len(deep) > len(groups) and {c["category"].split(":")[1] for c in deep} >= {
        "Food",
        "Housing",
    }
    assert sum(Decimal(c["amount"]) for c in deep) == spent


def test_api_filters_by_several_categories_at_once(client):
    url = "/api/transactions?month=all&kind=expense&limit=5000"
    food = client.get(f"{url}&category=Expenses:Food").json()
    housing = client.get(f"{url}&category=Expenses:Housing").json()
    both = client.get(f"{url}&category=Expenses:Food,Expenses:Housing").json()
    assert both["matching"] == food["matching"] + housing["matching"] > 0
    assert {x["category"].split(":")[1] for x in both["rows"]} == {"Food", "Housing"}
    mixed = client.get(f"{url}&category=Expenses:Food:Dining,Expenses:Housing").json()
    assert {x["category"] for x in mixed["rows"] if x["category"].startswith("Expenses:Food")} == {
        "Expenses:Food:Dining"
    }
    # the breakdown does not shrink to the selection, so more can be ticked
    assert both["by_category"] == client.get(url).json()["by_category"]
    # a trailing comma, spaces and an empty selection are harmless
    assert client.get(f"{url}&category=Expenses:Food,").json()["matching"] == food["matching"]
    assert client.get(f"{url}&category=").json()["matching"] == client.get(url).json()["matching"]


def test_api_category_filter_matches_whole_levels_and_cadence(client):
    r = client.get("/api/transactions?month=all&category=Expenses:Foo").json()
    assert r["matching"] == 0  # a prefix of a name is not a category
    rec = client.get("/api/transactions?month=all&kind=expense&cadence=recurring").json()
    assert rec["matching"] > 0 and {x["cadence"] for x in rec["rows"]} == {"recurring"}
    ess = client.get("/api/transactions?month=all&kind=expense&necessity=essential").json()
    assert ess["matching"] > 0 and {x["necessity"] for x in ess["rows"]} == {"essential"}


def test_money_sent_to_an_account_that_is_not_imported_is_a_transfer_of_what_left():
    from datetime import date
    from decimal import Decimal as D

    from beancount import loader
    from beancount.core import convert, data

    from fin import registry
    from fin.analytics.transactions import _classify
    from fin.analytics.valuation import Market

    entries, errors, _ = loader.load_string(
        """
2020-01-01 open Assets:Bank:Main
2020-01-01 open Equity:Closed-Accounts:Old
2020-11-15 * "Bank" "sent to my closed account"
  Assets:Bank:Main  -66.00 CHF
  Equity:Closed-Accounts:Old  66.00 CHF
"""
    )
    assert not errors
    led = ledger.Ledger(entries, errors, {}, None)
    txn = next(e for e in entries if isinstance(e, data.Transaction))
    market = Market.of(entries, "CHF")
    value = {id(p): market.convert(convert.get_weight(p), date(2020, 11, 15)) for p in txn.postings}
    kind, primary, amount = _classify(list(txn.postings), value, registry.of(led))
    assert (kind, primary) == ("transfer", None)
    assert amount == D("66.00") and isinstance(
        amount, D
    )  # not the int 0 that broke the Accounts pages
