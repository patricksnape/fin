"""UBS mortgage notices (PDF): what each loan's quarterly payment was made of, and when it started.

The e-banking mortgage page gives a loan's debt today and its amortization per year, which is only
enough to guess how a quarterly payment splits. The Safe keeps the bank's own notices:
 * a *maturity notice* each quarter and loan: the debt capital, the interest (rate, period,
   amount), the amortization (when there is one) and the total debited from the payment account;
 * a *payment* notice when a loan was paid out (its principal), and a credit advice for the money
   that reached your account.

`notices(files)` reads them, every notice checked against itself (interest + amortization = the
total due). `fin.mortgage.apply_notices` then splits the payments already booked from the account
statements into interest and amortization, and starts each loan from its real principal on the
day it was paid out. Like `ubs-pdf` this is meant to be run once, but is kept so the history can be
rebuilt.
"""

import functools
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from fin.importers.base import ImportFailed

SOURCE = "ubs-mortgage"
MONEY = r"\d{1,3}(?: \d{3})*\.\d\d|\d+\.\d\d"
LOAN = re.compile(r"Account no\. \d{3}-\d{6}\.(H\w\w) ?(\d{4})?")
AS_AT = re.compile(r"As at (\d\d\.\d\d\.\d{4})")
TOTAL = re.compile(rf"^Total amount due for payment ({MONEY})$")
CAPITAL = re.compile(rf"^Current debt capital ({MONEY})$")
# an interest row; the newer notices have a column with the number of days before the amount
INTEREST = {
    False: re.compile(rf"^Interest .*?(\d\d\.\d\d\.\d{{4}}) (\d\d\.\d\d\.\d{{4}}) ({MONEY})$"),
    True: re.compile(
        rf"^Interest .*?(\d\d\.\d\d\.\d{{4}}) (\d\d\.\d\d\.\d{{4}}) \d{{1,3}} ({MONEY})$"
    ),
}
AMORT = re.compile(rf"^Amortization ({MONEY})$")
PAYMENT = re.compile(rf"^Payment ({MONEY})$")
CREDIT = re.compile(rf"^Payment mortgage (\d\d\.\d\d\.\d{{4}}) ({MONEY})$")


@dataclass(frozen=True)
class Notice:
    kind: str  # "maturity" | "payout" | "credit"
    loan: (
        str | None
    )  # the loan's suffix in the account number (e.g. ABC), or None for a credit advice
    day: date  # a maturity notice's due day; a payout's or credit's value day
    capital: Decimal | None = None  # debt capital before the payment (maturity notices)
    interest: Decimal = Decimal(0)
    amortization: Decimal = Decimal(0)
    total: Decimal = Decimal(0)  # debited (maturity), paid out or credited
    path: str = ""


def _money(text: str) -> Decimal:
    return Decimal(text.replace(" ", ""))


def _day(text: str) -> date:
    return datetime.strptime(text, "%d.%m.%Y").date()


def _clean(line: str) -> str:
    """A heading printed twice over itself comes out with every character doubled."""
    line = line.strip()
    if len(line) > 6 and re.fullmatch(r"(?:(\S)\1|\s)+", line):
        return re.sub(r"(\S)\1", r"\1", line)
    return line


def _lines(path: Path) -> list[str]:
    try:
        import pdfplumber
    except ImportError as e:
        raise ImportFailed("pdfplumber is not installed: uv sync --extra pdf") from e
    with pdfplumber.open(path) as pdf:
        return [
            _clean(ln)
            for p in pdf.pages
            for ln in (p.extract_text(x_tolerance=2) or "").splitlines()
        ]


def read_notice(path: Path) -> Notice:
    stat = path.stat()
    return _read_cached(path, stat.st_mtime_ns, stat.st_size)


@functools.lru_cache(maxsize=256)
def _read_cached(path: Path, _mtime: int, _size: int) -> Notice:
    lines = _lines(path)
    text = "\n".join(lines)
    loan = LOAN.search(text)
    if "Credit advice" in text:
        m = next((CREDIT.match(ln) for ln in lines if CREDIT.match(ln)), None)
        if not m:
            raise ImportFailed(f"{path.name}: a credit advice without its mortgage payment line")
        return Notice("credit", None, _day(m.group(1)), total=_money(m.group(2)), path=path.name)
    if not loan:
        raise ImportFailed(f"{path.name}: not a UBS mortgage notice fin knows (no loan account)")
    suffix = loan.group(1)
    as_at = AS_AT.search(text)
    if "Maturity notice" in text:
        capital = next((CAPITAL.match(ln) for ln in lines if CAPITAL.match(ln)), None)
        total = next((TOTAL.match(ln) for ln in lines if TOTAL.match(ln)), None)
        if not (as_at and capital and total):
            raise ImportFailed(f"{path.name}: a maturity notice without its date, capital or total")
        row = INTEREST["Days" in text]  # the newer layout has a days column before the amount
        interest = sum((_money(m.group(3)) for ln in lines if (m := row.match(ln))), Decimal(0))
        amort = sum((_money(m.group(1)) for ln in lines if (m := AMORT.match(ln))), Decimal(0))
        if interest + amort != _money(total.group(1)):
            raise ImportFailed(
                f"{path.name}: interest and amortization do not add up to the total due"
            )
        return Notice("maturity", suffix, _day(as_at.group(1)), _money(capital.group(1)), interest, amort, _money(total.group(1)), path.name)  # fmt: skip
    pay = next((PAYMENT.match(ln) for ln in lines if PAYMENT.match(ln)), None)
    if pay and as_at and "We are pleased to confirm the following payment" in text:
        return Notice(
            "payout", suffix, _day(as_at.group(1)), total=_money(pay.group(1)), path=path.name
        )
    raise ImportFailed(
        f"{path.name}: a mortgage document fin does not book (certificate or cancellation)"
    )


def notices(files: list[Path]) -> list[Notice]:
    """Every maturity notice, payout and credit advice once, oldest first. Certificates and
    cancellations carry nothing to book and are skipped."""
    seen: dict[tuple, Notice] = {}
    for f in files:
        try:
            n = read_notice(f)
        except ImportFailed as e:
            if "does not book" in str(e):
                continue
            raise
        seen.setdefault((n.kind, n.loan, n.day, n.total), n)
    return sorted(seen.values(), key=lambda n: (n.day, n.kind, n.loan or ""))


# ---- as a source: no new transactions, but it settles what the statements left open -------------


def source_files(root: Path) -> list[Path]:
    return sorted((root / "sources" / SOURCE).glob("*.pdf"))


def accept(root: Path, path: Path) -> Path:
    """Keep a notice you downloaded from the Safe in sources/ubs-mortgage/ after reading it."""
    import shutil

    try:
        read_notice(path)
    except ImportFailed as e:
        if "does not book" not in str(e):  # a certificate or cancellation is kept, just not read
            raise
    out_dir = root / "sources" / SOURCE
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / path.name
    if not target.exists():
        shutil.copyfile(path, target)
    return target


def parse_files(files: list[Path]) -> list:
    return []  # the payments themselves come from the account statements


def after_import(root: Path) -> list[str]:
    from fin import mortgage

    done = mortgage.apply_notices(root, notices(source_files(root)))
    out = [f"{done[k]} {label}" for k, label in (("split", "payment(s) split into interest and amortization"), ("payouts", "loan payout(s) booked"), ("openings", "opening balance(s) replaced by the real start")) if done.get(k)]  # fmt: skip
    return [*out, *(f"mortgage mismatch: {p}" for p in mortgage.check(root))]
