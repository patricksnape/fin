from datetime import date
from decimal import Decimal as D

import pytest
from beancount import loader

from fin.analytics import rsu

LEDGER = """
option "operating_currency" "CHF"
2020-01-01 open Assets:Schwab:ACME
2020-01-01 open Assets:Schwab:Cash:USD
2020-01-01 open Assets:Bank
2020-01-01 open Income:Employer:RSU
2020-01-01 open Income:Employer:ESPP-Discount
2020-01-01 open Income:Investments:Gains:ACME
2020-01-01 open Expenses:Taxes:RSU-Withholding
2020-01-01 open Expenses:Fees:Broker
2020-01-01 commodity ACME
2022-09-01 price USD 0.91 CHF
2022-12-30 price USD 0.90 CHF
2023-02-15 price USD 0.92 CHF
2023-06-01 price USD 0.90 CHF
2023-12-29 price USD 0.88 CHF
2024-12-31 price USD 0.85 CHF
2022-09-30 price ACME 100 USD
2023-02-15 price ACME 150.25 USD
2023-06-01 price ACME 200 USD
2023-12-29 price ACME 180 USD
2024-12-31 price ACME 210 USD

2023-02-15 * "Acme" "vest 10 ACME (4 withheld for tax)"
  ref: "schwab:vest:111:20230215"
  award: "111"
  award_date: "2022-10-01"
  award_name: "RSU"
  Assets:Schwab:ACME  6 ACME {150.25 USD, 2023-02-15, "v111-20230215"}
  Expenses:Taxes:RSU-Withholding  601.00 USD
  Income:Employer:RSU  -1502.50 USD

2023-06-01 * "Acme" "Sale of 2 ACME"
  ref: "schwab:sale:1"
  Assets:Schwab:ACME  -2 ACME {150.25 USD, 2023-02-15} @ 200 USD
  Assets:Schwab:Cash:USD  399 USD
  Expenses:Fees:Broker  1 USD
  Income:Investments:Gains:ACME

2023-09-29 * "Acme" "ESPP purchase"
  ref: "schwab:espp:1"
  offering_start: "2023-03-31"
  Assets:Schwab:ACME  10 ACME {180 USD, 2023-09-29}
  Income:Employer:ESPP-Discount  -270 USD
  Assets:Bank  -1530 USD
"""
AS_OF = date(2024, 12, 31)


@pytest.fixture(scope="module")
def entries():
    entries, errors, _ = loader.load_string(LEDGER)
    assert not errors, errors
    return entries


def series(entries, program="rsu", root=None):
    out = rsu.report(entries, root, AS_OF)
    return next(s for s in out["series"] if s["program"] == program)


def test_a_vest_is_found_by_its_shape(entries):
    [v] = [e for e in rsu.events(entries) if e.program == "rsu"]
    assert (v.gross, v.withheld, v.net, v.fmv) == (D(10), D(4), D(6), D("150.25"))
    assert v.start == date(2022, 10, 1) and v.award == "111"


def test_realised_and_unrealised_results_and_the_exchange_rate_part(entries):
    row = series(entries)["events"][0]
    # sold 2 at 200 with a fee of 1: 2 x 49.75 - 1; held 4, priced 210 at the end
    assert row["sold"] == D(2) and row["left"] == D(4)
    assert row["realised_usd"] == D("98.50")
    # in francs: proceeds at 0.90 less cost at the vest day's 0.92
    assert row["realised_chf"] == D("82.64") and row["realised_fx_chf"] == D("-7.98")
    assert row["unrealised_usd"] == D("239.00")
    assert row["unrealised_chf"] == (4 * (D(210) * D("0.85") - D("150.25") * D("0.92"))).quantize(
        D("0.01")
    )


def test_growth_from_award_to_vest_uses_the_price_on_the_award_date(entries):
    row = series(entries)["events"][0]
    assert row["start_price"] == D(100)  # the quote of 2022-09-30 is the last before the award date
    assert row["growth"] == D("0.5025")
    assert row["gain_usd"] == D("502.50")  # 10 vested shares, 50.25 each
    [award] = series(entries)["awards"]
    assert award["growth"] == D("0.5025") and award["gross"] == D(10)


def test_without_a_price_near_the_award_date_there_is_no_growth_not_a_guess(entries):
    r = rsu
    text = LEDGER.replace('award_date: "2022-10-01"', 'award_date: "2021-01-04"')
    ents, errs, _ = loader.load_string(text)
    assert not errs
    row = r.report(ents, None, AS_OF)["series"][0]["events"][0]
    assert (
        "start_price" not in row
        and r.report(ents, None, AS_OF)["series"][0]["summary"]["missing_start_price"] == 1
    )


def test_a_history_file_supplies_the_award_date_price(entries, tmp_path):
    d = tmp_path / "sources" / "market-history"
    d.mkdir(parents=True)
    (d / "ACME.csv").write_text("Date,Open,High,Low,Close,Volume\n2022-09-30,1,1,1,90.50,1\n")
    text = LEDGER.replace('award_date: "2022-10-01"', 'award_date: "2022-10-02"')
    ents, _, _ = loader.load_string(text)
    got = next(x for x in rsu.report(ents, tmp_path, AS_OF)["series"] if x["program"] == "rsu")
    assert got["events"][0]["start_price"] == D("90.50")


def test_held_against_sold_at_the_vest(entries):
    row = series(entries)["events"][0]
    assert row["sell_at_event_usd"] == D("901.50")  # 6 shares at 150.25
    assert row["actual_usd"] == D(399) + D(4) * D(210)  # proceeds net of fee + what is left
    assert row["hold_all_usd"] == D(6) * D(210)


def test_an_espp_purchase_shows_its_discount_and_offering_start(entries):
    row = series(entries, "espp")["events"][0]
    assert row["paid"] == D(153) and row["discount"] == D("0.15")
    assert row["start"] == date(2023, 3, 31) and row["discount_usd"] == D(270)


def test_the_yearly_market_effect_matches_the_closed_form(entries):
    out = rsu.yearly_effect(entries, None, [2023, 2024], AS_OF)
    # 2023: end value of 14 shares, minus what the vest and purchase were worth when they arrived,
    # plus the two sold; all at the day's francs per dollar
    end_2023 = D(14) * D(180) * D("0.88")
    arrived = D(6) * D("150.25") * D("0.92") + D(10) * D(180) * D(
        "0.90"
    )  # espp day has no quote: 0.90 before
    sold = D(2) * D(200) * D("0.90")
    assert out[2023] == (end_2023 - arrived + sold).quantize(D("0.01"))
    assert out[2024] == (D(14) * D(210) * D("0.85") - end_2023).quantize(D("0.01"))


def test_an_award_can_be_priced_at_the_previous_close(entries, tmp_path):
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "rsu.yaml").write_text("ACME:\n  award_price: previous_close\n")
    d = tmp_path / "sources" / "market-history"
    d.mkdir(parents=True)
    (d / "ACME.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n2022-09-29,1,1,1,95,1\n2022-09-30,1,1,1,100,1\n2022-10-03,1,1,1,110,1\n"
    )
    text = LEDGER.replace('award_date: "2022-10-01"', 'award_date: "2022-10-03"')
    ents, _, _ = loader.load_string(text)
    got = next(x for x in rsu.report(ents, tmp_path, AS_OF)["series"] if x["program"] == "rsu")
    assert got["events"][0]["start_price"] == D(100)  # not the 110 of the award day itself
    (tmp_path / "rules" / "rsu.yaml").unlink()
    got = next(x for x in rsu.report(ents, tmp_path, AS_OF)["series"] if x["program"] == "rsu")
    assert got["events"][0]["start_price"] == D(110)


def test_awarded_against_worth_now_uses_the_shares_the_broker_says_were_granted(entries, tmp_path):
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "rsu.yaml").write_text("ACME:\n  granted:\n    '2022-10-01': 40\n")
    d = tmp_path / "sources" / "market-history"
    d.mkdir(parents=True)
    (d / "ACME.csv").write_text("Date,Open,High,Low,Close,Volume\n2022-09-30,1,1,1,100,1\n")
    got = next(x for x in rsu.report(entries, tmp_path, AS_OF)["series"] if x["program"] == "rsu")
    [a] = got["awards"]
    # 40 granted at 100 = 4000; 10 vested at 150.25; the other 30 at the latest price of 210
    assert (a["granted"], a["unvested"], a["award_value"]) == (D(40), D(30), D("4000.00"))
    assert a["vested_value"] == D("1502.50") and a["unvested_value"] == D("6300.00")
    assert a["worth_now"] == D("7802.50") and a["vs_award"] == D("0.9506")


PLAN = """
option "operating_currency" "CHF"
2020-01-01 open Assets:Plan:ZED
2020-01-01 open Income:Employer:RSU:ZED
2020-01-01 open Income:Employer:ESPP:ZED
2020-01-01 open Income:Employer:Salary
2020-01-01 open Expenses:Taxes:RSU-Withholding
2020-01-01 commodity ZED
2020-04-01 price USD 0.90 CHF
2020-05-01 price USD 0.90 CHF
2020-04-20 price ZED 25 USD
2021-06-01 price ZED 30 USD
2021-06-01 price USD 0.80 CHF

2020-04-16 * "Plan" "ZED shares received from the stock plan"
  ref: "plan:rcv:1"
  award: "A1"
  award_date: "2020-01-15"
  basis: "confirmation c.pdf"
  Assets:Plan:ZED  60 ZED @@ 1200.00 USD
  Expenses:Taxes:RSU-Withholding  800.00 USD
  Income:Employer:RSU:ZED  -2000.00 USD

2020-04-17 * "Plan" "ZED shares received from the stock plan"
  ref: "plan:rcv:2"
  basis: "estimate: shares at the market price of the day"
  Assets:Plan:ZED  10 ZED @@ 200.00 USD
  Income:Employer:RSU:ZED  -200.00 USD

2020-05-01 * "Plan" "ZED shares received from the purchase plan"
  ref: "plan:rcv:3"
  offering_start: "2020-01-01"
  basis: "confirmation p.pdf"
  Assets:Plan:ZED  40 ZED @@ 800.00 USD
  Expenses:Taxes:RSU-Withholding  200.00 USD
  Income:Employer:Salary  -637.50 USD
  Income:Employer:ESPP:ZED  -362.50 USD

2020-05-02 * "Plan" "ZED shares received from the purchase plan"
  ref: "plan:rcv:4"
  basis: "estimate: shares at the market price of the day"
  Assets:Plan:ZED  5 ZED @@ 100.00 USD
  Income:Employer:ESPP:ZED  -100.00 USD
"""


def _plan():
    ents, errors, _ = loader.load_string(PLAN)
    assert not errors, errors
    return ents


def test_a_plan_broker_receipt_priced_but_without_a_cost_lot_is_an_event_that_follows_no_lots():
    got = {v.ref: v for v in rsu.events(_plan())}
    vest = got["plan:rcv:1"]
    assert (vest.gross, vest.withheld, vest.net, vest.fmv) == (D(100), D(40), D(60), D(20))
    assert vest.lots is False and vest.estimate is False and vest.start == date(2020, 1, 15)
    assert got["plan:rcv:2"].estimate is True and got["plan:rcv:2"].withheld == 0
    # a purchase: 50 bought (40 arrived, 10 worth 200 kept for tax); 362.50 discount over 50 shares
    espp = got["plan:rcv:3"]
    assert (espp.gross, espp.paid) == (D(50), D("12.75")) and espp.start == date(2020, 1, 1)
    assert got["plan:rcv:4"].paid == got["plan:rcv:4"].fmv  # an estimate knows no discount


def test_the_report_marks_a_series_without_lots_and_leaves_an_estimate_discount_out():
    out = rsu.report(_plan(), None, date(2021, 6, 1))
    rsu_s = next(s for s in out["series"] if s["program"] == "rsu")
    espp_s = next(s for s in out["series"] if s["program"] == "espp")
    assert rsu_s["lots"] is False and rsu_s["estimates"] == 1
    assert [e["discount"] for e in espp_s["events"]] == [D("0.3625"), None]
    assert espp_s["events"][0]["hold_all_usd"] == D("1200.00")  # 40 shares at the latest 30


def test_a_history_quoted_after_a_split_is_put_back_and_a_split_counts_in_holding_all(tmp_path):
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "rsu.yaml").write_text(
        "ZED:\n  splits:\n    - {date: 2020-12-01, ratio: 4}\n"
    )
    d = tmp_path / "sources" / "market-history"
    d.mkdir(parents=True)
    (d / "ZED.csv").write_text("Date,Close\n2020-01-15,5.50\n")  # quoted corrected for the 4-for-1
    out = rsu.report(_plan(), tmp_path, date(2021, 6, 1))
    rsu_s = next(s for s in out["series"] if s["program"] == "rsu")
    first = rsu_s["events"][0]
    assert first["start_price"] == D("22.00")  # 5.50 x 4: what a share cost that day
    # 60 shares became 240; at the latest 30 that is 7200 (not 1800)
    assert first["hold_all_usd"] == D("7200.00")
