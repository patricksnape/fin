"""Interactive Brokers via the Flex Web Service: an Activity Flex Query you set up once in the
client portal, fetched as XML.

The token is read from where config says it lives (`[ibkr] token = "bitwarden:<item>"` in the data
repo's config.toml) at fetch time and kept in memory. IBKR puts the token in the request URL, so
no URL is ever logged or stored. The raw XML goes to `sources/ibkr/` minus the account holder's
address and contact details, and never passes through an LLM.

Protocol (Flex Web Service v3):
    GET <BASE>/SendRequest?t=<token>&q=<query id>&v=3[&fd=YYYYMMDD&td=YYYYMMDD]
        -> <FlexStatementResponse><Status>Success</Status><ReferenceCode>…</ReferenceCode>
    GET <BASE>/GetStatement?t=<token>&q=<reference code>&v=3
        -> the report (<FlexQueryResponse>), or <FlexStatementResponse> with ErrorCode 1019
           while it is still being generated
A query covers at most 365 days, so longer ranges are fetched one window at a time.

Booking (`parse_files` reads every archived window at once, because lots carry across them):
 * shares are lots at cost in `Assets:IBKR:<SYMBOL>`, each with a label: `b<tradeID>` for a buy,
   `t<transfer>-<n>` for a lot transferred in (with its original cost and acquisition date);
   a sale names the lots IBKR says it closed (the `Lot` rows that follow it), gains go to
   `Income:Investments:Gains:<SYMBOL>` and commissions to `Expenses:Fees:Broker`;
 * a currency trade is a conversion between `Assets:IBKR:Cash:<CCY>` accounts (IBKR's `netCash`
   is 0 for these: the legs are the quantity, the proceeds and the commission);
 * dividends, withholding tax, interest and fees from the cash transactions (DETAIL rows; the
   SUMMARY rows repeat them), dated when IBKR posted them, so its cash report agrees;
 * deposits, withdrawals and transfers in go to the clearing account for the transfer matcher;
 * a forward split replaces each lot by the split quantity at the same total cost and date.
 Anything else (another corporate action, a transfer out) is an error, never a guess.
`after_import` writes IBKR's daily prices and, per window, its closing cash and positions as
`balance` assertions, so any booking mistake fails the ledger check.
"""

import re
import time
import tomllib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from fin import secrets
from fin.importers.base import ImportFailed, RawTxn

SOURCE = "ibkr"
BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
WINDOW = timedelta(days=364)  # fd..td inclusive: at most 365 days
PAUSE = 1.5  # seconds between calls (IBKR allows 1 per second, 10 per minute)
PER_MINUTE = 9  # stay under IBKR's 10 calls per minute per token
IN_PROGRESS = {"1019"}  # statement generation in progress: ask again shortly
# answers that have turned out to be temporary: 1001 could not generate now, 1003 not available
# (seen for past windows that worked on a later try), 1004 incomplete, 1018 too many requests
RETRY = {"1001", "1003", "1004", "1018"}
MIN_SPLIT = timedelta(days=31)  # a window still refused is halved down to about a month
# what is archived of AccountInformation: no name, address, e-mail or phone
KEEP_ACCOUNT = {
    "accountId", "acctAlias", "currency", "accountType", "customerType", "accountCapabilities",
    "dateOpened", "dateFunded", "dateClosed", "lastTradedDate",
}  # fmt: skip


def settings(root: Path) -> dict:
    path = root / "config.toml"
    with path.open("rb") as f:
        cfg = tomllib.load(f).get("ibkr") or {}
    if not cfg.get("query_id") or not cfg.get("token"):
        raise ImportFailed('config.toml needs [ibkr] query_id = "…" and token = "bitwarden:<item>"')
    return cfg


def source_files(root: Path) -> list[Path]:
    return sorted((root / "sources" / SOURCE).glob("*_*.xml"))


def default_start(root: Path) -> date | None:
    """The day after the last archived window: IBKR's past days are final, and windows must not
    overlap (a window's commission credits are booked from its cash report)."""
    ends = [date.fromisoformat(f.stem.split("_")[1]) for f in source_files(root)]
    return max(ends) + timedelta(days=1) if ends else None


def windows(start: date, end: date) -> list[tuple[date, date]]:
    out, a = [], start
    while a <= end:
        b = min(a + WINDOW, end)
        out.append((a, b))
        a = b + timedelta(days=1)
    return out


# the other broker's account number on a transfer, and its holder's name
DROP_TRANSFER = ("account", "accountName")


def sanitize(xml: str | bytes) -> bytes:
    """The report minus personal details: the holder's (kept attributes: see KEEP_ACCOUNT) and the
    other side's account on transfers."""
    tree = ET.ElementTree(ET.fromstring(xml))
    for info in tree.iter("AccountInformation"):
        for key in [k for k in info.attrib if k not in KEEP_ACCOUNT]:
            del info.attrib[key]
    for tag in ("Transfer", "TransferLot"):
        for row in tree.iter(tag):
            for key in DROP_TRANSFER:
                row.attrib.pop(key, None)
    return ET.tostring(tree.getroot(), encoding="utf-8", xml_declaration=True)


_calls: deque[float] = deque(maxlen=PER_MINUTE)


def _wait_turn() -> None:
    """Sleep so that no more than PER_MINUTE calls fall in any 60 seconds."""
    if len(_calls) == PER_MINUTE and (wait := 60 - (time.monotonic() - _calls[0])) > 0:
        time.sleep(wait)
    _calls.append(time.monotonic())


def _get(endpoint: str, params: dict) -> ET.Element:
    _wait_turn()
    url = f"{BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "fin"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:  # fixed https host
            body = r.read().decode("utf-8")
    except OSError as e:  # never show the URL: it carries the token
        raise ImportFailed(f"IBKR {endpoint}: {type(e).__name__}") from None
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        raise ImportFailed(f"IBKR {endpoint}: the answer is not XML") from None


def _error(resp: ET.Element) -> tuple[str, str] | None:
    if resp.tag == "FlexStatementResponse" and resp.findtext("Status") != "Success":
        return resp.findtext("ErrorCode") or "?", resp.findtext("ErrorMessage") or "unknown error"
    return None


class Refused(ImportFailed):
    def __init__(self, code: str, message: str, what: str):
        super().__init__(f"IBKR {what}: {message} (code {code})")
        self.code = code


def _statement(token: str, query: str, a: date, b: date) -> str:
    sent = _get(
        "SendRequest", {"t": token, "q": query, "v": 3, "fd": f"{a:%Y%m%d}", "td": f"{b:%Y%m%d}"}
    )
    if err := _error(sent):
        raise Refused(err[0], err[1], "refused the request")
    ref = sent.findtext("ReferenceCode")
    for _ in range(40):
        time.sleep(PAUSE)
        got = _get("GetStatement", {"t": token, "q": ref, "v": 3})
        err = _error(got)
        if err and err[0] in IN_PROGRESS:
            continue
        if err:
            raise Refused(err[0], err[1], "could not produce the report")
        return ET.tostring(got, encoding="unicode")
    raise ImportFailed("IBKR is still generating the report; try again in a few minutes")


def _window(token: str, query: str, a: date, b: date, out_dir: Path, log) -> list[Path]:
    """One window, retried when IBKR's refusal looks temporary and halved if it persists."""
    path = out_dir / f"{a}_{b}.xml"
    if path.exists():  # fetched before: the past does not change
        log(f"  {a}..{b}: already saved")
        return []
    for attempt in range(3):
        try:
            xml = _statement(token, query, a, b)
            break
        except Refused as e:
            if e.code not in RETRY:
                raise
            last = e
            log(f"  {a}..{b}: {e}; trying again")
            time.sleep(20 * (attempt + 1))
    else:
        if b - a < MIN_SPLIT:
            raise last
        mid = a + (b - a) // 2
        log(f"  {a}..{b}: still refused; splitting it in two")
        return _window(token, query, a, mid, out_dir, log) + _window(
            token, query, mid + timedelta(days=1), b, out_dir, log
        )
    stmts = ET.fromstring(xml).findall(".//FlexStatement")
    if not stmts:
        raise ImportFailed("IBKR's report has no FlexStatement")
    got = {(s.get("fromDate"), s.get("toDate")) for s in stmts}
    if got != {(f"{a:%Y%m%d}", f"{b:%Y%m%d}")}:
        log(f"  asked for {a}..{b}, IBKR sent {sorted(got)} (it may not honour date ranges)")
    path.write_bytes(sanitize(xml))
    log(f"  {a}..{b}: saved")
    time.sleep(PAUSE)
    return [path]


def fetch(root: Path, start: date, end: date, log=print) -> list[Path]:
    """Archive the Flex report for `start`..`end`, one file per window of at most 365 days.
    Windows already archived are skipped, so a run that stopped part-way can simply be repeated."""
    cfg = settings(root)
    try:
        token = secrets.get(cfg["token"])
    except secrets.SecretError as e:
        raise ImportFailed(str(e)) from None
    out_dir = root / "sources" / SOURCE
    out_dir.mkdir(parents=True, exist_ok=True)
    end = min(end, date.today() - timedelta(days=1))  # IBKR has no statement for today yet
    written = []
    for a, b in windows(start, end):
        written += _window(token, str(cfg["query_id"]), a, b, out_dir, log)
    return written


CLEARING = "Assets:Transfers:Pending"
FEES = "Expenses:Fees:Broker"
INTEREST = "Income:Interest:IBKR"
PAYEE = "Interactive Brokers"
FEE_TYPES = {"Other Fees", "Commission Adjustments", "Broker Interest Paid"}
DERIVED = Path("ledger") / "imported" / SOURCE / "derived"
CENT = Decimal("0.01")  # IBKR states cash to 9 places; the ledger checks it to the cent


def _num(e: ET.Element, key: str) -> Decimal:
    return Decimal(e.get(key) or "0")


def _day(value: str) -> date:
    return date(int(value[:4]), int(value[4:6]), int(value[6:8]))


def commodity(symbol: str) -> str:
    """IBKR's symbol as a Beancount commodity (and account name part)."""
    name = re.sub(r"[^A-Z0-9]", "-", symbol.upper())
    return name if re.fullmatch(r"[A-Z][A-Z0-9-]*[A-Z0-9]", name) else f"X{name}"


def cash(ccy: str) -> str:
    return f"Assets:IBKR:Cash:{ccy}"


def holding(symbol: str) -> str:
    return f"Assets:IBKR:{commodity(symbol)}"


def _events(files: list[Path]) -> list[tuple]:
    """Every row fin books, once (windows may overlap), in booking order:
    (day, order, sequence, kind, element, attached rows)."""
    seen: set[str] = set()
    out = []
    seq = 0
    rank = {"transfer": 0, "split": 1, "trade": 2, "cash": 3}

    def add(kind: str, day: date, key: str, e: ET.Element, rows: list | None = None) -> None:
        nonlocal seq
        if key in seen:
            return
        seen.add(key)
        seq += 1
        # a sale comes after the buys and transfers of its day
        order = rank[kind] + (2 if kind == "trade" and e.get("buySell") == "SELL" else 0)
        out.append((day, order, seq, kind, e, rows or []))

    for f in files:
        root = ET.parse(f).getroot()
        for section, head, child, kind in (
            ("Trades", "Trade", "Lot", "trade"),
            ("Transfers", "Transfer", "TransferLot", "transfer"),
        ):
            for block in root.iter(section):
                current: list = []
                for e in [*block, None]:
                    if e is None or e.tag == head:
                        if current:
                            h, rows = current[0], current[1:]
                            day = _day(h.get("tradeDate") or h.get("date"))
                            add(kind, day, f"{kind}:{h.get('transactionID')}", h, rows)
                        current = [e] if e is not None else []
                    elif e.tag == child and current:
                        current.append(e)
        for e in root.iter("CashTransaction"):
            if e.get("levelOfDetail") == "DETAIL":
                add(
                    "cash",
                    _day(e.get("reportDate") or e.get("dateTime")),
                    f"cash:{e.get('transactionID')}",
                    e,
                )
        for e in root.iter("CorporateAction"):
            if e.get("levelOfDetail") == "DETAIL":
                add(
                    "split",
                    _day(e.get("reportDate") or e.get("dateTime")),
                    f"ca:{e.get('transactionID')}",
                    e,
                )
    return sorted(out, key=lambda x: x[:3])


class _Lots:
    """The lots fin has booked, so a sale or a split can name them exactly."""

    def __init__(self):
        self.held: dict[str, list[dict]] = {}

    def add(self, symbol, label, qty, ccy, day, opened, unit=None, total=None) -> str:
        lot = {
            "label": label,
            "qty": qty,
            "ccy": ccy,
            "day": day,
            "opened": opened,
            "unit": unit,
            "total": total,
        }
        self.held.setdefault(symbol, []).append(lot)
        return self.spec(lot)

    @staticmethod
    def spec(lot) -> str:
        cost = (
            f"{lot['unit']:f} {lot['ccy']}"
            if lot["unit"] is not None
            else f"# {lot['total']:f} {lot['ccy']}"
        )
        return f'{{{cost}, {lot["day"]}, "{lot["label"]}"}}'

    def take(self, symbol: str, qty: Decimal, opened: str | None) -> list[tuple[dict, Decimal]]:
        """`qty` shares from the lots opened at `opened` (IBKR's closed-lot time), oldest first;
        without one, oldest first across all lots."""
        lots = [x for x in self.held.get(symbol, []) if x["qty"] > 0]
        if opened is not None:
            exact = [x for x in lots if x["opened"] == opened]
            lots = exact or [x for x in lots if x["opened"][:8] == opened[:8]]
        out, left = [], qty
        for lot in lots:
            n = min(left, lot["qty"])
            if n > 0:
                lot["qty"] -= n
                out.append((lot, n))
                left -= n
        if left:
            raise ImportFailed(
                f"IBKR sold {qty} {symbol} opened {opened or '(any)'}, but fin holds no such lot"
            )
        return out


def _trade(e: ET.Element, lot_rows: list, lots: _Lots) -> RawTxn:
    ccy, q, p = e.get("currency"), _num(e, "quantity"), _num(e, "tradePrice")
    comm, comm_ccy, day = (
        _num(e, "ibCommission"),
        e.get("ibCommissionCurrency") or ccy,
        _day(e.get("tradeDate")),
    )
    ref = f"{SOURCE}:trade:{e.get('tradeID') or e.get('transactionID')}"
    fee = [f"{FEES}  {-comm} {comm_ccy}"] if comm else []
    if e.get("assetCategory") == "CASH":
        base, quote = e.get("symbol").split(".")
        proceeds = _num(e, "proceeds")
        if not q or not proceeds or (q > 0) == (proceeds > 0) or quote != ccy:
            raise ImportFailed(f"IBKR currency trade {ref} does not look like a conversion")
        legs = [
            f"{cash(base)}  {q} {base} @@ {abs(proceeds)} {quote}",
            f"{cash(quote)}  {proceeds} {quote}",
        ]
        legs += [*fee, f"{cash(comm_ccy)}  {comm} {comm_ccy}"] if comm else []
        return RawTxn(
            day,
            cash(base),
            q,
            base,
            PAYEE,
            f"convert {base}/{quote}",
            ref,
            postings=legs,
            review=False,
        )
    if e.get("assetCategory") != "STK":
        raise ImportFailed(f"unsupported assetCategory {e.get('assetCategory')!r} in {ref}")
    if comm and comm_ccy != ccy:
        fee.append(f"{cash(comm_ccy)}  {comm} {comm_ccy}")
        comm = Decimal(0)
    sym, net = e.get("symbol"), _num(e, "netCash")
    if e.get("buySell") == "BUY":
        spec = lots.add(sym, f"b{e.get('tradeID')}", q, ccy, day, e.get("dateTime"), unit=p)
        legs = [f"{holding(sym)}  {q} {commodity(sym)} {spec}", f"{cash(ccy)}  {net} {ccy}", *fee]
        return RawTxn(
            day, cash(ccy), net, ccy, PAYEE, f"buy {q} {sym}", ref, postings=legs, review=False
        )
    taken = []
    for row in lot_rows or [None]:
        n = abs(_num(row, "quantity")) if row is not None else abs(q)
        taken += lots.take(sym, n, row.get("openDateTime") if row is not None else None)
    if sum(n for _, n in taken) != abs(q):
        raise ImportFailed(f"IBKR's closed lots for {ref} do not add up to the shares sold")
    legs = [
        f'{holding(sym)}  -{n} {commodity(sym)} {{"{lot["label"]}"}} @ {p} {ccy}'
        for lot, n in taken
    ]
    legs += [f"{cash(ccy)}  {net} {ccy}", *fee, f"Income:Investments:Gains:{commodity(sym)}"]
    meta = {} if lot_rows else {"lots": "oldest first (IBKR listed none)"}
    return RawTxn(
        day,
        cash(ccy),
        net,
        ccy,
        PAYEE,
        f"sell {abs(q)} {sym}",
        ref,
        meta=meta,
        postings=legs,
        review=False,
    )


def _cash(e: ET.Element) -> RawTxn:
    kind, ccy, amount = e.get("type"), e.get("currency"), _num(e, "amount")
    day, ref = (
        _day(e.get("reportDate") or e.get("dateTime")),
        f"{SOURCE}:cash:{e.get('transactionID')}",
    )
    sym = e.get("symbol") or ""
    common = {
        "date": day,
        "account": cash(ccy),
        "amount": amount,
        "currency": ccy,
        "payee": PAYEE,
        "ref": ref,
        "review": False,
    }
    if kind == "Deposits/Withdrawals":
        return RawTxn(
            narration="deposit" if amount > 0 else "withdrawal", counter=CLEARING, **common
        )
    if kind == "Dividends":
        other, text = f"Income:Investments:Dividends:{commodity(sym)}", f"dividend {sym}"
    elif kind == "Withholding Tax":
        country = (re.search(r" - ([A-Z]{2}) Tax", e.get("description") or "") or [None, "Other"])[
            1
        ]
        other, text = f"Expenses:Taxes:Withholding:{country}", f"withholding tax {sym}".strip()
    elif kind == "Broker Interest Received":
        other, text = INTEREST, "interest"
    elif kind in FEE_TYPES:
        other, text = FEES, kind.lower()
    else:
        raise ImportFailed(f"unknown IBKR cash transaction type {kind!r} ({ref})")
    return RawTxn(narration=text, counter=other, **common)


def _transfer(e: ET.Element, lot_rows: list, lots: _Lots) -> RawTxn:
    ccy, day, ref = (
        e.get("currency"),
        _day(e.get("date")),
        f"{SOURCE}:transfer:{e.get('transactionID')}",
    )
    if e.get("direction") != "IN":
        raise ImportFailed(f"IBKR transfer out ({ref}) is not supported yet")
    whence = f"{e.get('type')} in from {e.get('deliveringBroker') or 'another broker'}"
    if e.get("assetCategory") == "CASH":
        amount = _num(e, "cashTransfer")
        return RawTxn(
            day, cash(ccy), amount, ccy, PAYEE, whence, ref, counter=CLEARING, review=False
        )
    if e.get("assetCategory") != "STK" or not lot_rows:
        raise ImportFailed(f"IBKR transfer {ref}: only shares with their lots are supported")
    sym, legs, total = e.get("symbol"), [], Decimal(0)
    for i, row in enumerate(lot_rows, 1):
        qty, cost, opened = _num(row, "quantity"), _num(row, "cost"), row.get("openDateTime")
        unit = cost / qty
        exact = unit == unit.quantize(Decimal("0.000001"))
        spec = lots.add(sym, f"t{e.get('transactionID')}-{i}", qty, ccy, _day(opened), opened,
                        unit=unit.normalize() if exact else None, total=None if exact else cost)  # fmt: skip
        legs.append(f"{holding(sym)}  {qty} {commodity(sym)} {spec}")
        total += cost
    if sum(_num(r, "quantity") for r in lot_rows) != _num(e, "quantity"):
        raise ImportFailed(f"IBKR transfer {ref}: its lots do not add up to the shares moved")
    legs.append(f"{CLEARING}  {-total} {ccy}")
    return RawTxn(
        day,
        holding(sym),
        _num(e, "quantity"),
        commodity(sym),
        PAYEE,
        f"{whence}: {sym}",
        ref,
        postings=legs,
        review=False,
    )


def _split(e: ET.Element, lots: _Lots) -> RawTxn:
    ref, sym, text = (
        f"{SOURCE}:ca:{e.get('transactionID')}",
        e.get("symbol"),
        e.get("description") or "",
    )
    m = re.search(r"SPLIT (\d+) FOR (\d+)", text)
    if e.get("type") != "FS" or not m:
        raise ImportFailed(
            f"IBKR corporate action {e.get('type')} ({ref}) needs a manual booking: {text}"
        )
    new, old = Decimal(m.group(1)), Decimal(m.group(2))
    legs, added, residual = [], Decimal(0), False
    for lot in [x for x in lots.held.get(sym, []) if x["qty"] > 0]:
        qty = lot["qty"] * new / old
        legs.append(f'{holding(sym)}  -{lot["qty"]} {commodity(sym)} {{"{lot["label"]}"}}')
        unit = lot["unit"] * old / new if lot["unit"] is not None else None
        if unit is not None and unit != unit.quantize(Decimal("0.000001")):
            unit = None
        total = (
            lot["total"]
            if lot["total"] is not None
            else (lot["unit"] * lot["qty"] if unit is None else None)
        )
        residual |= unit is None
        added += qty - lot["qty"]
        lot["qty"] = Decimal(0)
        spec = lots.add(
            sym,
            f"{lot['label']}s",
            qty,
            lot["ccy"],
            lot["day"],
            lot["opened"],
            unit=unit,
            total=total,
        )
        legs.append(f"{holding(sym)}  {qty} {commodity(sym)} {spec}")
    if added != _num(e, "quantity"):
        raise ImportFailed(
            f"IBKR split {ref}: fin's lots would gain {added} {sym}, IBKR says {_num(e, 'quantity')}"
        )
    if residual:  # a total cost spread over the new quantity can leave a rounding crumb
        legs.append(f"Income:Investments:Gains:{commodity(sym)}")
    day = _day(e.get("reportDate") or e.get("dateTime"))
    return RawTxn(
        day,
        holding(sym),
        added,
        commodity(sym),
        PAYEE,
        f"split {new} for {old}: {sym}",
        ref,
        postings=legs,
        review=False,
    )


def _credits(files: list[Path]) -> list[RawTxn]:
    """Commission credits IBKR redeemed into cash. They are in no row of the sections fetched,
    only in each window's cash report, so they are booked once per window, on its last day."""
    out = []
    for f in files:
        for st in ET.parse(f).getroot().iter("FlexStatement"):
            for e in st.iter("CashReportCurrency"):
                amount = _num(e, "commissionCreditsRedemption")
                if e.get("levelOfDetail") == "Currency" and amount:
                    ccy, day = e.get("currency"), _day(st.get("toDate"))
                    ref = f"{SOURCE}:credits:{ccy}:{st.get('fromDate')}-{st.get('toDate')}"
                    out.append(RawTxn(day, cash(ccy), amount, ccy, PAYEE, "commission credits redeemed", ref, counter=FEES, review=False))  # fmt: skip
    return out


def parse_files(files: list[Path]) -> list[RawTxn]:
    lots, out = _Lots(), _credits(files)
    for _day_, _order, _seq, kind, e, rows in _events(files):
        if kind == "trade":
            out.append(_trade(e, rows, lots))
        elif kind == "cash":
            out.append(_cash(e))
        elif kind == "transfer":
            out.append(_transfer(e, rows, lots))
        else:
            out.append(_split(e, lots))
    return out


def opened_on(files: list[Path]) -> date:
    for f in files:
        for info in ET.parse(f).getroot().iter("AccountInformation"):
            if info.get("dateOpened"):
                return _day(info.get("dateOpened"))
    return date(2020, 1, 1)


def needed_accounts(root: Path) -> dict[str, dict]:
    """Every account the archived history books to, opened on the day the IBKR account was."""
    files = source_files(root)
    since = opened_on(files)
    out: dict[str, dict] = {}
    for t in parse_files(files):
        for leg in t.postings or [t.account, t.counter or ""]:
            account = leg.split()[0] if leg else ""
            if account.startswith("Assets:IBKR:"):
                out[account] = {"group": "Investments", "institution": "IBKR", "since": since}
            elif account.startswith(("Expenses:Fees:", "Expenses:Taxes:")):
                out[account] = {"cadence": "regular", "necessity": "essential", "since": since}
            elif account.startswith("Income:"):
                out[account] = {"since": since}
    return out


def derived(files: list[Path]) -> str:
    """IBKR's daily prices, and its closing cash and positions per window as `balance` assertions
    (dated the day after, as Beancount asserts at the start of a day)."""
    prices: dict[tuple, str] = {}
    balances: dict[tuple, str] = {}
    held: set[str] = set()
    for f in files:
        root = ET.parse(f).getroot()
        for st in root.iter("FlexStatement"):
            after = _day(st.get("toDate")) + timedelta(days=1)
            for e in st.iter("PriorPeriodPosition"):
                prices[(e.get("date"), e.get("symbol"))] = (
                    f"{_day(e.get('date'))} price {commodity(e.get('symbol'))}  {e.get('price')} {e.get('currency')}"
                )
            for e in st.iter("CashReportCurrency"):
                if e.get("levelOfDetail") == "Currency":
                    ccy = e.get("currency")
                    balances[(after, cash(ccy))] = (
                        f"{after} balance {cash(ccy)}  {_num(e, 'endingCash').quantize(CENT)} {ccy}"
                    )
            listed = set()
            for e in st.iter("OpenPosition"):
                if e.get("levelOfDetail") == "SUMMARY":
                    sym = commodity(e.get("symbol"))
                    listed.add(sym)
                    prices[(e.get("reportDate"), e.get("symbol"))] = (
                        f"{_day(e.get('reportDate'))} price {sym}  {e.get('markPrice')} {e.get('currency')}"
                    )
                    balances[(after, sym)] = (
                        f"{after} balance Assets:IBKR:{sym}  {_num(e, 'position')} {sym}"
                    )
            for e in st.iter("Trade"):
                if e.get("assetCategory") == "STK":
                    held.add(commodity(e.get("symbol")))
            for e in st.iter("Transfer"):
                if e.get("assetCategory") == "STK":
                    held.add(commodity(e.get("symbol")))
            for sym in held - listed:  # sold out by the end of the window
                balances[(after, sym)] = f"{after} balance Assets:IBKR:{sym}  0 {sym}"
    head = (
        "; Generated by `fin import ibkr` from sources/ibkr/: IBKR's prices and closing balances.\n"
    )
    return head + "\n".join(sorted(prices.values()) + [""] + sorted(balances.values())) + "\n"


def after_import(root: Path) -> list[str]:
    from fin import config, gitops, ledger, prices

    path = root / DERIVED / "ibkr.beancount"
    text = derived(source_files(root))
    out = []
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
                    "IBKR's own balances disagree with the ledger:",
                    *ledger.format_errors(errors)[:10],
                ]
            gitops.commit_all(root, "ibkr: prices and closing balances")
            out.append("wrote IBKR prices and closing balances (all agree with the ledger)")
    from fin import transfers

    out += transfers.share_move_report(root)
    try:
        n = prices.update(root)
    except OSError as e:
        return [*out, f"could not fetch exchange rates ({e}); run `fin prices` when online"]
    return [*out, *([f"added {n} exchange rate(s)"] if n else [])]
