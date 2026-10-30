"""VIAC (pillar 3a) from the web app's own responses, recorded from a logged-in browser.

`fetch` opens a real browser window with a persistent profile; you log in (2FA included). fin then
shows the 3a pages and writes down every JSON answer the app itself receives from `/rest/web/`
(`sources/viac/<day>_<login>_capture.json`). Nothing but the response bodies is kept: no request
header, cookie or token, and nothing passes through an LLM. The app answers its endpoints only with
headers its own scripts add, so fin reads what the page loads instead of calling them.

`parse_root` turns the newest capture of each login into the ledger's view of each portfolio:

 * the portfolio's cash account books every row of `p3a/portfolio/transactions`: contributions and
   a transfer-in from another provider, fee charges, interest, dividends, and the cash side of every
   trade. Each row carries the cash balance after it, which `derived()` asserts, so a missing or
   doubled row fails the ledger loudly;
 * the securities account holds what the trades bought, at cost, and a month-end value mark keeps it
   at what the portfolio is worth (the daily value in `wealth/summary`, less the cash), the
   difference going to `Income:Investments:Unrealized:VIAC`.

`rules/viac.yaml` maps each portfolio number to its ledger account:

    portfolios:
      "<portfolio number>": Assets:Pension:Acme:Growth
    transfer_in: Assets:Pension:Acme:Old   # where a transfer-in came from (default: clearing)
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fin.importers.base import ImportFailed, RawTxn

SOURCE = "viac"
HOME = "https://app.viac.ch/"
PAGES = (  # the app's routes worth recording, in the order they are shown
    "#/p3a/portfolio/overview",
    "#/documents",
)
API = "/rest/web/"
IN_SYNC = False  # a login of its own (2FA): run `fin fetch viac`
LOGGED_IN = re.compile(r"#/(products|p3a|documents|hub)")
BROWSE_SECONDS = 90  # time to open each portfolio yourself before fin stops recording


def profile_dir() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "fin" / "browser" / SOURCE


def source_files(root: Path) -> list[Path]:
    return sorted((root / "sources" / SOURCE).glob("*_capture.json"))


CLEARING = "Assets:Transfers:Pending"
FEES = "Expenses:Fees:Pension"
INTEREST = "Income:Interest:VIAC"
DIVIDENDS = "Income:Investments:Dividends:VIAC"
MARKS = "Income:Investments:Unrealized:VIAC"
DERIVED = Path("ledger") / "imported" / SOURCE / "derived"
EPS = Decimal("0.005")


def _conf(root: Path) -> dict:
    import yaml

    path = root / "rules" / "viac.yaml"
    return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}


def _dec(x) -> Decimal:
    return Decimal(str(x))


def _newest_per_login(files: list[Path]) -> list[Path]:
    """The latest capture of each login: every capture lists the whole history, so older ones add
    nothing."""
    best: dict[str, Path] = {}
    for f in sorted(files):
        parts = f.stem.split("_")
        login = parts[1] if len(parts) > 2 else ""
        best[login] = f
    return list(best.values())


def _answers(path: Path) -> dict:
    out = {}
    for r in json.loads(path.read_text())["responses"]:
        out[r["url"].split(API, 1)[1].split("?")[0]] = r["body"]
    return out


class Portfolio:
    """One portfolio as the capture shows it: its bookings (oldest first), the cash after each day
    and the value series."""

    def __init__(self, number: str, account: str, rows: list[dict], wealth: list[dict]):
        self.number, self.account = number, account
        self.cash = f"{account}:Cash"
        self.securities = f"{account}:Securities"
        oldest_first = list(reversed(rows))  # the app lists newest first; a day keeps its order
        self.rows = sorted(oldest_first, key=lambda r: r["valueDate"])
        # the app lists newest first: the first listed row of a day is its last booking
        self.cash_after: dict[date, Decimal] = {}
        for r in rows:
            self.cash_after.setdefault(
                date.fromisoformat(r["valueDate"]), _dec(r["balanceAfterBooking"])
            )
        self.wealth = {date.fromisoformat(w["date"]): _dec(w["value"]["__value"]) for w in wealth}

    def cash_on(self, day: date) -> Decimal:
        days = [d for d in self.cash_after if d <= day]
        return self.cash_after[max(days)] if days else Decimal(0)

    def wealth_on(self, day: date) -> Decimal | None:
        days = [d for d in self.wealth if d <= day]
        return self.wealth[max(days)] if days else None


def portfolios(root: Path, files: list[Path]) -> list[Portfolio]:
    mapping = {str(k): v for k, v in (_conf(root).get("portfolios") or {}).items()}
    out = []
    for path in _newest_per_login(files):
        ans = _answers(path)
        try:
            tx = ans["p3a/portfolio/transactions"]["transactions"]
            wealth = ans["wealth/summary"]["p3aSummary"]["portfolioWealthSummaries"]
        except KeyError as e:
            raise ImportFailed(
                f"{path.name}: no {e.args[0]} in the capture; run `fin fetch viac` and open "
                "each portfolio"
            ) from e
        for number, rows in tx.items():
            if number not in mapping:
                raise ImportFailed(
                    f"{path.name}: portfolio {number} has no ledger account: add it under "
                    "`portfolios:` in rules/viac.yaml"
                )
            out.append(Portfolio(number, mapping[number], rows, wealth[number]["dailyWealth"]))
    return out


def _month_ends(first: date, last: date) -> list[date]:
    days = []
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        days.append(min(date.fromordinal(nxt.toordinal() - 1), last))
        y, m = nxt.year, nxt.month
    return sorted(set(days))


def _bookings(pf: Portfolio, transfer_in: str) -> list[RawTxn]:
    out: list[RawTxn] = []
    seen: dict[tuple, int] = {}
    for r in pf.rows:
        kind, day, amount = r["type"], date.fromisoformat(r["valueDate"]), _dec(r["amountInChf"])
        doc = r.get("documentNumber") or "-"
        key = (kind, day, doc)
        seen[key] = seen.get(key, 0) + 1
        ref = f"{SOURCE}:{pf.number}:{day}:{kind}:{doc}:{seen[key]}"
        meta = {"kind": kind, **({"document": doc} if doc != "-" else {})}
        counter = {
            "CONTRIBUTION": CLEARING,
            "TRANSFER": transfer_in,
            "FEE_CHARGE": FEES,
            "INTEREST": INTEREST,
            "DIVIDEND": DIVIDENDS,
            "DIVIDEND_CANCELLATION": DIVIDENDS,
            "TRADE_BUY": pf.securities,
            "TRADE_SELL": pf.securities,
        }.get(kind)
        if counter is None:
            raise ImportFailed(f"VIAC booking type {kind!r} is not known: teach the parser")
        out.append(
            RawTxn(
                day, pf.cash, amount, "CHF", "VIAC", kind.replace("_", " ").lower(), ref,
                meta=meta, counter=counter, review=False,
            )
        )  # fmt: skip
    return out


def _marks(pf: Portfolio, bookings: list[RawTxn]) -> list[RawTxn]:
    """A mark at each month end (and the last day of the value series) that brings the securities
    account to the portfolio's value less its cash."""
    if not pf.wealth or not bookings:
        return []
    book = Decimal(0)
    out: list[RawTxn] = []
    flows = sorted((t.date, -t.amount) for t in bookings if t.counter == pf.securities)
    i = 0
    first = min(t.date for t in bookings)
    for day in _month_ends(first, max(pf.wealth)):
        while i < len(flows) and flows[i][0] <= day:
            book += flows[i][1]
            i += 1
        value = pf.wealth_on(day)
        if value is None:
            continue
        delta = (value - pf.cash_on(day) - book).quantize(Decimal("0.01"))
        if abs(delta) < EPS:
            continue
        book += delta
        out.append(
            RawTxn(
                day, pf.securities, delta, "CHF", "VIAC", "value mark", f"{SOURCE}:{pf.number}:mark:{day}",
                meta={"basis": "VIAC's daily portfolio value less its cash"}, counter=MARKS, review=False,
            )
        )  # fmt: skip
    return out


def parse_root(root: Path, files: list[Path]) -> list[RawTxn]:
    transfer_in = _conf(root).get("transfer_in") or CLEARING
    out: list[RawTxn] = []
    for pf in portfolios(root, files):
        rows = _bookings(pf, transfer_in)
        out += rows + _marks(pf, rows)
    return out


def parse_files(files: list[Path]) -> list[RawTxn]:
    raise ImportFailed("VIAC needs its data folder (rules/viac.yaml): use parse_root")


def needed_accounts(root: Path) -> dict[str, dict]:
    out: dict[str, dict] = {
        FEES: {"cadence": "regular", "necessity": "essential"},
        INTEREST: {},
        DIVIDENDS: {},
        MARKS: {},
    }
    conf = _conf(root)
    for account in (conf.get("portfolios") or {}).values():
        out[f"{account}:Cash"] = {"group": "Pension", "institution": "VIAC"}
        out[f"{account}:Securities"] = {"group": "Pension", "institution": "VIAC"}
    if conf.get("transfer_in"):
        out[conf["transfer_in"]] = {"group": "Pension"}
    return out


def derived(root: Path, files: list[Path]) -> str:
    """Each day's closing cash as a `balance` assertion (the day after), from the balance VIAC prints
    after the day's last booking."""
    lines = []
    for pf in portfolios(root, files):
        for day, bal in pf.cash_after.items():
            lines.append(
                f"{day + timedelta(days=1)} balance {pf.cash}  {bal.quantize(Decimal('0.01'))} CHF"
            )
    head = "; Generated by `fin import viac` from the captures in sources/viac/.\n"
    return head + "\n".join(sorted(set(lines))) + "\n"


def after_import(root: Path) -> list[str]:
    from fin import config, gitops, ledger

    files = source_files(root)
    if not files:
        return []
    path = root / DERIVED / "viac.beancount"
    text = derived(root, files)
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
                "VIAC's cash balance disagrees with the ledger:",
                *ledger.format_errors(errors)[:10],
            ]
        gitops.commit_all(root, "viac: cash balances")
    return ["every day's cash balance agrees with the ledger"]


def fetch(root: Path, start: date, end: date, log=print, login: str = "main") -> list[Path]:
    """Record what the web app loads. `login` names whose account it is (a partner has their own)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ImportFailed("playwright is not installed: uv sync --extra browser") from e

    seen: dict[tuple[str, str], dict] = {}

    def on_response(resp) -> None:
        url = resp.url
        if API not in url or "application/json" not in (resp.headers.get("content-type") or ""):
            return
        key = (resp.request.method, url)
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001  a body that is not JSON (or is gone) is not recorded
            return
        seen[key] = {"method": key[0], "url": url, "status": resp.status, "body": body}

    out_dir = root / "sources" / SOURCE
    out_dir.mkdir(parents=True, exist_ok=True)
    profile_dir().mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile_dir()), channel="chrome", headless=False, no_viewport=True
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.on("response", on_response)
            page.goto(HOME)
            log("Log in in the browser window (including 2FA). Waiting for the overview...")
            deadline = time.time() + 600
            while not LOGGED_IN.search(page.url) and time.time() < deadline:
                page.wait_for_timeout(500)
            if not LOGGED_IN.search(page.url):
                raise ImportFailed("timed out waiting for you to log in")
            for route in PAGES:
                page.goto(HOME + route)
                page.wait_for_timeout(6000)
            log(
                f"Recording for {BROWSE_SECONDS} s more: open each portfolio and its "
                "transactions in the window now."
            )
            page.wait_for_timeout(BROWSE_SECONDS * 1000)
        finally:
            ctx.close()
    if not seen:
        raise ImportFailed("the app answered nothing fin could record; has the site changed?")
    path = out_dir / f"{date.today()}_{login}_capture.json"
    path.write_text(
        json.dumps(
            {
                "fetched": datetime.now().astimezone().isoformat(timespec="seconds"),
                "login": login,
                "responses": sorted(seen.values(), key=lambda r: r["url"]),
            },
            indent=1,
        )
    )
    log(f"  recorded {len(seen)} answers")
    return [path]
