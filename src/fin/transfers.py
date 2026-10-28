"""Pair the two halves of a transfer between your own accounts, so it is not counted as spending.

Paying a card bill leaves the bank account in one place and reaches the card in another; moving
money between your own bank accounts does the same. Each half is imported as its own transaction
(so each can be deduplicated by its ref), with the other leg on `Assets:Transfers:Pending`, or on
Uncategorized when the importer could not tell. Matching two halves rewrites both to
`Assets:Transfers:Pending` and links them with `transfer: "<other ref>"`: the clearing account then
nets to zero, and whatever stays on it is a transfer whose other half is missing.

Two kinds of pair, both needing equal and opposite amounts in the same currency:
 * a card payment (the card's side already sits on the clearing account) with a debit to the card
   company on a bank account, within a few days;
 * a debit and a credit between two different bank accounts, within a few days.
When more than one candidate is equally good the pair is left alone and reported as ambiguous:
a wrong match is worse than a missing one.
"""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from beancount.core import data

from fin import config, gitops, labels, ledger

CLEARING = "Assets:Transfers:Pending"
UNSORTED = {labels.UNCATEGORIZED, "Income:Uncategorized", CLEARING}
# a bank debit whose payee or text names one of these is a card payment
CARD_PAYEES = ("swisscard", "ubs card center")
# accounts that send and receive transfers
BANK_PREFIX = ("Assets:UBS:", "Assets:Wise:", "Assets:IBKR:Cash:", "Assets:Schwab:Cash:")
CARD_WINDOW = 5  # days between the bank debit and the card crediting it
OWN_WINDOW = 3  # days between the two sides of a move between own bank accounts


@dataclass
class Leg:
    ref: str
    date: date
    account: str  # the real account (bank or card)
    amount: Decimal  # signed change of `account`
    currency: str
    payee: str
    counter: str
    file: str
    line: int
    narration: str = ""


FEES = "Expenses:Fees"


def _legs(led) -> list[Leg]:
    """The movements that may be one half of a transfer. A transfer's fee (Wise takes it out of the
    amount sent) may sit on its own `Expenses:Fees` line: the leg is then what reaches the clearing
    account, which is what the other side receives."""
    out = []
    for e in led.entries:
        if not isinstance(e, data.Transaction) or not (e.meta or {}).get("ref"):
            continue
        postings = list(e.postings)
        if len(postings) == 3 and sum(p.account == FEES for p in postings) == 1:
            postings = [p for p in postings if p.account != FEES]
        if "transfer" in e.meta or len(postings) != 2:
            continue
        real = [p for p in postings if p.account.startswith(("Assets:", "Liabilities:"))]
        other = [p for p in postings if p not in real]
        if len(real) == 2 and any(p.account == CLEARING for p in real):
            real = [p for p in postings if p.account != CLEARING]
            other = [p for p in postings if p.account == CLEARING]
        if len(real) != 1 or len(other) != 1 or other[0].account not in UNSORTED:
            continue
        p = real[0]
        # what moves is what lands on the other side of the books (the same as the account's change,
        # except where a fee was taken out of it)
        amount = -other[0].units.number if len(e.postings) == 3 else p.units.number
        out.append(
            Leg(
                e.meta["ref"], e.date, p.account, amount, p.units.currency,
                e.payee or "", other[0].account, e.meta["filename"], e.meta["lineno"], e.narration or "",
            )
        )  # fmt: skip
    return out


def _is_card_payment(bank: Leg) -> bool:
    text = f"{bank.payee} {bank.narration}".lower()
    return (
        bank.account.startswith(BANK_PREFIX)
        and bank.amount < 0
        and any(c in text for c in CARD_PAYEES)
    )


def find_pairs(legs: list[Leg]) -> tuple[list[tuple[Leg, Leg]], list[Leg]]:
    """`(pairs, ambiguous)`. Candidate pairs are ranked by day gap; a leg whose best candidates tie
    is left unmatched."""
    edges: list[tuple[int, Leg, Leg]] = []
    cards = [
        leg for leg in legs if leg.counter == CLEARING and not leg.account.startswith(BANK_PREFIX)
    ]
    banks = [leg for leg in legs if leg.account.startswith(BANK_PREFIX)]
    by_amount: dict[tuple, list[Leg]] = defaultdict(list)  # only equal-and-opposite legs pair
    for bank in banks:
        by_amount[(bank.currency, bank.amount)].append(bank)
    for card in cards:
        for bank in by_amount[(card.currency, -card.amount)]:
            gap = abs((card.date - bank.date).days)
            if _is_card_payment(bank) and gap <= CARD_WINDOW:
                edges.append((gap, bank, card))
    for debit in (leg for leg in banks if leg.amount < 0):
        for credit in by_amount[(debit.currency, -debit.amount)]:
            gap = abs((debit.date - credit.date).days)
            if debit.account != credit.account and gap <= OWN_WINDOW:
                edges.append((gap, debit, credit))
    edges.sort(key=lambda e: (e[0], e[1].ref, e[2].ref))
    touching = Counter((gap, ref) for gap, a, b in edges for ref in {a.ref, b.ref})
    used: set[str] = set()
    pairs, ambiguous = [], []
    for gap, a, b in edges:
        if a.ref in used or b.ref in used:
            continue
        used |= {a.ref, b.ref}
        # another candidate just as close for either leg: a tie, so neither is matched
        if touching[(gap, a.ref)] + touching[(gap, b.ref)] > 2:
            ambiguous += [a, b]
        else:
            pairs.append((a, b))
    return pairs, ambiguous


CROSS_WINDOW = 3  # days between a CHF movement at the bank and the foreign one at Wise (a weekend)
CROSS_TOLERANCE = Decimal("0.06")  # how far the amounts may be from that day's exchange rate (fees)


def find_cross_pairs(legs: list[Leg], market, used: set[str]) -> list[tuple[Leg, Leg]]:
    """`[(bank leg in CHF, Wise leg in another currency)]` for money sent to (or taken from) Wise
    that Wise converted on arrival: no CHF amount exists on the Wise side, so the two are matched on
    being already marked as transfers, close in time, opposite in direction, and equal in value at
    that day's rate within a tolerance for fees. A leg with more than one candidate is left alone."""
    banks = [x for x in legs if x.ref not in used and x.counter == CLEARING and x.account.startswith("Assets:UBS:") and x.currency == "CHF"]  # fmt: skip
    wise = [x for x in legs if x.ref not in used and x.counter == CLEARING and x.account.startswith("Assets:Wise:") and x.currency != "CHF"]  # fmt: skip
    edges = []
    for b in banks:
        for w in wise:
            if (b.amount > 0) == (w.amount > 0) or abs((b.date - w.date).days) > CROSS_WINDOW:
                continue
            rate = market.price(w.currency, "CHF", w.date)
            if not rate:
                continue
            expected = abs(w.amount) * rate
            if abs(abs(b.amount) - expected) / expected <= CROSS_TOLERANCE:
                edges.append((b, w))
    per_bank, per_wise = Counter(b.ref for b, _ in edges), Counter(w.ref for _, w in edges)
    return [(b, w) for b, w in edges if per_bank[b.ref] == 1 and per_wise[w.ref] == 1]


def _rewrite_priced(block: list[str], other_ref: str, chf: Decimal) -> list[str]:
    """The foreign-currency leg: its asset posting carries the CHF it cost (`@@`), and the clearing
    posting is in CHF, so it nets against the bank's side exactly."""
    out = [re.sub(r"\s+#review\b", "", block[0])]
    credit = None
    for line in block[1:]:
        m = re.match(r"^(\s+Assets:Wise:\S+\s+)(-?[\d.]+) (\S+)$", line)
        if m:
            credit = Decimal(m.group(2)) > 0
            line = f"{m.group(1)}{m.group(2)} {m.group(3)} @@ {chf} CHF"
        out.append(line)
    out = [
        re.sub(
            rf"^(\s+{re.escape(CLEARING)}\s+)-?[\d.]+ \S+$",
            rf"\g<1>{-chf if credit else chf} CHF",
            ln,
        )
        for ln in out
    ]
    spot = next(i for i, ln in enumerate(out) if labels._POSTING.match(ln))
    out.insert(spot, f'  transfer: "{other_ref}"')
    return out


def _rewrite(block: list[str], other_ref: str) -> list[str]:
    out = [re.sub(r"\s+#review\b", "", block[0])]
    for line in block[1:]:
        if re.match(r"^\s+(Expenses:Uncategorized|Income:Uncategorized)\s", line):
            line = re.sub(r"(Expenses|Income):Uncategorized", CLEARING, line, count=1)
        out.append(line)
    spot = next(i for i, line in enumerate(out) if labels._POSTING.match(line))
    out.insert(spot, f'  transfer: "{other_ref}"')
    return out


def match(root: Path, dry_run: bool = False) -> dict[str, int]:
    """Find and link transfer pairs. Returns counts: pairs, ambiguous, card_payments_unmatched."""
    with gitops.write_lock(root):
        led = ledger.load(config.main_ledger(root))
        legs = _legs(led)
        pairs, ambiguous = find_pairs(legs)
        paired = {leg.ref for pair in pairs for leg in pair}
        from fin.analytics.valuation import Market

        cross = find_cross_pairs(legs, Market.of(led.entries, "CHF"), paired)
        paired |= {leg.ref for pair in cross for leg in pair}
        lone = [leg for leg in legs if _is_card_payment(leg) and leg.ref not in paired]
        result = {
            "pairs": len(pairs) + len(cross),
            "ambiguous": len(ambiguous) // 2,
            "card_payments_unmatched": len(lone),
        }
        if dry_run or not (pairs or cross):
            return result
        other = {a.ref: b.ref for a, b in [*pairs, *cross]} | {
            b.ref: a.ref for a, b in [*pairs, *cross]
        }
        priced = {w.ref: abs(b.amount) for b, w in cross}  # the Wise leg's CHF price
        by_ref = {leg.ref: leg for pair in [*pairs, *cross] for leg in pair}
        by_file: dict[str, list[tuple[int, str]]] = {}
        for ref, leg in by_ref.items():
            by_file.setdefault(leg.file, []).append((leg.line - 1, ref))
        before = {p: Path(p).read_text() for p in by_file}
        try:
            for path, items in by_file.items():
                lines = before[path].split("\n")
                for start, ref in sorted(items, reverse=True):
                    end = labels._block_end(lines, start)
                    block = lines[start:end]
                    lines[start:end] = (
                        _rewrite_priced(block, other[ref], priced[ref])
                        if ref in priced
                        else _rewrite(block, other[ref])
                    )
                Path(path).write_text("\n".join(lines))
            errors = ledger.load(config.main_ledger(root)).errors
            if errors:
                raise ValueError(
                    "ledger would have errors: " + "; ".join(ledger.format_errors(errors)[:3])
                )
        except BaseException:
            for path, text in before.items():
                Path(path).write_text(text)
            raise
        gitops.commit_all(root, f"transfers: matched {len(pairs) + len(cross)} pair(s)")
    return result


# Shares moved between brokers (a plan broker's vested lots to IBKR) keep their cost basis: the
# sending side books each lot's cost to the clearing account, the receiving side books the lots
# it got. `share_moves` checks lot by lot (security and acquisition date) that both agree.
SENDERS = ("Assets:Schwab:", "Assets:ETrade:")
RECEIVERS = ("Assets:IBKR:",)
IN_FLIGHT = 14  # days a share transfer may take


def _moved_lots(led, prefixes: tuple[str, ...], sign: int) -> dict[tuple[str, date], list]:
    """(commodity, lot date) -> [shares, cost, last day moved], for lots moved through clearing."""
    out: dict[tuple[str, date], list] = defaultdict(lambda: [Decimal(0), Decimal(0), None])
    for e in led.entries:
        if not isinstance(e, data.Transaction) or not any(
            p.account == CLEARING for p in e.postings
        ):
            continue
        for p in e.postings:
            if p.cost is not None and p.account.startswith(prefixes) and p.units.number * sign > 0:
                row = out[(p.units.currency, p.cost.date)]
                row[0] += abs(p.units.number)
                row[1] += abs(p.units.number) * p.cost.number
                row[2] = max(row[2] or e.date, e.date)
    return out


def share_moves(led, today: date | None = None) -> dict[str, list[str]]:
    """Problems with shares moved between brokers, by kind (each a 'SYMBOL lot of DATE' line):
    `mismatch` (shares or cost differ), `not_arrived` (left over IN_FLIGHT days ago, nothing came),
    `no_sender` (arrived, but no broker fin imports sent it), `in_flight` (left recently)."""
    today = today or ledger.as_of(led)
    sent, got = _moved_lots(led, SENDERS, -1), _moved_lots(led, RECEIVERS, +1)
    out: dict[str, list[str]] = {
        "mismatch": [],
        "not_arrived": [],
        "no_sender": [],
        "in_flight": [],
    }
    for key in sorted(set(sent) | set(got)):
        what = f"{key[0]} lot of {key[1]}"
        s, g = sent.get(key), got.get(key)
        if s and g:
            if s[0] != g[0] or abs(s[1] - g[1]) >= Decimal("0.01"):
                out["mismatch"].append(what)
        elif s:
            late = (today - s[2]).days > IN_FLIGHT
            out["not_arrived" if late else "in_flight"].append(what)
        else:
            out["no_sender"].append(what)
    return out


def share_move_report(root: Path) -> list[str]:
    """One line per kind of problem, for the importers' after-import summary."""
    led = ledger.load(config.main_ledger(root))
    found = share_moves(led)
    labels_ = {
        "mismatch": "share transfers whose shares or cost basis differ between the brokers",
        "not_arrived": "lots sent over 2 weeks ago that never arrived",
        "no_sender": "lots that arrived from a broker fin does not import (yet)",
        "in_flight": "lots on their way",
    }
    lines = []
    for kind, items in found.items():
        if items:
            shown = ", ".join(items[:5]) + (f" and {len(items) - 5} more" if len(items) > 5 else "")
            lines.append(f"{len(items)} {labels_[kind]}: {shown}")
    if not found["mismatch"] and not found["not_arrived"]:
        lines.insert(0, "share transfers between brokers: every lot's cost basis agrees")
    return lines
