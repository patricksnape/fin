import json
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from fin.importers import base, ubs_pdf

HEAD = [
    "Ada Example",
    "UBS personal account CHF",
    "CH00 0000 0000 0000 0000 G",
    "Account Statement 01.03.2019-31.03.2019",
    "Account 000-000000.40G",
]
TABLE = "Date Information Debits Credits Value date Balance"
FOOT = "Form without signature Page 1 / 1"


def _statement(
    rows: list[str], start="01.03.2019", end="31.03.2019", account="40G", head=None
) -> list[str]:
    top = head or [*HEAD[:3], f"Account Statement {start}-{end}", f"Account 000-000000.{account}"]
    return [*top, TABLE, *rows, FOOT]


@pytest.fixture
def pdfs(monkeypatch, tmp_path):
    """Statement PDFs by name -> their pages (lists of text lines)."""
    pages: dict[str, list[list[str]]] = {}

    def fake(path: Path):
        return pages[path.name]

    monkeypatch.setattr(ubs_pdf, "_pages", fake)

    def make(name, *page_lines):
        pages[name] = [list(p) for p in page_lines]
        path = tmp_path / name
        path.write_bytes(b"%PDF-1.4 fake")
        return path

    return make


OPEN = "01.03.19 Opening balance 1 000.00"
CLOSE = "31.03.19 Closing balance 1 288.50"
ROWS = [
    OPEN,
    "04.03.19 DEBIT CARD PAYMENT 12.50 01.03.19 987.50",
    "Cafe Example",
    "CARD 12345678-1 1234",
    "ON 01.03.19 12:00:00 ID: 1234567",
    "15.03.19 SALARY PAYMENT 1 500.00 15.03.19 2 487.50",
    "Employer AG",
    "20.03.19 DIRECT DEBIT 1 200.00 20.03.19 1 287.50",
    "Landlord GmbH",
    "Rent March",
    "29.03.19 BALANCE CLOSING OF SERVICE PRICES 0.00 29.03.19 1 287.50",
    "1 times E-Banking CHF domestic",
    "30.03.19 CREDIT 1.00 30.03.19 1 288.50",
    "Friend",
    CLOSE,
]


def test_rows_take_their_sign_from_the_running_balance(pdfs):
    f = pdfs("a.pdf", _statement(ROWS))
    st = ubs_pdf.read_statement(f)
    assert (st.account, st.start, st.end, st.opening, st.closing) == (
        "40G",
        date(2019, 3, 1),
        date(2019, 3, 31),
        D("1000.00"),
        D("1288.50"),
    )
    assert [r["signed"] for r in st.rows] == [
        D("-12.50"),
        D("1500.00"),
        D("-1200.00"),
        D("0.00"),
        D("1.00"),
    ]
    assert st.rows[0]["detail"][0] == "Cafe Example"


def test_payee_is_the_first_line_that_is_not_a_payment_marker(pdfs):
    out = ubs_pdf.parse_files([pdfs("a.pdf", _statement(ROWS))])
    card, salary, rent = out[0], out[1], out[2]
    assert card.payee == "Cafe Example" and "CARD 12345678-1 1234" in card.narration
    assert (card.amount, card.account, card.date) == (
        D("-12.50"),
        "Assets:UBS:Checking",
        date(2019, 3, 4),
    )
    assert card.meta == {"bank_type": "Debit Card Payment", "value_date": "2019-03-01"}
    assert salary.payee == "Employer AG" and salary.amount == D("1500.00")
    assert rent.narration == "Direct Debit · Rent March"
    assert all(t.review for t in (card, rent))
    assert card.counter is None and rent.counter is None  # debits wait as uncategorized expenses
    assert salary.counter == "Income:Employer:Salary" and not salary.review


def test_bank_charges_are_filed_as_fees_without_review(pdfs):
    fee = ubs_pdf.parse_files([pdfs("a.pdf", _statement(ROWS))])[3]
    assert fee.counter == "Expenses:Fees" and not fee.review and fee.payee == "UBS Switzerland AG"
    assert "1 times E-Banking CHF domestic" in fee.narration


def test_a_row_that_does_not_match_the_balance_is_refused(pdfs):
    rows = [*ROWS]
    rows[1] = "04.03.19 DEBIT CARD PAYMENT 12.60 01.03.19 987.50"
    with pytest.raises(base.ImportFailed, match="does not match the running balance"):
        ubs_pdf.read_statement(pdfs("a.pdf", _statement(rows)))


def test_rows_must_end_at_the_closing_balance(pdfs):
    rows = [*ROWS[:-1], "31.03.19 Closing balance 1 288.51"]
    with pytest.raises(base.ImportFailed, match="different balance than the closing balance"):
        ubs_pdf.read_statement(pdfs("a.pdf", _statement(rows)))


def test_page_headers_and_footers_are_not_detail(pdfs):
    first = _statement([*ROWS[:4]])[:-1] + [FOOT]  # a footer closes page 1 mid-statement
    second = ["Ada Example", "CH00 0000 0000 0000 0000 G", TABLE, *ROWS[4:], FOOT]
    st = ubs_pdf.read_statement(pdfs("a.pdf", first, second))
    assert len(st.rows) == 5
    assert all(
        "Form without" not in d and "Ada Example" not in d for r in st.rows for d in r["detail"]
    )


def test_checkpoint_rows_and_trailing_minus_amounts(pdfs):
    rows = [
        "01.03.19 Opening balance 1 000.00",
        "02.03.19 Balance carried forward 1 000.00",
        "03.03.19 CANC.MORT.MAT. 100.00- 03.03.19 900.00",
        "31.03.19 Closing balance 900.00",
    ]
    st = ubs_pdf.read_statement(pdfs("m.pdf", _statement(rows, account="M2X")))
    assert [r["signed"] for r in st.rows] == [D("-100.00")]


def test_unknown_files_are_refused(pdfs):
    with pytest.raises(base.ImportFailed, match="not a UBS account statement"):
        ubs_pdf.read_statement(pdfs("x.pdf", ["Some other document", "nothing here"]))


def test_the_same_period_twice_counts_once_and_sub_accounts_wait_for_a_mapping(pdfs):
    a, b = pdfs("a.pdf", _statement(ROWS)), pdfs("b.pdf", _statement(ROWS))
    other = pdfs(
        "m.pdf",
        _statement(
            ["01.03.19 Opening balance 5.00", "31.03.19 Closing balance 5.00"], account="M2X"
        ),
    )
    assert len(ubs_pdf.statements([a, b, other])) == 2
    assert len(ubs_pdf.parse_files([a, b, other])) == 5  # only the mapped account is booked


def test_identical_rows_get_distinct_refs(pdfs):
    rows = [
        "01.03.19 Opening balance 100.00",
        "02.03.19 PAYMENT UBS TWINT 10.00 02.03.19 90.00",
        "Kiosk",
        "02.03.19 PAYMENT UBS TWINT 10.00 02.03.19 80.00",
        "Kiosk",
        "31.03.19 Closing balance 80.00",
    ]
    out = ubs_pdf.parse_files([pdfs("a.pdf", _statement(rows))])
    assert len({t.ref for t in out}) == 2
    again = ubs_pdf.parse_files([pdfs("a.pdf", _statement(rows))])
    assert [t.ref for t in out] == [t.ref for t in again]  # stable across runs


def _api(root, day="2024-01-01"):
    src = root / "sources" / "ubs"
    src.mkdir(parents=True, exist_ok=True)
    (src / f"{day}_2024-02-01_current.json").write_text(json.dumps({"startDate": day, "pages": []}))


def test_only_history_before_the_api_is_booked_and_balances_are_asserted(data, pdfs, monkeypatch):
    from fin import cli, config, ledger

    _api(data, "2019-03-20")
    f = pdfs("a.pdf", _statement(ROWS))
    ubs_pdf.accept(data, f)
    out = ubs_pdf.finish(data, ubs_pdf.parse_files([f]))
    assert [t.date.day for t in out] == [4, 15]  # 20 March onward is the API's
    (data / "ledger" / "accounts.beancount").open("a").write(
        "2016-01-01 open Assets:UBS:Checking\n"
    )
    assert ubs_pdf.opening_balances(data) == [
        (
            date(2019, 3, 1),
            "Assets:UBS:Checking",
            D("1000.00"),
            "CHF",
            "opening:Assets:UBS:Checking",
        )
    ]
    assert (
        ubs_pdf.derived(data).count("balance Assets:UBS:Checking") == 1
    )  # a statement reaching into the API's days is asserted too, against the API's rows
    led = ledger.load(config.main_ledger(data))
    assert not [e for e in led.errors if "Assets:UBS" not in str(e)]
    assert cli is not None


def test_import_end_to_end_and_taking_downloaded_files(data, pdfs):
    from typer.testing import CliRunner

    from fin import cli, config, ledger

    _api(data, "2019-04-01")
    f = pdfs("a.pdf", _statement(ROWS))
    runner = CliRunner()
    first = runner.invoke(cli.app, ["import", "ubs-pdf", str(f)])
    assert first.exit_code == 0, first.output
    assert "kept a.pdf" in first.output and "imported 5 new" in first.output
    assert "agrees with the ledger" in first.output
    again = runner.invoke(cli.app, ["import", "ubs-pdf", str(f)])
    assert "imported 0 new" in again.output
    led = ledger.load(config.main_ledger(data))
    assert not led.errors


def test_interest_is_income_and_negative_interest_a_fee(pdfs):
    rows = [
        "01.03.19 Opening balance 100.00",
        "10.03.19 INTEREST CALCULATION BALANCE 1.00 10.03.19 101.00",
        "20.03.19 INTEREST CALCULATION BALANCE 0.50 20.03.19 100.50",
        "31.03.19 Closing balance 100.50",
    ]
    up, down = ubs_pdf.parse_files([pdfs("a.pdf", _statement(rows))])
    assert (up.counter, down.counter) == ("Income:Interest", "Expenses:Fees")
    assert not up.review and not down.review


def test_other_credits_wait_as_income(pdfs):
    credit = ubs_pdf.parse_files([pdfs("a.pdf", _statement(ROWS))])[4]
    assert credit.payee == "Friend" and credit.counter == "Income:Uncategorized" and credit.review


def test_a_fetch_starts_a_little_before_the_newest_statement_held(data):
    folder = data / "sources" / "ubs-pdf"
    folder.mkdir(parents=True)
    assert ubs_pdf.default_start(data) is None
    for name in ("1_AccountStatement_20260101111111.pdf", "1_AccountStatement_20261001122357.pdf"):
        (folder / name).write_bytes(b"%PDF")
    assert ubs_pdf.default_start(data) == date(2026, 7, 31)  # 62 days before 2026-10-01


def test_sync_skips_sources_that_need_their_own_login_and_those_without_a_fetch(data, monkeypatch):
    from typer.testing import CliRunner

    from fin import cli, importers

    assert ubs_pdf.IN_SYNC is False and not hasattr(importers.get("etrade"), "fetch")
    seen = []
    monkeypatch.setattr(
        cli, "_fetch_and_import", lambda root, name, **kw: seen.append(name) or name
    )
    CliRunner().invoke(cli.app, ["sync", "--help"])
    run = CliRunner().invoke(cli.app, ["sync"])
    assert "ubs-pdf" not in seen, run.output
