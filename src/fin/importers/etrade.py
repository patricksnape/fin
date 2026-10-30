"""E*TRADE stock plan brokerage statements (PDF): an employer's plan account that held shares
released from RSUs and ESPP, sold some of them, wired the proceeds to your bank and delivered the
rest to another broker.

You download the monthly "Brokerage Statement" PDFs from E*TRADE yourself and run
`fin import etrade <files>`. fin keeps them in `sources/etrade/statements/`. A statement is text, not a
scan, in a layout this module knows as `v1` (the 2017-2020 statements). A statement it does not
recognise is refused, never guessed: a newer layout needs a new parser beside this one.

What the statements say, and what they do not:
 * They list every share received from the plan (net of the shares kept for tax), sale, dividend,
   wire, delivery to another broker and split, with dates and amounts, plus the closing cash and shares.
 * They do not give the cost of a share, the grant it came from, or the tax kept at release. So the
   account holds plain shares, not cost lots. A receipt is booked as plan income at the market price
   of the day (an estimate, marked as one in the transaction's metadata); a sale books the broker's
   price and fees, with no gain (the cost is not known); a delivery books the value the receiving
   broker booked for the same shares, so the two sides cancel on the clearing account.

Every statement is checked against itself: the cash it opens with, plus what it books, must equal
what it closes with, and the shares it closes with, less what it books, are the shares it opened
with. Two statements that follow each other must agree. Where a month is missing, the difference
is booked as an opening balance on the first day of the next statement, so a gap is visible in the
ledger, never silent. The closing balances become `balance` assertions.
"""

import functools
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fin.importers.base import ImportFailed, RawTxn

SOURCE = "etrade"
LAYOUT = "v1"
CLEARING = "Assets:Transfers:Pending"
EQUITY = "Equity:Opening-Balances"
FEES = "Expenses:Fees:Broker"
TAX_DUE = "Expenses:Taxes:RSU-Withholding"
DERIVED = Path("ledger") / "imported" / SOURCE / "derived"
FILES = Path("sources") / SOURCE / "statements"
CENT = Decimal("0.01")
QUOTE_WITHIN = timedelta(days=7)  # a quote this close to a day stands for it
IBKR_WITHIN = timedelta(days=4)  # the receiving broker books a delivery within a few days

SYM = r"[A-Z][A-Z.]{0,5}"
NUM = r"-?[\d,]+(?:\.\d+)?"
DAY = r"\d\d/\d\d/\d\d"
CID = re.compile(r"\(cid:\d+\)")
HEADER = re.compile(
    r"Account Number:\s*([\d-]+)\s+Statement Period\s*:\s*"
    r"([A-Z][a-z]+ \d{1,2}, \d{4})\s*-\s*([A-Z][a-z]+ \d{1,2}, \d{4})"
)
TRADE = re.compile(rf"^({DAY}) ({DAY}) (.+?) ({SYM}) (Sold|Bought) ({NUM}) ({NUM}) ({NUM})$")
WIRE = re.compile(rf"^({DAY}) Wire(?: Disb)? (.+?) ({NUM})$")
SWEEP = re.compile(rf"^({DAY}) Adjustment (OLINK|TO T-1) TID (\d+) ({NUM})$")
DUE = re.compile(rf"^({DAY}) Adjustment \$DUE ({SYM}) (\d+) ({NUM})$")
DIVIDEND = re.compile(rf"^({DAY}) Dividend (.+?) ({SYM}) ({NUM})(?: ({NUM}))?$")
SHARES = re.compile(rf"^({DAY}) (.+?) ({SYM}) (Receive|Deliver|Adjustment|Split) ({NUM})$")
CLOSING = re.compile(rf"^Closing Balance (?:[\d.]+ )?({NUM})$")


def source_files(root: Path) -> list[Path]:
    return sorted((root / FILES).glob("*.pdf"))


def accept(root: Path, path: Path) -> Path:
    """Keep a PDF you downloaded in sources/etrade/statements/ after checking fin can read it. The
    same file twice is kept once."""
    import shutil

    read_statement(path)  # refuses anything it does not recognise or that does not add up
    out_dir = root / FILES
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / path.name
    if not target.exists():
        shutil.copyfile(path, target)
    return target


# ---- reading a statement -----------------------------------------------------------------------


def _num(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def _day(text: str) -> date:
    return datetime.strptime(text, "%m/%d/%y").date()


@dataclass
class Statement:
    path: Path
    account: str  # the last four digits of the account number
    start: date
    end: date
    cash_open: Decimal
    cash_close: Decimal
    held: dict[str, Decimal]  # the shares held at the end, per symbol
    prices: dict[str, Decimal] = field(default_factory=dict)  # the closing price per symbol
    events: list[dict] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, date, date]:
        return self.account, self.start, self.end

    def moved(self, sym: str) -> Decimal:
        """The shares this statement adds to the account (negative: takes away)."""
        total = Decimal(0)
        for e in self.events:
            if e.get("sym") != sym:
                continue
            if e["kind"] == "receive" or e["kind"] == "trade":
                total += e["qty"]
            elif e["kind"] == "deliver":
                total -= e["qty"]
            elif e["kind"] == "split":
                total += e["added"]
        return total

    def symbols(self) -> set[str]:
        return set(self.held) | {e["sym"] for e in self.events if "sym" in e}

    def opening_shares(self, sym: str) -> Decimal:
        return self.held.get(sym, Decimal(0)) - self.moved(sym)

    def cash_moved(self) -> Decimal:
        """What the rows add to the cash balance: sale proceeds that settled by the end (less
        purchases), net dividends, less wires and tax due. A sweep to the settlement fund is a pair
        that cancels."""
        total = Decimal(0)
        for e in self.events:
            if e["kind"] == "trade" and e["settle"] <= self.end:
                total += e["amount"] if e["qty"] < 0 else -e["amount"]
            elif e["kind"] == "dividend":
                total += e["gross"] - e["withheld"]
            elif e["kind"] in ("wire", "due"):
                total -= e["amount"]
        return total


def _pages(path: Path) -> list[list[str]]:
    try:
        import pdfplumber
    except ImportError as e:
        raise ImportFailed("pdfplumber is not installed: uv sync --extra pdf") from e
    with pdfplumber.open(path) as pdf:
        return [(p.extract_text(x_tolerance=2) or "").splitlines() for p in pdf.pages]


def read_statement(path: Path) -> Statement:
    """One statement PDF -> its rows, checked against its own cash. Reading takes a few seconds, so a
    file is read once per run (and again if it changes)."""
    stat = path.stat()
    return _read_cached(path, stat.st_mtime_ns, stat.st_size)


@functools.lru_cache(maxsize=256)
def _read_cached(path: Path, _mtime: int, _size: int) -> Statement:
    return parse_lines(path, [CID.sub("", ln).strip() for p in _pages(path) for ln in p])


def parse_lines(path: Path, lines: list[str]) -> Statement:
    name = path.name
    head = next((m for ln in lines if (m := HEADER.search(ln))), None)
    cash_at = next((i for i, ln in enumerate(lines) if ln.startswith("CASH BALANCE")), None)
    if not head or not any(
        ln.startswith(("PORTFOLIO HOLDINGS", "ACCOUNT HOLDINGS")) for ln in lines
    ):
        raise ImportFailed(
            f"{name}: not a brokerage statement in the layout fin reads ({LAYOUT}, 2017-2020); "
            "a newer layout needs a new parser"
        )
    account = head.group(1).replace("-", "")[-4:]
    start, end = (datetime.strptime(head.group(i), "%B %d, %Y").date() for i in (2, 3))
    opening = closing = Decimal(0)  # an account that held no cash prints no cash section
    if cash_at is not None:
        opening = closing = None
        for ln in lines[cash_at:]:
            if opening is None and ln.startswith("Opening Balance"):
                opening = _num(ln.split()[-1])
            elif opening is not None and (m := CLOSING.match(ln)):
                closing = _num(m.group(1))
                break
        if opening is None or closing is None:
            raise ImportFailed(f"{name}: no opening and closing cash balance")
    held, quoted = _holdings(lines)
    st = Statement(path, account, start, end, opening, closing, held, quoted)
    _rows(st, lines)
    if st.cash_open + st.cash_moved() != st.cash_close:
        raise ImportFailed(
            f"{name}: the cash rows do not add up to the closing balance "
            f"({st.cash_open} + {st.cash_moved()} is not {st.cash_close})"
        )
    return st


def _holdings(lines: list[str]) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """The shares held at the end and the closing price, from the stocks table:
    `SYMBOL StkPln quantity price value ...`, or without a quantity when nothing is held. The table of
    unvested awards has the same account type but starts with a grant date."""
    held: dict[str, Decimal] = {}
    quoted: dict[str, Decimal] = {}
    for ln in lines:
        t = ln.split()
        if "StkPln" not in t or re.match(r"\d+/\d+/\d{4}$", t[0]):
            continue
        i = t.index("StkPln")
        if i == 0 or not re.fullmatch(SYM, t[i - 1]):
            raise ImportFailed(f"cannot read the holding {ln!r}")
        rest = t[i + 1 :]
        held[t[i - 1]] = Decimal(0) if len(rest) <= 3 else _num(rest[0])
        quoted[t[i - 1]] = _num(rest[0] if len(rest) <= 3 else rest[1])
    return {s: q for s, q in held.items() if q}, quoted


def _rows(st: Statement, lines: list[str]) -> None:
    """The dated rows. A line that is not dated and not one of the few detail lines belongs to the
    row above (the time of a trade, a dividend's record date) and is ignored."""
    sweeps: dict[str, list[Decimal]] = {}
    adjustments: dict[str, list[tuple[date, str, Decimal]]] = {}
    for i, ln in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if not re.match(DAY, ln):
            continue
        if m := TRADE.match(ln):
            qty = _num(m.group(6))
            st.events.append(
                {
                    "kind": "trade",
                    "date": _day(m.group(1)),
                    "settle": _day(m.group(2)),
                    "sym": m.group(4),
                    "qty": qty if m.group(5) == "Bought" else -abs(qty),
                    "price": _num(m.group(7)),
                    "amount": _num(m.group(8)),
                }
            )
        elif m := SWEEP.match(ln):
            sweeps.setdefault(m.group(3), []).append(
                _num(m.group(4)) * (1 if m.group(2) == "OLINK" else -1)
            )
        elif m := DUE.match(ln):
            st.events.append(
                {
                    "kind": "due",
                    "date": _day(m.group(1)),
                    "sym": m.group(2),
                    "id": m.group(3),
                    "amount": _num(m.group(4)),
                }
            )
        elif m := WIRE.match(ln):
            st.events.append(
                {
                    "kind": "wire",
                    "date": _day(m.group(1)),
                    "text": m.group(2),
                    "amount": _num(m.group(3)),
                }
            )
        elif m := DIVIDEND.match(ln):
            one, two = _num(m.group(4)), m.group(5)
            gross, withheld = (_num(two), one) if two else (one, Decimal(0))
            if not two and _corrected(lines, i):  # a reversal: the text names it, not a column
                gross = -gross
            st.events.append(
                {
                    "kind": "dividend",
                    "date": _day(m.group(1)),
                    "sym": m.group(3),
                    "gross": gross,
                    "withheld": withheld,
                }
            )
        elif m := SHARES.match(ln):
            _share_row(st, m, nxt, lines[i + 2] if i + 2 < len(lines) else "", adjustments)
        elif "Adjustment" in ln or " Wire" in ln or " Dividend" in ln:
            raise ImportFailed(f"{st.path.name}: a row fin does not recognise: {ln!r}")
    for tid, amounts in sweeps.items():
        if sum(amounts) != 0:
            raise ImportFailed(f"{st.path.name}: the sweep {tid} does not cancel")
    for refid, rows in adjustments.items():
        if sum(q for _, _, q in rows) != 0:
            raise ImportFailed(f"{st.path.name}: the share transfer {refid} does not cancel")
    for e in st.events:
        if e["kind"] == "deliver":
            e["refid"] = next(
                (r for r, rows in adjustments.items() if rows[0][0] == e["date"]), e.get("refid")
            )
    st.events.sort(key=lambda e: (e["date"], e["kind"]))


def _corrected(lines: list[str], i: int) -> bool:
    """Whether a dividend row is reversed by a correction: the label follows it, before the next row."""
    for ln in lines[i + 1 : i + 5]:
        if re.match(DAY, ln):
            return False
        if "DIVIDEND CORRECTION" in ln:
            return True
    return False


def _share_row(st, m, nxt: str, nxt2: str, adjustments) -> None:
    day, _, sym, kind, qty = (
        _day(m.group(1)),
        m.group(2),
        m.group(3),
        m.group(4),
        _num(m.group(5)),
    )
    if kind == "Receive":
        native = re.search(r"RAND (\d+)", nxt)
        st.events.append(
            {
                "kind": "receive",
                "date": day,
                "sym": sym,
                "qty": qty,
                "id": native.group(1) if native else None,
            }
        )
    elif kind == "Deliver":
        broker = re.search(r"BRK#(\d+)", nxt)
        st.events.append(
            {
                "kind": "deliver",
                "date": day,
                "sym": sym,
                "qty": abs(qty),
                "broker": broker.group(1) if broker else None,
            }
        )
    elif kind == "Split":
        on = re.search(r"SPLIT ON (\d+) SHS", f"{nxt} {nxt2}")
        if not on:
            raise ImportFailed(f"{st.path.name}: a split without the shares it was on")
        st.events.append(
            {"kind": "split", "date": day, "sym": sym, "added": qty, "on": _num(on.group(1))}
        )
    else:  # Adjustment: shares moved between the plan's account types, in and out on the same day
        refid = re.search(r"REFID:(\d+)", f"{nxt} {nxt2}")
        if not re.search(r"TFR (FROM|TO) TYPE", f"{nxt} {nxt2}") or not refid:
            raise ImportFailed(
                f"{st.path.name}: a share adjustment fin does not recognise: {nxt!r}"
            )
        adjustments.setdefault(refid.group(1), []).append((day, sym, qty))


# ---- prices ------------------------------------------------------------------------------------


class Prices:
    """What a share was worth on a day, in dollars as quoted then: the ledger's own quotes first, then
    a daily history file in the data repo (`sources/market-history/<SYMBOL>.csv`, a `Close` column as
    Yahoo gives it, corrected for splits). `rules/etrade.yaml` says which splits to undo:

        splits:
          ACME: [{date: 2021-06-15, ratio: 2}]   # quoted closes before this day are divided by 2

    A day with no quote within a week is an error, not a guess."""

    def __init__(self, root: Path | None):
        self.root = root
        self.conf: dict = {}
        self.pmap = None
        self.history: dict[str, list[tuple[date, Decimal]]] = {}
        if root is not None:
            self._load()

    def _load(self) -> None:
        import yaml
        from beancount.core import data
        from beancount.core import prices as bprices

        from fin import config, ledger

        path = self.root / "rules" / "etrade.yaml"
        self.conf = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
        led = ledger.load(config.main_ledger(self.root))
        self.pmap = bprices.build_price_map([e for e in led.entries if isinstance(e, data.Price)])

    def _history(self, sym: str) -> list[tuple[date, Decimal]]:
        if sym not in self.history:
            import csv

            rows: list[tuple[date, Decimal]] = []
            path = self.root / "sources" / "market-history" / f"{sym}.csv"
            if path.exists():
                with path.open(newline="", encoding="utf-8-sig") as f:
                    for r in csv.DictReader(f):
                        try:
                            rows.append((date.fromisoformat(r["Date"][:10]), Decimal(r["Close"])))
                        except (KeyError, ValueError, ArithmeticError):
                            continue
            self.history[sym] = sorted(rows)
        return self.history[sym]

    def _unsplit(self, sym: str, day: date) -> Decimal:
        factor = Decimal(1)
        for s in (self.conf.get("splits") or {}).get(sym) or []:
            if s["date"] > day:
                factor *= Decimal(str(s["ratio"]))
        return factor

    def on(self, sym: str, day: date) -> Decimal:
        from beancount.core import prices as bprices

        if self.pmap is not None:
            quoted, price = bprices.get_price(self.pmap, (sym, "USD"), day)
            if price is not None and day - quoted <= QUOTE_WITHIN:
                return price
        rows = [r for r in self._history(sym) if r[0] <= day] if self.root else []
        if rows and day - rows[-1][0] <= QUOTE_WITHIN:
            return rows[-1][1] * self._unsplit(sym, day)
        raise ImportFailed(
            f"no {sym} price within a week of {day}: add a daily history to "
            f"sources/market-history/{sym}.csv"
        )


# ---- booking -----------------------------------------------------------------------------------


def cash_account() -> str:
    return "Assets:ETrade:Cash:USD"


def holding(sym: str) -> str:
    return f"Assets:ETrade:{commodity(sym)}"


def commodity(sym: str) -> str:
    from fin.importers.ibkr import commodity as c

    return c(sym)


def _ref(account: str, kind: str, *parts, nth: int = 1) -> str:
    text = "|".join(str(p) for p in (account, kind, *parts, nth))
    return f"{SOURCE}:{account}:{kind}:{hashlib.sha256(text.encode()).hexdigest()[:16]}"


def statements(files: list[Path]) -> list[Statement]:
    """Every statement once (a period can be saved twice), oldest first."""
    seen: dict[tuple, Statement] = {}
    for f in files:
        st = read_statement(f)
        if st.key in seen and (
            seen[st.key].cash_close,
            seen[st.key].held,
            len(seen[st.key].events),
        ) != (
            st.cash_close,
            st.held,
            len(st.events),
        ):
            raise ImportFailed(
                f"{f.name} and {seen[st.key].path.name} cover the same period but differ"
            )
        seen.setdefault(st.key, st)
    return sorted(seen.values(), key=lambda s: (s.account, s.start))


def _usd(x: Decimal) -> Decimal:
    return x.quantize(CENT)


def _gap(st: Statement, prev: Statement | None, price: Callable) -> list[RawTxn]:
    """What the account held when this statement opens, over what the previous one (if any) closed
    with: shares and cash that arrived or left in months fin has no statement for."""
    out: list[RawTxn] = []
    for sym in sorted(st.symbols() | (set(prev.held) if prev else set())):
        delta = st.opening_shares(sym) - (prev.held.get(sym, Decimal(0)) if prev else Decimal(0))
        if delta:
            value = _usd(abs(delta) * price(sym, st.start))
            sign = 1 if delta > 0 else -1
            out.append(
                RawTxn(
                    st.start, holding(sym), delta, commodity(sym), "E*TRADE",
                    f"{sym} shares in or out before this statement begins",
                    _ref(st.account, "gap", st.start, sym),
                    postings=[
                        f"{holding(sym)}  {delta} {commodity(sym)} @@ {value} USD",
                        f"{EQUITY}  {-sign * value} USD",
                    ],
                    meta={"basis": "estimate: market price of the day"},
                    review=False,
                )
            )  # fmt: skip
    cash = st.cash_open - (prev.cash_close if prev else Decimal(0))
    if cash:
        out.append(
            RawTxn(
                st.start, cash_account(), cash, "USD", "E*TRADE",
                "cash in or out before this statement begins",
                _ref(st.account, "gap", st.start, "cash"),
                counter=EQUITY, review=False,
            )
        )  # fmt: skip
    return out


def _delivery_value(led, sym: str, day: date, qty: Decimal) -> tuple[Decimal, str] | None:
    """The value the receiving broker booked for `qty` shares delivered on `day`: its transfer
    transaction on the clearing account, within a few days, for exactly these shares."""
    from beancount.core import data

    for e in led.entries:
        if not isinstance(e, data.Transaction) or not (
            0 <= (e.date - day).days <= IBKR_WITHIN.days
        ):
            continue
        stock = [
            p for p in e.postings
            if p.account.startswith("Assets:IBKR:") and p.units.currency == commodity(sym)
        ]  # fmt: skip
        clearing = [p for p in e.postings if p.account == CLEARING]
        if stock and clearing and sum(p.units.number for p in stock) == qty:
            return -sum(p.units.number for p in clearing), (e.meta or {}).get("ref", "")
    return None


def _txns(st: Statement, price: Callable, led) -> list[RawTxn]:
    out: list[RawTxn] = []
    seen: dict[tuple, int] = {}

    def nth(*key) -> int:
        seen[key] = seen.get(key, 0) + 1
        return seen[key]

    for e in st.events:
        a, kind, sym = st.account, e["kind"], e.get("sym")
        if kind == "receive":
            p = price(sym, e["date"])
            value = _usd(e["qty"] * p)
            ref = (
                f"{SOURCE}:{a}:rcv:{e['id']}"
                if e["id"]
                else _ref(a, "rcv", e["date"], sym, e["qty"], nth(kind, e["date"], e["qty"]))
            )
            out.append(
                RawTxn(
                    e["date"], holding(sym), e["qty"], commodity(sym), "E*TRADE",
                    f"{sym} shares received from the stock plan", ref,
                    postings=[
                        f"{holding(sym)}  {e['qty']} {commodity(sym)} @@ {value} USD",
                        f"Income:Employer:RSU:{commodity(sym)}  {-value} USD",
                    ],
                    meta={"price": f"{p:f}", "basis": "estimate: net shares at the market price of the day"},
                    review=False,
                )
            )  # fmt: skip
        elif kind == "trade":
            n = abs(e["qty"])
            gross = _usd(n * e["price"])
            fee = (gross - e["amount"]) if e["qty"] < 0 else (e["amount"] - gross)
            side = "sold" if e["qty"] < 0 else "bought"
            legs = [
                f"{holding(sym)}  {e['qty']} {commodity(sym)} @@ {gross} USD",
                f"{cash_account()}  {e['amount'] if e['qty'] < 0 else -e['amount']} USD",
            ]
            if fee:
                legs.append(f"{FEES}  {fee} USD")
            out.append(
                RawTxn(
                    e["date"], holding(sym), e["qty"], commodity(sym), "E*TRADE",
                    f"{side} {n} {sym} at {e['price']}",
                    _ref(a, "trade", e["date"], e["settle"], sym, e["qty"], e["price"], e["amount"], nth(kind, e["date"], e["qty"], e["price"])),
                    postings=legs, meta={"settles": e["settle"].isoformat()}, review=False,
                )
            )  # fmt: skip
        elif kind == "dividend":
            legs = [
                f"{cash_account()}  {e['gross'] - e['withheld']} USD",
                f"Income:Investments:Dividends:{commodity(sym)}  {-e['gross']} USD",
            ]
            if e["withheld"]:
                legs.insert(1, f"Expenses:Taxes:Withholding:US  {e['withheld']} USD")
            out.append(
                RawTxn(
                    e["date"], cash_account(), e["gross"] - e["withheld"], "USD", "E*TRADE",
                    f"dividend {sym}", _ref(a, "div", e["date"], sym, e["gross"], nth(kind, e["date"], sym)),
                    postings=legs, review=False,
                )
            )  # fmt: skip
        elif kind == "wire":
            out.append(
                RawTxn(
                    e["date"], cash_account(), -e["amount"], "USD", "E*TRADE",
                    f"wire out: {e['text'].lower()}",
                    _ref(a, "wire", e["date"], e["amount"], nth(kind, e["date"], e["amount"])),
                    counter=CLEARING, review=False,
                )
            )  # fmt: skip
        elif kind == "due":
            out.append(
                RawTxn(
                    e["date"], cash_account(), -e["amount"], "USD", "E*TRADE",
                    "tax due on the stock plan, paid from sale proceeds",
                    f"{SOURCE}:{a}:due:{e['id']}", counter=TAX_DUE, review=False,
                )
            )  # fmt: skip
        elif kind == "split":
            out.append(
                RawTxn(
                    e["date"], holding(sym), e["added"], commodity(sym), "E*TRADE",
                    f"split: {e['added']} more {sym} on {e['on']}",
                    _ref(a, "split", e["date"], sym, e["added"]),
                    # the new shares come at no price, so the position keeps its value
                    postings=[f"{holding(sym)}  {e['added']} {commodity(sym)} @ 0 USD"],
                    review=False,
                )
            )  # fmt: skip
        elif kind == "deliver":
            found = _delivery_value(led, sym, e["date"], e["qty"]) if led is not None else None
            value, ref_to = found if found else (_usd(e["qty"] * price(sym, e["date"])), "")
            meta = (
                {"moved_to": ref_to} if ref_to else {"basis": "estimate: market price of the day"}
            )
            out.append(
                RawTxn(
                    e["date"], holding(sym), -e["qty"], commodity(sym), "E*TRADE",
                    f"delivered {e['qty']} {sym} to another broker",
                    f"{SOURCE}:{a}:deliver:{e.get('refid') or _ref(a, 'd', e['date'], e['qty'])[-16:]}",
                    postings=[
                        f"{holding(sym)}  {-e['qty']} {commodity(sym)} @@ {value} USD",
                        f"{CLEARING}  {value} USD",
                    ],
                    meta=meta, review=False,
                )
            )  # fmt: skip
    return out


def parse_files(
    files: list[Path], root: Path | None = None, price: Callable | None = None, led=None
) -> list[RawTxn]:
    """Every transaction the statements show, plus the opening balances where one statement does not
    continue from the one before."""
    price = price or Prices(root).on
    out: list[RawTxn] = []
    prev: Statement | None = None
    for st in statements(files):
        if prev is not None and prev.account != st.account:
            prev = None
        if prev is not None and prev.end + timedelta(days=1) == st.start:
            for sym in sorted(st.symbols() | set(prev.held)):
                if st.opening_shares(sym) != prev.held.get(sym, Decimal(0)):
                    raise ImportFailed(
                        f"{st.path.name}: opens with {st.opening_shares(sym)} {sym}, but "
                        f"{prev.path.name} closed with {prev.held.get(sym, 0)}"
                    )
            if st.cash_open != prev.cash_close:
                raise ImportFailed(
                    f"{st.path.name}: opens with other cash than {prev.path.name} closed with"
                )
        else:
            out += _gap(st, prev, price)
        out += _txns(st, price, led)
        prev = st
    return out


def parse_root(root: Path, files: list[Path]) -> list[RawTxn]:
    from fin import config, ledger

    return parse_files(files, root, led=ledger.load(config.main_ledger(root)))


def needed_accounts(root: Path) -> dict[str, dict]:
    sts = statements(source_files(root))
    since = min((st.start for st in sts), default=date(2016, 1, 1))
    out: dict[str, dict] = {
        cash_account(): {"group": "Cash", "institution": "E*TRADE", "since": since},
        FEES: {"cadence": "regular", "necessity": "essential", "since": since},
        TAX_DUE: {"cadence": "regular", "necessity": "essential", "since": since},
        "Expenses:Taxes:Withholding:US": {
            "cadence": "regular",
            "necessity": "essential",
            "since": since,
        },
    }
    for sym in sorted({s for st in sts for s in st.symbols()}):
        out[holding(sym)] = {"group": "Investments", "institution": "E*TRADE", "since": since}
        out[f"Income:Employer:RSU:{commodity(sym)}"] = {"since": since}
        out[f"Income:Investments:Dividends:{commodity(sym)}"] = {"since": since}
    return out


def derived(files: list[Path]) -> str:
    """Each statement's closing price as a `price` directive, and its closing shares and cash as `balance`
    assertions (the day after it ends). The
    cash of a statement with a sale that settles after its last day is left out: the statement counts
    the money when it arrives, the ledger when the shares go."""
    lines = []
    for st in statements(files):
        after = st.end + timedelta(days=1)
        for sym in sorted(st.symbols()):
            lines.append(f"{after} balance {holding(sym)}  {st.held.get(sym, 0)} {commodity(sym)}")
        for sym, price in sorted(st.prices.items()):  # the statement's own closing quote
            lines.append(f"{st.end} price {commodity(sym)}  {price} USD")
        straddles = any(
            e["kind"] == "trade" and e["date"] <= st.end < e["settle"] for e in st.events
        )
        if not straddles:
            lines.append(f"{after} balance {cash_account()}  {st.cash_close} USD")
    head = "; Generated by `fin import etrade` from the statements in sources/etrade/statements/.\n"
    return head + "\n".join(sorted(set(lines))) + "\n"


def after_import(root: Path) -> list[str]:
    from fin import config, gitops, ledger, prices, transfers

    path = root / DERIVED / "balances.beancount"
    text = derived(source_files(root))
    out: list[str] = []
    with gitops.write_lock(root):
        before = path.read_text() if path.exists() else None
        if before != text:
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
            gitops.commit_all(root, "etrade: statement balances")
            out.append("every statement's closing balance agrees with the ledger")
    out += transfers.share_move_report(root)
    try:  # dollars held since the first statement need the exchange rate of those days
        n = prices.update(root)
    except OSError as e:
        return [*out, f"could not fetch exchange rates ({e}); run `fin prices` when online"]
    return [*out, *([f"added {n} exchange rate(s)"] if n else [])]
