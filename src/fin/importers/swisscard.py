"""Swisscard (Miles & More) via the card portal's own JSON API, fetched from a logged-in browser.

`fetch` opens a real browser window with a persistent profile; you log in (2FA included) and fin
then calls the portal's endpoints from inside that page, so no password or cookie ever leaves the
browser and the raw responses go straight to `sources/swisscard/` without passing through an LLM.
`parse` turns those archived files into RawTxns.

Endpoint (found by watching the portal, not documented by Swisscard; may change without notice):
    POST /api/v1/accounts/<acct>/transactions  {"dateMin": "YYYY-MM-DD", "dateMax": "YYYY-MM-DD"}
    -> {"summary": {...}, "transactions": [{id, description, state, amount: {value, currency},
        authorizationDateTime, cardId, merchant: {name, ...}, ...}]}
"""

import json
import os
import re
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fin.importers.base import ImportFailed, RawTxn

SOURCE = "swisscard"
ACCOUNT = "Liabilities:Swisscard:MilesMore"
TRANSFER = "Assets:Transfers:Pending"  # paying the card bill, until a transfer matcher pairs it
PORTAL = "https://app.swisscard.ch/accounts/0/transactions"
TXN_URL = re.compile(r"/api/v1/accounts/(\d+)/transactions")
# Pending authorizations are never imported; an unknown state is an error, never a guess.
BOOKED = {"POSTED"}
PENDING = {"AUTHORIZED", "PENDING"}
# The API returns at most this many transactions per request and has no paging we know of, so a
# full response means "there may be more": split the date range until no response is full.
PAGE_CAP = 50


def profile_dir() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "fin" / "browser" / SOURCE


def parse(raw: dict) -> list[RawTxn]:
    """One archived response file -> booked transactions. Sign: a charge (positive value) lowers
    the liability account."""
    out = []
    for t in raw["response"]["transactions"]:
        state = str(t["state"]).upper()
        if state in PENDING:
            continue
        if state not in BOOKED:
            raise ImportFailed(f"unknown Swisscard transaction state {t['state']!r} (id {t['id']})")
        amount = t["amount"]
        merchant = (t.get("merchant") or {}).get("name") or t["description"]
        narration = "" if t["description"] == merchant else t["description"]
        booked = date.fromisoformat(t["postingDate"][:10])
        meta = {}
        if cat := (t.get("merchant") or {}).get("category"):
            meta["merchant_category"] = cat
        if cat := (t.get("transactionDataEnrichment") or {}).get("category"):
            meta["portal_category"] = cat
        if t.get("cardId"):
            meta["card"] = str(t["cardId"])[:8]  # the portal's ids are 64-character hashes
        if t["effectiveDate"][:10] != t["postingDate"][:10]:
            meta["value_date"] = t["effectiveDate"][:10]
        if cc := t.get("currencyConversion"):
            local = cc["localAmount"]
            meta["original_amount"] = f"{local['value']} {local['currency']}"
            meta["fx_rate"] = str(cc["exchangeRate"])
        out.append(
            RawTxn(
                date=booked,
                account=ACCOUNT,
                amount=-Decimal(str(amount["value"])),
                currency=amount["currency"],
                payee=merchant,
                narration=narration,
                ref=f"{SOURCE}:{t['id']}",
                meta=meta,
                counter=TRANSFER if meta.get("portal_category") == "PAYMENT" else None,
            )
        )
    return out


def parse_files(files: list[Path]) -> list[RawTxn]:
    seen: dict[str, RawTxn] = {}
    for f in files:
        for t in parse(json.loads(f.read_text())):
            seen[t.ref] = t  # months overlap at the edges; the same id is the same transaction
    return list(seen.values())


def source_files(root: Path) -> list[Path]:
    return sorted((root / "sources" / SOURCE).glob("*_*.json"))


def raw_record(root: Path, native_id: str) -> dict | None:
    """The full record the portal sent for one transaction, from the archived responses."""
    for f in source_files(root):
        for t in json.loads(f.read_text())["response"]["transactions"]:
            if str(t["id"]) == native_id:
                return t
    return None


def month_ranges(start: date, end: date):
    d = start.replace(day=1)
    while d <= end:
        nxt = (d + timedelta(days=32)).replace(day=1)
        yield max(d, start), min(nxt - timedelta(days=1), end)
        d = nxt


class TruncatedResponse(Exception):
    """The portal's reply was not complete JSON (seen on some long ranges)."""


def fetch_range(post, lo: date, hi: date):
    """Yield (lo, hi, body) for sub-ranges of lo..hi, none of which came back full."""
    try:
        body = post(lo, hi)
    except TruncatedResponse:
        if lo == hi:
            raise ImportFailed(f"{lo}: the portal keeps sending an incomplete response") from None
        body = None  # a long range sometimes comes back cut off: ask for halves instead
    if body is not None and len(body["transactions"]) < PAGE_CAP:
        yield lo, hi, body
    elif lo == hi:
        raise ImportFailed(f"{lo}: a single day returned {PAGE_CAP} transactions; cannot split")
    else:
        mid = lo + (hi - lo) // 2
        yield from fetch_range(post, lo, mid)
        yield from fetch_range(post, mid + timedelta(days=1), hi)


def fetch(root: Path, start: date, end: date, log=print) -> list[Path]:
    """Open a browser, wait for you to log in, then archive one raw response per month."""
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
            acct: list[str] = []
            page.on(
                "request",
                lambda r: (
                    acct.append(m.group(1)) if (m := TXN_URL.search(r.url)) and not acct else None
                ),
            )
            page.goto(PORTAL)
            log(
                "Log in in the browser window (including 2FA). Waiting for the transactions page..."
            )
            deadline = time.time() + 600
            while not acct and time.time() < deadline:
                page.wait_for_timeout(500)
            if not acct:
                raise ImportFailed("timed out waiting for you to log in")

            def post(lo: date, hi: date) -> dict:
                result = page.evaluate(
                    """async ([acct, lo, hi]) => {
                        const r = await fetch(`/api/v1/accounts/${acct}/transactions`, {
                            method: 'POST', credentials: 'same-origin',
                            headers: {'content-type': 'application/json', accept: 'application/json'},
                            body: JSON.stringify({dateMin: lo, dateMax: hi})});
                        return {status: r.status, text: r.ok ? await r.text() : null};
                    }""",
                    [acct[0], lo.isoformat(), hi.isoformat()],
                )
                if result["status"] != 200:
                    raise ImportFailed(f"{lo}..{hi}: HTTP {result['status']} (session expired?)")
                try:
                    return json.loads(result["text"])
                except json.JSONDecodeError:
                    raise TruncatedResponse from None

            for m_lo, m_hi in month_ranges(start, end):
                n = 0
                for lo, hi, body in fetch_range(post, m_lo, m_hi):
                    path = out_dir / f"{lo}_{hi}.json"
                    path.write_text(
                        json.dumps(
                            {
                                "fetched": datetime.now()
                                .astimezone()
                                .isoformat(timespec="seconds"),
                                "dateMin": lo.isoformat(),
                                "dateMax": hi.isoformat(),
                                "response": body,
                            },
                            indent=1,
                        )
                    )
                    written.append(path)
                    n += len(body["transactions"])
                log(f"  {m_lo:%Y-%m}: {n} transactions")
        finally:
            ctx.close()
    return written
