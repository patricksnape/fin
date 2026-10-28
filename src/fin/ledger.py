"""Load the ledger (cached by newest mtime under ledger/) and run beanquery against it."""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from beancount import loader
from beancount.core import data

# Beancount's own pickle cache does not notice new files matched by `include "dir/*.beancount"`, so
# a freshly written month or rules file would be silently missing. Our cache below is keyed on
# the newest mtime of every file under ledger/, which does.
loader.initialize(use_cache=False)

_cache: dict[Path, tuple[float, "Ledger"]] = {}


@dataclass
class Ledger:
    entries: list
    errors: list
    options: dict
    path: Path


def _newest_mtime(ledger_dir: Path) -> float:
    return max((p.stat().st_mtime for p in ledger_dir.rglob("*.beancount")), default=0.0)


def load(main: Path) -> Ledger:
    main = main.resolve()
    stamp = _newest_mtime(main.parent)
    cached = _cache.get(main)
    if cached and cached[0] == stamp:
        return cached[1]
    entries, errors, options = loader.load_file(str(main))
    ledger = Ledger(entries, errors, options, main)
    _cache[main] = (stamp, ledger)
    return ledger


def format_errors(errors: list) -> list[str]:
    out = []
    for e in errors:
        src = getattr(e, "source", None) or {}
        where = f"{src.get('filename', '?')}:{src.get('lineno', '?')}"
        out.append(f"{where}: {e.message}")
    return out


def query(main: Path, bql: str) -> tuple[list[str], list[tuple]]:
    """Run a BQL query. Returns (column names, rows)."""
    import beanquery

    conn = beanquery.connect(f"beancount:{main}")
    cur = conn.execute(bql)
    columns = [d.name for d in cur.description]
    return columns, cur.fetchall()


class EmptyLedger(Exception):
    """The ledger has no transactions yet, so there is nothing to report on."""


def has_transactions(led: Ledger) -> bool:
    return any(isinstance(e, data.Transaction) for e in led.entries)


def as_of(led: Ledger) -> date:
    """The date the data runs through: the latest transaction. Other directives (price quotes,
    settings, confirmations) can be dated later without meaning more transactions exist."""
    dates = [e.date for e in led.entries if isinstance(e, data.Transaction)]
    if not dates:
        raise EmptyLedger("the ledger has no transactions yet")
    return max(dates)


def valuation_date(led: Ledger) -> date:
    """The date to value things at: the later of the last transaction and the last price quote, so
    a new appraisal or market price counts even before the next transaction (as BQL's value() does)."""
    prices = [e.date for e in led.entries if isinstance(e, data.Price)]
    return max([as_of(led), *prices])
