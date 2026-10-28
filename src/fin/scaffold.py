"""Create a fresh data repo with the standard layout."""

from pathlib import Path

from fin.gitops import git

MAIN = """option "title" "Finances"
option "operating_currency" "CHF"
; amounts in a currency with no decimals (yen) and prices from a total (`@@`) leave residues far below a cent
option "inferred_tolerance_default" "*:0.001"
option "operating_currency" "USD"
option "operating_currency" "EUR"

include "settings.beancount"
include "accounts.beancount"
include "commodities.beancount"
include "prices.beancount"
include "opening/*.beancount"
include "imported/*/*/*.beancount"
include "manual/*.beancount"
"""

ACCOUNTS = """; Open every account here. Expense accounts must carry cadence/necessity defaults:
; 2020-01-01 open Expenses:Food:Groceries CHF
;   cadence: "regular"
;   necessity: "essential"
;
; Cash, investment and debt accounts say which institution they belong to and where they count in
; net worth (Cash, Investments, Pension, Property, Vehicles, Other; Liabilities, Mortgage). Add them
; with `fin account add` or Settings > Accounts, or by hand:
; 2024-01-01 open Liabilities:UBS:CreditCard CHF
;   institution: "UBS"
;   group: "Liabilities"
2020-01-01 open Equity:Opening-Balances
2020-01-01 open Assets:Transfers:Pending
"""

GITIGNORE = """inbox/
.fin.lock
state/llm_audit.jsonl
.*.picklecache
"""

SETTINGS = """; Settings that change what your numbers mean live here, in the ledger, so any Beancount
; tool sees them. For custom directives the latest date wins.
;
; Purchases at or above this amount are "major" and kept out of monthly spend (override one
; transaction with `major: TRUE` / `major: FALSE` metadata; mark an account with `major: TRUE`
; on its `open` line):
2020-01-01 custom "fin-major-threshold" 2000 CHF
;
; Target weights by asset class, for the allocation drift view:
; 2020-01-01 custom "fin-allocation-target" "equity" 0.75
; 2020-01-01 custom "fin-allocation-target" "bonds" 0.15
; 2020-01-01 custom "fin-allocation-target" "cash" 0.10
;
;
; FIRE assumptions (latest date wins; the FIRE page can save these for you):
; 2020-01-01 custom "fin-fire" "withdrawal-rate" 0.035
; 2020-01-01 custom "fin-fire" "real-return" 0.04
; 2020-01-01 custom "fin-fire" "birth-year" 1990
; 2020-01-01 custom "fin-fire" "pension-access-age" 60
; 2020-01-01 custom "fin-fire" "target-age" 55
; 2020-01-01 custom "fin-fire" "target-spend" 60000 CHF   ; optional: otherwise your last 12 months
;
; Employer stock (shown as a concentration risk): add `employer: TRUE` to its `commodity` directive.
; Recurring payments you confirm or reject are recorded in manual/recurring.beancount.
"""

CONFIG = """# Preferences that are not part of your accounting. No secrets here: a token is named by where it
# lives (e.g. a Bitwarden item) and read only when it is needed.
# Settings that change what your numbers mean (major purchase threshold, allocation targets,
# recurring confirmations) live in the ledger: see ledger/settings.beancount.

# Account prefixes shown as their own lines instead of "spend":
# non_spend_prefixes = ["Expenses:Taxes", "Expenses:Social"]

# [[transfer_pairs]]
# a = "Assets:UBS:Checking"
# b = "Liabilities:Swisscard:MilesMore"
"""

PRIVACY = """# Strings stripped before anything leaves this machine.
redact_names: []
people: []
level: blind
"""

FILES = {
    "ledger/main.beancount": MAIN,
    "ledger/settings.beancount": SETTINGS,
    "ledger/accounts.beancount": ACCOUNTS,
    "ledger/commodities.beancount": "",
    "ledger/prices.beancount": "",
    "config.toml": CONFIG,
    "privacy.yaml": PRIVACY,
    "memory.md": "# Standing instructions for the agent\n",
    ".gitignore": GITIGNORE,
    # sync merges new transactions from two machines by ref instead of by line (see mergedriver.py)
    ".gitattributes": "ledger/imported/** merge=finref\nledger/manual/** merge=finref\n",
}

# Beancount errors on an include glob that matches nothing, so each glob in MAIN gets a
# placeholder file. Importers add real files next to these.
PLACEHOLDER = "; placeholder so the include glob in main.beancount always matches\n"
PLACEHOLDERS = [
    "ledger/opening/_placeholder.beancount",
    "ledger/imported/_placeholder/_/_placeholder.beancount",
    "ledger/manual/_placeholder.beancount",
]

# git does not track empty dirs, so each gets a .gitkeep
DIRS = [
    "state",
    "sources",
    "documents",
    "rules",
    "dashboards",
    "inbox",
]


def init_data(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise FileExistsError(f"{root} is not empty")
    for name, content in FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for name in PLACEHOLDERS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(PLACEHOLDER)
    for d in DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)
        if d != "inbox":
            (root / d / ".gitkeep").touch()
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
