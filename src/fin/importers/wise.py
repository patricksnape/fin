"""Wise (multi-currency account and card) via the web app's own JSON API, fetched from a logged-in
browser, as for UBS.

`fetch` opens a real browser window with a persistent profile; you log in (2FA included) and fin then
calls the site's endpoints from inside that page. The one header the site's scripts attach to every
call is copied from its own first request and kept in memory only; the session itself is a cookie
the browser holds. Statements are archived per currency in `sources/wise/` without the account
holder's address or bank details, and never pass through an LLM. `parse` turns them into RawTxns.

Endpoints (found by watching the site, not documented by Wise; may change without notice):
    GET /gateway/v4/profiles/<profile>/balances?types=STANDARD   -> [{id, currency, amount, ...}]
    GET /gateway/v1/profiles/<profile>/balance-statements/<balance>/statement.json
        ?currency=<CCY>&intervalStart=<ISO>&intervalEnd=<ISO>&type=COMPACT
        -> {transactions: [...newest first], startOfStatementBalance, endOfStatementBalance, ...}
Each row's `amount` is the full change of that balance (fees included: `runningBalance` moves by
exactly `amount`); `totalFees` says how much of it was Wise's fee. A conversion between two balances
appears in both statements under one `referenceNumber` and is booked once, from the side it left.
"""

import json
import os
import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from fin.importers.base import ImportFailed, RawTxn

SOURCE = "wise"
HOME = "https://wise.com/all-transactions"
PROFILE = re.compile(r"/gateway/v\d+/profiles/(\d+)/")
AUTH_HEADERS = ("x-access-token",)
CLEARING = "Assets:Transfers:Pending"
FEES_ACCOUNT = "Expenses:Fees"
UNCATEGORIZED_IN = "Income:Uncategorized"
# Recipients that are really your own accounts (a broker, say) are listed in rules/wise.yaml:
#   own_recipients: ["my broker llc"]
# money sent there is a transfer, not spending.
ZURICH = ZoneInfo("Europe/Zurich")
NEEDED_ACCOUNTS = {FEES_ACCOUNT: {"cadence": "regular", "necessity": "essential"}}
# what is archived of each statement row; the rest (card holder name, account numbers) is dropped
KEEP = ("type", "date", "amount", "totalFees", "details", "exchangeDetails", "runningBalance", "referenceNumber")  # fmt: skip
KEEP_DETAILS = (
    "type", "description", "paymentReference", "category", "merchant", "amount", "senderName",
    "sourceAmount", "targetAmount", "rate",
)  # fmt: skip


def profile_dir() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "fin" / "browser" / SOURCE


def account(currency: str) -> str:
    return f"Assets:Wise:{currency}"


def source_files(root: Path) -> list[Path]:
    return sorted((root / "sources" / SOURCE).glob("*_*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(), parse_float=Decimal)


def needed_accounts(root: Path) -> dict[str, dict]:
    """One account per currency that has an archived statement, plus the fees account."""
    first: dict[str, date] = {}
    for f in source_files(root):
        raw = _load(f)
        day = datetime.fromisoformat(raw["query"]["intervalStart"]).astimezone(ZURICH).date()
        first[raw["currency"]] = min(first.get(raw["currency"], day), day)
    own = {
        account(ccy): {"group": "Cash", "institution": "Wise", "since": day}
        for ccy, day in first.items()
    }
    earliest = min(first.values(), default=date(2020, 1, 1))
    return {
        **own,
        **{a: {**kw, "since": earliest} for a, kw in NEEDED_ACCOUNTS.items()},
        "Expenses:Uncategorized": {
            "cadence": "regular",
            "necessity": "discretionary",
            "since": earliest,
        },
        "Income:Uncategorized": {"since": earliest},
    }


def sanitize(statement: dict, currency: str) -> dict:
    """What fin keeps of a statement: the rows (minus card holder and account numbers), the
    balances either side, and the holder's name (to recognize transfers between your own
    accounts). The holder's address and Wise's bank details are not kept."""
    holder = statement.get("accountHolder") or {}
    rows = []
    for t in statement["transactions"]:
        row = {k: t[k] for k in KEEP if k in t}
        details = {k: v for k, v in (t.get("details") or {}).items() if k in KEEP_DETAILS}
        if (t.get("details") or {}).get("recipient"):
            details["recipient"] = {"name": t["details"]["recipient"].get("name")}
        row["details"] = details
        rows.append(row)
    query = {k: v for k, v in (statement.get("query") or {}).items() if k != "profileId"}
    return {
        "currency": currency,
        "holder": " ".join(x for x in (holder.get("firstName"), holder.get("lastName")) if x),
        "query": query,
        "startOfStatementBalance": statement.get("startOfStatementBalance"),
        "endOfStatementBalance": statement.get("endOfStatementBalance"),
        "transactions": rows,
    }


def _name(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return " ".join(s.casefold().split())


def _stamp(t: dict) -> datetime:
    return datetime.fromisoformat(t["date"])


def _num(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _row_key(t: dict) -> str:
    """A statement row: Wise's reference is shared by a purchase and its refund (and by the two
    halves of a split capture), so the booking time goes with it. Stable across fetches."""
    return f"{t['referenceNumber']}:{_stamp(t).strftime('%Y%m%dT%H%M%S%f')}"


def _money(m: dict | None) -> str:
    return f"{_num(m['value'])} {m['currency']}" if m else ""


def _rows(files: list[Path]) -> dict[str, tuple[dict, dict]]:
    """Every archived row once (overlapping fetches repeat rows): key -> (row, its statement)."""
    seen: dict[str, tuple[dict, dict]] = {}
    for f in files:
        raw = _load(f)
        for t in raw["transactions"]:
            seen.setdefault(f"{raw['currency']}:{_row_key(t)}", (t, raw))
    return seen


def own_recipients(root: Path) -> tuple[str, ...]:
    path = root / "rules" / "wise.yaml"
    if not path.exists():
        return ()
    import yaml

    names = (yaml.safe_load(path.read_text()) or {}).get("own_recipients") or []
    return tuple(_name(n) for n in names if n)


def parse_root(root: Path, files: list[Path]) -> list[RawTxn]:
    return parse_files(files, own_recipients(root))


def parse_files(files: list[Path], own: tuple[str, ...] = ()) -> list[RawTxn]:
    """`own`: recipient names (normalized) that are your own accounts, e.g. a broker."""
    rows = _rows(files)
    # a conversion's credit side, by reference: the debit side books both
    credits = {
        t["referenceNumber"]: t
        for t, _ in rows.values()
        if t["details"].get("type") == "CONVERSION" and t["type"] == "CREDIT"
    }
    debits = {
        t["referenceNumber"]
        for t, _ in rows.values()
        if t["details"].get("type") == "CONVERSION" and t["type"] == "DEBIT"
    }
    out = []
    for t, raw in rows.values():
        kind, details, ccy = t["details"].get("type"), t["details"], raw["currency"]
        if kind == "CONVERSION" and t["type"] == "CREDIT":
            if t["referenceNumber"] not in debits:
                raise ImportFailed(
                    f"Wise conversion {t['referenceNumber']} into {ccy}: the statement it left "
                    "is not archived (fetch every currency)"
                )
            continue
        out.append(_txn(t, raw, kind, details, ccy, credits, own))
    return out


def _txn(t, raw, kind, details, ccy, credits, own_names=()) -> RawTxn:
    amount = _num(t["amount"]["value"])
    if t["amount"]["currency"] != ccy:
        raise ImportFailed(f"Wise row {t['referenceNumber']} is not in its statement's currency")
    meta: dict[str, str] = {}
    fee = _num((t.get("totalFees") or {}).get("value") or 0)
    if fee:
        meta["fee"] = f"{fee} {t['totalFees']['currency']}"
    common = {
        "date": _stamp(t).astimezone(ZURICH).date(),
        "account": account(ccy),
        "amount": amount,
        "currency": ccy,
        "ref": f"{SOURCE}:{_row_key(t)}",
        "meta": meta,
    }
    me = _name(raw.get("holder"))
    if kind == "CARD":
        merchant = details.get("merchant") or {}
        for key, value in (
            ("merchant_category", merchant.get("category")),
            ("portal_category", details.get("category")),
            ("merchant_city", merchant.get("city")),
            ("merchant_country", merchant.get("country")),
        ):
            if value:
                meta[key] = str(value).strip()
        spent = details.get("amount") or {}
        if spent.get("currency") and spent["currency"] != ccy:
            meta["original_amount"] = _money(spent)
        return RawTxn(
            payee=str(merchant.get("name") or details.get("description") or "Wise card").strip(),
            narration=" ".join(
                str(x).strip()
                for x in ("card ·", merchant.get("city"), merchant.get("country"))
                if x
            ),
            **common,
        )
    if kind == "CONVERSION":
        into = credits.get(t["referenceNumber"])
        if not into:
            raise ImportFailed(
                f"Wise conversion {t['referenceNumber']} from {ccy}: the statement it went to is "
                "not archived (fetch every currency)"
            )
        got = (_num(into["amount"]["value"]), into["amount"]["currency"])
        return RawTxn(
            payee="Wise",
            narration=f"converted {ccy} to {got[1]}",
            counter=account(got[1]),
            counter_amount=got,
            review=False,
            **{**common, "ref": f"{SOURCE}:{t['referenceNumber']}"},
        )
    if kind in ("TRANSFER", "DEPOSIT"):
        who = (
            details.get("senderName")
            if kind == "DEPOSIT"
            else (details.get("recipient") or {}).get("name")
        )
        own = (bool(me) and _name(who) == me) or any(b and b in _name(who) for b in own_names)
        if not own:
            meta["counterparty"] = "person"  # never sent to a model (see fin.privacy)
        text = " · ".join(
            x for x in (details.get("description"), details.get("paymentReference")) if x
        )
        payee = str(who or "Wise transfer").strip()
        if own and kind == "TRANSFER" and amount < 0 and fee and t["totalFees"]["currency"] == ccy:
            # Wise's fee is inside the amount; the other side receives the amount without it, so the
            # transfer is booked at what arrives (to pair exactly) and the fee on its own line
            legs = [
                f"{account(ccy)}  {amount} {ccy}",
                f"{CLEARING}  {-amount - fee} {ccy}",
                f"{FEES_ACCOUNT}  {fee} {ccy}",
            ]
            return RawTxn(payee=payee, narration=text, postings=legs, review=False, **common)
        return RawTxn(
            payee=payee,
            narration=text,
            counter=CLEARING if own else (UNCATEGORIZED_IN if amount > 0 else None),
            review=not own,
            **common,
        )
    if kind == "MONEY_ADDED":  # topped up from a bank account or a card: the other half is there
        return RawTxn(payee="Wise", narration=str(details.get("description") or "top-up"), counter=CLEARING, review=False, **common)  # fmt: skip
    if (
        kind == "UNKNOWN"
        and amount > 0
        and "refund" in str(details.get("description") or "").lower()
    ):
        # Wise returning a charge it made in error (seen once: "Overcharge refund")
        return RawTxn(payee="Wise", narration=str(details["description"]), counter=FEES_ACCOUNT, review=False, **common)  # fmt: skip
    if kind == "CARD_ORDER_CHECKOUT":
        return RawTxn(payee="Wise", narration=str(details.get("description") or "card order"), counter=FEES_ACCOUNT, review=False, **common)  # fmt: skip
    raise ImportFailed(f"unknown Wise transaction type {t['type']}/{kind}")


def opening_balances(root: Path) -> list[tuple[date, str, Decimal, str, str]]:
    """What each balance held when its archived history starts: the statement's own opening
    balance, dated on the statement's first day."""
    earliest: dict[str, dict] = {}
    for f in source_files(root):
        raw = _load(f)
        start = raw["query"]["intervalStart"]
        if (
            raw["currency"] not in earliest
            or start < earliest[raw["currency"]]["query"]["intervalStart"]
        ):
            earliest[raw["currency"]] = raw
    out = []
    for ccy, raw in sorted(earliest.items()):
        amount = _num((raw.get("startOfStatementBalance") or {}).get("value") or 0)
        day = datetime.fromisoformat(raw["query"]["intervalStart"]).astimezone(ZURICH).date()
        out.append((day, account(ccy), amount, ccy, f"opening:{account(ccy)}"))
    return out


DERIVED = Path("ledger") / "imported" / SOURCE / "derived"


def derived(root: Path) -> str:
    """Each statement's closing balance as a `balance` assertion, dated the day after the statement
    ends: whatever the ledger holds of a Wise balance at that point must be exactly what Wise says."""
    lines = set()
    for f in source_files(root):
        raw = _load(f)
        end = datetime.fromisoformat(raw["query"]["intervalEnd"]).astimezone(ZURICH).date()
        closing = _num(raw["endOfStatementBalance"]["value"])
        after = end + timedelta(days=1)
        lines.add(f"{after} balance {account(raw['currency'])}  {closing} {raw['currency']}")
    head = (
        "; Generated by `fin import wise` from sources/wise/: each statement's closing balance.\n"
    )
    return head + "\n".join(sorted(lines)) + "\n"


def after_import(root: Path) -> list[str]:
    """Write the balance assertions; other currencies also need exchange rates before anything can
    be valued in the base currency."""
    from fin import config, gitops, ledger, opening, prices

    out = []
    if n := opening.write(
        root, SOURCE
    ):  # a balance that held money before its history (idempotent)
        out.append(f"wrote {n} opening balance(s)")
    path = root / DERIVED / "balances.beancount"
    text = derived(root)
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
                    "a Wise statement's balance disagrees with the ledger:",
                    *ledger.format_errors(errors)[:10],
                ]
            gitops.commit_all(root, "wise: statement balances")
            out.append("every Wise statement's closing balance agrees with the ledger")
    try:
        n = prices.update(root)
    except OSError as e:
        return [*out, f"could not fetch exchange rates ({e}); run `fin prices` when online"]
    return [*out, *([f"added {n} exchange rate(s)"] if n else [])]


def _interval(d: date, end: bool = False) -> str:
    """A Zurich day boundary as the UTC instant Wise expects."""
    local = datetime.combine(d + timedelta(days=1) if end else d, datetime.min.time(), ZURICH)
    utc = local.astimezone(ZoneInfo("UTC")) - (timedelta(milliseconds=1) if end else timedelta(0))
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def fetch(root: Path, start: date, end: date, log=print) -> list[Path]:
    """Open a browser, wait for you to log in, then archive each currency's statement from
    `start` to `end` (one file per currency that has any history)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ImportFailed("playwright is not installed: uv sync --extra browser") from e

    out_dir = root / "sources" / SOURCE
    out_dir.mkdir(parents=True, exist_ok=True)
    profile_dir().mkdir(parents=True, exist_ok=True)
    written = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile_dir()), channel="chrome", headless=False, no_viewport=True
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            auth: dict[str, str] = {}
            profile: list[str] = []

            def seen(request):
                if "/gateway/" not in request.url:
                    return
                headers = request.headers
                if not auth and all(k in headers for k in AUTH_HEADERS):
                    auth.update({k: headers[k] for k in AUTH_HEADERS})
                m = PROFILE.search(request.url)
                if m and not profile:
                    profile.append(m.group(1))

            page.on("request", seen)
            page.goto(HOME)
            log("Log in in the browser window (including 2FA). Waiting for your transactions...")
            deadline = time.time() + 600
            while not (auth and profile) and time.time() < deadline:
                page.wait_for_timeout(500)
            if not (auth and profile):
                raise ImportFailed("timed out waiting for you to log in")

            def call(url: str) -> str:
                result = page.evaluate(
                    """async ([url, headers]) => {
                        const r = await fetch(url, {credentials: 'include',
                            headers: {accept: 'application/json', ...headers}});
                        return {status: r.status, text: await r.text()};
                    }""",
                    [url, auth],
                )
                if result["status"] == 401:
                    raise ImportFailed("Wise answered 401 (session expired?): log in again")
                if result["status"] != 200:
                    raise ImportFailed(f"Wise GET {url.split('?')[0]}: HTTP {result['status']}")
                return result["text"]

            base = f"/gateway/v4/profiles/{profile[0]}/balances?types=STANDARD"
            balances = json.loads(call(base))
            for b in balances:
                ccy = b["currency"]
                url = (
                    f"/gateway/v1/profiles/{profile[0]}/balance-statements/{b['id']}/statement.json"
                    f"?currency={ccy}&intervalStart={_interval(start)}"
                    f"&intervalEnd={_interval(end, end=True)}&type=COMPACT"
                )
                statement = sanitize(json.loads(call(url)), ccy)
                opening = (statement["startOfStatementBalance"] or {}).get("value")
                if not statement["transactions"] and not opening:
                    continue
                statement["fetched"] = datetime.now().astimezone().isoformat(timespec="seconds")
                path = out_dir / f"{start}_{end}_{ccy}.json"
                path.write_text(json.dumps(statement, indent=1))
                written.append(path)
                log(f"  {ccy}: {len(statement['transactions'])} transactions since {start}")
        finally:
            ctx.close()
    return written
