"""Opening balances: what an account held before the first transaction fin has for it.

A source that knows its own running balance (UBS does) provides `opening_balances(root)`; a
balance read off a statement is recorded with `set_manual`. The entries go in
`ledger/opening/<name>.beancount`, balanced against Equity:Opening-Balances, and are skipped if
their ref is already in the ledger, so running this twice changes nothing.
"""

from pathlib import Path

from fin import config, gitops, importers, ledger
from fin.importers.base import ImportFailed, _q, existing_refs

EQUITY = "Equity:Opening-Balances"


def write(root: Path, source: str) -> int:
    """Append the missing opening-balance entries for a source; returns how many were written."""
    mod = importers.get(source)
    if not hasattr(mod, "opening_balances"):
        raise ImportFailed(
            f"{source} cannot tell what its accounts held before the first transaction"
        )
    return _append(root, source, mod.opening_balances(root))


def set_manual(root: Path, account: str, amount, day, currency: str = "CHF") -> int:
    """Record an opening balance you read off a statement. `amount` is the signed balance of
    `account` (a debt is negative). Skipped if this account already has one."""
    return _append(root, "manual", [(day, account, amount, currency, f"opening:{account}")])


def _append(root: Path, name: str, entries: list) -> int:
    with gitops.write_lock(root):
        have = existing_refs(root)
        fresh = [o for o in entries if o[4] not in have and o[2] != 0]
        if not fresh:
            return 0
        path = root / "ledger" / "opening" / f"{name}.beancount"
        path.parent.mkdir(parents=True, exist_ok=True)
        before = path.stat().st_size if path.exists() else None
        with path.open("a") as f:
            for day, account, amount, ccy, ref in fresh:
                f.write(
                    f'{"\n" if f.tell() else ""}{day} * "Opening balance" "{_q(account)}"\n'
                    f'  ref: "{_q(ref)}"\n'
                    f"  {account}  {amount} {ccy}\n"
                    f"  {EQUITY}  {-amount} {ccy}\n"
                )
        errors = ledger.load(config.main_ledger(root)).errors
        if errors:
            if before is None:
                path.unlink()
            else:
                with path.open("r+") as f:
                    f.truncate(before)
            raise ImportFailed("; ".join(ledger.format_errors(errors)[:3]))
        gitops.commit_all(root, f"opening balances for {name}: {len(fresh)}")
    return len(fresh)
