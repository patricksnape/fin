"""UBS mortgages: split each quarterly payment into interest and amortization, and carry the debt.

The cash account the mortgage is paid from shows one debit per loan per quarter ("Maturity",
"HYPOTHEK <loan number>"). It is interest plus that quarter's share of the direct amortization, and
UBS does not say how it divides. The e-banking's mortgage page does give each loan's outstanding
amount and its direct amortization per year, so a quarter's amortization is a quarter of that and
the rest is interest. Each loan becomes a liability account; its opening balance is whatever makes
the ledger agree with the bank's figure today (the amount owed now plus the amortization booked).

`fin fetch ubs` archives the mortgage page's data under `sources/ubs/mortgages/<date>.json`.
Which ledger account each loan uses, and the cash account the payments come from, live in
`rules/mortgages.yaml` (loan numbers are written there the first time a loan is seen):

    pays_from: Assets:UBS:Checking
    accounts:
      "0000 00000000.ABC 0001": Liabilities:UBS:Mortgage:Loan1
"""

import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml
from beancount.core import data

from fin import account_admin, config, gitops, labels, ledger, opening
from fin.importers.base import ImportFailed

INTEREST = "Expenses:Housing:MortgageInterest"
DEFAULT_PAYS_FROM = "Assets:UBS:Checking"
OPENED_ON = date(2000, 1, 1)
LOAN_NO = re.compile(r"\d+\.[A-Z0-9]+ \d+$")


def snapshot_dir(root: Path) -> Path:
    return root / "sources" / "ubs" / "mortgages"


def latest(root: Path) -> dict | None:
    files = sorted(snapshot_dir(root).glob("*.json"))
    return json.loads(files[-1].read_text()) if files else None


def loans(snapshot: dict) -> list[dict]:
    """`{number, balance, amortization_per_year, start}` per mortgage, oldest first."""
    detail = snapshot.get("mortgages") or {}
    out = []
    for estate in detail.get("mortgagesByRealEstates") or []:
        for m in estate.get("mortgages") or []:
            out.append(
                {
                    "number": m["number"],
                    "balance": Decimal(str(m["mortgageAmount"]["amount"])),
                    "amortization_per_year": Decimal(str(m["directAmortizationPerYear"]["amount"])),
                    "start": m["startDate"],
                }
            )
    return sorted(out, key=lambda m: (m["start"], m["number"]))


def _conf_path(root: Path) -> Path:
    return root / "rules" / "mortgages.yaml"


def _conf(root: Path) -> dict:
    path = _conf_path(root)
    return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}


def pays_from(root: Path) -> str:
    """The cash account the quarterly payments are debited from (`pays_from` in the config)."""
    return str(_conf(root).get("pays_from") or DEFAULT_PAYS_FROM)


def account_names(root: Path, found: list[dict]) -> dict[str, str]:
    """Loan number -> liability account, remembered in rules/mortgages.yaml so names stay put."""
    conf = _conf(root)
    known = dict(conf.get("accounts") or {})
    changed = False
    for loan in found:
        if loan["number"] not in known:
            known[loan["number"]] = f"Liabilities:UBS:Mortgage:Loan{len(known) + 1}"
            changed = True
    if changed:
        path = _conf_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        conf["accounts"] = known  # keep the other settings (pays_from)
        path.write_text(yaml.safe_dump(conf, sort_keys=False, allow_unicode=True))
    return known


def _loan_for(narration: str, found: list[dict]) -> dict | None:
    m = re.search(r"\d+\.[A-Z0-9]+ \d+", narration)
    if not m:
        return None
    return next((x for x in found if x["number"].endswith(m.group(0))), None)


def apply(root: Path) -> dict[str, int]:
    """Split the unsplit mortgage payments, open the loan accounts and their opening balances.
    Returns counts; does nothing before the first mortgage snapshot has been fetched."""
    snap = latest(root)
    if not snap:
        return {}
    found = loans(snap)
    names = account_names(root, found)
    cash = pays_from(root)
    led = ledger.load(config.main_ledger(root))
    opened = {e.account for e in led.entries if isinstance(e, data.Open)}
    todo = []
    for e in led.entries:
        if not isinstance(e, data.Transaction) or e.payee != "Maturity":
            continue
        if "mortgage" in e.meta or not e.narration.upper().startswith("HYPOTHEK"):
            continue
        accts = {p.account for p in e.postings}
        if accts != {cash, labels.UNCATEGORIZED}:
            continue
        loan = _loan_for(e.narration, found)
        if not loan:
            continue
        paid = -next(p for p in e.postings if p.account == cash).units.number
        amortization = loan["amortization_per_year"] / 4
        if paid < amortization:
            raise ImportFailed(
                f"{e.date}: a mortgage payment smaller than a quarter's amortization; "
                "the split would put interest below zero"
            )
        todo.append((e, names[loan["number"]], amortization, paid - amortization))
    if todo:
        for account in {names[x["number"]] for x in found} - opened:
            account_admin.add_account(
                root,
                account,
                group="Mortgage",
                institution="UBS",
                currencies=("CHF",),
                since=OPENED_ON,
            )
        if INTEREST not in opened:
            account_admin.add_account(
                root, INTEREST, cadence="regular", necessity="essential", since=OPENED_ON
            )
        _rewrite(root, todo)
    return {"split": len(todo), "opening": _opening(root, found, names)}


def _rewrite(root: Path, todo: list) -> None:
    with gitops.write_lock(root):
        by_file: dict[str, list] = {}
        for e, account, amort, interest in todo:
            by_file.setdefault(e.meta["filename"], []).append(
                (e.meta["lineno"] - 1, account, amort, interest)
            )
        before = {p: Path(p).read_text() for p in by_file}
        try:
            for path, items in by_file.items():
                lines = before[path].split("\n")
                for start, account, amort, interest in sorted(items, reverse=True):
                    end = labels._block_end(lines, start)
                    block = [re.sub(r"\s+#review\b", "", lines[start])]
                    for line in lines[start + 1 : end]:
                        if line.strip().startswith(labels.UNCATEGORIZED):
                            ccy = line.split()[-1]
                            block += [
                                f"  {account}  {amort} {ccy}",
                                f"  {INTEREST}  {interest} {ccy}",
                            ]
                        else:
                            block.append(line)
                    spot = next(i for i, ln in enumerate(block) if labels._POSTING.match(ln))
                    block.insert(spot, f'  mortgage: "{account}"')
                    lines[start:end] = block
                Path(path).write_text("\n".join(lines))
            errors = ledger.load(config.main_ledger(root)).errors
            if errors:
                raise ImportFailed("; ".join(ledger.format_errors(errors)[:3]))
        except BaseException:
            for path, text in before.items():
                Path(path).write_text(text)
            raise
        gitops.commit_all(
            root, f"mortgage: split {len(todo)} payment(s) into interest and amortization"
        )


def _opening(root: Path, found: list[dict], names: dict[str, str]) -> int:
    """Each loan starts at what makes the books agree with the bank today: what is owed now plus
    the amortization the ledger has paid off since."""
    led = ledger.load(config.main_ledger(root))
    first = min((e.date for e in led.entries if isinstance(e, data.Transaction)), default=None)
    if first is None:
        return 0
    n = 0
    for loan in found:
        account = names[loan["number"]]
        paid_off = sum(
            (
                p.units.number
                for e in led.entries
                if isinstance(e, data.Transaction)
                for p in e.postings
                if p.account == account
            ),
            Decimal(0),
        )
        n += opening.set_manual(
            root, account, -(loan["balance"] + paid_off), first.replace(month=1, day=1)
        )
    return n


def check(root: Path) -> list[str]:
    """Where the ledger and the bank's latest snapshot disagree (one line each; empty is good)."""
    snap = latest(root)
    if not snap:
        return []
    found = loans(snap)
    names = account_names(root, found)
    led = ledger.load(config.main_ledger(root))
    problems = []
    for loan in found:
        account = names[loan["number"]]
        owed = -sum(
            (
                p.units.number
                for e in led.entries
                if isinstance(e, data.Transaction)
                for p in e.postings
                if p.account == account
            ),
            Decimal(0),
        )
        if owed != loan["balance"]:
            problems.append(f"{account}: ledger {owed}, bank {loan['balance']}")
    return problems


def _suffix(number: str) -> str:
    m = re.search(r"\.(H\w\w) ", number + " ")
    return m.group(1) if m else ""


def _edit_blocks(root: Path, jobs: list, message: str) -> None:
    """Rewrite transaction blocks in place (bottom-up per file); `jobs` is `[(entry, fn(block))]`.
    The ledger must still load afterwards, or every file is restored."""
    with gitops.write_lock(root):
        before = {e.meta["filename"]: Path(e.meta["filename"]).read_text() for e, _ in jobs}
        try:
            for e, fn in sorted(jobs, key=lambda j: (j[0].meta["filename"], -j[0].meta["lineno"])):
                lines = Path(e.meta["filename"]).read_text().split("\n")
                start = e.meta["lineno"] - 1
                end = labels._block_end(lines, start)
                lines[start:end] = fn(lines[start:end])
                Path(e.meta["filename"]).write_text("\n".join(lines))
            errors = ledger.load(config.main_ledger(root)).errors
            if errors:
                raise ImportFailed("; ".join(ledger.format_errors(errors)[:3]))
        except BaseException:
            for path, text in before.items():
                Path(path).write_text(text)
            raise
        gitops.commit_all(root, message)


def _recount(
    account: str, parts: list[tuple[str, Decimal]], ccy: str = "CHF", note: str | None = None
):
    """A block rewriter: the one posting that is still unsorted becomes `parts`."""

    def fn(block: list[str]) -> list[str]:
        out = [re.sub(r"\s+#review\b", "", block[0])]
        replaced = False
        for line in block[1:]:
            if (
                re.match(
                    rf"^\s+({re.escape(labels.UNCATEGORIZED)}|{re.escape(INTEREST)}|Equity:Opening-Balances)\s",
                    line,
                )
                and not replaced
            ):
                out += [f"  {a}  {n} {ccy}" for a, n in parts if n]
                replaced = True
            else:
                out.append(line)
        if not replaced:
            raise ImportFailed("a mortgage payment without an unsorted posting to settle")
        if note:  # the new note replaces one that was there
            out = [ln for ln in out if not re.match(r"^\s+note:", ln)]
        spot = next(i for i, ln in enumerate(out) if labels._POSTING.match(ln))
        out.insert(spot, f'  mortgage: "{account}"')
        if note:
            out.insert(spot + 1, f'  note: "{note}"')
        return out

    return fn


def apply_notices(root: Path, notices: list) -> dict[str, int]:
    """Settle the loans from the bank's own notices (see importers.ubs_mortgage):
     * each quarterly payment already booked from the account statements becomes interest plus
       amortization (the amortization reduces the loan), exactly as the notice says;
     * a loan's payout starts it at its real principal on its real day: when the money reached
       your account, that payment is the drawdown; when it went straight to the seller
       (`payout_to` in rules/mortgage-notices.yaml names the asset it bought), the principal is
       booked as that asset against the loan;
     * the opening balances the bank's figure once implied for those loans are dropped.
    Running it again changes nothing."""
    from datetime import timedelta

    snap = latest(root)
    if not snap:
        raise ImportFailed("no mortgage snapshot yet: run `fin fetch ubs` first")
    found = loans(snap)
    names = account_names(root, found)
    by_suffix = {_suffix(n["number"]): names[n["number"]] for n in found}
    conf_path = root / "rules" / "mortgage-notices.yaml"
    payout_to = (
        (yaml.safe_load(conf_path.read_text()) or {}).get("payout_to", {})
        if conf_path.exists()
        else {}
    )
    led = ledger.load(config.main_ledger(root))
    txns = [e for e in led.entries if isinstance(e, data.Transaction)]
    cash = pays_from(root)
    on_cash = lambda e: next((p for p in e.postings if p.account == cash), None)
    used: set[str] = set()
    jobs, split, payouts, new_payouts = [], 0, 0, []

    for n in (x for x in notices if x.kind == "maturity"):
        account = by_suffix[n.loan]
        hits = [
            e for e in txns
            if "mortgage" not in e.meta and e.meta.get("ref") not in used and on_cash(e) is not None
            and (e.meta.get("bank_type") or e.payee or "").upper() in ("MATURITY", "Maturity".upper())
            and on_cash(e).units.number == -n.total and n.day - timedelta(days=3) <= e.date <= n.day + timedelta(days=12)
        ]  # fmt: skip
        if not hits:
            continue
        if len(hits) > 1:
            raise ImportFailed(
                f"{n.day}: {len(hits)} payments of {n.total} could be the same notice"
            )
        used.add(hits[0].meta["ref"])
        jobs.append(
            (hits[0], _recount(account, [(account, n.amortization), (INTEREST, n.interest)]))
        )
        split += 1

    for n in (x for x in notices if x.kind in ("payout", "credit") and x.loan):
        account = by_suffix[n.loan]
        credit = [
            e for e in txns
            if on_cash(e) is not None and on_cash(e).units.number == n.total and abs((e.date - n.day).days) <= 10
            and not any(p.account == account for p in e.postings)
        ]  # fmt: skip
        if credit:
            jobs.append(
                (
                    credit[0],
                    _recount(
                        account, [(account, -n.total)], note="the loan was paid out to this account"
                    ),
                )
            )
            payouts += 1
        elif not any(
            e.meta.get("ref") == f"mortgage:payout:{n.loan}:{n.day}" for e in txns
        ) and not any(
            p.account == account and p.units.number == -n.total for e in txns for p in e.postings
        ):
            if n.loan not in payout_to:
                raise ImportFailed(
                    f"the {n.loan} loan was paid out without reaching your accounts: name what it bought under payout_to in rules/mortgage-notices.yaml"
                )
            new_payouts.append((n, account))
            payouts += 1
    if jobs:
        _edit_blocks(root, jobs, f"mortgage: settle {len(jobs)} payment(s) from the bank's notices")
    for (
        n,
        account,
    ) in new_payouts:  # entries that did not exist yet, after the edits to existing ones
        path = root / "ledger" / "manual" / "mortgage-payouts.beancount"
        path.parent.mkdir(parents=True, exist_ok=True)
        with gitops.write_lock(root):
            before = path.read_text() if path.exists() else None
            text = (
                f'{n.day} * "UBS" "Mortgage paid out"\n  ref: "mortgage:payout:{n.loan}:{n.day}"\n'
                f"  {payout_to[n.loan]}  {n.total} CHF\n  {account}  -{n.total} CHF\n"
            )
            path.write_text((before + "\n" if before else "") + text)
            errors = ledger.load(config.main_ledger(root)).errors
            if errors:
                path.write_text(before) if before is not None else path.unlink()
                raise ImportFailed("; ".join(ledger.format_errors(errors)[:3]))
            gitops.commit_all(root, f"mortgage: the {n.loan} loan was paid out on {n.day}")

    # a loan with a real start no longer needs the opening balance implied by today's debt
    dropped = 0
    paid_out = {by_suffix[n.loan] for n in notices if n.kind == "payout"}
    path = root / "ledger" / "opening" / "manual.beancount"
    if paid_out and path.exists():
        with gitops.write_lock(root):
            text = path.read_text()
            keep = [
                b
                for b in text.split("\n\n")
                if not any(f'ref: "opening:{a}"' in b for a in paid_out)
            ]
            dropped = len(text.split("\n\n")) - len(keep)
            if dropped:
                path.write_text("\n\n".join(keep))
                errors = ledger.load(config.main_ledger(root)).errors
                if errors:
                    path.write_text(text)
                    raise ImportFailed("; ".join(ledger.format_errors(errors)[:3]))
                gitops.commit_all(
                    root,
                    "mortgage: loans start at their real payout, not an implied opening balance",
                )
    return {"split": split, "payouts": payouts, "openings": dropped}
