"""Settings that live in the ledger, so any Beancount tool sees what we see.

| What                          | Where in the ledger                                           |
|-------------------------------|---------------------------------------------------------------|
| base currency                 | `option "operating_currency"` (first one)                     |
| always-major accounts         | `major: TRUE` metadata on the account's `open` directive      |
| employer stock                | `employer: TRUE` metadata on its `commodity` directive        |
| major purchase threshold      | `YYYY-MM-DD custom "fin-major-threshold" 2000 CHF`            |
| allocation targets            | `YYYY-MM-DD custom "fin-allocation-target" "equity" 0.75`     |
| recurring confirm/reject      | `YYYY-MM-DD custom "fin-recurring" "confirmed" "serafe" Expenses:Government:Serafe` |
| one transaction is major      | `major: TRUE` (or FALSE) metadata on that transaction         |
| payee label rule              | `YYYY-MM-DD custom "fin-rule" "twint p p"` with `category:` / `cadence:` / `necessity:` / `major:` metadata |
| account institution and group | `institution:` / `group:` metadata on the account's `open` directive (see registry.py) |
| FIRE assumptions              | `YYYY-MM-DD custom "fin-fire" "withdrawal-rate" 0.035` (and the others below) |

For custom directives the latest date wins, so changing your mind is a new line, not an edit,
and the history stays in the file.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from beancount.core import data
from beancount.core.amount import Amount

from fin import gitops
from fin.ledger import Ledger

RECURRING_FILE = "ledger/manual/recurring.beancount"
FIRE_FILE = "ledger/manual/fire.beancount"

# FIRE assumption -> (lowest, highest) allowed; target-spend is an amount, the rest are numbers
FIRE_PARAMS: dict[str, tuple[Decimal, Decimal]] = {
    "withdrawal-rate": (Decimal("0.01"), Decimal("0.10")),
    "real-return": (Decimal("-0.05"), Decimal("0.15")),
    "target-spend": (Decimal(1), Decimal(10_000_000)),
    "birth-year": (Decimal(1900), Decimal(2100)),
    "pension-access-age": (Decimal(50), Decimal(75)),
    "target-age": (Decimal(30), Decimal(80)),
}


@dataclass
class LedgerSettings:
    base_currency: str = "CHF"
    major_threshold: Decimal | None = None
    major_accounts: list[str] = field(default_factory=list)
    employer_commodity: str | None = None
    targets: dict[str, Decimal] = field(default_factory=dict)
    confirmed: set[str] = field(default_factory=set)
    rejected: set[str] = field(default_factory=set)
    fire: dict[str, Decimal] = field(default_factory=dict)  # FIRE assumptions, latest wins
    problems: list[str] = field(
        default_factory=list
    )  # malformed directives, reported by `fin doctor`


def check_fire(param: str, number: Decimal) -> None:
    if param not in FIRE_PARAMS:
        raise ValueError(f"unknown FIRE setting {param!r}; choose from {', '.join(FIRE_PARAMS)}")
    low, high = FIRE_PARAMS[param]
    if not low <= number <= high:
        raise ValueError(f"{param} must be between {low} and {high}")


def series_id(payee_norm: str, account: str) -> str:
    return f"{payee_norm}|{account}"


def read(led: Ledger) -> LedgerSettings:
    s = LedgerSettings()
    s.base_currency = (led.options.get("operating_currency") or ["CHF"])[0]
    for e in led.entries:
        if isinstance(e, data.Open) and (e.meta or {}).get("major") is True:
            s.major_accounts.append(e.account)
        elif isinstance(e, data.Commodity) and (e.meta or {}).get("employer") is True:
            s.employer_commodity = e.currency
    for e in sorted((e for e in led.entries if isinstance(e, data.Custom)), key=lambda e: e.date):
        v = [x.value for x in e.values]
        where = f"{e.meta.get('filename')}:{e.meta.get('lineno')}"
        try:
            if e.type == "fin-major-threshold":
                amount = v[0]
                if not isinstance(amount, Amount):
                    raise ValueError("expected an amount like 2000 CHF")
                s.major_threshold = amount.number
            elif e.type == "fin-allocation-target":
                s.targets[str(v[0])] = Decimal(v[1])
            elif e.type == "fin-fire":
                param, value = str(v[0]), v[1]
                number = value.number if isinstance(value, Amount) else Decimal(value)
                check_fire(param, number)
                s.fire[param] = number
            elif e.type == "fin-recurring":
                status, sid = v[0], series_id(str(v[1]), str(v[2]))
                if status not in ("confirmed", "rejected"):
                    raise ValueError('status must be "confirmed" or "rejected"')
                (s.confirmed if status == "confirmed" else s.rejected).add(sid)
                (s.rejected if status == "confirmed" else s.confirmed).discard(sid)
        except (IndexError, ValueError, TypeError, ArithmeticError) as err:
            s.problems.append(f"{where}: bad {e.type} directive ({err})")
    return s


def _append_directive(root: Path, relpath: str, line: str, message: str) -> None:
    """Append one directive to a ledger file under the write lock; keep it only if the ledger still
    loads without errors, then commit. Anything else restores the file exactly."""
    from fin import config, ledger

    path = root / relpath
    with gitops.write_lock(root):
        before = path.read_text() if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((before + "\n" if before else "") + line)
        errors = ledger.load(config.main_ledger(root)).errors
        if errors:
            if before is None:
                path.unlink()
            else:
                path.write_text(before)
            raise ValueError(
                "ledger would have errors: " + "; ".join(ledger.format_errors(errors)[:3])
            )
        gitops.commit_all(root, message)


def record_recurring(
    root: Path, status: str, payee_norm: str, account: str, day: date | None = None
) -> None:
    """Confirm or reject a recurring series by appending a directive to the ledger."""
    from fin import config, ledger

    if status not in ("confirmed", "rejected"):
        raise ValueError("status must be confirmed or rejected")
    if not re.fullmatch(r"[a-z ]+", payee_norm):
        raise ValueError(f"not a normalized payee: {payee_norm!r}")
    # Beancount does not check accounts named inside custom directives, so we do
    opened = {
        e.account for e in ledger.load(config.main_ledger(root)).entries if isinstance(e, data.Open)
    }
    if account not in opened:
        raise ValueError(f"{account} is not an open account")
    line = f'{day or date.today()} custom "fin-recurring" "{status}" "{payee_norm}" {account}\n'
    _append_directive(root, RECURRING_FILE, line, f"recurring: {status} {payee_norm}")


def record_fire(
    root: Path, param: str, value: Decimal | int | str, day: date | None = None
) -> None:
    """Save one FIRE assumption by appending a directive (the latest one wins; history stays)."""
    from fin import config, ledger

    number = Decimal(str(value))
    check_fire(param, number)
    if param == "target-spend":
        ccy = read(ledger.load(config.main_ledger(root))).base_currency
        shown = f"{number} {ccy}"
    else:
        shown = (
            str(int(number)) if number == number.to_integral() else format(number.normalize(), "f")
        )
    line = f'{day or date.today()} custom "fin-fire" "{param}" {shown}\n'
    _append_directive(root, FIRE_FILE, line, f"fire: {param} {shown}")


def doctor(root: Path, led: Ledger) -> list[tuple[str, bool, str]]:
    """Health checks for settings: (name, ok, detail)."""
    from fin import config

    s = read(led)
    stale = config.stale_config_keys(root)
    checks = [
        (
            "settings in config.toml",
            not stale,
            f"{', '.join(stale)} now live in the ledger (see ledger/settings.beancount); config.toml copies are ignored"
            if stale
            else "none left behind",
        ),
        (
            "settings directives",
            not s.problems,
            "; ".join(s.problems) if s.problems else "all well-formed",
        ),
    ]
    from fin import registry

    used = {p.account for e in led.entries if isinstance(e, data.Transaction) for p in e.postings}
    accts = [a for a in registry.of(led).balance_sheet() if a.account in used]
    bad = [f"{a.account}: {a.problem}" for a in accts if a.problem]
    guessed = [f"{a.account} (counted as {a.group})" for a in accts if a.guessed]
    checks += [
        (
            "account groups are valid",
            not bad,
            "; ".join(bad) if bad else "every group is one of the known groups",
        ),
        (
            "every account has a group",
            not guessed,
            f"no group set for {', '.join(guessed)}; run `fin account set <account> --group ...`"
            if guessed
            else "all set (or settled by the account name)",
        ),
    ]
    if s.targets:
        total = sum(s.targets.values())
        checks.append(
            (
                "allocation targets sum to 1",
                abs(total - 1) < Decimal("0.001"),
                f"they sum to {total}",
            )
        )
    return checks
