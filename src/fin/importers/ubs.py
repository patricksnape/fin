"""UBS e-banking (Switzerland) cash accounts via the e-banking site's own JSON API, fetched from a
logged-in browser.

`fetch` opens a real browser window with a persistent profile; you log in (2FA included) and fin then
calls the site's endpoints from inside that page. The few headers the site's own scripts attach to
every call are copied from its first request and kept in memory only: no password, cookie or key is
ever written to disk, and the raw responses go straight to `sources/ubs/` without passing through an
LLM. `parse` turns those archived files into RawTxns.

Endpoints (found by watching the site, not documented by UBS; may change without notice):
    GET  /api/v3/cash-accounts?...                       -> the accounts: _embedded."cash-accounts"[]
    POST /api/v1/cash-transactions/search?accountId=<id>
         {"sort": "bookingDate+desc", "startDate": "YYYY-MM-DD", "description": "enhanced",
          "numberOfTransactions": <= 50}
         -> {"_embedded": {"cash-transactions": [...]}, "_links": {"next": {href, method,
            parameters}}, "page": {...}, "metaInformation": {"earliestStartDate": ...}}
Every account type seen so far (current, savings, the account the mortgage is paid from) is a cash
account and uses these same two calls. Credit cards and the mortgage debt itself do not.
"""

import hashlib
import json
import os
import re
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from fin.importers.base import ImportFailed, RawTxn

SOURCE = "ubs"
HOME = "https://ebanking-ch.ubs.com"
PAGE_SIZE = 50  # the API rejects anything larger
CASH_ACCOUNTS = re.compile(r"/api/v3/cash-accounts\?")
SEARCH = "/api/v1/cash-transactions/search"
# Headers the site's scripts add to every API call, without which it answers 401.
AUTH_HEADERS = ("apikey", "x-requested-with", "x-ubs-spa-location", "accept-language")
# The e-banking's own account names -> ledger accounts. Your other accounts (savings, a second
# current account) go in rules/ubs.yaml:
#   accounts: {"Savings": "Assets:UBS:Savings"}
ACCOUNTS = {
    "Current": "Assets:UBS:Checking",
    "Credit Card": "Liabilities:UBS:CreditCard",  # one card account, however many cards it has
}
BOOKED = {"booked"}
PENDING = {"pending", "reserved"}  # reserved: a card authorization that has not been booked
CLEARING = "Assets:Transfers:Pending"
CARD_ACCOUNTS = "/api/v2/credit-card-accounts?valuationCurrency=CHF"
CARD_TRANSACTIONS = "/api/v1/credit-card-transactions"
ZURICH = ZoneInfo("Europe/Zurich")
UNCATEGORIZED_IN = "Income:Uncategorized"
FEES = "accountInterestsAndFees"  # the bank's own code for charges and interest it books itself
FEES_ACCOUNT = "Expenses:Fees"
INTEREST_ACCOUNT = "Income:Interest"
SALARY_ACCOUNT = "Income:Employer:Salary"  # credits the bank itself calls "salary payment"
# Accounts the parser files transactions under, opened on import if missing.
NEEDED_ACCOUNTS = {
    FEES_ACCOUNT: {"cadence": "regular", "necessity": "essential"},
    INTEREST_ACCOUNT: {},
    SALARY_ACCOUNT: {},
}


def profile_dir() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "fin" / "browser" / SOURCE


def account_map(root: Path) -> dict[str, str]:
    path = root / "rules" / "ubs.yaml"
    if not path.exists():
        return dict(ACCOUNTS)
    import yaml

    extra = (yaml.safe_load(path.read_text()) or {}).get("accounts") or {}
    return {**ACCOUNTS, **extra}


def needed_accounts(root: Path) -> dict[str, dict]:
    """The bank's own accounts (cash in Cash, the card under Liabilities) plus fees and interest."""
    own = {
        account: {
            "group": "Liabilities" if account.startswith("Liabilities:") else "Cash",
            "institution": "UBS",
        }
        for account in account_map(root).values()
    }
    return {**own, **NEEDED_ACCOUNTS}


def card_key(t: dict) -> str:
    """UBS gives each item a new `_id` in every session. A card item is identified by what it is:
    when, how much, where, on which card, and its number on the statement (unique together)."""
    parts = (t.get("transactionDate"), t["postingAmount"]["amount"], t.get("details"), t.get("alias"), t.get("transactionNr"))  # fmt: skip
    digest = hashlib.sha256(
        "|".join("" if v is None else str(v) for v in parts).encode()
    ).hexdigest()
    return f"card:{digest[:16]}"


def cash_key(t: dict, raw: dict) -> str:
    """A cash item by the bank's transaction number, per account (a move between your own
    accounts carries the same number on both sides)."""
    if not t.get("businessTransactionId"):
        raise ImportFailed(f"UBS transaction without a transaction number ({t.get('_id')})")
    return f"{slug(raw['account']['displayName'])}:{t['businessTransactionId']}"


def native_id(t: dict, raw: dict) -> str:
    return card_key(t) if raw.get("kind") == "card" else cash_key(t, raw)


def _texts(elements) -> list[str]:
    """The text of each `{"text": {"value": ...}}` element; separators and other elements are
    layout only."""
    out = []
    for e in elements or []:
        value = ((e or {}).get("text") or {}).get("value")
        if value:
            out.append(str(value).strip())
    return out


def _body(description: dict) -> dict[str, str]:
    """The labelled lines under a transaction (`Reason for payment`, ...): label -> value."""
    fields = {}
    for block in description.get("bodyElements") or []:
        for entry in block.get("list") or []:
            lines = entry.get("lineItems") or []
            if len(lines) < 2:
                continue
            label = " ".join(
                t for item in lines[0].get("items") or [] for t in _texts(item.get("labelElements"))
            )
            value = " · ".join(t for item in lines[1].get("items") or [] for t in _texts([item]))
            if label and value:
                fields[label] = value
    return fields


def _items(page: dict) -> list[dict]:
    """The transactions of one archived page, whether a cash account's or a credit card's."""
    embedded = page.get("_embedded") or {}
    return embedded.get("cash-transactions") or embedded.get("transactions") or []


def _local_day(stamp: str) -> date:
    """A card timestamp like 2026-01-04T23:00:00Z is midnight in Zurich: take the local day."""
    return datetime.fromisoformat(stamp).astimezone(ZURICH).date()


def parse_card(raw: dict, account: str) -> list[RawTxn]:
    """One archived card-account file -> booked transactions. `postingAmount` is negative for a
    charge, which is RawTxn's convention for a debt growing. The purchase day is the date; the
    text in `details` is the merchant, padded with its city."""
    out = []
    for page in raw["pages"]:
        for t in _items(page):
            status = str(t["transactionStatus"]).lower()
            if status in PENDING:
                continue
            if status not in BOOKED:
                raise ImportFailed(f"unknown UBS card status {t['transactionStatus']!r}")
            details = re.split(r"\s{2,}", str(t.get("details") or "").strip(), maxsplit=1)
            payee = details[0] or t.get("merchantName") or "UBS card"
            city = details[1].strip() if len(details) > 1 else ""
            amount = _decimal(t["postingAmount"]["amount"])
            meta = {}
            if t.get("merchantName"):
                meta["merchant_category"] = str(t["merchantName"])
            if t.get("merchantGroupCode"):
                meta["merchant_group"] = str(t["merchantGroupCode"])
            if t.get("alias"):
                meta["card"] = str(t["alias"])
            booked_on = _local_day(t["transactionDate"])
            if t.get("valueDate") and t["valueDate"][:10] != booked_on.isoformat():
                meta["value_date"] = t["valueDate"][:10]
            original = t.get("originalAmount") or {}
            if original.get("currency") and original["currency"] != t["postingAmount"]["currency"]:
                meta["original_amount"] = f"{original['amount']} {original['currency']}"
                if t.get("effectiveExchangeRate"):
                    meta["fx_rate"] = str(t["effectiveExchangeRate"])
            payment = amount > 0 and payee.upper().startswith("DIRECT DEBIT")
            out.append(
                RawTxn(
                    date=booked_on,
                    account=account,
                    amount=amount,
                    currency=t["postingAmount"]["currency"],
                    payee=payee,
                    narration=" · ".join(
                        x for x in (city, t.get("parentTransactionDescription", "").strip()) if x
                    ),
                    ref=f"{SOURCE}:{card_key(t)}",
                    meta=meta,
                    counter=CLEARING if payment else None,
                    review=not payment,
                )
            )
    return out


def parse(raw: dict, account: str) -> list[RawTxn]:
    """One archived account file -> booked transactions. `amount` is already signed from the
    account's side (a debit is negative), which is exactly RawTxn's convention."""
    out = []
    for page in raw["pages"]:
        for t in _items(page):
            status = str(t["status"]).lower()
            if status in PENDING:
                continue
            if status not in BOOKED:
                raise ImportFailed(f"unknown UBS transaction status {t['status']!r} ({t['_id']})")
            description = (t.get("_embedded") or {}).get("description") or {}
            heading = description.get("heading") or {}
            head = _texts(heading.get("headlineElements"))
            body = _body(description)
            kinds = [
                k.lower()
                for k in _texts((description.get("subheading") or {}).get("headlineElements"))
            ]
            payee = head[0] if head else "UBS"
            narration = " · ".join(
                [
                    *head[1:],
                    *_texts(heading.get("bylineElements")),
                    *_texts((description.get("subheading") or {}).get("headlineElements")),
                    *([body["Reason for payment"]] if "Reason for payment" in body else []),
                ]
            )
            booked = date.fromisoformat((t.get("bookingDate") or t["tradeDate"])[:10])
            deal = (t.get("bankTransactionCode") or {}).get("proprietary", {}).get("dealType")
            meta = {}
            if deal:
                meta["ubs_type"] = deal
            if t.get("valueDate") and t["valueDate"][:10] != booked.isoformat():
                meta["value_date"] = t["valueDate"][:10]
            if t.get("businessTransactionId"):
                meta["transaction_no"] = str(t["businessTransactionId"])
            amount = _decimal(t["amount"])
            banks_own = deal == FEES  # the bank says what these are: no review needed
            counter = UNCATEGORIZED_IN if amount > 0 else None
            if banks_own:
                counter = INTEREST_ACCOUNT if amount > 0 else FEES_ACCOUNT
            salary = amount > 0 and "salary payment" in kinds  # net pay; payslips split it later
            out.append(
                RawTxn(
                    date=booked,
                    account=account,
                    amount=amount,
                    currency=t["currency"],
                    payee=payee,
                    narration=narration,
                    ref=f"{SOURCE}:{cash_key(t, raw)}",
                    meta=meta,
                    counter=SALARY_ACCOUNT if salary else counter,
                    review=not (banks_own or salary),
                )
            )
    return out


def _decimal(value):
    from decimal import InvalidOperation

    try:
        return Decimal(str(value))
    except InvalidOperation as e:
        raise ImportFailed(f"UBS amount {value!r} is not a number") from e


def parse_files(files: list[Path]) -> list[RawTxn]:
    seen: dict[str, RawTxn] = {}
    for f in files:
        raw = json.loads(f.read_text())
        root = f.parent.parent.parent  # <root>/sources/ubs/<file>
        mapped = account_map(root)
        name = raw["account"]["displayName"]
        if name not in mapped:
            raise ImportFailed(
                f"no ledger account for the UBS account {name!r}: add it under `accounts:` in "
                "rules/ubs.yaml"
            )
        for t in (parse_card if raw.get("kind") == "card" else parse)(raw, mapped[name]):
            seen[t.ref] = t  # fetches overlap; the same id is the same transaction
    return list(seen.values())


def source_files(root: Path) -> list[Path]:
    return sorted((root / "sources" / SOURCE).glob("*_*.json"))


def raw_record(root: Path, native: str) -> dict | None:
    """The full record UBS sent for one transaction, from the archived responses."""
    for f in source_files(root):
        raw = json.loads(f.read_text())
        if (raw.get("kind") == "card") != native.startswith("card:"):
            continue
        for page in raw["pages"]:
            for t in _items(page):
                if (
                    str(t.get("status") or t.get("transactionStatus")).lower() in BOOKED
                    and native_id(t, raw) == native
                ):
                    return t
    return None


def opening_balances(root: Path) -> list[tuple[date, str, Decimal, str, str]]:
    """What each account held before its first archived transaction, from the bank's own running
    balance: `(date, ledger account, amount, currency, ref)`. The balance after the oldest booked
    transaction minus that transaction's amount is exactly the balance before it. Dated on the
    first day archived, so the books start where your history does."""
    mapped = account_map(root)
    earliest: dict[str, dict] = {}
    for f in source_files(root):
        raw = json.loads(f.read_text())
        name = raw["account"]["displayName"]
        key = (raw["startDate"], raw.get("fetched", ""))  # the first archive of an account decides
        if name not in earliest or key < (
            earliest[name]["startDate"],
            earliest[name].get("fetched", ""),
        ):
            earliest[name] = raw
    out = []
    for name, raw in sorted(earliest.items()):
        if name not in mapped:
            raise ImportFailed(f"no ledger account for the UBS account {name!r}")
        if raw.get("kind") == "card":
            card = _card_opening(raw, mapped[name])
            if card:
                out.append(card)
            continue
        booked = [t for p in raw["pages"] for t in _items(p) if str(t["status"]).lower() in BOOKED]
        if not booked:
            continue
        oldest = booked[-1]  # the API sorts newest first
        amount = _decimal(oldest["runningBalance"]) - _decimal(oldest["amount"])
        first = min(
            date.fromisoformat(raw["startDate"]), date.fromisoformat(oldest["tradeDate"][:10])
        )
        out.append((first, mapped[name], amount, oldest["currency"], f"opening:{mapped[name]}"))
    return out


def _card_opening(raw: dict, account: str):
    """A card shows no running balance, only what is owed today. What it held before its first
    archived transaction is that, minus everything booked since (so the books end up equal to the
    bank's figure today). Reserved items are not booked and not counted."""
    # pages can overlap (the same item at the end of one and the start of the next): count it once
    booked = list(
        {
            card_key(t): t
            for p in raw["pages"]
            for t in _items(p)
            if str(t["transactionStatus"]).lower() in BOOKED
        }.values()
    )
    if not booked:
        return None
    flows = sum((_decimal(t["postingAmount"]["amount"]) for t in booked), Decimal(0))
    first = min(_local_day(t["transactionDate"]) for t in booked)
    ccy = raw["account"]["currency"]
    return (first, account, _decimal(raw["account"]["balance"]) - flows, ccy, f"opening:{account}")


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _fetch_cards(call, root: Path, today: date, log) -> None:
    """Archive each credit card account: every transaction the e-banking keeps (about two years),
    following its `next` links, plus the balance owed today."""
    listing = call("GET", CARD_ACCOUNTS)
    accounts = (listing.get("_embedded") or {}).get("creditCardAccounts") or []
    for acct in accounts:
        url = f"{CARD_TRANSACTIONS}?creditCardAccountIds={acct['id']}&transactionDateType=PURCHASE"
        pages = [call("GET", url)]
        while nxt := (pages[-1].get("_links") or {}).get("next"):
            pages.append(call("GET", nxt["href"]))
        days = [_local_day(t["transactionDate"]) for p in pages for t in _items(p)]
        first = min(days) if days else today
        name = (
            "Credit Card" if len(accounts) == 1 else f"Credit Card {acct.get('alias', '')}".strip()
        )
        path = root / "sources" / SOURCE / f"{first}_{today}_{slug(name)}.json"
        path.write_text(
            json.dumps(
                {
                    "fetched": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "startDate": first.isoformat(),
                    "kind": "card",
                    "account": {
                        "displayName": name,
                        "accountType": acct.get("accountType"),
                        "currency": acct["currency"],
                        "balance": acct["balance"]["amount"],  # negative: owed
                    },
                    "pages": pages,
                },
                indent=1,
            )
        )
        log(f"  {name}: {len(days)} transactions since {first}")


MORTGAGES_PAGE = f"{HOME}/app/OQJ/1/ebanking/spa.html#/lending/mortgages/real-estate-overview?navitemid=MortgagesPg"
MORTGAGE_API = "/api/v1/real-estate/mortgages/"


def _fetch_mortgages(page, root: Path, today: date, log) -> None:
    """Open the e-banking's mortgage page and archive what it loads: the amount owed on each
    mortgage and its yearly amortization. The page makes the calls itself, so nothing needs to be
    worked out about their parameters. Skipped quietly if there are no mortgages."""
    responses = []
    page.on("response", lambda r: responses.append(r) if MORTGAGE_API in r.url and r.ok else None)
    page.goto(MORTGAGES_PAGE)
    deadline = time.time() + 20
    while time.time() < deadline and not any("aggregations" not in r.url for r in responses):
        page.wait_for_timeout(500)
    detail = next((r for r in responses if "aggregations" not in r.url), None)
    if detail is None:
        log("  mortgages: nothing loaded, skipped")
        return
    out_dir = root / "sources" / SOURCE / "mortgages"
    out_dir.mkdir(parents=True, exist_ok=True)
    body = {
        "fetched": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mortgages": detail.json(),
    }
    agg = next((r for r in responses if "aggregations" in r.url), None)
    if agg is not None:
        body["aggregations"] = agg.json()
    (out_dir / f"{today}.json").write_text(json.dumps(body, indent=1))
    log("  mortgages: archived")


def after_import(root: Path) -> list[str]:
    """Split mortgage payments into interest and amortization (needs a mortgage snapshot)."""
    from fin import mortgage

    done = mortgage.apply(root)
    out = []
    if done.get("split"):
        out.append(f"split {done['split']} mortgage payment(s) into interest and amortization")
    out += [f"mortgage mismatch: {p}" for p in mortgage.check(root)]
    return out


def fetch(root: Path, start: date, end: date, log=print) -> list[Path]:
    """Open a browser, wait for you to log in, then archive every booked page of each cash account
    from `start` on, one file per account."""
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
            accounts_url: list[str] = []

            def seen(request):
                if "/api/" not in request.url:
                    return
                headers = request.headers
                if "apikey" in headers and not auth:
                    auth.update({k: headers[k] for k in AUTH_HEADERS if k in headers})
                if CASH_ACCOUNTS.search(request.url) and not accounts_url:
                    accounts_url.append(request.url)

            page.on("request", seen)
            page.goto(HOME)
            log("Log in in the browser window (including 2FA). Waiting for the accounts page...")
            deadline = time.time() + 600
            while not (auth and accounts_url) and time.time() < deadline:
                page.wait_for_timeout(500)
            if not (auth and accounts_url):
                raise ImportFailed("timed out waiting for you to log in")

            def call(method: str, url: str, body: dict | None = None) -> dict:
                result = page.evaluate(
                    """async ([method, url, body, headers]) => {
                        const r = await fetch(url, {method, credentials: 'same-origin',
                            headers: {accept: 'application/json', ...headers,
                                      ...(body ? {'content-type': 'application/json'} : {})},
                            body: body ? JSON.stringify(body) : undefined});
                        return {status: r.status, text: await r.text()};
                    }""",
                    [method, url, body, auth],
                )
                if result["status"] == 401:
                    raise ImportFailed("UBS answered 401 (session expired?): log in again")
                if result["status"] != 200:
                    raise ImportFailed(f"UBS {method} {url.split('?')[0]}: HTTP {result['status']}")
                return json.loads(result["text"])

            listing = call("GET", accounts_url[0])
            mapped = account_map(root)
            for acct in listing["_embedded"]["cash-accounts"]:
                name = acct["displayName"]
                if name not in mapped:
                    log(f"  {name}: no ledger account in rules/ubs.yaml, skipped")
                    continue
                query = f"?accountId={acct['_id']}"
                body = {
                    "sort": "bookingDate+desc",
                    "description": "enhanced",
                    "numberOfTransactions": PAGE_SIZE,
                }
                # the bank says how far back it goes; asking for more is an error
                probe = call(
                    "POST",
                    f"{SEARCH}{query}",
                    {**body, "numberOfTransactions": 1, "startDate": end.isoformat()},
                )
                earliest = date.fromisoformat(probe["metaInformation"]["earliestStartDate"][:10])
                first = max(start, earliest)
                pages = []
                result = call("POST", f"{SEARCH}{query}", {**body, "startDate": first.isoformat()})
                pages.append(result)
                while nxt := (result.get("_links") or {}).get("next"):
                    result = call("POST", nxt["href"], nxt["parameters"])
                    pages.append(result)
                path = out_dir / f"{first}_{end}_{slug(name)}.json"
                path.write_text(
                    json.dumps(
                        {
                            "fetched": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "startDate": first.isoformat(),
                            "account": {
                                k: acct.get(k)
                                for k in ("displayName", "accountType", "rubric", "accountCurrency")
                            },
                            "pages": pages,
                        },
                        indent=1,
                    )
                )
                written.append(path)
                n = sum(len(p["_embedded"]["cash-transactions"]) for p in pages)
                log(f"  {name}: {n} transactions since {first}")
            _fetch_cards(call, root, end, log)
            _fetch_mortgages(page, root, end, log)
        finally:
            ctx.close()
    return written
