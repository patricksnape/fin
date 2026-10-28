"""Shared importer pipeline: RawTxn -> beancount text -> one file per source-month, deduped by ref.

Parsing is always code, never an LLM. An import only ever appends transactions whose `ref` is not
already in the ledger, so re-importing the same raw file is a no-op.
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from beancount.core import data

from fin import config, gitops, ledger

UNCATEGORIZED = "Expenses:Uncategorized"


class ImportFailed(Exception):
    """A raw file the parser does not understand. Never guess: fix the parser instead."""


@dataclass
class RawTxn:
    date: date
    account: str  # the source's own account, e.g. Liabilities:Swisscard:MilesMore
    amount: Decimal  # signed change of `account`: negative = more debt / money out
    currency: str
    payee: str
    narration: str
    ref: str  # globally unique: "<source>:<native id>"
    meta: dict[str, str] = field(default_factory=dict)
    counter: str | None = None  # the other posting when the source knows it (e.g. a transfer)
    review: bool = True  # False when the source itself says what this is (e.g. a bank fee)
    # the other posting's (amount, currency) when it is in another currency, e.g. a conversion
    # between two of your own balances; `account` then carries the total price (`@@`)
    counter_amount: tuple[Decimal, str] | None = None
    # every posting line, for a transaction that is more than two legs (a trade: shares at cost,
    # cash, a fee); `account`/`amount` then only describe it
    postings: list[str] | None = None


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def render(t: RawTxn) -> str:
    """Two postings: the source account, and Expenses:Uncategorized (unless known) flagged #review.
    Categorization (rules, ML) runs on these afterwards and only touches the uncategorized leg."""
    other = t.counter or UNCATEGORIZED  # a refund is just a negative expense
    own, theirs = f"{t.amount} {t.currency}", f"{-t.amount} {t.currency}"
    if t.counter_amount:
        number, ccy = t.counter_amount
        own, theirs = f"{own} @@ {abs(number)} {ccy}", f"{number} {ccy}"
    legs = t.postings or [f"{t.account}  {own}", f"{other}  {theirs}"]
    lines = [
        f'{t.date} * "{_q(t.payee)}" "{_q(t.narration)}"' + (" #review" if t.review else ""),
        f'  ref: "{_q(t.ref)}"',
        *(f'  {k}: "{_q(v)}"' for k, v in t.meta.items()),
        *(f"  {leg}" for leg in legs),
    ]
    return "\n".join(lines)


def ensure_accounts(root: Path, accounts: dict[str, dict]) -> None:
    """Open the accounts a source files its transactions under, if they are not open yet. `accounts`
    maps name -> keyword arguments for account_admin.add_account (cadence, necessity, ...). An
    account already open but from a later day than the source's `since` is opened earlier, so
    history older than the books so far fits."""
    from fin import account_admin

    led = ledger.load(config.main_ledger(root))
    opened = {e.account: e for e in led.entries if isinstance(e, data.Open)}
    for account, kwargs in accounts.items():
        if account not in opened:
            account_admin.add_account(root, account, **{"since": date(2000, 1, 1), **kwargs})
        elif (since := kwargs.get("since")) and since < opened[account].date:
            _open_earlier(root, opened[account], since)


def _open_earlier(root: Path, e: data.Open, since: date) -> None:
    with gitops.write_lock(root):
        path = Path(e.meta["filename"])
        lines = path.read_text().split("\n")
        i = e.meta["lineno"] - 1
        lines[i] = lines[i].replace(str(e.date), str(since), 1)
        before = path.read_text()
        path.write_text("\n".join(lines))
        errors = ledger.load(config.main_ledger(root)).errors
        if errors:
            path.write_text(before)
            raise ImportFailed("; ".join(ledger.format_errors(errors)[:3]))
        gitops.commit_all(root, f"account: {e.account} open from {since}")


def existing_refs(root: Path) -> set[str]:
    led = ledger.load(config.main_ledger(root))
    return {
        e.meta["ref"] for e in led.entries if isinstance(e, data.Transaction) and "ref" in e.meta
    }


def write_new(root: Path, source: str, txns: list[RawTxn]) -> tuple[int, int]:
    """Append transactions not yet in the ledger. Returns (written, skipped_as_duplicates)."""
    with gitops.write_lock(root):
        seen = existing_refs(root)
        fresh = [t for t in sorted(txns, key=lambda t: (t.date, t.ref)) if t.ref not in seen]
        if len({t.ref for t in txns}) != len(txns):
            raise ImportFailed("duplicate ref inside one import: the parser's ids are not unique")
        by_month: dict[str, list[RawTxn]] = {}
        for t in fresh:
            by_month.setdefault(f"{t.date:%Y-%m}", []).append(t)
        undo: list[tuple[Path, int | None]] = []  # (file, size before our append; None = new file)
        for month, group in by_month.items():
            path = root / "ledger" / "imported" / source / month[:4] / f"{month}.beancount"
            path.parent.mkdir(parents=True, exist_ok=True)
            before = path.stat().st_size if path.exists() else None
            undo.append((path, before))
            with path.open("a") as f:
                if before:
                    f.write("\n")
                f.write("\n\n".join(render(t) for t in group) + "\n")
        if undo:
            led = ledger.load(config.main_ledger(root))
            if led.errors:
                for path, before in undo:
                    if before is None:
                        path.unlink()
                    else:
                        with path.open("r+") as f:
                            f.truncate(before)
                raise ImportFailed("; ".join(ledger.format_errors(led.errors)))
    return len(fresh), len(txns) - len(fresh)


def default_start(
    root: Path, source: str, today: date | None = None, days: int = 90, overlap: int = 14
) -> date:
    """Where an incremental fetch should begin: shortly before the last archived day, so late
    postings are picked up (dedupe by ref makes the overlap harmless). First run: `days` back."""
    today = today or date.today()
    ends = []
    for f in (root / "sources" / source).glob("*_*.json"):
        try:
            ends.append(date.fromisoformat(f.stem.split("_")[1]))
        except (IndexError, ValueError):
            continue
    return max(ends) - timedelta(days=overlap) if ends else today - timedelta(days=days)


_POSTING = re.compile(r"^  [A-Z][A-Za-z0-9-]*:")


def backfill_meta(root: Path, txns: list[RawTxn]) -> int:
    """Add metadata a newer parser records to transactions imported earlier. Only keys that are
    missing are added, nothing is overwritten, and the ledger is restored if it stops loading.
    Returns how many transactions were touched."""
    with gitops.write_lock(root):
        led = ledger.load(config.main_ledger(root))
        want = {t.ref: t.meta for t in txns if t.meta}
        by_file: dict[str, list[tuple[int, dict]]] = {}
        for e in led.entries:
            ref = (e.meta or {}).get("ref") if isinstance(e, data.Transaction) else None
            missing = {k: v for k, v in want.get(ref, {}).items() if k not in e.meta} if ref else {}
            if missing:
                by_file.setdefault(e.meta["filename"], []).append((e.meta["lineno"] - 1, missing))
        before = {p: Path(p).read_text() for p in by_file}
        try:
            for path, items in by_file.items():
                lines = before[path].split("\n")
                for start, missing in sorted(items, reverse=True):
                    end = start + 1
                    while end < len(lines) and lines[end].strip():
                        end += 1
                    spot = next(i for i in range(start, end) if _POSTING.match(lines[i]))
                    lines[spot:spot] = [f'  {k}: "{_q(v)}"' for k, v in missing.items()]
                Path(path).write_text("\n".join(lines))
            errors = ledger.load(config.main_ledger(root)).errors
            if errors:
                raise ImportFailed("; ".join(ledger.format_errors(errors)[:3]))
        except BaseException:
            for path, text in before.items():
                Path(path).write_text(text)
            raise
        n = sum(len(v) for v in by_file.values())
        if n:
            gitops.commit_all(root, f"backfill metadata on {n} transaction(s)")
    return n
