import math
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from fin import demo, ledger, ledger_settings, scaffold
from fin.analytics import fire
from fin.api.app import create_app
from fin.api.context import Context
from fin.gitops import git, is_dirty

D = Decimal


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    root = tmp_path_factory.mktemp("fire") / "repo"
    demo.create_demo(root)
    return root


@pytest.fixture(scope="module")
def data(root):
    return Context(root).data()


# ---- the projection maths, against closed forms ------------------------------------------------


def test_simulation_matches_the_compound_interest_formula():
    r, c, a0 = D("0.04"), D(1000), D(50000)
    path = fire.simulate(a0, c, D(0), D(0), D(0), D(0), D(0), r, False, 0, False, months=120)
    g = (1 + float(r)) ** (1 / 12)
    expected = float(a0) * g**120 + float(c) * (g**120 - 1) / (g - 1)
    assert abs(float(path[120]) - expected) < 0.01 and path[0] == a0


def test_pension_joins_only_from_its_access_month():
    args = (D(1000), D(0), D(5000), D(0), D(0), D(0), D(0), D(0))
    never = fire.simulate(
        *args, include_pension=False, pension_from_month=3, include_home=False, months=6
    )
    gated = fire.simulate(
        *args, include_pension=True, pension_from_month=3, include_home=False, months=6
    )
    assert never == [D(1000)] * 7
    assert gated[:3] == [D(1000)] * 3 and gated[3:] == [D(6000)] * 4


def test_home_equity_grows_by_principal_until_the_debt_is_gone():
    path = fire.simulate(
        D(0), D(0), D(0), D(0), D(1000), D(400), D(1000), D(0), False, 0, True, months=5
    )
    assert path == [
        D(1000),
        D(1400),
        D(1800),
        D(2000),
        D(2000),
        D(2000),
    ]  # 400, 400, then the last 200


# ---- a ledger with a known answer --------------------------------------------------------------

ACCOUNTS = """2020-01-01 open Assets:Bank CHF
2020-01-01 open Income:Job CHF
2020-01-01 open Expenses:Food CHF
  cadence: "regular"
  necessity: "essential"
2020-01-01 open Equity:Opening-Balances
"""


def simple(tmp_path, pay=5000, food=1000, fire_lines=""):
    """Twelve months of CHF 5,000 pay and CHF 1,000 food, opening cash 1,000, no markets."""
    root = tmp_path / "simple"
    scaffold.init_data(root)
    (root / "ledger" / "accounts.beancount").write_text(ACCOUNTS)
    parts = [
        '2024-12-31 * "Open" "Open"\n  ref: "t:o"\n  Assets:Bank  1000 CHF\n  Equity:Opening-Balances\n'
    ]
    for m in range(1, 13):
        parts.append(
            f'2025-{m:02d}-10 * "Coop" "Food"\n  ref: "t:f{m}"\n  Expenses:Food  {food} CHF\n  Assets:Bank  -{food} CHF\n'
        )
        parts.append(
            f'2025-{m:02d}-25 * "Job" "Pay"\n  ref: "t:p{m}"\n  Income:Job  -{pay} CHF\n  Assets:Bank  {pay} CHF\n'
        )
    parts.append(
        '2025-12-31 * "Coop" "Food"\n  ref: "t:last"\n  Expenses:Food  0.01 CHF\n  Assets:Bank  -0.01 CHF\n'
    )
    (root / "ledger" / "manual" / "all.beancount").write_text("\n".join(parts) + "\n" + fire_lines)
    return Context(root).data()


def test_known_ledger_gives_the_known_years(tmp_path):
    d = simple(tmp_path)
    a = fire.Assumptions(withdrawal_rate=D("0.04"), real_return=D(0))
    s = fire.status(d, a)
    # 12 x 1,000 food, plus the cent that keeps December a complete month
    assert s["basis"]["months_used"] == 12 and s["basis"]["annual_spend"] == D("12000.01")
    assert s["fi_number"] == D("300000.25")  # 12,000.01 / 4%
    assert s["assets"]["investable"] == D("48999.99")  # 1,000 + 12 x (5,000 - 1,000) - 0.01
    net = D(12 * 4000) - D("0.01")
    assert s["contributions"]["investable_monthly"] == (net / 12).quantize(D("0.01"))
    months_needed = math.ceil(
        (D("300000.25") - D("48999.99")) / (net / 12)
    )  # no returns: a straight line
    base = next(x for x in s["scenarios"] if x["key"] == "base")
    assert months_needed == 63 and base["years_to_fi"] == fire._years(63)
    assert base["fi_date"] == date(2031, 3, 1)  # 63 months after December 2025


def test_nothing_saved_means_never(tmp_path):
    d = simple(tmp_path, pay=1000, food=1000)
    s = fire.status(d, fire.Assumptions(real_return=D(0)))
    base = next(x for x in s["scenarios"] if x["key"] == "base")
    assert base["years_to_fi"] is None and base["fi_date"] is None
    assert s["progress"] < 1 and [c for c in s["savings_curve"] if c["years_to_fi"] is None]


def test_a_target_spend_overrides_what_you_actually_spend(tmp_path):
    d = simple(tmp_path)
    s = fire.status(
        d, fire.Assumptions(withdrawal_rate=D("0.04"), target_spend=D(100000), real_return=D(0))
    )
    assert s["fi_number"] == D("2500000.00") and s["basis"]["spend_source"] == "target"
    assert s["basis"]["measured_annual_spend"] == D(
        "12000.01"
    )  # we still show what you really spent


# ---- on the demo ledger ------------------------------------------------------------------------


def test_assumptions_come_from_the_ledger(data):
    a = fire.from_ledger(data)
    assert (a.withdrawal_rate, a.real_return, a.birth_year, a.pension_access_age, a.target_age) == (
        D("0.035"),
        D("0.04"),
        1988,
        60,
        55,
    )
    assert a.target_spend is None


def test_contributions_leave_out_returns_and_one_offs(data):
    s = fire.status(data, fire.from_ledger(data))
    months = fire._months_window(data)
    cf = data.cashflow[data.cashflow["month"].isin(months)]
    total = lambda c: sum(cf[c], D(0))
    returns = total("investment_income") + total("realized_gains")
    assert returns > 0
    expected = (
        total("net") - returns + total("major") - D(7200) - D(12000)
    ) / 12  # pension and principal go elsewhere
    assert s["contributions"]["investable_monthly"] == expected.quantize(D("0.01"))
    assert s["contributions"]["pension_monthly"] == D("600.00") and s["contributions"][
        "mortgage_principal_monthly"
    ] == D("1000.00")
    assert s["basis"]["one_off_purchases_left_out"] == D(
        "44600.00"
    )  # renovation, laptop and sofa-free year: 20,100 + the car


def test_flows_match_bql(root, data):
    main = root / "ledger" / "main.beancount"
    (pension,) = ledger.query(
        main,
        "SELECT sum(number) WHERE account ~ '^Assets:Pension' AND number > 0 AND date >= 2025-10-01 AND date <= 2026-09-30",
    )[1][0]
    (principal,) = ledger.query(
        main,
        "SELECT sum(number) WHERE account ~ '^Liabilities:Mortgage' AND number > 0 AND date >= 2025-10-01 AND date <= 2026-09-30",
    )[1][0]
    flows = fire._flows(data, date(2025, 10, 1), date(2026, 9, 30))
    assert (
        flows["pension"] == pension
        and flows["principal"] == principal
        and flows["fixed_assets"] == D(24500)
    )


def test_scenarios_are_ordered_and_the_curve_agrees_with_the_headline(data):
    a = fire.from_ledger(data)
    s = fire.status(data, a)
    low, base, high = (x["years_to_fi"] for x in s["scenarios"])
    assert low > base > high
    years = [c["years_to_fi"] for c in s["savings_curve"]]
    finite = [y for y in years if y is not None]
    assert (
        finite == sorted(finite, reverse=True) and years[0] is None or years[0] > years[-1]
    )  # save more, get there sooner
    # the curve at your actual savings rate is the base scenario
    rate = s["contributions"]["savings_rate"]
    income_year = s["contributions"]["investable_monthly"] * 12 / rate
    spend_s = (1 - rate) * income_year - (
        s["contributions"]["pension_monthly"] + s["contributions"]["mortgage_principal_monthly"]
    ) * 12
    path = fire.simulate(
        s["assets"]["counted"],
        rate * income_year / 12,
        D(0),
        D(0),
        D(0),
        D(0),
        D(0),
        a.real_return,
        False,
        0,
        False,
    )
    at_rate = fire._years(fire._first_reaching(path, spend_s / a.withdrawal_rate))
    assert abs(at_rate - base) <= D("0.1")


def test_pension_counts_only_from_its_access_age(data):
    young = fire.status(data, fire.Assumptions(birth_year=1988), include_pension=True)
    assert (
        young["assets"]["counted"] == young["assets"]["investable"]
    )  # about 38: the pension is locked
    assert young["pension"]["months_until_access"] > 12 * 20
    old = fire.status(data, fire.Assumptions(birth_year=1960), include_pension=True)
    assert old["pension"]["months_until_access"] == 0
    assert old["assets"]["counted"] == old["assets"]["investable"] + old["assets"]["pension"]


def test_home_equity_is_optional(data):
    a = fire.from_ledger(data)
    without = fire.status(data, a)
    with_home = fire.status(data, a, include_home=True)
    assert (
        with_home["assets"]["counted"] - without["assets"]["counted"]
        == D("464000.00")
        == with_home["assets"]["home_equity"]
    )
    assert with_home["progress"] > without["progress"]
    base = lambda s: next(x for x in s["scenarios"] if x["key"] == "base")["years_to_fi"]
    assert base(with_home) < base(without)


def test_coast_fire_uses_age_and_the_return(data):
    s = fire.status(data, fire.from_ledger(data))
    c = s["coast"]
    assert c["target_age"] == 55 and D("16") < c["years_left"] < D("18")
    assert c["number"] < s["fi_number"] and c["reached"] == (s["assets"]["counted"] >= c["number"])
    assert fire.status(data, fire.Assumptions())["coast"] is None  # no birth year, no coast


def test_projection_points_start_today_and_end_past_the_goal(data):
    s = fire.status(data, fire.from_ledger(data))
    proj = s["projection"]
    assert proj[0]["base"] == s["assets"]["counted"] and proj[0]["date"] == date(2026, 10, 1)
    assert proj[-1]["base"] > s["fi_number"] and all(p["fi_number"] == s["fi_number"] for p in proj)
    assert all(a["low"] <= a["base"] <= a["high"] for a in proj)


# ---- saving assumptions into the ledger --------------------------------------------------------


def test_assumptions_are_saved_as_dated_ledger_lines(tmp_path):
    root = tmp_path / "repo"
    demo.create_demo(root)
    ledger_settings.record_fire(root, "withdrawal-rate", "0.04", day=date(2026, 10, 17))
    ledger_settings.record_fire(root, "target-spend", 60000, day=date(2026, 10, 17))
    text = (root / ledger_settings.FIRE_FILE).read_text()
    assert '2026-10-17 custom "fin-fire" "withdrawal-rate" 0.04' in text
    assert '2026-10-17 custom "fin-fire" "target-spend" 60000 CHF' in text
    assert not is_dirty(root) and "fire: target-spend" in git(root, "log", "-1", "--format=%s")
    led = Context(root).data().led
    a = fire.Assumptions.from_ledger(ledger_settings.read(led).fire)
    assert a.withdrawal_rate == D("0.04") and a.target_spend == D(60000)


def test_bad_values_are_refused_everywhere(tmp_path):
    root = tmp_path / "repo"
    demo.create_demo(root)
    before = (root / ledger_settings.FIRE_FILE).exists()
    for param, value in (
        ("withdrawal-rate", "0.5"),
        ("real-return", "-1"),
        ("birth-year", "1800"),
        ("nonsense", "1"),
    ):
        with pytest.raises(ValueError):
            ledger_settings.record_fire(root, param, value)
    assert (root / ledger_settings.FIRE_FILE).exists() == before and not is_dirty(root)
    (root / "ledger" / "manual" / "bad.beancount").write_text(
        '2026-01-01 custom "fin-fire" "withdrawal-rate" 3\n\n2026-01-02 custom "fin-fire" "colour" 1\n'
    )
    led = Context(root).load_ledger()
    assert len(ledger_settings.read(led).problems) == 2


# ---- the API -----------------------------------------------------------------------------------


def test_api_reports_and_tries_out_assumptions_without_saving(tmp_path):
    root = tmp_path / "repo"
    demo.create_demo(root)
    c = TestClient(create_app(root, backend=object()))
    base = c.get("/api/fire").json()
    assert (
        base["status"]["ready"]
        and not base["differs_from_saved"]
        and base["saved"]["withdrawal_rate"] == "0.035"
    )
    tried = c.get("/api/fire?withdrawal_rate=0.04&include_home=true").json()
    assert tried["differs_from_saved"] and D(tried["status"]["fi_number"]) < D(
        base["status"]["fi_number"]
    )
    assert c.get("/api/fire").json()["saved"]["withdrawal_rate"] == "0.035"  # nothing was written
    assert not (root / ledger_settings.FIRE_FILE).exists()
    assert (
        c.get("/api/fire?withdrawal_rate=0.5").status_code == 400
        and c.get("/api/fire?birth_year=1700").status_code == 400
    )
    assert "withdrawal_rate" in base["limits"]


def test_api_saves_only_what_changed(tmp_path):
    root = tmp_path / "repo"
    demo.create_demo(root)
    c = TestClient(create_app(root, backend=object()))
    saved = c.put(
        "/api/fire/settings",
        json={"withdrawal_rate": "0.04", "real_return": "0.04", "target_age": 50},
    ).json()
    assert saved["saved"]["withdrawal_rate"] == "0.04" and saved["saved"]["target_age"] == 50
    lines = (root / ledger_settings.FIRE_FILE).read_text().splitlines()
    assert [ln.split('"')[3] for ln in lines if ln] == [
        "withdrawal-rate",
        "target-age",
    ]  # real-return was unchanged
    assert c.put("/api/fire/settings", json={"withdrawal_rate": "0.9"}).status_code == 400
    assert c.get("/api/fire").json()["saved"]["withdrawal_rate"] == "0.04"


def test_numbers_are_written_as_plain_ledger_tokens(tmp_path):
    root = tmp_path / "repo"
    demo.create_demo(root)
    for param, value, shown in (
        ("target-age", 50, "50"),
        ("birth-year", D("1990"), "1990"),
        ("pension-access-age", "60", "60"),
        ("withdrawal-rate", "0.0350", "0.035"),
        ("real-return", D("0.045"), "0.045"),
        ("target-spend", D("60000.50"), "60000.50 CHF"),
    ):
        ledger_settings.record_fire(root, param, value, day=date(2026, 10, 17))
        last = (root / ledger_settings.FIRE_FILE).read_text().splitlines()[-1]
        assert last.endswith(f'"{param}" {shown}'), param
    a = fire.Assumptions.from_ledger(ledger_settings.read(Context(root).load_ledger()).fire)
    assert (a.target_age, a.birth_year, a.withdrawal_rate, a.target_spend) == (
        50,
        1990,
        D("0.035"),
        D("60000.50"),
    )
