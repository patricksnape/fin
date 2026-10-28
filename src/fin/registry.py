"""What each account is: its institution and net worth group.

Both come from metadata on the account's `open` directive, so they are plaintext like everything else:

    2024-01-01 open Liabilities:UBS:CreditCard CHF
      institution: "UBS"
      group: "Liabilities"

Without metadata the old conventions apply: the institution is the second path segment, and the
group follows the path (`Assets:Pension:*` is Pension, `*:Cash:*` is Cash...). An asset account that
only ever holds fiat currency is Cash, anything else is Investments; those are guesses, and
`fin doctor` lists accounts that rely on one.
"""

import re
from dataclasses import dataclass
from datetime import date

from beancount.core import data

from fin.ledger import Ledger

# net worth groups, in display order; a Mortgage is a liability that also feeds home equity
GROUPS = ["Cash", "Investments", "Pension", "Property", "Vehicles", "Other", "Liabilities"]
ASSET_GROUPS = ("Cash", "Investments", "Pension", "Property", "Vehicles", "Other")
LIABILITY_GROUPS = ("Liabilities", "Mortgage")
ROLES = {
    "Pension": "pension",
    "Property": "property",
    "Vehicles": "vehicle",
    "Mortgage": "mortgage",
}
FIAT = {"CHF", "USD", "EUR"}
BALANCE_SHEET = ("Assets:", "Liabilities:")
NAME = re.compile(
    r"(Assets|Liabilities|Expenses|Income):[A-Z0-9][A-Za-z0-9-]*(:[A-Z0-9][A-Za-z0-9-]*)*"
)


def allowed_groups(account: str) -> tuple[str, ...]:
    return LIABILITY_GROUPS if account.startswith("Liabilities:") else ASSET_GROUPS


def path_choice(account: str) -> str | None:
    """The group the path alone settles, or None when it takes a guess."""
    if account.startswith("Liabilities:"):
        return "Mortgage" if account.startswith("Liabilities:Mortgage") else "Liabilities"
    for prefix, choice in (
        ("Assets:Pension", "Pension"),
        ("Assets:Property", "Property"),
        ("Assets:Vehicle", "Vehicles"),
        ("Assets:Receivable", "Other"),
        ("Assets:Transfers", "Other"),
    ):
        if account.startswith(prefix):
            return choice
    return "Cash" if ":Cash" in account else None


def guess_choice(currencies) -> str:
    return "Cash" if currencies and set(currencies) <= FIAT else "Investments"


@dataclass(frozen=True)
class Account:
    account: str
    institution: str
    choice: str | None  # Cash, Mortgage...; None outside the balance sheet
    group: str | None  # the net worth group: a Mortgage counts under Liabilities
    role: str | None
    currencies: tuple[str, ...]
    opened: date | None
    closed: date | None
    explicit_group: str | None
    explicit_institution: str | None
    guessed: bool  # no group metadata and the path did not settle it
    problem: str | None


def _resolve(account: str, meta: dict, currencies, opened, closed) -> Account:
    meta = meta or {}
    path_inst = account.split(":")[1] if account.count(":") else account
    inst_meta = meta.get("institution")
    inst_meta = str(inst_meta).strip() if inst_meta else None
    choice = group_meta = problem = None
    guessed = False
    if account.startswith(BALANCE_SHEET):
        raw = meta.get("group")
        if raw is not None:
            group_meta = str(raw).strip()
            match = next(
                (g for g in allowed_groups(account) if g.lower() == group_meta.lower()), None
            )
            if match:
                choice = match
            else:
                problem = f'group "{group_meta}" is not one of {", ".join(allowed_groups(account))}'
        if choice is None:
            choice = path_choice(account)
            if choice is None:
                choice, guessed = guess_choice(currencies), True
    group = "Liabilities" if choice == "Mortgage" else choice
    return Account(
        account=account,
        institution=inst_meta or path_inst,
        choice=choice,
        group=group,
        role=ROLES.get(choice) if choice else None,
        currencies=tuple(sorted(currencies)),
        opened=opened,
        closed=closed,
        explicit_group=group_meta,
        explicit_institution=inst_meta,
        guessed=guessed and group_meta is None,
        problem=problem,
    )


class Registry:
    def __init__(self, accounts: dict[str, Account], seen: dict[str, set[str]]):
        self.accounts = accounts
        self._seen = seen

    def get(self, account: str) -> Account:
        """An account never opened (rare) is resolved from its path and what it held."""
        found = self.accounts.get(account)
        return found or _resolve(account, {}, self._seen.get(account, set()), None, None)

    def group(self, account: str) -> str:
        return self.get(account).group or ""

    def institution(self, account: str) -> str:
        return self.get(account).institution

    def role(self, account: str) -> str | None:
        return self.get(account).role

    def is_role(self, account: str, *roles: str) -> bool:
        return self.get(account).role in roles

    def balance_sheet(self) -> list[Account]:
        return [a for a in self.accounts.values() if a.account.startswith(BALANCE_SHEET)]


def build(led: Ledger) -> Registry:
    opens: dict[str, data.Open] = {}
    closes: dict[str, date] = {}
    seen: dict[str, set[str]] = {}
    for e in led.entries:
        if isinstance(e, data.Open):
            opens[e.account] = e
        elif isinstance(e, data.Close):
            closes[e.account] = e.date
        elif isinstance(e, data.Transaction):
            for p in e.postings:
                if p.units.number is not None:
                    seen.setdefault(p.account, set()).add(p.units.currency)
    accounts = {}
    for name, o in opens.items():
        currencies = set(o.currencies or ()) | seen.get(name, set())
        accounts[name] = _resolve(name, o.meta, currencies, o.date, closes.get(name))
    return Registry(accounts, seen)


def of(led: Ledger) -> Registry:
    """The registry for a loaded ledger, built once per load."""
    reg = getattr(led, "_registry", None)
    if reg is None:
        reg = build(led)
        led._registry = reg
    return reg
