"""Charles Schwab (Equity Award Center and the brokerage account vested shares land in) via the
site's own JSON API, fetched from a logged-in browser, as for UBS and Wise.

`fetch` opens a real browser window with a persistent profile; you log in and fin then calls the
transaction-history endpoints from inside the page. The headers the site's scripts attach to its
own transaction-history call (a short-lived bearer token among them) are copied in memory only;
nothing is written but the raw rows, to `sources/schwab/`, and they never pass through an LLM.

Endpoints (found by watching the site, not documented by Schwab; may change without notice), all
under https://ausgateway.schwab.com/api/is.TransactionHistoryWeb/TransactionHistoryInterface/
TransactionHistory/:
    GET  init                              -> {profile: {accounts: [{id, nickName, productTypeCode, ...}]}}
    POST equity-award-center/transactions  {timeFrame: "SpecifyDateRange", fromDate, toDate, ...}
         -> {transactions: [{typeName, eventDate, symbol, quantity, transactionDetails: [...]}]}
    POST brokerage/transactions            {selectedAccountId, timeFrame, fromDate, toDate, bookmark, ...}
         -> {brokerageTransactions: [...], bookmark}
A request spans at most 4 years; the brokerage history starts at most 4 years back, the vest
history goes further. Schwab drops older rows, so every archive is kept.

Booking:
 * a vest: the net shares are a lot at the vest-day fair market value (`v<award>-<date>`), the
   withheld shares' value is tax (`Expenses:Taxes:RSU-Withholding`), and the gross value is
   income (`Income:Employer:RSU`);
 * a security transfer out (always to IBKR) takes the oldest lots to the clearing account at their
   cost, where IBKR's side of the transfer is waiting at the same cost;
 * the brokerage rows "Stock Plan Activity" are the vested net shares arriving, already booked
   from the vest history, so they are skipped;
 * lots vested before the brokerage history begins left before it (they were transferred within
   days of each vest); they are moved to the clearing account on its first day.
Anything else is an error, never a guess.
"""

import hashlib
import json
import os
import re
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fin.importers.base import ImportFailed, RawTxn
from fin.importers.ibkr import commodity

SOURCE = "schwab"
HOME = "https://client.schwab.com/retail/accounts/history/#/"
GATEWAY = "https://ausgateway.schwab.com/api/is.TransactionHistoryWeb/TransactionHistoryInterface/TransactionHistory/"
HISTORY_CALL = re.compile(r"TransactionHistory/(brokerage|equity-award-center)/transactions$")
# the headers the site sets on its own call (the browser adds cookies, origin and the like)
KEEP_HEADERS = re.compile(
    r"^(authorization|schwab-.*|correlatorid|accept|content-type|cache-control|pragma|method)$"
)
MAX_SPAN = 4  # years per request, and how far back the brokerage history goes
EAC_TYPES = ["EXERCISES", "SARS", "LAPSES", "SHARESALE", "JOURNAL", "GIFT", "DEPOSITS", "TRANSFER", "DISBURSEMENTS", "CASHTXN"]  # fmt: skip
BROKERAGE_TYPES = [
    "Adjustments", "AtmActivity", "BillPay", "CorporateActions", "Checks", "Deposits",
    "DividendsAndCapitalGains", "ElectronicTransfers", "Fees", "Interest", "Misc",
    "SecurityTransfers", "Taxes", "Trades", "VisaDebitCard", "Withdrawals",
]  # fmt: skip
CLEARING = "Assets:Transfers:Pending"
INCOME = "Income:Employer:RSU"
WITHHELD = "Expenses:Taxes:RSU-Withholding"
PAYEE = "Charles Schwab"
DIVIDENDS = {"Qualified Dividend", "Cash Dividend", "Non-Qualified Div", "Special Dividend"}
TAXES = {"NRA Tax Adj", "NRA Withholding", "Foreign Tax Paid"}


def profile_dir() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "fin" / "browser" / SOURCE


EXPORTS = "exports"  # files you downloaded with Schwab's own Export button, kept as they came


def source_files(root: Path) -> list[Path]:
    base = root / "sources" / SOURCE
    return sorted(base.glob("*_*.json")) + sorted(
        p for p in (base / EXPORTS).glob("*") if p.is_file()
    )


# ---- Schwab's own Export files ---------------------------------------------------------------
# The brokerage account exports as JSON, the Equity Award Center as CSV (each vest a row, then a
# row of its award details). Both carry the same fields as the site's own data, under other
# names; they are read into the same shape, so a row seen both ways is booked once.

EAC_CSV_HEADER = ["Date", "Action", "Symbol", "Description", "Quantity", "FeesAndCommissions", "DisbursementElection", "Amount", "AwardDate", "AwardId", "FairMarketValuePrice", "SalePrice", "SharesSoldWithheldForTaxes", "NetSharesDeposited", "Taxes"]  # fmt: skip


def _iso(us_date: str) -> str:
    return _day(us_date).isoformat()


def _read(path: Path) -> dict:
    """Any Schwab file fin keeps, as {kind, fromDate, toDate, rows} with the site's field names."""
    if path.suffix.lower() == ".csv":
        import csv

        with path.open(newline="", encoding="utf-8-sig") as f:
            lines = list(csv.reader(f))
        if not lines or lines[0] != EAC_CSV_HEADER:
            raise ImportFailed(
                f"{path.name}: not a Schwab Equity Award Center export (unknown columns)"
            )
        rows, current = [], None
        for line in lines[1:]:
            r = dict(zip(EAC_CSV_HEADER, line, strict=False))
            if r["Date"]:
                current = {"typeName": "LapseViewModel" if r["Action"] == "Lapse" else r["Action"],
                           "eventDate": r["Date"], "action": r["Action"], "symbol": r["Symbol"],
                           "quantity": r["Quantity"], "description": r["Description"], "transactionDetails": []}  # fmt: skip
                rows.append(current)
            elif current is not None and r["AwardId"]:
                current["transactionDetails"].append({
                    "awardDate": r["AwardDate"], "marketValue": r["FairMarketValuePrice"],
                    "salePrice": r["SalePrice"], "shareSoldWithheld": r["SharesSoldWithheldForTaxes"],
                    "netShares": r["NetSharesDeposited"], "totalTaxes": r["Taxes"],
                    "fieldNameToDisplayValueLookup": {"awardId": r["AwardId"], "fairMarketValuePrice": r["FairMarketValuePrice"]},
                })  # fmt: skip
        return {"kind": "eac", "fromDate": None, "toDate": None, "rows": rows}
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if "kind" in raw:  # fin's own archive
        return raw
    if "BrokerageTransactions" in raw:
        names = {"Date": "transactionDate", "Action": "action", "Symbol": "symbol", "Description": "description",
                 "Quantity": "shareQuantity", "Price": "executionPrice", "Fees & Comm": "feesAndCommission", "Amount": "amount"}  # fmt: skip
        rows = [
            {names[k]: v for k, v in r.items() if k in names} for r in raw["BrokerageTransactions"]
        ]
        return {
            "kind": "brokerage",
            "fromDate": _iso(raw["FromDate"]),
            "toDate": _iso(raw["ToDate"]),
            "rows": rows,
        }
    raise ImportFailed(
        f"{path.name}: not a Schwab export fin knows (send one so a reader can be added)"
    )


def accept(root: Path, path: Path) -> Path:
    """Keep a file you downloaded from Schwab in sources/schwab/exports/, after checking fin can
    read it. The same file twice is kept once."""
    import shutil

    _read(path)  # refuses anything it does not recognise
    out_dir = root / "sources" / SOURCE / EXPORTS
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / path.name
    if target.exists() and target.read_bytes() != path.read_bytes():
        target = (
            out_dir
            / f"{path.stem}-{hashlib.sha256(path.read_bytes()).hexdigest()[:8]}{path.suffix}"
        )
    if not target.exists():
        shutil.copyfile(path, target)
    return target


def holding(symbol: str) -> str:
    return f"Assets:Schwab:{commodity(symbol)}"


def cash(ccy: str = "USD") -> str:
    return f"Assets:Schwab:Cash:{ccy}"


def _money(text) -> Decimal:
    """`$1,234.56`, `-$9.99`, `957`, `` -> Decimal (empty is 0)."""
    s = str(text or "").replace("$", "").replace(",", "").strip()
    return Decimal(s) if s else Decimal(0)


def _day(text: str) -> date:
    """`02/17/2023`, or `02/17/2023 as of 02/15/2023` (posted, then effective): the posting day."""
    return datetime.strptime(text[:10], "%m/%d/%Y").date()


def years_back(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:  # 29 February
        return d.replace(year=d.year - years, day=28)


def windows(start: date, end: date) -> list[tuple[date, date]]:
    """Calendar years, clipped to start..end."""
    out, a = [], start
    while a <= end:
        b = min(date(a.year, 12, 31), end)
        out.append((a, b))
        a = b + timedelta(days=1)
    return out


# ---- parsing ---------------------------------------------------------------------------------


def _load(files: list[Path], kind: str) -> list[dict]:
    """Every archived row of one kind once (fetches overlap), oldest first."""
    seen: dict[str, dict] = {}
    for f in files:
        raw = _read(f)
        if raw.get("kind") != kind:
            continue
        counts: dict[str, int] = {}
        for row in reversed(raw["rows"]):  # newest first, in archives and exports alike
            key = _row_key(row, kind)
            counts[key] = counts.get(key, 0) + 1
            seen.setdefault(f"{key}#{counts[key]}", row)
    return sorted(seen.values(), key=lambda r: _day(r.get("eventDate") or r.get("transactionDate")))


def _row_key(row: dict, kind: str) -> str:
    if kind == "eac":
        detail = (row.get("transactionDetails") or [{}])[0]
        award = (detail.get("fieldNameToDisplayValueLookup") or {}).get("awardId", "")
        return f"{row.get('typeName')}|{row.get('eventDate')}|{award}|{row.get('quantity')}"
    parts = [
        row.get(k)
        for k in ("transactionDate", "action", "symbol", "shareQuantity", "amount", "description")
    ]
    return hashlib.sha256("|".join(str(p or "") for p in parts).encode()).hexdigest()[:16]


class _Lots:
    def __init__(self):
        self.held: dict[str, list[dict]] = {}

    def add(self, symbol, label, qty, unit, day) -> str:
        self.held.setdefault(symbol, []).append(
            {"label": label, "qty": qty, "unit": unit, "day": day}
        )
        return f'{{{unit:f} USD, {day}, "{label}"}}'

    def take(self, symbol: str, qty: Decimal) -> list[tuple[dict, Decimal]]:
        out, left = [], qty
        for lot in self.held.get(symbol, []):
            n = min(left, lot["qty"])
            if n > 0:
                lot["qty"] -= n
                out.append((lot, n))
                left -= n
        if left:
            raise ImportFailed(f"Schwab moved {qty} {symbol} out, but fin holds {qty - left} there")
        return out

    def remaining(self) -> list[tuple[str, dict]]:
        return [(s, lot) for s, lots in self.held.items() for lot in lots if lot["qty"] > 0]


def _award_meta(detail: dict) -> dict[str, str]:
    """When the award was granted and what it is called: the vest's own record carries both."""
    out = {}
    if detail.get("awardDate"):
        out["award_date"] = _iso(detail["awardDate"])
    if detail.get("awardName"):
        out["award_name"] = str(detail["awardName"])
    return out


def award_meta(files: list[Path]) -> dict[str, dict[str, str]]:
    """`{ref: {award_date, award_name}}` for every vest in the archived files, so vests booked before
    the importer wrote these can be given them (`fin rsu backfill`)."""
    out = {}
    for row in _load(files, "eac"):
        details = row.get("transactionDetails") or []
        if row.get("typeName") != "LapseViewModel" or len(details) != 1:
            continue
        look = details[0].get("fieldNameToDisplayValueLookup") or {}
        award = look.get("awardId") or "award"
        out[f"{SOURCE}:vest:{award}:{_day(row['eventDate']):%Y%m%d}"] = _award_meta(details[0])
    return out


def _vest(row: dict, lots: _Lots) -> RawTxn:
    if row.get("typeName") != "LapseViewModel" or row.get("action") != "Lapse":
        raise ImportFailed(
            f"unknown Schwab equity award row {row.get('typeName')}/{row.get('action')}"
        )
    details = row.get("transactionDetails") or []
    if len(details) != 1:
        raise ImportFailed(
            f"Schwab vest on {row.get('eventDate')} has {len(details)} award details"
        )
    d = details[0]
    look = d.get("fieldNameToDisplayValueLookup") or {}
    day, sym = _day(row["eventDate"]), row["symbol"]
    gross, fmv = (
        _money(row["quantity"]),
        _money(look.get("fairMarketValuePrice") or d.get("marketValue")),
    )
    withheld, net = _money(d.get("shareSoldWithheld")), _money(d.get("netShares"))
    if d.get("salePrice"):
        raise ImportFailed(f"Schwab vest on {day} sold shares to cover; fin does not book that yet")
    if not fmv or gross != withheld + net:
        raise ImportFailed(f"Schwab vest on {day}: shares do not add up (gross, withheld, net)")
    award = look.get("awardId") or "award"
    label = f"v{award}-{day:%Y%m%d}"
    spec = lots.add(sym, label, net, fmv, day)
    legs = [
        f"{holding(sym)}  {net} {commodity(sym)} {spec}",
        f"{WITHHELD}  {withheld * fmv} USD",
        f"{INCOME}  {-gross * fmv} USD",
    ]
    meta = {"award": str(award), "taxes": f"{_money(d.get('totalTaxes'))} USD", **_award_meta(d)}
    return RawTxn(day, holding(sym), net, commodity(sym), PAYEE, f"vest {gross} {sym} ({withheld} withheld for tax)",
                  f"{SOURCE}:vest:{award}:{day:%Y%m%d}", meta=meta, postings=legs, review=False)  # fmt: skip


def _move_out(lots: _Lots, sym: str, qty: Decimal, day: date, ref: str, text: str) -> RawTxn:
    taken = lots.take(sym, qty)
    legs = [f'{holding(sym)}  -{n} {commodity(sym)} {{"{lot["label"]}"}}' for lot, n in taken]
    cost = sum(n * lot["unit"] for lot, n in taken)
    legs.append(f"{CLEARING}  {cost} USD")
    return RawTxn(
        day, holding(sym), -qty, commodity(sym), PAYEE, text, ref, postings=legs, review=False
    )


def _brokerage(row: dict, lots: _Lots, ref: str) -> RawTxn | None:
    action, sym, day = row.get("action"), row.get("symbol") or "", _day(row["transactionDate"])
    qty, amount = _money(row.get("shareQuantity")), _money(row.get("amount"))
    if action == "Stock Plan Activity":
        return None  # vested net shares arriving: booked from the vest history
    if action == "Security Transfer" and sym and qty < 0:
        return _move_out(lots, sym, -qty, day, ref, f"transfer out {-qty} {sym}")
    common = {
        "date": day,
        "account": cash(),
        "amount": amount,
        "currency": "USD",
        "payee": PAYEE,
        "ref": ref,
        "review": False,
    }
    if action == "Security Transfer" and not sym and amount:
        return RawTxn(
            narration="cash transfer out" if amount < 0 else "cash transfer in",
            counter=CLEARING,
            **common,
        )
    if action in DIVIDENDS:
        return RawTxn(
            narration=f"dividend {sym}",
            counter=f"Income:Investments:Dividends:{commodity(sym)}",
            **common,
        )
    if action in TAXES:
        return RawTxn(
            narration=f"withholding tax {sym}".strip(),
            counter="Expenses:Taxes:Withholding:US",
            **common,
        )
    raise ImportFailed(f"unknown Schwab brokerage action {action!r} on {day}")


def brokerage_start(files: list[Path]) -> date | None:
    starts = [
        date.fromisoformat(r["fromDate"]) for r in map(_read, files) if r["kind"] == "brokerage"
    ]
    return min(starts) if starts else None


def parse_files(files: list[Path]) -> list[RawTxn]:
    vests, moves = _load(files, "eac"), _load(files, "brokerage")
    first = brokerage_start(files)
    events = [(_day(v["eventDate"]), 0, "vest", v) for v in vests]
    seen: dict[str, int] = {}
    for row in moves:
        key = _row_key(row, "brokerage")
        seen[key] = seen.get(key, 0) + 1
        events.append((_day(row["transactionDate"]), 1, f"{SOURCE}:{key}:{seen[key]}", row))
    lots, out, bridged = _Lots(), [], False
    for day, _, kind, row in sorted(events, key=lambda e: (e[0], e[1])):
        if first and not bridged and day >= first:
            out += _before_history(lots, first)
            bridged = True
        if kind == "vest":
            out.append(_vest(row, lots))
        elif (t := _brokerage(row, lots, kind)) is not None:
            out.append(t)
    if first and not bridged:
        out += _before_history(lots, first)
    return out


def _before_history(lots: _Lots, first: date) -> list[RawTxn]:
    """Lots vested before the brokerage history begins had already been moved on (to IBKR)."""
    out = []
    for sym, lot in list(lots.remaining()):
        if lot["day"] < first:
            ref = f"{SOURCE}:before-history:{lot['label']}"
            t = _move_out(
                lots,
                sym,
                lot["qty"],
                first,
                ref,
                f"transferred out before Schwab's history ({sym})",
            )
            t.meta["note"] = "Schwab's brokerage history starts here; this lot had left before it"
            out.append(t)
    return out


def after_import(root: Path) -> list[str]:
    from fin import transfers

    return transfers.share_move_report(root)


def needed_accounts(root: Path) -> dict[str, dict]:
    files = source_files(root)
    txns = parse_files(files)
    since = min((t.date for t in txns), default=date(2020, 1, 1))
    out: dict[str, dict] = {}
    for t in txns:
        for leg in t.postings or [t.account, t.counter or ""]:
            account = leg.split()[0] if leg else ""
            if account.startswith("Assets:Schwab:"):
                out[account] = {"group": "Investments", "institution": "Schwab", "since": since}
            elif account.startswith("Expenses:Taxes:"):
                out[account] = {"cadence": "regular", "necessity": "essential", "since": since}
            elif account.startswith("Income:"):
                out[account] = {"since": since}
    return out


# ---- fetching --------------------------------------------------------------------------------


def fetch(root: Path, start: date, end: date, log=print) -> list[Path]:
    """Open a browser, wait for you to log in, then archive the vest history and each brokerage
    account's history from `start` (the brokerage history from at most 4 years back)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ImportFailed("playwright is not installed: uv sync --extra browser") from e

    out_dir = root / "sources" / SOURCE
    out_dir.mkdir(parents=True, exist_ok=True)
    profile_dir().mkdir(parents=True, exist_ok=True)
    end = min(end, date.today())
    written = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile_dir()), channel="chrome", headless=False, no_viewport=True
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            auth: dict[str, str] = {}

            def seen(request):
                if not auth and HISTORY_CALL.search(request.url.split("?")[0]):
                    auth.update({k: v for k, v in request.headers.items() if KEEP_HEADERS.match(k)})

            page.on("request", seen)
            page.goto(HOME)
            log("Log in in the browser window. Waiting for the transaction history page...")
            deadline = time.time() + 600
            while not auth and time.time() < deadline:
                page.wait_for_timeout(1000)
                url = page.url
                if (
                    "client.schwab.com" in url
                    and "login" not in url.lower()
                    and "accounts/history" not in url
                ):
                    page.goto(HOME)  # logged in, but landed elsewhere
            if not auth:
                raise ImportFailed("timed out waiting for you to log in")

            def call(method: str, path: str, body: dict | None = None) -> dict:
                result = page.evaluate(
                    """([method, url, headers, body]) => new Promise(res => {
                        const x = new XMLHttpRequest();
                        x.open(method, url);
                        for (const [k, v] of Object.entries(headers)) x.setRequestHeader(k, v);
                        x.onload = () => res({status: x.status, text: x.responseText});
                        x.onerror = () => res({status: 0, text: ''});
                        x.send(body ? JSON.stringify(body) : null);
                    })""",
                    [method, GATEWAY + path, auth, body],
                )
                if result["status"] in (401, 403):
                    raise ImportFailed("Schwab refused the session (expired?): log in again")
                if result["status"] != 200:
                    detail = ""
                    try:
                        detail = json.dumps(json.loads(result["text"]).get("errors"))[:200]
                    except (ValueError, AttributeError):
                        pass
                    raise ImportFailed(f"Schwab {path}: HTTP {result['status']} {detail}")
                return json.loads(result["text"])

            def save(kind: str, a: date, b: date, rows: list) -> None:
                path = out_dir / f"{a}_{b}_{kind}.json"
                stamp = datetime.now().astimezone().isoformat(timespec="seconds")
                path.write_text(json.dumps({"fetched": stamp, "kind": kind, "fromDate": a.isoformat(), "toDate": b.isoformat(), "rows": rows}, indent=1))  # fmt: skip
                written.append(path)
                log(f"  {kind} {a}..{b}: {len(rows)} rows")

            for a, b in windows(start, end):
                body = {"selectedAccountId": "", "timeFrame": "SpecifyDateRange", "symbol": "",
                        "selectedTransactionTypes": EAC_TYPES, "sortColumn": "eventDateSortValue",
                        "sortDirection": "Descending", "exportType": "Csv",
                        "fromDate": a.isoformat(), "toDate": b.isoformat()}  # fmt: skip
                save(
                    "eac",
                    a,
                    b,
                    call("POST", "equity-award-center/transactions", body).get("transactions")
                    or [],
                )

            accounts = [
                x for x in (call("GET", "init").get("profile") or {}).get("accounts") or []
                if x.get("productTypeCode") and x.get("id") and not x.get("isClosed")
            ]  # fmt: skip
            if len(accounts) != 1:
                raise ImportFailed(f"expected one Schwab brokerage account, found {len(accounts)}")
            acct = accounts[0]
            earliest = years_back(end, MAX_SPAN)
            for a, b in windows(max(start, earliest), end):
                rows, bookmark = [], None
                for _ in range(50):
                    body = {"accountNickname": acct.get("nickName") or "", "exportType": "Csv",
                            "includeOptionsInSearch": False, "isSpsLinkedUkAccount": False,
                            "selectedAccountId": acct["id"], "selectedTransactionTypes": BROKERAGE_TYPES,
                            "sortColumn": "Date", "sortDirection": "Descending", "symbol": "",
                            "timeFrame": "SpecifyDateRange", "fromDate": a.isoformat(),
                            "toDate": b.isoformat(), "bookmark": bookmark, "shouldPaginate": True}  # fmt: skip
                    page_ = call("POST", "brokerage/transactions", body)
                    rows += page_.get("brokerageTransactions") or []
                    bookmark = page_.get("bookmark")
                    if not bookmark:
                        break
                save("brokerage", a, b, rows)
        finally:
            ctx.close()
    return written
