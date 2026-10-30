from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from fin.importers import base, etrade
from fin.importers.base import ImportFailed, RawTxn

PRICE = lambda sym, day: D(20)


def _statement(
    start="March 1, 2021",
    end="March 31, 2021",
    cash=("100.00", "108.50"),
    held="ACME StkPln 10 20.0000 200.00 100.00 0.00 0.00%",
    rows=(),
    account="000-000001",
) -> list[str]:
    """One statement's text as pdfplumber reads it (the page furniture left out)."""
    return [
        f"Account Number: {account} Statement Period : (cid:32){start} - {end} Account Type: INDIVIDUAL",
        "PORTFOLIO HOLDINGS",
        "CASH BALANCE",
        f"Opening Balance {cash[0]}",
        f"Closing Balance 100.00 {cash[1]}",
        "TOTAL CASH & CASH EQUIVALENTS 100.00% $" + cash[1],
        "STOCKS, OPTIONS & EXCHANGE-TRADED FUNDS (65.00% of Holdings)",
        held,
        "TOTAL STOCKS, OPTIONS ETF $200.00",
        "UNVESTED RESTRICTED STOCKS",
        "11/9/2018 111111 RSU ACME StkPln 99 $0.00 $20.00 $1,980.00",
        "TRANSACTION HISTORY",
        *rows,
    ]


ROWS = [
    "03/04/21 03/06/21 ACME CORP ACME Sold -10 20.0000 195.00",
    "09:30",
    "03/06/21 Wire WIRE OUT 195.00",
    "03/06/21 Adjustment OLINK TID 111 195.00",
    "03/06/21 Adjustment TO T-1 TID 111 195.00",
    "03/15/21 Dividend ACME CORP ACME 1.50 10.00",
    "CASH DIV ON 10 SHS / REC 03/11/21 PAY 03/15/21",
    "03/18/21 ACME CORP ACME Receive 20",
    "RAND 777",
]


@pytest.fixture
def pdfs(monkeypatch, tmp_path):
    pages: dict[str, list[str]] = {}
    monkeypatch.setattr(etrade, "_pages", lambda path: [pages[path.name]])

    def make(name: str, lines: list[str]) -> Path:
        pages[name] = lines
        path = tmp_path / name
        path.write_bytes(b"%PDF-1.4 fake" + name.encode())
        return path

    return make


def test_a_statement_is_read_and_checked_against_its_own_cash(pdfs):
    st = etrade.read_statement(pdfs("a.pdf", _statement(rows=ROWS)))
    assert (st.start, st.end, st.account) == (date(2021, 3, 1), date(2021, 3, 31), "0001")
    assert st.held == {"ACME": 10} and (st.cash_open, st.cash_close) == (D("100.00"), D("108.50"))
    kinds = sorted(e["kind"] for e in st.events)
    assert kinds == ["dividend", "receive", "trade", "wire"]  # the sweep pair cancels
    assert st.opening_shares("ACME") == 0  # 10 held - 20 received + 10 sold


def test_cash_that_does_not_add_up_is_refused(pdfs):
    with pytest.raises(ImportFailed, match="do not add up to the closing balance"):
        etrade.read_statement(pdfs("a.pdf", _statement(cash=("100.00", "109.00"), rows=ROWS)))


def test_a_layout_fin_does_not_know_is_refused_by_name(pdfs):
    with pytest.raises(ImportFailed, match="layout fin reads"):
        etrade.read_statement(pdfs("a.pdf", ["Account summary", "Something else"]))


def test_a_row_it_cannot_read_is_refused_not_skipped(pdfs):
    rows = [*ROWS, "03/20/21 Adjustment MYSTERY 12 5.00"]
    with pytest.raises(ImportFailed, match="does not recognise"):
        etrade.read_statement(pdfs("a.pdf", _statement(rows=rows)))


def test_a_sweep_that_does_not_cancel_is_refused(pdfs):
    rows = [r for r in ROWS if "TO T-1" not in r]
    with pytest.raises(ImportFailed, match="does not cancel"):
        etrade.read_statement(pdfs("a.pdf", _statement(rows=rows)))


def test_no_cash_section_means_no_cash(pdfs):
    lines = [ln for ln in _statement(cash=("0.00", "0.00")) if "Balance" not in ln]
    lines = [ln for ln in lines if not ln.startswith(("CASH BALANCE", "TOTAL CASH"))]
    st = etrade.read_statement(pdfs("a.pdf", lines))
    assert (st.cash_open, st.cash_close) == (0, 0)


def test_a_dividend_correction_reverses_the_row_it_follows(pdfs):
    rows = [
        "03/18/21 Dividend ACME CORP ACME 0.40",
        "S/T CAP GNS 10 SHS",
        "DIVIDEND CORRECTION",
        "03/18/21 Dividend ACME CORP ACME 0.40",
        "S/T CAP GNS 10 SHS",
    ]
    st = etrade.read_statement(
        pdfs("a.pdf", _statement(cash=("100.00", "100.00"), held="", rows=rows))
    )
    assert [e["gross"] for e in st.events] == [D("-0.40"), D("0.40")]


def test_a_delivery_pairs_its_transfer_rows_and_leaves_the_account(pdfs):
    rows = [
        "03/20/21 ACME CORP ACME Adjustment 5",
        "TFR FROM TYPE 7",
        "REFID:22132231134096",
        "03/20/21 ACME CORP ACME Deliver -5",
        "FREE DEL BRK#0534",
        "03/20/21 ACME CORP ACME Adjustment -5",
        "TFR TO TYPE 1",
        "REFID:22132231134096",
        "03/21/21 ACME CORP ACME Receive 5",
        "RAND 1",
    ]
    st = etrade.read_statement(
        pdfs("a.pdf", _statement(cash=("100.00", "100.00"), held="", rows=rows))
    )
    deliver = next(e for e in st.events if e["kind"] == "deliver")
    assert deliver["qty"] == 5 and deliver["refid"] == "22132231134096"
    assert st.opening_shares("ACME") == 0


def test_the_first_statement_books_what_it_opens_with(pdfs):
    f = pdfs("a.pdf", _statement(held="ACME StkPln 15 20.0000 300.00 100.00 0.00 0.00%", rows=ROWS))
    txns = etrade.parse_files([f], price=PRICE)
    gap = [t for t in txns if ":gap:" in t.ref]
    assert [(t.currency, t.amount) for t in gap] == [("ACME", 5), ("USD", D("100.00"))]
    assert "@@ 100.00 USD" in gap[0].postings[0] and etrade.EQUITY in gap[0].postings[1]


def test_consecutive_statements_must_agree_and_a_missing_month_is_booked_visibly(pdfs):
    march = pdfs("m.pdf", _statement(rows=ROWS))
    april_ok = _statement(
        start="April 1, 2021",
        end="April 30, 2021",
        cash=("108.50", "108.50"),
        rows=[],
    )
    txns = etrade.parse_files([march, pdfs("a.pdf", april_ok)], price=PRICE)
    assert [t.ref for t in txns if ":gap:" in t.ref] == [
        t.ref for t in txns if ":gap:" in t.ref and t.date == date(2021, 3, 1)
    ]  # only the first statement opens with a gap

    other_cash = _statement(start="April 1, 2021", end="April 30, 2021", cash=("1.00", "1.00"))
    with pytest.raises(ImportFailed, match="other cash"):
        etrade.parse_files([march, pdfs("b.pdf", other_cash)], price=PRICE)
    other_shares = _statement(
        start="April 1, 2021",
        end="April 30, 2021",
        cash=("108.50", "108.50"),
        held="ACME StkPln 11 20.0000 220.00 100.00 0.00 0.00%",
    )
    with pytest.raises(ImportFailed, match="opens with 11"):
        etrade.parse_files([march, pdfs("c.pdf", other_shares)], price=PRICE)

    june = _statement(start="June 1, 2021", end="June 30, 2021", cash=("120.00", "120.00"), held="")
    june = [ln for ln in june]
    gap = [
        t
        for t in etrade.parse_files([march, pdfs("j.pdf", june)], price=PRICE)
        if t.date == date(2021, 6, 1)
    ]
    assert {t.currency for t in gap} == {"ACME", "USD"}  # 10 shares out, 11.50 cash in, unobserved


def test_the_same_period_saved_twice_counts_once(pdfs):
    a, b = pdfs("a.pdf", _statement(rows=ROWS)), pdfs("b.pdf", _statement(rows=ROWS))
    assert len(etrade.statements([a, b])) == 1


def test_bookings_balance_in_dollars_and_in_shares(pdfs):
    from beancount import loader

    f = pdfs("a.pdf", _statement(rows=ROWS))
    txns = etrade.parse_files([f], price=PRICE)
    by_kind = {t.ref.split(":")[2]: t for t in txns}
    sale = by_kind["trade"]
    assert sale.postings == [
        "Assets:ETrade:ACME  -10 ACME @@ 200.00 USD",
        "Assets:ETrade:Cash:USD  195.00 USD",
        "Expenses:Fees:Broker  5.00 USD",
    ]
    assert "Expenses:Taxes:Withholding:US  1.50 USD" in by_kind["div"].postings
    receive = by_kind["rcv"]
    assert receive.ref == "etrade:0001:rcv:777"
    assert receive.postings[0] == "Assets:ETrade:ACME  20 ACME @@ 400.00 USD"
    text = "\n".join(
        [
            *(
                f"2000-01-01 open {a}"
                for a in (
                    "Assets:ETrade:ACME",
                    "Assets:ETrade:Cash:USD",
                    "Assets:Transfers:Pending",
                    "Expenses:Fees:Broker",
                    "Expenses:Taxes:Withholding:US",
                    "Equity:Opening-Balances",
                    "Income:Employer:RSU:ACME",
                    "Income:Investments:Dividends:ACME",
                )
            ),
            *(base.render(t) + "\n" for t in txns),
        ]
    )
    _entries, errors, _ = loader.load_string(text)
    assert not errors, errors


def test_end_to_end_balances_assert_and_a_second_import_adds_nothing(data, pdfs):
    from typer.testing import CliRunner

    from fin import cli, config, ledger

    history = data / "sources" / "market-history"
    history.mkdir(parents=True)
    (history / "ACME.csv").write_text(
        "Date,Close\n2021-03-01,20.00\n2021-03-15,20.00\n2021-03-20,20.00\n"
    )
    f = pdfs("a.pdf", _statement(rows=ROWS))
    runner = CliRunner()
    first = runner.invoke(cli.app, ["import", "etrade", str(f)])
    assert first.exit_code == 0, first.output
    assert "kept a.pdf" in first.output and "agrees with the ledger" in first.output
    again = runner.invoke(cli.app, ["import", "etrade", str(f)])
    assert "imported 0 new" in again.output
    led = ledger.load(config.main_ledger(data))
    assert not led.errors
    assert any(
        e.meta.get("ref") == "etrade:0001:rcv:777" for e in led.entries if hasattr(e, "postings")
    )


def test_a_delivery_takes_the_value_the_receiving_broker_booked(data, pdfs):
    from typer.testing import CliRunner

    from fin import cli, config, ledger, transfers

    history = data / "sources" / "market-history"
    history.mkdir(parents=True)
    (history / "ACME.csv").write_text(
        "Date,Close\n2021-03-01,20.00\n2021-03-15,20.00\n2021-03-20,20.00\n"
    )
    base.ensure_accounts(
        data, {"Assets:IBKR:ACME": {"group": "Investments", "institution": "IBKR"}}
    )
    ibkr = RawTxn(
        date(2021, 3, 20), "Assets:IBKR:ACME", D(5), "ACME", "Interactive Brokers",
        "transfer in 5 ACME", "ibkr:transfer:1", review=False,
        postings=[
            'Assets:IBKR:ACME  5 ACME {21.00 USD, 2021-03-20, "t1"}',
            "Assets:Transfers:Pending  -105.00 USD",
        ],
    )  # fmt: skip
    base.write_new(data, "ibkr", [ibkr])
    rows = [
        "03/20/21 ACME CORP ACME Adjustment 5",
        "TFR FROM TYPE 7",
        "REFID:9",
        "03/20/21 ACME CORP ACME Deliver -5",
        "FREE DEL BRK#0534",
        "03/20/21 ACME CORP ACME Adjustment -5",
        "TFR TO TYPE 1",
        "REFID:9",
        "03/21/21 ACME CORP ACME Receive 5",
        "RAND 1",
    ]
    f = pdfs("a.pdf", _statement(cash=("100.00", "100.00"), held="", rows=rows))
    out = CliRunner().invoke(cli.app, ["import", "etrade", str(f)])
    assert out.exit_code == 0, out.output
    led = ledger.load(config.main_ledger(data))
    assert not led.errors
    deliver = next(e for e in led.entries if (getattr(e, "meta", None) or {}).get("moved_to"))
    assert deliver.meta["moved_to"] == "ibkr:transfer:1"
    pending = sum(
        p.units.number
        for e in led.entries
        if hasattr(e, "postings")
        for p in e.postings
        if p.account == "Assets:Transfers:Pending"
    )
    assert pending == 0  # the two sides cancel
    assert not transfers.share_moves(led)["no_sender"]  # the link stands in for the lot check


def test_tax_due_is_a_cash_debit_and_a_split_adds_shares_at_no_price(pdfs):
    rows = [
        "03/04/21 03/06/21 ACME CORP ACME Sold -10 20.0000 195.00",
        "03/06/21 Adjustment $DUE ACME 4242 60.00",
        "03/06/21 Adjustment OLINK TID 5 195.00",
        "03/06/21 Adjustment TO T-1 TID 5 195.00",
        "03/10/21 ACME CORP ACME Split 30",
        "STK SPLIT ON 10 SHS",
        "REC 03/01/21 PAY 03/09/21",
        "03/18/21 ACME CORP ACME Receive 10",
        "RAND 5",
    ]
    f = pdfs(
        "a.pdf",
        _statement(
            cash=("100.00", "235.00"),
            held="ACME StkPln 40 20.0000 800.00 100.00 0.00 0.00%",
            rows=rows,
        ),
    )
    by_kind = {t.ref.split(":")[2]: t for t in etrade.parse_files([f], price=PRICE)}
    assert by_kind["due"].counter == etrade.TAX_DUE and by_kind["due"].amount == D("-60.00")
    assert by_kind["split"].postings == ["Assets:ETrade:ACME  30 ACME @ 0 USD"]
    assert by_kind["due"].ref == "etrade:0001:due:4242"


def test_the_importer_is_registered_and_names_no_employer():
    from fin import importers

    assert importers.get("etrade") is etrade
    assert "AAPL" not in Path(etrade.__file__).read_text()


def test_a_dollar_wire_from_the_plan_broker_pairs_with_the_franc_deposit_at_the_bank():
    from fin import transfers as T

    class Rates:
        def price(self, *_):
            return D("0.94")

    def leg(ref, day, account, amount, ccy):
        return T.Leg(ref, day, account, D(amount), ccy, "", T.CLEARING, "f", 1)

    bank = leg("ubs:1", date(2018, 3, 2), "Assets:UBS:Checking", "900.00", "CHF")
    wire = leg("etrade:1", date(2018, 2, 28), "Assets:ETrade:Cash:USD", "-1000.00", "USD")
    pairs = T.find_cross_pairs([bank, wire], Rates(), set())
    assert [(b.ref, w.ref) for b, w in pairs] == [("ubs:1", "etrade:1")]
    block = [
        '2018-02-28 * "E*TRADE" "wire out: fx wire"',
        '  ref: "etrade:1"',
        "  Assets:ETrade:Cash:USD  -1000.00 USD",
        "  Assets:Transfers:Pending  1000.00 USD",
    ]
    out = T._rewrite_priced(block, "ubs:1", D("900.00"))
    assert "  Assets:ETrade:Cash:USD  -1000.00 USD @@ 900.00 CHF" in out
    assert "  Assets:Transfers:Pending  900.00 CHF" in out


def test_the_closing_price_of_each_statement_becomes_a_quote_and_balances_are_asserted(pdfs):
    text = etrade.derived([pdfs("a.pdf", _statement(rows=ROWS))])
    assert "2021-03-31 price ACME  20.0000 USD" in text
    assert "2021-04-01 balance Assets:ETrade:ACME  10 ACME" in text
    assert "2021-04-01 balance Assets:ETrade:Cash:USD  108.50 USD" in text


def test_shares_held_without_a_cost_are_valued_through_the_currency_they_are_quoted_in():
    from beancount import loader
    from beancount.core import data

    from fin.analytics.valuation import Market

    entries, errors, _ = loader.load_string(
        """
2021-01-01 open Assets:ETrade:ACME
2021-01-01 open Equity:Opening-Balances
2021-03-01 price ACME 20.00 USD
2021-03-01 price USD 0.90 CHF
2021-03-02 * "plain shares"
  Assets:ETrade:ACME  10 ACME @@ 200.00 USD
  Equity:Opening-Balances  -200.00 USD
"""
    )
    assert not errors
    txn = next(e for e in entries if isinstance(e, data.Transaction))
    pos = txn.postings[0]
    assert pos.cost is None
    from beancount.core.position import Position

    market = Market.of(entries, "CHF")
    assert market.value(Position(pos.units, None), date(2021, 3, 5)) == D(
        "180.00"
    )  # 10 x 20 x 0.90
    with pytest.raises(ValueError, match="No ACME/CHF price"):
        market.value(Position(pos.units, None), date(2021, 2, 1))  # before any quote
