"""Add, edit and close accounts by writing `open` / `close` directives into the ledger.

Everything is plain Beancount in `ledger/accounts.beancount`. As with label edits, a change is kept
only if the ledger still loads, and is committed in one go.
"""

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from beancount.core import data

from fin import gitops, registry
from fin.labels import CADENCES, NECESSITIES

ACCOUNTS_FILE = "ledger/accounts.beancount"
_META = re.compile(r"^  ([a-z][\w-]*):")


def _reg(root: Path):
    from fin import config, ledger

    led = ledger.load(config.main_ledger(root))
    return led, registry.of(led)


def check_name(account: str) -> None:
    if not registry.NAME.fullmatch(account) or account.count(":") < 1:
        raise ValueError(
            f"{account!r} is not an account name: use e.g. Liabilities:UBS:CreditCard "
            "(Assets, Liabilities, Expenses or Income, then capitalised parts)"
        )


def _check_group(account: str, group: str | None) -> str | None:
    if group is None:
        return None
    if not account.startswith(registry.BALANCE_SHEET):
        raise ValueError("only assets and liabilities have a group")
    match = next((g for g in registry.allowed_groups(account) if g.lower() == group.lower()), None)
    if not match:
        raise ValueError(f"group must be one of {', '.join(registry.allowed_groups(account))}")
    return match


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    if '"' in value or "\n" in value:
        raise ValueError("institution cannot contain quotes or line breaks")
    return value or None


def add_account(
    root: Path,
    account: str,
    group: str | None = None,
    institution: str | None = None,
    currencies: tuple[str, ...] = (),
    cadence: str | None = None,
    necessity: str | None = None,
    since: date | None = None,
) -> str:
    """Open an account. Returns the directive written. A balance sheet account always gets an
    explicit group, so nothing is left to a guess."""
    from fin.ledger_settings import _append_directive

    check_name(account)
    _, reg = _reg(root)
    if account in reg.accounts:
        raise ValueError(f"{account} already exists")
    ccys = tuple(c.upper() for c in currencies)
    if not all(re.fullmatch(r"[A-Z][A-Z0-9'._-]{0,23}", c) for c in ccys):
        raise ValueError("currencies look like CHF or EUR")
    meta: list[tuple[str, str]] = []
    if account.startswith(registry.BALANCE_SHEET):
        group = _check_group(account, group)
        group = group or registry.path_choice(account)
        if group is None and ccys:
            group = registry.guess_choice(ccys)
        if group is None:
            raise ValueError(
                f"choose a group for {account}: {', '.join(registry.allowed_groups(account))}"
            )
        inst = _clean(institution)
        if inst and inst != account.split(":")[1]:
            meta.append(("institution", f'"{inst}"'))
        meta.append(("group", f'"{group}"'))
    elif account.startswith("Expenses:"):
        cadence, necessity = cadence or "regular", necessity or "discretionary"
        if cadence not in CADENCES or necessity not in NECESSITIES:
            raise ValueError("cadence and necessity must be one of the known values")
        meta += [("cadence", f'"{cadence}"'), ("necessity", f'"{necessity}"')]
    elif group or institution or cadence or necessity:
        raise ValueError("income accounts take no labels")
    day = since or date.today()
    head = f"{day} open {account}" + (f" {','.join(ccys)}" if ccys else "")
    text = "\n".join([head, *(f"  {k}: {v}" for k, v in meta)]) + "\n"
    _append_directive(root, ACCOUNTS_FILE, text, f"account: open {account}")
    return text


def set_account(root: Path, account: str, changes: dict) -> None:
    """Change `group`, `institution` and/or `major` (True/False: whether the account's large
    payments count as major purchases) on an existing account; None removes the setting."""
    from fin import config, ledger

    unknown = set(changes) - {"group", "institution", "major"}
    if unknown:
        raise ValueError(f"unknown setting: {', '.join(sorted(unknown))}")
    with gitops.write_lock(root):
        led, reg = _reg(root)
        if account not in reg.accounts:
            raise KeyError(account)
        if "group" in changes:
            changes["group"] = _check_group(account, changes["group"])
        if "institution" in changes:
            changes["institution"] = _clean(changes["institution"])
        entry = next(e for e in led.entries if isinstance(e, data.Open) and e.account == account)
        path, start = Path(entry.meta["filename"]), entry.meta["lineno"] - 1
        before = path.read_text()
        lines = before.split("\n")
        end = start + 1
        while end < len(lines) and _META.match(lines[end]):
            end += 1
        block = lines[start:end]
        for key, value in changes.items():
            block = [ln for ln in block if not (m := _META.match(ln)) or m.group(1) != key]
            if isinstance(value, bool):
                block.append(f"  {key}: {'TRUE' if value else 'FALSE'}")
            elif value is not None:
                block.append(f'  {key}: "{value}"')
        lines[start:end] = block
        path.write_text("\n".join(lines))
        errors = ledger.load(config.main_ledger(root)).errors
        if errors:
            path.write_text(before)
            raise ValueError(
                "ledger would have errors: " + "; ".join(ledger.format_errors(errors)[:3])
            )
        gitops.commit_all(root, f"account: update {account}")


def close_account(root: Path, account: str, day: date | None = None) -> None:
    """Close an account that holds nothing (Beancount rejects postings after it)."""
    from fin.ledger_settings import _append_directive

    led, reg = _reg(root)
    info = reg.accounts.get(account)
    if info is None:
        raise KeyError(account)
    if info.closed:
        raise ValueError(f"{account} is already closed")
    day = day or date.today()
    held: dict[str, Decimal] = {}
    for e in led.entries:
        if isinstance(e, data.Transaction) and e.date <= day:
            for p in e.postings:
                if p.account == account and p.units.number is not None:
                    held[p.units.currency] = held.get(p.units.currency, Decimal(0)) + p.units.number
    left = {c: n for c, n in held.items() if n != 0}
    if left:
        raise ValueError(f"{account} still holds " + ", ".join(f"{n} {c}" for c, n in left.items()))
    _append_directive(root, ACCOUNTS_FILE, f"{day} close {account}\n", f"account: close {account}")
