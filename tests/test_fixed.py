from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from fin import config, demo, ledger
from fin.analytics import allocation, fixed, networth, spend
from fin.api.app import create_app


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    root = tmp_path_factory.mktemp("fixed") / "repo"
    demo.create_demo(root)
    return root


@pytest.fixture(scope="module")
def led(root):
    return ledger.load(root / "ledger" / "main.beancount")


def bql(root, query):
    return ledger.query(root / "ledger" / "main.beancount", query)[1]


def test_home_car_and_mortgage_by_your_own_valuations(led):
    f = fixed.fixed_assets(led)
    home, car = f["assets"]
    assert (home["value"], home["cost"], home["unrealized"]) == (
        Decimal(1140000),
        Decimal(1000000),
        Decimal(140000),
    )
    assert home["valued_on"] == date(2026, 10, 1) and not home["stale"]
    assert (car["value"], car["cost"]) == (
        Decimal(21800),
        Decimal(24500),
    )  # it lost value, and says so
    assert f["mortgage_balance"] == Decimal(
        700000 - 24 * 1000
    )  # 24 monthly payments of CHF 1,000 principal
    assert f["home_equity"] == Decimal(1140000) - f["mortgage_balance"]
    assert f["loan_to_value"] == (f["mortgage_balance"] / Decimal(1140000)).quantize(
        Decimal("0.0001")
    )


def test_valuations_match_bql(root, led):
    f = fixed.fixed_assets(led)
    for asset in f["assets"]:
        rows = bql(
            root,
            f"SELECT convert(value(sum(position)), 'CHF') WHERE account = '{asset['account']}'",
        )
        (cell,) = rows[0]
        assert abs(next(iter(cell.get_positions())).units.number - asset["value"]) <= Decimal(
            "0.005"
        ), asset["account"]
    (debt,) = bql(
        root, "SELECT sum(number) WHERE account = 'Liabilities:Mortgage:UBS' AND currency = 'CHF'"
    )[0]
    assert -debt == f["mortgage_balance"]


def test_an_old_valuation_is_flagged(led):
    soon = fixed.fixed_assets(led, as_of=date(2027, 6, 1))
    assert not soon["assets"][0]["stale"] and soon["assets"][0]["value"] == Decimal(1140000)
    late = fixed.fixed_assets(led, as_of=date(2028, 1, 1))
    assert late["assets"][0]["stale"]  # the latest estimate is over a year old: time to update it


def test_net_worth_has_property_vehicle_and_mortgage_groups(led):
    nw = networth.net_worth(led)
    latest = nw[nw["date"] == nw["date"].max()].set_index("group")["value"]
    assert latest["Property"] == Decimal("1140000.00") and latest["Vehicles"] == Decimal("21800.00")
    assert latest["Liabilities"] < Decimal(-676000)  # mortgage plus the card
    before = nw[nw["date"] == date(2026, 3, 31)].set_index("group")["value"]
    after = nw[nw["date"] == date(2026, 4, 30)].set_index("group")["value"]
    assert before["Vehicles"] == 0 and after["Vehicles"] == Decimal(
        "24500.00"
    )  # the car arrives as an asset


def test_homes_and_cars_are_not_investable(led):
    held = allocation.holdings(led)
    assert not any(a.startswith(("Assets:Property", "Assets:Vehicle")) for a in held["account"])
    assert held["value"].sum() < Decimal(200000)


def test_only_the_interest_is_spending_not_the_principal(root, led):
    model = spend.build(led, config.load_settings(root, led))
    interest = model.postings[
        model.postings["account"] == "Expenses:Housing:MortgageInterest"
    ].sort_values("date")
    assert interest["amount"].iloc[0] == Decimal("933.33")  # 700,000 at 1.6% / 12
    assert interest["amount"].is_monotonic_decreasing  # less debt, less interest
    assert not any(
        "Vehicle" in a for a in model.postings["account"]
    )  # the car purchase is not an expense
    assert "ubs hypothek|Expenses:Housing:MortgageInterest" in {
        s.id for s in model.series if s.status == "active"
    }


def test_api_exposes_fixed_assets(root):
    c = TestClient(create_app(root, backend=object()))
    body = c.get("/api/networth").json()
    assert {"Property", "Vehicles"} <= set(body["groups"])
    fx = body["fixed"]
    assert fx["home_equity"] == "464000.00" and [a["commodity"] for a in fx["assets"]] == [
        "HOME",
        "CAR",
    ]
    assert fx["assets"][0]["valued_on"] == "2026-10-01"
