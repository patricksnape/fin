"""`fin audit`: does the ledger still say what the raw sources say?

`fin check` proves the ledger balances and its statement balances hold. The audit goes the other
way: it parses every source's raw files again (nothing is written) and compares the result with the
ledger by `ref`, checks that the generated statement balances are current, that accounts have
balance assertions, that the clearing account is empty, and names what the books rest on estimates
or holes for. Findings are ERROR (the ledger disagrees with a source), WARN (a gap nobody has
explained) or INFO (a named exception or an estimate, with its total).

Only the source's own posting is compared (date, units, currency). The other leg is rewritten on
purpose by rules, labels and the transfer matcher.
"""

from __future__ import annotations

import inspect
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import yaml
from beancount.core import data
from beancount.core.position import Position

from fin import config, importers, ledger, registry
from fin.importers.base import ImportFailed

ERROR, WARN, INFO = "ERROR", "WARN", "INFO"
CLEARING = "Assets:Transfers:Pending"
OPENING = "Equity:Opening-Balances"
STALE = timedelta(days=45)  # transactions this long after the last assertion are unchecked
ESTIMATE_TAGS = ("etrade-unconfirmed", "untracked-external")


@dataclass
class Finding:
    severity: str
    area: str  # "diff ubs", "derived wise", "coverage", "clearing", "estimates"
    text: str
    detail: list[str] = field(default_factory=list)  # refs, dates, amounts: no payee or narration


def _rules(root: Path) -> list[dict]:
    path = root / "rules" / "audit.yaml"
    conf = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    return list(conf.get("exceptions") or [])


def _units(txn: data.Transaction, account: str) -> Counter:
    total: Counter = Counter()
    for p in txn.postings:
        if p.account == account and p.units.number is not None:
            total[p.units.currency] += p.units.number
    return total


# ---- 1. the raw files against the ledger -------------------------------------------------------


def _parse(root: Path, mod):
    files = mod.source_files(root)
    if not files:
        return None
    parse_root = getattr(mod, "parse_root", None)
    return parse_root(root, files) if parse_root else mod.parse_files(files)


def diff_source(root: Path, led, name: str, by_ref: dict[str, list]) -> list[Finding]:
    area = f"diff {name}"
    mod = importers.get(name)
    try:
        parsed = _parse(root, mod)
    except ImportFailed as e:
        return [Finding(ERROR, area, f"the parser refuses its own files: {e}")]
    except Exception as e:  # noqa: BLE001  a crash is a finding, and the other sources still run
        return [Finding(ERROR, area, f"the parser failed: {type(e).__name__}: {e}")]
    if parsed is None:
        return [Finding(INFO, area, "no raw files")]
    if not parsed:
        return [Finding(INFO, area, "the parser yields no transactions (applied elsewhere)")]
    out: list[Finding] = []
    seen = Counter(t.ref for t in parsed)
    dup = sorted(r for r, n in seen.items() if n > 1)
    ledger_dup = sorted(r for r in seen if len(by_ref.get(r, [])) > 1)
    missing, changed = [], []
    for t in parsed:
        found = by_ref.get(t.ref)
        if not found:
            missing.append(f"{t.date} {t.ref} {t.amount} {t.currency}")
            continue
        e = found[0]
        have = _units(e, t.account).get(t.currency)
        # a fee beside the amount (a conversion's commission) may sit on the same account
        single = any(
            p.account == t.account and p.units.currency == t.currency and p.units.number == t.amount
            for p in e.postings
        )
        if e.date != t.date or (have != t.amount and not single):
            changed.append(
                f"{t.ref}: source {t.date} {t.amount} {t.currency}, ledger {e.date} {have}"
            )
    prefix = f"{mod.SOURCE}:"
    extra = sorted(r for r in by_ref if r.startswith(prefix) and r not in seen)
    for sev, label, rows in (
        (ERROR, "duplicated in the parse", dup),
        (ERROR, "duplicated in the ledger", ledger_dup),
        (ERROR, "in the source, not in the ledger", missing),
        (ERROR, "differ from the source", changed),
        (WARN, "in the ledger, no longer in the source", extra),
    ):
        if rows:
            out.append(Finding(sev, area, f"{len(rows)} {label}", rows))
    if not out:
        out.append(Finding(INFO, area, f"{len(parsed)} rows, all in the ledger as parsed"))
    return out


# ---- 2. the generated statement balances are current -------------------------------------------


def check_derived(root: Path, name: str) -> list[Finding]:
    area = f"derived {name}"
    mod = importers.get(name)
    fn = getattr(mod, "derived", None)
    if fn is None:
        return []
    folder = root / "ledger" / "imported" / mod.SOURCE / "derived"
    on_disk = sorted(folder.glob("*.beancount")) if folder.exists() else []
    try:
        arg = root if "root" in inspect.signature(fn).parameters else mod.source_files(root)
        if not arg:
            return []
        want = fn(arg)
    except Exception as e:  # noqa: BLE001  reported, not raised
        return [Finding(ERROR, area, f"could not render: {type(e).__name__}: {e}")]
    if len(on_disk) != 1:
        return [Finding(ERROR, area, f"expected one generated balances file, found {len(on_disk)}")]
    if on_disk[0].read_text() != want:
        return [Finding(WARN, area, f"statement balances out of date: run `fin import {name}`")]
    return [Finding(INFO, area, "statement balances current")]


# ---- 3. assertion coverage ---------------------------------------------------------------------


def check_coverage(led, silenced: set[str]) -> list[Finding]:
    sourced = {s + ":" for s in importers.SOURCES}
    last_txn: dict[str, date] = {}
    for e in led.entries:
        if isinstance(e, data.Transaction):
            ref = (e.meta or {}).get("ref", "")
            if any(ref.startswith(s) for s in sourced):
                for p in e.postings:
                    last_txn[p.account] = max(last_txn.get(p.account, e.date), e.date)
    last_bal: dict[str, date] = {}
    for e in led.entries:
        if isinstance(e, data.Balance):
            last_bal[e.account] = max(last_bal.get(e.account, e.date), e.date)
    none, stale, quiet = [], [], []
    for acct in registry.of(led).balance_sheet():
        name = acct.account
        if name == CLEARING or name not in last_txn:
            continue
        has = last_bal.get(name)
        problem = none if has is None else stale if last_txn[name] - has > STALE else None
        if problem is None:
            continue
        (quiet if name in silenced else problem).append(
            f"{name}: last transaction {last_txn[name]}, last assertion {has or 'never'}"
        )
    out = []
    if none:
        out.append(
            Finding(WARN, "coverage", f"{len(none)} account(s) with no balance assertion", none)
        )
    if stale:
        out.append(
            Finding(
                WARN,
                "coverage",
                f"{len(stale)} account(s) with transactions long after the last assertion",
                stale,
            )
        )
    if quiet:
        out.append(
            Finding(
                INFO,
                "coverage",
                f"{len(quiet)} account(s) without current assertions, named in rules/audit.yaml",
                quiet,
            )
        )
    return out or [Finding(INFO, "coverage", "every importer-fed account has a current assertion")]


# ---- 4. the clearing account -------------------------------------------------------------------


def check_clearing(led) -> list[Finding]:
    net: dict[str, Decimal] = defaultdict(Decimal)
    open_legs: dict[str, list[str]] = defaultdict(list)
    for e in led.entries:
        if not isinstance(e, data.Transaction):
            continue
        for p in e.postings:
            if p.account != CLEARING or p.units.number is None:
                continue
            ccy = p.units.currency
            net[ccy] += p.units.number
            if "transfer" not in (e.meta or {}):
                open_legs[ccy].append(
                    f"{e.date} {(e.meta or {}).get('ref', '?')} {p.units.number} {ccy}"
                )
    out = []
    for ccy in sorted(net):
        n = net[ccy].quantize(Decimal("0.01"))
        if n:
            out.append(
                Finding(WARN, "clearing", f"{ccy}: net {n} in the clearing account", open_legs[ccy])
            )
    return out or [Finding(INFO, "clearing", "the clearing account nets to zero in every currency")]


# ---- 5. estimates and named holes --------------------------------------------------------------


def check_estimates(led, exceptions: list[dict]) -> list[Finding]:
    from fin.analytics.valuation import Market

    market = Market.of(led.entries, "CHF")

    def chf(e: data.Transaction, skip: tuple[str, ...] = ()) -> Decimal:
        total = Decimal(0)
        for p in e.postings:
            if p.units.number is None or p.units.number <= 0 or p.account.startswith(skip):
                continue
            try:
                total += market.value(Position(p.units, None), e.date)
            except ValueError:
                pass
        return total

    first: dict[str, date] = {}
    for e in led.entries:
        if isinstance(e, data.Transaction):
            for p in e.postings:
                first.setdefault(p.account, e.date)
    kinds: dict[str, list[str]] = defaultdict(list)
    totals: dict[str, Decimal] = defaultdict(Decimal)

    def add(kind: str, e: data.Transaction, amount: Decimal):
        kinds[kind].append(f"{e.date} {(e.meta or {}).get('ref', '-')}")
        totals[kind] += amount

    notes = {x["hole"]: x["note"] for x in exceptions if "hole" in x}
    tag_notes = {x["tag"]: x["note"] for x in exceptions if "tag" in x}
    for e in led.entries:
        if not isinstance(e, data.Transaction):
            continue
        if str((e.meta or {}).get("basis", "")).startswith("estimate"):
            add("estimated value (basis: estimate)", e, chf(e, ("Income:",)))
        for tag in sorted(e.tags or ()):
            if tag in ESTIMATE_TAGS or tag in tag_notes:
                add(
                    f"tagged #{tag}" + (f" ({tag_notes[tag]})" if tag in tag_notes else ""),
                    e,
                    chf(e),
                )
        accounts = {p.account for p in e.postings}
        if OPENING in accounts and any(
            first.get(a, e.date) < e.date
            for a in accounts
            if a.startswith(("Assets:", "Liabilities:"))
        ):
            add("gap entry (opening balance booked after the account began)", e, chf(e))
        for p in e.postings:
            if p.account in notes:
                add(f"{p.account} ({notes[p.account]})", e, chf(e))
    out = [
        Finding(INFO, "estimates", f"{k}: {len(rows)} entries, about CHF {totals[k]:,.0f}", rows)
        for k, rows in sorted(kinds.items())
    ]
    return out or [Finding(INFO, "estimates", "no estimates or named holes")]


# ---- the whole audit ---------------------------------------------------------------------------


def run(root: Path, only: str | None = None) -> list[Finding]:
    led = ledger.load(config.main_ledger(root))
    exceptions = _rules(root)
    by_ref: dict[str, list] = defaultdict(list)
    for e in led.entries:
        if isinstance(e, data.Transaction) and (e.meta or {}).get("ref"):
            by_ref[e.meta["ref"]].append(e)
    out: list[Finding] = []
    if led.errors:
        out.append(
            Finding(ERROR, "ledger", f"{len(led.errors)} Beancount error(s): run `fin check`")
        )
    names = [only] if only else list(importers.SOURCES)
    for name in names:
        out += diff_source(root, led, name, by_ref)
        out += check_derived(root, name)
    if not only:
        silenced = {x["account"] for x in exceptions if "account" in x}
        out += check_coverage(led, silenced)
        out += check_clearing(led)
        out += check_estimates(led, exceptions)
    return out


def report(findings: list[Finding], day: date | None = None) -> str:
    """The details for `inbox/audit.md`: refs, dates and amounts, never a payee or a narration."""
    lines = [f"# Audit {day or date.today()}", ""]
    for sev in (ERROR, WARN, INFO):
        rows = [f for f in findings if f.severity == sev]
        if not rows:
            continue
        lines += [f"## {sev}", ""]
        for f in rows:
            lines.append(f"- **{f.area}**: {f.text}")
            lines += [f"  - {d}" for d in f.detail]
        lines.append("")
    return "\n".join(lines)
