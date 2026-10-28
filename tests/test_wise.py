import json
from datetime import date
from decimal import Decimal as D

import pytest

from fin.importers import base, wise


def _row(
    ref, kind, value, ccy="GBP", when="2026-08-03T18:30:00.123456Z", fee=0, balance=0, **details
):
    return {
        "type": "CREDIT" if value > 0 else "DEBIT",
        "date": when,
        "amount": {"value": value, "currency": ccy, "zero": value == 0},
        "totalFees": {"value": fee, "currency": ccy, "zero": fee == 0},
        "details": {
            "type": kind,
            "description": details.pop("description", kind.title()),
            **details,
        },
        "exchangeDetails": None,
        "runningBalance": {"value": balance, "currency": ccy, "zero": balance == 0},
        "referenceNumber": ref,
        "attachment": None,
        "activityAssetAttributions": [],
    }


def _statement(ccy, rows, start_balance=0, end_balance=0):
    return {
        "accountHolder": {
            "type": "PERSONAL", "profileId": 1, "firstName": "Ada", "lastName": "Example",
            "address": {"addressFirstLine": "1 Secret Street", "city": "Zurich", "postCode": "8000"},
        },
        "issuer": {"name": "Wise Europe SA"},
        "bankDetails": [{"accountNumbers": [{"value": "CH00 0000"}]}],
        "transactions": rows,
        "startOfStatementBalance": {"value": start_balance, "currency": ccy},
        "endOfStatementBalance": {"value": end_balance, "currency": ccy},
        "query": {
            "intervalStart": "2023-12-31T23:00:00Z", "intervalEnd": "2026-10-28T22:59:59.999Z",
            "currency": ccy, "profileId": 1, "type": "COMPACT",
        },
    }  # fmt: skip


def _card(ref, amount, ccy="GBP", when="2026-08-03T18:30:00.123456Z", spent=None, fee=0):
    return _row(
        ref, "CARD", amount, ccy, when, fee=fee,
        amount=spent or {"value": -amount, "currency": ccy},
        category="Restaurants", cardLastFourDigits="1234", cardHolderFullName="Ada Example",
        merchant={"name": "Cafe X", "city": "Pittsburgh", "country": "US",
                  "category": "Eating Places, Restaurants"},
    )  # fmt: skip


def _archive(root, ccy, rows, **kw):
    src = root / "sources" / "wise"
    src.mkdir(parents=True, exist_ok=True)
    path = src / f"2024-01-01_2026-10-28_{ccy}.json"
    path.write_text(json.dumps(wise.sanitize(_statement(ccy, rows, **kw), ccy)))
    return path


def test_sanitize_keeps_rows_and_drops_address_and_account_numbers():
    rows = [
        _card("CARD-1", -10.5),
        _row("TRANSFER-2", "TRANSFER", -20, recipient={"name": "Bo", "bankAccount": "GB00 1"},
             senderAccount="CH00 2", recipientAccountNumber="3"),
    ]  # fmt: skip
    kept = wise.sanitize(_statement("GBP", rows), "GBP")
    text = json.dumps(kept)
    for gone in ("Secret Street", "CH00", "GB00", "cardHolderFullName", "profileId", "Wise Europe"):
        assert gone not in text
    assert kept["holder"] == "Ada Example"
    assert kept["transactions"][1]["details"]["recipient"] == {"name": "Bo"}
    assert kept["transactions"][0]["details"]["merchant"]["city"] == "Pittsburgh"


def test_card_rows_refunds_and_fees(tmp_path):
    rows = [
        _card("CARD-7", 4.0, when="2026-08-05T09:00:00.5Z"),  # refund: same reference
        _card("CARD-7", -12.34, spent={"value": 16.0, "currency": "USD"}, fee=0.12),
    ]
    [refund, spend] = sorted(
        wise.parse_files([_archive(tmp_path, "GBP", rows)]), key=lambda t: t.amount
    )[::-1]
    assert spend.account == "Assets:Wise:GBP" and spend.amount == D("-12.34")
    assert spend.date == date(2026, 8, 3) and spend.payee == "Cafe X" and spend.review
    assert spend.meta["original_amount"] == "16.0 USD"
    assert spend.meta["fee"] == "0.12 GBP"
    assert spend.meta["merchant_category"] == "Eating Places, Restaurants"
    assert spend.meta["merchant_country"] == "US"
    assert refund.amount == D("4.0") and refund.ref != spend.ref


def test_late_night_utc_is_the_next_zurich_day(tmp_path):
    [t] = wise.parse_files(
        [_archive(tmp_path, "GBP", [_card("CARD-1", -1, when="2026-08-03T22:30:00Z")])]
    )
    assert t.date == date(2026, 8, 4)


def test_overlapping_fetches_count_a_row_once(tmp_path):
    first = _archive(tmp_path, "GBP", [_card("CARD-1", -1)])
    second = first.with_name("2026-08-01_2026-10-28_GBP.json")
    second.write_text(first.read_text())
    assert len(wise.parse_files([first, second])) == 1


def test_conversion_is_one_transaction_from_the_side_it_left(tmp_path):
    out = _row("BALANCE-9", "CONVERSION", -100, "CHF", fee=0.4)
    into = _row("BALANCE-9", "CONVERSION", 89.21, "GBP")
    files = [_archive(tmp_path, "CHF", [out]), _archive(tmp_path, "GBP", [into])]
    [t] = wise.parse_files(files)
    assert (t.account, t.amount, t.counter, t.counter_amount) == (
        "Assets:Wise:CHF",
        D(-100),
        "Assets:Wise:GBP",
        (D("89.21"), "GBP"),
    )
    assert not t.review
    text = base.render(t)
    assert "Assets:Wise:CHF  -100 CHF @@ 89.21 GBP" in text
    assert "Assets:Wise:GBP  89.21 GBP" in text


def test_conversion_needs_both_statements(tmp_path):
    with pytest.raises(base.ImportFailed, match="not archived"):
        wise.parse_files([_archive(tmp_path, "GBP", [_row("BALANCE-9", "CONVERSION", 89.21)])])


def test_transfers_to_yourself_clear_and_to_others_are_people(tmp_path):
    rows = [
        _row("TRANSFER-1", "TRANSFER", -50, recipient={"name": "ADA  EXAMPLE"}),
        _row(
            "TRANSFER-2", "TRANSFER", -60, recipient={"name": "Bo Other"}, paymentReference="rent"
        ),
        _row("TRANSFER-3", "DEPOSIT", 70, senderName="Cy Friend"),
        _row("BALANCE-4", "MONEY_ADDED", 80, description="Topped up account"),
    ]
    by = {t.ref.split(":")[1]: t for t in wise.parse_files([_archive(tmp_path, "GBP", rows)])}
    own, other, deposit, topup = (
        by[k] for k in ("TRANSFER-1", "TRANSFER-2", "TRANSFER-3", "BALANCE-4")
    )
    assert own.counter == wise.CLEARING and not own.review and "counterparty" not in own.meta
    assert other.counter is None and other.meta["counterparty"] == "person"
    assert other.narration.endswith("rent")
    assert deposit.counter == "Income:Uncategorized" and deposit.meta["counterparty"] == "person"
    assert topup.counter == wise.CLEARING and not topup.review


def test_unknown_kinds_are_an_error(tmp_path):
    with pytest.raises(base.ImportFailed, match="unknown Wise transaction type"):
        wise.parse_files([_archive(tmp_path, "GBP", [_row("X-1", "SOMETHING_NEW", -1)])])


def test_opening_balance_from_the_statement(tmp_path):
    _archive(tmp_path, "JPY", [_card("CARD-1", -500, "JPY")], start_balance=1200)
    [(day, account, amount, ccy, ref)] = wise.opening_balances(tmp_path)
    assert (day, account, amount, ccy, ref) == (
        date(2024, 1, 1),
        "Assets:Wise:JPY",
        D(1200),
        "JPY",
        "opening:Assets:Wise:JPY",
    )


def test_interval_is_zurich_midnight_in_utc():
    assert wise._interval(date(2024, 1, 1)) == "2023-12-31T23:00:00.000Z"
    assert wise._interval(date(2026, 7, 1), end=True) == "2026-07-01T21:59:59.999Z"


def test_import_balances_rates_and_transfer_pairing(data, monkeypatch):
    from fin import cli, config, gitops, ledger, opening, prices
    from fin.analytics.postings import expense_postings

    def rates(ccy, quote, start, end):
        return {start: D("1.1"), date(2026, 8, 1): D("1.07")}

    monkeypatch.setattr(prices, "fetch_rates", rates)
    with (data / "ledger" / "accounts.beancount").open("a") as f:
        f.write(
            "".join(
                f"2020-01-01 open {a}\n"
                for a in ("Assets:UBS:Checking", "Expenses:Uncategorized", "Income:Uncategorized")
            )
        )
    gitops.commit_all(data, "bank")
    base.write_new(data, "ubs", [base.RawTxn(
        date=date(2026, 8, 2), account="Assets:UBS:Checking", amount=D(-80), currency="CHF",
        payee="Wise Payments", narration="", ref="ubs:to-wise",
    )])  # fmt: skip
    _archive(
        data,
        "CHF",
        [_row("BALANCE-4", "MONEY_ADDED", 80, "CHF", when="2026-08-03T08:00:00Z")],
        end_balance=80,
    )
    _archive(data, "GBP", [_card("CARD-1", -12.5)], start_balance=20, end_balance=7.5)
    cli._fetch_and_import(data, "wise", fetch=False)
    assert (
        opening.write(data, "wise") == 0
    )  # the import already wrote it: GBP had money before the history
    led = ledger.load(config.main_ledger(data))
    assert not led.errors
    gbp = sum(p.units.number for e in led.entries if type(e).__name__ == "Transaction"
              for p in e.postings if p.account == "Assets:Wise:GBP")  # fmt: skip
    assert gbp == D("7.5")
    pending = [p.units.number for e in led.entries if type(e).__name__ == "Transaction"
               for p in e.postings if p.account == wise.CLEARING]  # fmt: skip
    assert sum(pending) == 0 and len(pending) == 2  # the UBS debit paired with the top-up
    spent = expense_postings(led)
    row = spent[spent["orig_ccy"] == "GBP"].iloc[0]
    assert row["amount"] == D("13.38")  # 12.5 GBP at the 1.07 rate of 2026-08-01


def test_prices_fetch_only_what_is_missing(data, monkeypatch):
    from fin import gitops, prices

    table = {date(2026, 7, 31): D("1.08"), date(2026, 8, 3): D("1.09"), date(2026, 8, 4): D("1.1")}
    asked = []

    def rates(ccy, quote, start, end):
        asked.append((ccy, quote, start, end))
        return {d: r for d, r in table.items() if start <= d <= end}

    monkeypatch.setattr(prices, "fetch_rates", rates)
    with (data / "ledger" / "accounts.beancount").open("a") as f:
        f.write("2020-01-01 open Assets:Wise:GBP\n2020-01-01 open Expenses:Uncategorized\n")
    gitops.commit_all(data, "accounts")
    base.write_new(data, "wise", [base.RawTxn(
        date=date(2026, 8, 3), account="Assets:Wise:GBP", amount=D(-1), currency="GBP",
        payee="X", narration="", ref="wise:x",
    )])  # fmt: skip
    assert prices.update(data, today=date(2026, 8, 3)) == 2
    assert asked[0] == ("GBP", "CHF", date(2026, 7, 27), date(2026, 8, 3))  # a week of lead
    assert prices.update(data, today=date(2026, 8, 3)) == 0 and len(asked) == 1
    assert prices.update(data, today=date(2026, 8, 4)) == 1
    assert asked[-1][2] == date(2026, 8, 4)
    text = (data / "ledger" / "prices.beancount").read_text()
    assert "2026-08-04 price GBP  1.1 CHF" in text


def test_prices_fill_in_before_the_first_recorded_rate():
    from fin import prices

    have = (date(2024, 1, 2), date(2026, 8, 3))
    assert prices._missing(date(2024, 1, 2), have, date(2026, 8, 3)) == []
    assert prices._missing(date(2024, 1, 5), have, date(2026, 8, 4)) == [
        (date(2026, 8, 4), date(2026, 8, 4))
    ]
    # a source with older history: its first use needs rates from before
    assert prices._missing(date(2019, 1, 8), have, date(2026, 8, 3)) == [
        (date(2019, 1, 1), date(2024, 1, 1))
    ]


def test_a_refund_of_a_wise_overcharge_is_a_fee_refund(tmp_path):
    rows = [_row("OVERCHARGE_INCIDENTS-1", "UNKNOWN", 0.03, "CHF", description="Overcharge refund")]
    [t] = wise.parse_files([_archive(tmp_path, "CHF", rows)])
    assert t.counter == "Expenses:Fees" and not t.review and t.amount == D("0.03")
    with pytest.raises(base.ImportFailed, match="unknown Wise transaction type"):
        wise.parse_files(
            [_archive(tmp_path, "GBP", [_row("X-1", "UNKNOWN", -1, description="Something else")])]
        )


def test_closing_balances_become_assertions(tmp_path):
    _archive(tmp_path, "GBP", [_card("CARD-1", -12.5)], start_balance=20, end_balance=7.5)
    text = wise.derived(tmp_path)
    assert "2026-10-29 balance Assets:Wise:GBP  7.5 GBP" in text


def _leg(ref, day, account, amount, ccy):
    from fin import transfers as T

    return T.Leg(ref, day, account, D(amount), ccy, "", T.CLEARING, "f", 1)


class _Rates:
    def __init__(self, rate):
        self.rate = rate

    def price(self, ccy, quote, day):
        return self.rate


def test_cross_currency_pairs_need_a_close_rate_and_a_single_candidate():
    from fin import transfers as T

    bank = _leg("ubs:1", date(2021, 4, 21), "Assets:UBS:Checking", "-1262.40", "CHF")
    gbp = _leg("wise:1", date(2021, 4, 21), "Assets:Wise:GBP", "1000", "GBP")
    near = T.find_cross_pairs([bank, gbp], _Rates(D("1.25")), set())
    assert [(b.ref, w.ref) for b, w in near] == [
        ("ubs:1", "wise:1")
    ]  # 1000 x 1.25 within 6% of 1262
    assert T.find_cross_pairs([bank, gbp], _Rates(D("1.0")), set()) == []  # far from the day's rate
    other = _leg("wise:2", date(2021, 4, 22), "Assets:Wise:GBP", "1000", "GBP")
    assert (
        T.find_cross_pairs([bank, gbp, other], _Rates(D("1.25")), set()) == []
    )  # two candidates: left alone
    late = _leg("wise:3", date(2021, 4, 30), "Assets:Wise:GBP", "1000", "GBP")
    assert T.find_cross_pairs([bank, late], _Rates(D("1.25")), set()) == []  # too far apart in time
    same_way = _leg("wise:4", date(2021, 4, 21), "Assets:Wise:GBP", "-1000", "GBP")
    assert T.find_cross_pairs([bank, same_way], _Rates(D("1.25")), set()) == []  # both money out


def test_the_foreign_leg_carries_the_chf_price_so_clearing_nets_to_zero():
    from fin import transfers as T

    block = [
        '2021-04-21 * "Wise" "Topped up account" #review',
        '  ref: "wise:1"',
        "  Assets:Wise:GBP  1000 GBP",
        "  Assets:Transfers:Pending  -1000 GBP",
    ]
    out = T._rewrite_priced(block, "ubs:1", D("1262.40"))
    assert "#review" not in out[0] and '  transfer: "ubs:1"' in out
    assert "  Assets:Wise:GBP  1000 GBP @@ 1262.40 CHF" in out
    assert "  Assets:Transfers:Pending  -1262.40 CHF" in out


def test_a_transfer_to_your_own_broker_is_a_transfer_and_its_fee_is_split_off(tmp_path):
    rows = [
        _row(
            "TRANSFER-9",
            "TRANSFER",
            -4202.15,
            "USD",
            fee=2.15,
            recipient={"name": "Example Broker LLC"},
            paymentReference="U1234567",
        )
    ]
    own = ("example broker",)  # rules/wise.yaml: own_recipients
    [t] = wise.parse_files([_archive(tmp_path, "USD", rows)], own)
    text = base.render(t)
    assert (
        "Assets:Wise:USD  -4202.15 USD" in text
        and "Assets:Transfers:Pending  4200.00 USD" in text
        and "Expenses:Fees  2.15 USD" in text
    )
    [plain] = wise.parse_files([_archive(tmp_path, "USD", rows)])
    assert plain.review  # without the setting it is a payment to someone
    assert (
        not t.review and "counterparty" not in t.meta
    )  # not a person: your own account at a broker
    other = [_row("TRANSFER-8", "TRANSFER", -50, "GBP", recipient={"name": "Example Broker LLC"})]
    [u] = wise.parse_files([_archive(tmp_path, "GBP", other)], own)
    assert u.counter == "Assets:Transfers:Pending"  # no fee: a plain transfer


def test_a_transfer_with_its_fee_on_its_own_line_pairs_at_what_arrives(data):
    from fin import config, ledger, transfers

    with (data / "ledger" / "accounts.beancount").open("a") as f:
        f.write(
            "".join(
                f"2019-01-01 open {a}\n"
                for a in (
                    "Assets:Wise:USD",
                    "Assets:IBKR:Cash:USD",
                    "Expenses:Uncategorized",
                    "Income:Uncategorized",
                    "Expenses:Fees",
                )
            )
        )
    base.write_new(data, "wise", [base.RawTxn(
        date=date(2021, 3, 10), account="Assets:Wise:USD", amount=D("-4202.15"), currency="USD", payee="Example Broker",
        narration="", ref="wise:out", postings=["Assets:Wise:USD  -4202.15 USD", "Assets:Transfers:Pending  4200.00 USD", "Expenses:Fees  2.15 USD"],
    )])  # fmt: skip
    base.write_new(data, "ibkr", [base.RawTxn(
        date=date(2021, 3, 11), account="Assets:IBKR:Cash:USD", amount=D(4200), currency="USD", payee="IBKR",
        narration="", ref="ibkr:in", counter="Assets:Transfers:Pending",
    )])  # fmt: skip
    assert transfers.match(data)["pairs"] == 1
    assert not ledger.load(config.main_ledger(data)).errors


def test_own_recipients_come_from_the_data_folder(tmp_path):
    assert wise.own_recipients(tmp_path) == ()
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "wise.yaml").write_text("own_recipients: ['Example Broker']\n")
    assert wise.own_recipients(tmp_path) == ("example broker",)
