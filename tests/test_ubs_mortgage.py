import json
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest
import yaml

from fin import config, gitops, ledger, mortgage
from fin.importers import base
from fin.importers import ubs_mortgage as M

MATURITY = [
    "UBS SARON Mortgage CHF",
    "Account no. 000-123456.H2A 0001",
    "Maturity notice",
    "AAss aatt 3300..0099..22002211",  # printed twice over itself: every character doubled
    "General information Amount in CHF",
    "Current debt capital 500 000.00",
    "Description Capital amount Comp. Margin Interest From Until Days Amount",
    "in CHF SARON rate in CHF",
    "Interest 500 000.00 1.1000% 0.8000% 1.9000% 01.07.2021 30.09.2021 92 2 427.78",
    "Amortization 2 500.00",
    "Total amount due for payment 4 927.78",
    "Account no. 000-123456.M2Z",
    "Form without signature Page 1 / 1",
]
OLD_LAYOUT = [  # no days column: the amount's thousand separator must not read as days
    "Account no. 000-123456.H2B 0002",
    "Maturity notice",
    "As at 31.12.2021",
    "Current debt capital 80 000.00",
    "Description Capital amount in CHF Interest rate From Until Amount in CHF",
    "Interest 80 000.00 0.9000 % 30.07.2021 31.12.2021 1 104.00",
    "Total amount due for payment 1 104.00",
]
PAYOUT = [
    "Account no. 000-123456.H2B 0002",
    "Payment",
    "As at 30.07.2021",
    "We are pleased to confirm the following payment:",
    "Payment 80 000.00",
]


@pytest.fixture
def pdfs(monkeypatch, tmp_path):
    texts: dict[str, list[str]] = {}
    monkeypatch.setattr(M, "_lines", lambda path: [M._clean(ln) for ln in texts[path.name]])

    def make(name, lines):
        texts[name] = lines
        path = tmp_path / name
        path.write_bytes(b"%PDF fake")
        return path

    return make


def test_maturity_notice_fields_and_the_doubled_heading(pdfs):
    n = M.read_notice(pdfs("a.pdf", MATURITY))
    assert (n.kind, n.loan, n.day) == ("maturity", "H2A", date(2021, 9, 30))
    assert (n.capital, n.interest, n.amortization, n.total) == (
        D("500000.00"),
        D("2427.78"),
        D("2500.00"),
        D("4927.78"),
    )


def test_the_older_layout_has_no_days_column(pdfs):
    n = M.read_notice(pdfs("b.pdf", OLD_LAYOUT))
    assert (n.interest, n.amortization, n.total) == (D("1104.00"), D(0), D("1104.00"))


def test_payout_and_credit_notices(pdfs):
    p = M.read_notice(pdfs("p.pdf", PAYOUT))
    assert (p.kind, p.loan, p.day, p.total) == ("payout", "H2B", date(2021, 7, 30), D("80000.00"))
    c = M.read_notice(
        pdfs(
            "c.pdf",
            [
                "Account no. 000-123456.M2Z",
                "Credit advice",
                "We will credit you with the following:",
                "Payment mortgage 30.07.2021 80 000.00",
            ],
        )
    )
    assert (c.kind, c.loan, c.total) == ("credit", None, D("80000.00"))


def test_a_notice_that_does_not_add_up_is_refused(pdfs):
    bad = [
        ln.replace("Total amount due for payment 4 927.78", "Total amount due for payment 4 927.79")
        for ln in MATURITY
    ]
    with pytest.raises(base.ImportFailed, match="do not add up"):
        M.read_notice(pdfs("a.pdf", bad))


def test_certificates_and_duplicates_are_skipped_or_counted_once(pdfs):
    cert = pdfs("cert.pdf", ["Account no. 000-123456.H2A 0001", "Interest certificate"])
    a, b = pdfs("a.pdf", MATURITY), pdfs("b.pdf", MATURITY)
    assert [n.kind for n in M.notices([cert, a, b])] == ["maturity"]


@pytest.fixture
def books(data):
    with (data / "ledger" / "accounts.beancount").open("a") as f:
        f.write(
            "".join(
                f"2020-01-01 open {a}\n"
                for a in (
                    "Assets:UBS:Savings", "Assets:Property:Home", "Expenses:Uncategorized",
                    "Income:Uncategorized", "Liabilities:UBS:Mortgage:Loan1", "Liabilities:UBS:Mortgage:Loan2",
                    mortgage.INTEREST,
                )
            )
        )  # fmt: skip
    (data / "ledger" / "manual").mkdir(exist_ok=True)
    (data / "ledger" / "opening" / "manual.beancount").write_text(
        '2022-01-01 * "Opening balance" "x"\n  ref: "opening:Liabilities:UBS:Mortgage:Loan1"\n'
        "  Liabilities:UBS:Mortgage:Loan1  -1000 CHF\n  Equity:Opening-Balances  1000 CHF\n"
    )
    folder = data / "sources" / "ubs" / "mortgages"
    folder.mkdir(parents=True)
    loan = lambda no, bal, am, start: {
        "number": no,
        "mortgageAmount": {"amount": bal},
        "directAmortizationPerYear": {"amount": am},
        "startDate": start,
    }
    (folder / "2026-10-27.json").write_text(json.dumps({"mortgages": {"mortgagesByRealEstates": [{"mortgages": [
        loan("0000 00123456.H2A 0001", "497500", "10000", "2021-06-30"),
        loan("0000 00123456.H2B 0002", "79700", "1200", "2021-07-30"),
    ]}]}}))  # fmt: skip
    (data / "rules").mkdir(exist_ok=True)
    (data / "rules" / "mortgages.yaml").write_text("pays_from: Assets:UBS:Savings\n")
    (data / "rules" / "mortgage-notices.yaml").write_text(
        yaml.safe_dump({"payout_to": {"H2A": "Assets:Property:Home"}})
    )
    gitops.commit_all(data, "setup")
    return data


def _pay(day, ref, amount, payee="Maturity", counter=None):
    return base.RawTxn(
        date=day, account="Assets:UBS:Savings", amount=D(amount), currency="CHF", payee=payee,
        narration="Maturity", ref=ref, meta={"bank_type": "Maturity"}, counter=counter,
    )  # fmt: skip


def _notice(kind, loan, day, total, interest=D(0), amort=D(0)):
    return M.Notice(
        kind,
        loan,
        day,
        D(1) if kind == "maturity" else None,
        D(interest),
        D(amort),
        D(total),
        "x.pdf",
    )


def test_notices_settle_payments_payouts_and_openings(books):
    # the statements left: two quarterly payments (one with amortization), and the loan's drawdown
    base.write_new(books, "ubs-pdf", [
        _pay(date(2021, 9, 30), "p1", "-145.30"),
        _pay(date(2021, 12, 31), "p2", "-432.10"),
        _pay(date(2021, 8, 4), "p3", "80000", payee="Payment", counter="Equity:Opening-Balances"),
    ])  # fmt: skip
    gitops.commit_all(books, "payments")
    ns = [
        _notice("maturity", "H2B", date(2021, 9, 30), "145.30", interest="145.30"),
        _notice("maturity", "H2B", date(2021, 12, 31), "432.10", interest="132.10", amort="300"),
        _notice("payout", "H2B", date(2021, 7, 30), "80000"),
        _notice("payout", "H2A", date(2021, 6, 30), "500000"),
    ]
    done = mortgage.apply_notices(books, ns)
    assert done == {"split": 2, "payouts": 2, "openings": 1}
    led = ledger.load(config.main_ledger(books))
    assert not led.errors
    by = {
        e.meta.get("ref"): {p.account: p.units.number for p in e.postings}
        for e in led.entries
        if hasattr(e, "postings")
    }
    assert by["p1"] == {"Assets:UBS:Savings": D("-145.30"), mortgage.INTEREST: D("145.30")}
    assert by["p2"] == {
        "Assets:UBS:Savings": D("-432.10"),
        "Liabilities:UBS:Mortgage:Loan2": D(300),
        mortgage.INTEREST: D("132.10"),
    }
    assert by["p3"]["Liabilities:UBS:Mortgage:Loan2"] == D(
        -80000
    )  # the drawdown reached the account
    assert by["mortgage:payout:H2A:2021-06-30"] == {
        "Assets:Property:Home": D(500000),
        "Liabilities:UBS:Mortgage:Loan1": D(-500000),
    }
    assert not [
        e
        for e in led.entries
        if hasattr(e, "meta") and str(e.meta.get("ref", "")).startswith("opening:Liabilities")
    ]
    assert mortgage.apply_notices(books, ns) == {
        "split": 0,
        "payouts": 0,
        "openings": 0,
    }  # idempotent


def test_a_payout_that_bought_something_needs_to_say_what(books):
    (books / "rules" / "mortgage-notices.yaml").write_text("payout_to: {}\n")
    with pytest.raises(base.ImportFailed, match="payout_to"):
        mortgage.apply_notices(books, [_notice("payout", "H2A", date(2021, 6, 30), "500000")])


def test_the_source_takes_downloaded_files(books, pdfs):
    from typer.testing import CliRunner

    from fin import cli

    f = pdfs("a.pdf", MATURITY)
    out = CliRunner().invoke(cli.app, ["import", "ubs-mortgage", str(f)])
    assert out.exit_code == 0, out.output
    assert "kept a.pdf" in out.output
    assert Path(books / "sources" / "ubs-mortgage" / "a.pdf").exists()
