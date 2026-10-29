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
