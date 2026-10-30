import json
from datetime import date
from decimal import Decimal as D

import pytest
from beancount import loader

from fin.importers import base, viac
from fin.importers.base import ImportFailed

RULES = """
portfolios:
  "ACME-1": Assets:Pension:Acme:Growth
transfer_in: Assets:Pension:Acme:Old
"""


def _wealth(day, value):
    return {"date": day, "value": {"__type": "VIAC_DECIMAL", "__value": value}}


def _capture(root, name="2026-03-01_main_capture.json", rows=None, wealth=None):
    # the app lists newest first
    rows = rows or [
        {"type": "FEE_CHARGE", "amountInChf": -2.0, "valueDate": "2021-01-31", "balanceAfterBooking": 98.0, "documentNumber": "D3"},
        {"type": "TRADE_BUY", "amountInChf": -900.0, "valueDate": "2021-01-10", "balanceAfterBooking": 100.0, "documentNumber": "D2"},
        {"type": "CONTRIBUTION", "amountInChf": 1000.0, "valueDate": "2021-01-05", "balanceAfterBooking": 1000.0},
    ]  # fmt: skip
    wealth = wealth or [_wealth("2021-01-31", "1010"), _wealth("2021-02-14", "1030")]
    answers = [
        ("p3a/portfolio/transactions", {"transactions": {"ACME-1": rows}}),
        ("wealth/summary", {"p3aSummary": {"portfolioWealthSummaries": {"ACME-1": {"dailyWealth": wealth}}}}),
    ]  # fmt: skip
    folder = root / "sources" / "viac"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(
        json.dumps({"responses": [{"method": "GET", "url": f"https://app.example/rest/web/{p}?x=1", "status": 200, "body": b} for p, b in answers]})
    )  # fmt: skip
    (root / "rules").mkdir(exist_ok=True)
    (root / "rules" / "viac.yaml").write_text(RULES)


def _parse(root):
    return viac.parse_root(root, viac.source_files(root))


def test_bookings_go_to_the_cash_account_with_their_counters_and_the_securities_are_marked(
    tmp_path,
):
    _capture(tmp_path)
    by_kind = {t.meta.get("kind", "mark"): t for t in _parse(tmp_path)}
    assert by_kind["CONTRIBUTION"].counter == "Assets:Transfers:Pending"
    assert by_kind["CONTRIBUTION"].account == "Assets:Pension:Acme:Growth:Cash"
    assert by_kind["TRADE_BUY"].counter == "Assets:Pension:Acme:Growth:Securities"
    assert by_kind["FEE_CHARGE"].counter == "Expenses:Fees:Pension"
    marks = [t for t in _parse(tmp_path) if t.ref.endswith(tuple(f":mark:{d}" for d in ("2021-01-31", "2021-02-14")))]  # fmt: skip
    # 2021-01-31: worth 1010, 98 of it cash, 900 bought at cost: the securities are 912, so +12;
    # 2021-02-14 (the last day of the series): 1030 - 98 - 912 = +20
    assert [(t.date, t.amount) for t in marks] == [(date(2021, 1, 31), D("12.00")), (date(2021, 2, 14), D("20.00"))]  # fmt: skip
    assert marks[0].counter == "Income:Investments:Unrealized:VIAC"


def test_each_days_cash_balance_is_asserted_the_day_after_and_the_ledger_agrees(tmp_path):
    _capture(tmp_path)
    text = viac.derived(tmp_path, viac.source_files(tmp_path))
    assert "2021-01-06 balance Assets:Pension:Acme:Growth:Cash  1000.00 CHF" in text
    assert "2021-02-01 balance Assets:Pension:Acme:Growth:Cash  98.00 CHF" in text
    accounts = [
        "Assets:Pension:Acme:Growth:Cash", "Assets:Pension:Acme:Growth:Securities", "Assets:Transfers:Pending",
        "Expenses:Fees:Pension", "Income:Investments:Unrealized:VIAC",
    ]  # fmt: skip
    ledger_text = "\n".join(
        [
            *(f"2000-01-01 open {a}" for a in accounts),
            *(base.render(t) + "\n" for t in _parse(tmp_path)),
            text,
        ]
    )
    _entries, errors, _ = loader.load_string(ledger_text)
    assert not errors, errors
    # a doubled row would break the assertion
    doubled = ledger_text.replace(
        text,
        '2021-01-31 * "VIAC" "fee again"\n  Assets:Pension:Acme:Growth:Cash  -2.00 CHF\n'
        "  Expenses:Fees:Pension  2.00 CHF\n\n" + text,
    )
    _entries, errors, _ = loader.load_string(doubled)
    assert errors


def test_a_booking_type_it_does_not_know_and_a_portfolio_without_an_account_are_refused(tmp_path):
    odd = [{"type": "WHATEVER", "amountInChf": 1.0, "valueDate": "2021-01-05", "balanceAfterBooking": 1.0}]  # fmt: skip
    _capture(tmp_path, rows=odd)
    with pytest.raises(ImportFailed, match="WHATEVER"):
        _parse(tmp_path)
    _capture(tmp_path)
    (tmp_path / "rules" / "viac.yaml").write_text("portfolios: {}\n")
    with pytest.raises(ImportFailed, match="no ledger account"):
        _parse(tmp_path)


def test_only_the_newest_capture_of_a_login_counts(tmp_path):
    _capture(tmp_path, "2026-01-01_main_capture.json", rows=[
        {"type": "CONTRIBUTION", "amountInChf": 5.0, "valueDate": "2020-01-01", "balanceAfterBooking": 5.0}
    ])  # fmt: skip
    _capture(tmp_path)
    assert {t.date for t in _parse(tmp_path) if t.meta.get("kind")} == {date(2021, 1, 5), date(2021, 1, 10), date(2021, 1, 31)}  # fmt: skip
    _capture(tmp_path, "2026-03-01_partner_capture.json")  # another login keeps its own
    assert len(viac.source_files(tmp_path)) == 3
    assert len(viac._newest_per_login(viac.source_files(tmp_path))) == 2


def test_a_capture_without_the_transactions_asks_to_open_each_portfolio(tmp_path):
    folder = tmp_path / "sources" / "viac"
    folder.mkdir(parents=True)
    (folder / "2026-03-01_main_capture.json").write_text(json.dumps({"responses": []}))
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "viac.yaml").write_text(RULES)
    with pytest.raises(ImportFailed, match="open each portfolio"):
        _parse(tmp_path)


def test_the_recorder_finds_its_captures(tmp_path):
    folder = tmp_path / "sources" / "viac"
    folder.mkdir(parents=True)
    (folder / "2026-10-30_main_capture.json").write_text("{}")
    (folder / "notes.txt").write_text("")
    assert [p.name for p in viac.source_files(tmp_path)] == ["2026-10-30_main_capture.json"]
    assert viac.IN_SYNC is False
