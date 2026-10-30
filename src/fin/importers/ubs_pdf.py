"""UBS account statements (PDF) for the history the e-banking API no longer offers.

The e-banking site's own API reaches back to January 2024; the Safe (Documents) keeps monthly
account statements for about ten years. Run `fin fetch ubs-pdf` to download the ones you do not hold
yet (you log in and open the Safe yourself; see `fetch`), or download them yourself and run
`fin import ubs-pdf <files>`. fin keeps the PDFs in `sources/ubs-pdf/` and books every row they list
before the API's history begins; rows from the day the API starts are the API's, so nothing is booked
twice, and the statements' balances check the API's rows.

A statement is text, not a scan. Each page repeats a header block and ends with a footer; the rows
are `date | information | amount | value date | running balance`, followed by lines of detail
(payee, reference, card number). The amount is printed in a debit or a credit column that the text
does not tell apart, so the sign always comes from the change in the running balance, and every
row is checked against it: a statement whose rows do not add up to its own closing balance is
refused, never guessed. The running balances also become `balance` assertions in the ledger.
"""

import functools
import hashlib
import re
import tempfile
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from fin.importers import base
from fin.importers.base import ImportFailed, RawTxn

SOURCE = "ubs-pdf"
CLEARING = "Assets:Transfers:Pending"
FEES_ACCOUNT = "Expenses:Fees"
CHECKING = "Assets:UBS:Checking"
DERIVED = Path("ledger") / "imported" / SOURCE / "derived"
# the account in the statement's header -> the ledger account (rules/ubs-pdf.yaml can add more)
ACCOUNTS = {
    "40G": CHECKING
}  # the others (a mortgage or tax account's own statements) are mapped in rules/ubs-pdf.yaml

NUM = r"-?\d{1,3}(?: \d{3})*\.\d\d-?|-?\d+\.\d\d-?"  # a trailing minus is a negative amount
ROW = re.compile(rf"^(\d\d\.\d\d\.\d\d) (.+?)(?: ({NUM}) (\d\d\.\d\d\.\d\d))? ({NUM})$")
PERIOD = re.compile(r"Account Statement\s*(\d\d\.\d\d\.\d{4})\s*-\s*(\d\d\.\d\d\.\d{4})")
ACCOUNT_NO = re.compile(r"\b\d{3}-\d{6}\.([0-9A-Z]{3})\b")
TABLE_HEAD = re.compile(r"^Date Information Debits Credits Value date Balance")
FOOTER = re.compile(r"^Form without signature")
# detail lines that say how a payment was made, not whom it went to
MARKERS = re.compile(
    r"^(CARD \S|ON \d\d\.\d\d\.\d\d|TWINT-ACC|AMOUNT |\d+ times |REFERENCE|ISR |ESR )",
    re.IGNORECASE,
)
FEE_ROWS = ("BALANCE CLOSING OF SERVICE PRICES",)  # the bank's own charges
INTEREST_ROWS = ("INTEREST CALCULATION BALANCE",)  # interest earned (or, if negative, paid)
SALARY_ROWS = ("SALARY PAYMENT",)  # net pay; payslips split it later
INTEREST_ACCOUNT = "Income:Interest"
SALARY_ACCOUNT = "Income:Employer:Salary"
UNCATEGORIZED_IN = "Income:Uncategorized"
CARD_COMPANIES = ("swisscard", "ubs card center")
X_TOL = 2  # smaller than the default keeps the spaces between words


def source_files(root: Path) -> list[Path]:
    return sorted((root / "sources" / SOURCE).glob("*.pdf"))


def accept(root: Path, path: Path) -> Path:
    """Keep a PDF you downloaded from the Safe in sources/ubs-pdf/ after checking it is an account
    statement fin can read. The same file twice is kept once."""
    import shutil

    read_statement(path)  # refuses anything it does not recognise or that does not add up
    out_dir = root / "sources" / SOURCE
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / path.name
    if not target.exists():
        shutil.copyfile(path, target)
    return target


# ---- reading a statement -----------------------------------------------------------------------


def _num(text: str) -> Decimal:
    text = text.replace(" ", "")
    return -Decimal(text[:-1]) if text.endswith("-") else Decimal(text)


def _day(text: str) -> date:
    return datetime.strptime(text, "%d.%m.%y" if len(text) == 8 else "%d.%m.%Y").date()


class Statement:
    def __init__(
        self,
        path: Path,
        account: str,
        start: date,
        end: date,
        opening: Decimal,
        closing: Decimal,
        rows: list[dict],
    ):
        self.path, self.account, self.start, self.end = path, account, start, end  # fmt: skip
        self.opening, self.closing, self.rows = opening, closing, rows

    @property
    def key(self) -> tuple[str, date, date]:
        return self.account, self.start, self.end


def _pages(path: Path) -> list[list[str]]:
    try:
        import pdfplumber
    except ImportError as e:
        raise ImportFailed("pdfplumber is not installed: uv sync --extra pdf") from e
    with pdfplumber.open(path) as pdf:
        return [(p.extract_text(x_tolerance=X_TOL) or "").splitlines() for p in pdf.pages]


def read_statement(path: Path) -> Statement:
    """One statement PDF -> its rows, every one checked against the running balance. Reading a PDF
    takes about a second, so a file is read once per run (and again if it changes)."""
    stat = path.stat()
    return _read_cached(path, stat.st_mtime_ns, stat.st_size)


@functools.lru_cache(maxsize=512)
def _read_cached(path: Path, _mtime: int, _size: int) -> Statement:
    pages = _pages(path)
    head = "\n".join(pages[0][:40]) if pages else ""
    period, account = PERIOD.search(head), ACCOUNT_NO.search(head)
    if not period or not account:
        raise ImportFailed(
            f"{path.name}: not a UBS account statement fin knows (no period or account)"
        )
    start, end = _day(period.group(1)), _day(period.group(2))
    rows: list[dict] = []
    for lines in pages:
        try:
            first = next(i for i, ln in enumerate(lines) if TABLE_HEAD.match(ln)) + 1
        except StopIteration:
            continue  # a page of the document without table rows (e.g. a closing note)
        last = next((i for i, ln in enumerate(lines) if FOOTER.match(ln)), len(lines))
        for line in lines[first:last]:
            m = ROW.match(line.strip())
            if m:
                rows.append({"date": m.group(1), "info": m.group(2), "amount": m.group(3), "value": m.group(4), "balance": m.group(5), "detail": []})  # fmt: skip
            elif rows:
                rows[-1]["detail"].append(line.strip())
    if not rows or not rows[0]["info"].lower().startswith("opening balance"):
        raise ImportFailed(f"{path.name}: the first row is not the opening balance")
    if not rows[-1]["info"].lower().startswith("closing balance"):
        raise ImportFailed(f"{path.name}: the last row is not the closing balance")
    opening, closing = _num(rows[0]["balance"]), _num(rows[-1]["balance"])
    prev, body = opening, []
    for r in rows[1:-1]:
        balance, amount = _num(r["balance"]), _num(r["amount"]) if r["amount"] else None
        if amount is None and balance == prev:
            continue  # a balance shown part-way (a checkpoint), no movement
        if amount is None or abs(balance - prev) != abs(amount):
            raise ImportFailed(
                f"{path.name}: the row of {r['date']} does not match the running balance"
            )
        r["signed"] = balance - prev
        r["balance_after"] = balance
        prev = balance
        body.append(r)
    if prev != closing:
        raise ImportFailed(
            f"{path.name}: the rows end at a different balance than the closing balance"
        )
    # the closing row's detail lines are the bank's page notes, not part of the last transaction
    if body:
        body[-1]["detail"] = body[-1]["detail"][: _until_notes(body[-1]["detail"])]
    return Statement(path, account.group(1), start, end, opening, closing, body)


def _until_notes(detail: list[str]) -> int:
    """Where a last row's own detail ends: the bank's closing remarks start at a repeated header."""
    for i, line in enumerate(detail):
        if line.lower().startswith(("closing balance", "total")):
            return i
    return len(detail)


# ---- booking -----------------------------------------------------------------------------------


def _payee(info: str, detail: list[str]) -> tuple[str, list[str]]:
    """The first detail line that is not a how-it-was-paid marker is whom it was; the remaining
    lines (card, reference, address) are the narration."""
    text = [ln for ln in detail if ln]
    for i, line in enumerate(text):
        if not MARKERS.match(line):
            return line.strip(), text[:i] + text[i + 1 :]
    return info.title(), text


def _ref(account: str, row: dict, nth: int) -> str:
    parts = (
        account,
        row["date"],
        row["info"],
        row["amount"],
        row["balance"],
        "|".join(row["detail"]),
        str(nth),
    )
    return f"{SOURCE}:{account}:{hashlib.sha256('|'.join(parts).encode()).hexdigest()[:16]}"


def _txn(account: str, ledger_account: str, row: dict, nth: int) -> RawTxn:
    info, signed = row["info"], row["signed"]
    payee, rest = _payee(info, row["detail"])
    kind = info.upper()
    bank_own = kind in FEE_ROWS or kind in INTEREST_ROWS  # the bank says what these are
    salary = kind in SALARY_ROWS and signed > 0
    counter = None
    if kind in FEE_ROWS:
        counter = FEES_ACCOUNT
    elif kind in INTEREST_ROWS:
        counter = INTEREST_ACCOUNT if signed > 0 else FEES_ACCOUNT
    elif salary:
        counter = SALARY_ACCOUNT
    elif signed > 0:
        counter = UNCATEGORIZED_IN  # as for the API: a credit waits for review as income
    meta = {"bank_type": info.title()}
    if row["value"] and row["value"] != row["date"]:
        meta["value_date"] = _day(row["value"]).isoformat()
    text = " · ".join(rest)
    return RawTxn(
        date=_day(row["date"]),
        account=ledger_account,
        amount=signed,
        currency="CHF",
        payee="UBS Switzerland AG" if bank_own else payee,
        narration=(info.title() + (" · " + text if text else "")),
        ref=_ref(account, row, nth),
        meta=meta,
        counter=counter,
        review=not (bank_own or salary),
    )


def statements(files: list[Path]) -> list[Statement]:
    """Every statement once (the Safe can list a period twice), oldest first."""
    seen: dict[tuple, Statement] = {}
    for f in files:
        st = read_statement(f)
        if st.key in seen and (
            seen[st.key].opening,
            seen[st.key].closing,
            len(seen[st.key].rows),
        ) != (st.opening, st.closing, len(st.rows)):
            raise ImportFailed(
                f"{f.name} and {seen[st.key].path.name} cover the same period but differ"
            )
        seen.setdefault(st.key, st)
    return sorted(seen.values(), key=lambda s: (s.account, s.start))


def account_map(root: Path | None = None) -> dict[str, str]:
    out = dict(ACCOUNTS)
    if root is not None and (path := root / "rules" / "ubs-pdf.yaml").exists():
        import yaml

        out.update((yaml.safe_load(path.read_text()) or {}).get("accounts") or {})
    return out


def parse_files(files: list[Path], root: Path | None = None) -> list[RawTxn]:
    mapped = account_map(root)
    out: list[RawTxn] = []
    for st in statements(files):
        if st.account not in mapped:
            continue  # a sub-account fin has no ledger account for (add it in rules/ubs-pdf.yaml)
        seen: dict[tuple, int] = {}
        for row in st.rows:
            key = (row["date"], row["info"], row["amount"], row["balance"], tuple(row["detail"]))
            seen[key] = seen.get(key, 0) + 1
            out.append(_txn(st.account, mapped[st.account], row, seen[key]))
    return out


def parse_root(root: Path, files: list[Path]) -> list[RawTxn]:
    return finish(root, parse_files(files, root))


def finish(root: Path, txns: list[RawTxn]) -> list[RawTxn]:
    """Leave out what the e-banking API already provides: rows from its first day on."""
    first = api_start(root)
    return [t for t in txns if first is None or t.date < first]


def api_start(root: Path) -> date | None:
    """The first day of the UBS API's history, from its archives."""
    from fin.importers import ubs

    days = []
    for f in ubs.source_files(root):
        days.append(date.fromisoformat(ubs.json.loads(f.read_text())["startDate"]))
    return min(days) if days else None


def opening_balances(root: Path) -> list[tuple[date, str, Decimal, str, str]]:
    """What each account held when its first statement begins."""
    mapped, first = account_map(root), {}
    for st in statements(source_files(root)):
        first.setdefault(st.account, st)
    cutoff = api_start(root)
    return [
        (st.start, mapped[a], st.opening, "CHF", f"opening:{mapped[a]}")
        for a, st in first.items()
        if a in mapped and (cutoff is None or st.start < cutoff)
    ]


def needed_accounts(root: Path) -> dict[str, dict]:
    since = min((st.start for st in statements(source_files(root))), default=date(2016, 1, 1))
    return {
        **{
            a: {"group": "Cash", "institution": "UBS", "since": since}
            for a in account_map(root).values()
        },
        FEES_ACCOUNT: {"cadence": "regular", "necessity": "essential", "since": since},
        INTEREST_ACCOUNT: {"since": since},
        SALARY_ACCOUNT: {"since": since},
        base.UNCATEGORIZED: {"cadence": "regular", "necessity": "discretionary", "since": since},
        "Income:Uncategorized": {"since": since},
    }


# ---- fetching from the Safe --------------------------------------------------------------------

IN_SYNC = (
    False  # `fin sync` skips it: the Safe is a second login, so run `fin fetch ubs-pdf` yourself
)
SAFE_PAGE = "/app/H36/app/#/bankDocuments"
STATEMENT = "Account statement"  # the Safe's own name for the monthly document (English interface)
ROWS = ".item-list__row.folder__row--file"
LIST = ".item-list__content"
TITLE = ".nodes-details__title-node"  # the selected document's file name
DOWNLOAD = "#file-details-download-button"
STATEMENT_SLACK = 62  # days before the newest statement we hold that a fetch looks back


def default_start(root: Path) -> date | None:
    """A little before the newest statement we already hold (statements come monthly, some yearly).
    The file name carries the day it was issued, so no PDF has to be read."""
    days = [
        date.fromisoformat(f"{m[1][:4]}-{m[1][4:6]}-{m[1][6:]}")
        for p in source_files(root)
        if (m := re.search(r"_AccountStatement_(\d{8})", p.name))
    ]
    return date.fromordinal(max(days).toordinal() - STATEMENT_SLACK) if days else None


_ROW_INFO = """(rows) => rows.map(r => ({
    id: r.id,
    type: (r.querySelector('.edocs-col-type') || {}).textContent?.trim() || '',
    day: (r.querySelector('.folder__column-date') || {}).textContent?.trim() || '',
    account: (r.querySelector('.edocs-col-account') || {}).textContent?.trim() || '',
}))"""


def fetch(root: Path, start: date, end: date, log=print) -> list[Path]:
    """Open the browser, wait for you to log in and open the Safe (Documents), then download the
    account statements dated from `start` that `sources/ubs-pdf/` does not have yet. The Safe's own API
    is encrypted in the page, so this reads the list the page shows and presses its download button.
    Each file is checked (`accept`) before it is kept; one that does not read is an error."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ImportFailed("playwright is not installed: uv sync --extra browser") from e
    from fin.importers import ubs

    have = {p.name for p in source_files(root)}
    written: list[Path] = []
    ubs.profile_dir().mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p, tempfile.TemporaryDirectory() as tmp:
        ctx = p.chromium.launch_persistent_context(
            str(ubs.profile_dir()),
            channel="chrome",
            headless=False,
            no_viewport=True,
            accept_downloads=True,
        )
        try:
            first = ctx.pages[0] if ctx.pages else ctx.new_page()
            first.goto(ubs.HOME)
            log("Log in in the browser window (including 2FA), then open Documents (the Safe).")
            safe = None
            deadline = time.time() + 600
            while safe is None and time.time() < deadline:
                first.wait_for_timeout(500)
                safe = next((pg for pg in ctx.pages if SAFE_PAGE in pg.url), None)
            if safe is None:
                raise ImportFailed("timed out waiting for you to open the Safe's bank documents")
            safe.wait_for_selector(ROWS, timeout=60_000)
            done: set[str] = set()
            reached_start = False
            while not reached_start:
                infos = safe.eval_on_selector_all(ROWS, _ROW_INFO)
                fresh = [i for i in infos if i["id"] not in done]
                if not fresh:
                    before = len(infos)
                    safe.eval_on_selector(LIST, "el => { el.scrollTop = el.scrollHeight }")
                    safe.wait_for_timeout(1500)
                    if len(safe.query_selector_all(ROWS)) == before:
                        break  # the list has no more rows
                    continue
                for info in fresh:
                    done.add(info["id"])
                    day = datetime.strptime(info["day"], "%d.%m.%Y").date()
                    if day < start:
                        reached_start = True  # newest first: nothing older is wanted
                        break
                    if info["type"] != STATEMENT:
                        continue
                    row = safe.locator(f'[id="{info["id"]}"]')
                    row.scroll_into_view_if_needed()
                    row.locator(".edocs-col-type").click()
                    safe.wait_for_timeout(700)
                    name = safe.locator(TITLE).first.inner_text().strip()
                    if name in have:
                        continue
                    with safe.expect_download(timeout=60_000) as dl:
                        safe.click(DOWNLOAD)
                    target = Path(tmp) / name
                    dl.value.save_as(target)
                    written.append(accept(root, target))
                    have.add(name)
                    log(f"  {info['day']}: {name}")
        finally:
            ctx.close()
    if not written:
        log("  no new account statements")
    return written


def derived(root: Path) -> str:
    """Each statement's closing balance as a `balance` assertion (the day after it ends). Statements
    from the API's years are asserted too: the rows are the API's, so the statement is an independent
    check of them."""
    mapped = account_map(root)
    lines = []
    for st in statements(source_files(root)):
        if st.account in mapped:
            lines.append(
                f"{date.fromordinal(st.end.toordinal() + 1)} balance {mapped[st.account]}  {st.closing} CHF"
            )
    head = "; Generated by `fin import ubs-pdf` from the account statements in sources/ubs-pdf/.\n"
    return head + "\n".join(sorted(set(lines))) + "\n"


def after_import(root: Path) -> list[str]:
    from fin import config, gitops, ledger

    path = root / DERIVED / "balances.beancount"
    text = derived(root)
    with gitops.write_lock(root):
        before = path.read_text() if path.exists() else None
        if before == text:
            return []
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        errors = ledger.load(config.main_ledger(root)).errors
        if errors:
            if before is None:
                path.unlink()
            else:
                path.write_text(before)
            return [
                "a statement's balance disagrees with the ledger:",
                *ledger.format_errors(errors)[:10],
            ]
        gitops.commit_all(root, "ubs-pdf: statement balances")
    return ["every statement's closing balance agrees with the ledger"]
