"""The review queue: transactions that still need a person to decide, one row per merchant.

A transaction needs review when it carries #review (a tentative automatic guess) or sits on
Expenses:Uncategorized. Deciding is per merchant: confirming writes a payee rule (optionally only
for the merchant type, for shops like Digitec that sell everything), so the next import knows.
"""

from collections import Counter
from decimal import Decimal
from pathlib import Path

from beancount.core import data

from fin import config, labels, ledger
from fin.analytics.recurring import normalize_payee


def _pending(led, payee_key: str | None = None):
    for e in led.entries:
        if not isinstance(e, data.Transaction) or not (e.meta or {}).get("ref"):
            continue
        # the expense posting, or the income posting of money that came in
        spent = [p for p in e.postings if p.account.startswith(("Expenses:", "Income:"))]
        if len(spent) != 1:
            continue
        unsorted = spent[0].account in (labels.UNCATEGORIZED, labels.INCOME_UNCATEGORIZED)
        if "review" not in (e.tags or ()) and not unsorted:
            continue
        key = normalize_payee(e.payee or "")
        if key and (payee_key is None or key == payee_key):
            yield e, spent[0], key


def queue(led) -> list[dict]:
    """Merchants with something to decide, biggest first."""
    groups: dict[str, dict] = {}
    for e, spent, key in _pending(led):
        g = groups.setdefault(
            key,
            {
                "payee_key": key,
                "payee": e.payee,
                "refs": [],
                "payments": [],
                "categories": Counter(),
                "sources": Counter(),
                "merchant_types": Counter(),
                "total": Decimal(0),
                "currency": spent.units.currency,
            },
        )
        g["refs"].append(e.meta["ref"])
        g["payments"].append([e.date.isoformat(), str(spent.units.number), e.meta["ref"]])
        g["categories"][spent.account] += 1
        g["sources"][e.meta.get("categorized") or "unknown"] += 1
        if mt := e.meta.get("merchant_category"):
            g["merchant_types"][mt] += 1
        g["total"] += spent.units.number
    rows = []
    for g in groups.values():
        cat = g["categories"].most_common(1)[0][0]
        rows.append(
            {
                "payee_key": g["payee_key"],
                "payee": g["payee"],
                "count": len(g["refs"]),
                "refs": g["refs"],
                "payments": sorted(
                    g["payments"]
                ),  # [date, amount, ref], so a page can total any period and pick payments
                "category": cat,
                "needs_category": cat in (labels.UNCATEGORIZED, labels.INCOME_UNCATEGORIZED),
                "source": g["sources"].most_common(1)[0][0],
                "merchant_types": [m for m, _ in g["merchant_types"].most_common()],
                "total": str(g["total"]),
                "currency": g["currency"],
            }
        )
    return sorted(rows, key=lambda r: (not r["needs_category"], -abs(Decimal(r["total"]))))


def confirm(
    root: Path,
    payee_key: str,
    category: str | None = None,
    merchant_type: str | None = None,
    per_type: bool = False,
    remember: bool = True,
    refs: list[str] | None = None,
) -> dict:
    """Settle a merchant: set (or keep) its category on every pending transaction, drop #review and
    the automatic provenance, and (unless `remember` is off) remember the choice as a payee rule.
    With `refs`, only those payments are settled; the others stay pending."""
    led = ledger.load(config.main_ledger(root))
    rows = [
        (e, s)
        for e, s, _ in _pending(led, payee_key)
        if (not per_type or e.meta.get("merchant_category") == merchant_type)
        and (refs is None or e.meta["ref"] in refs)
    ]
    if per_type and not merchant_type:
        raise ValueError("choose which merchant type the rule is for")
    if not rows:
        raise KeyError(payee_key)
    current = Counter(s.account for _, s in rows).most_common(1)[0][0]
    category = category or current
    if category in (labels.UNCATEGORIZED, labels.INCOME_UNCATEGORIZED):
        raise ValueError("choose a category")
    edits = {e.meta["ref"]: {"category": category, "tags_remove": ["review"]} for e, _ in rows}
    changed = labels.apply(root, edits, f"review: {payee_key} -> {category}")
    if not remember:
        return {"changed": changed, "rule": None}
    labels.record_rule(
        root, payee_key, {"category": category}, merchant_type=merchant_type if per_type else None
    )
    return {
        "changed": changed,
        "rule": labels.rule_key(payee_key, merchant_type if per_type else None),
    }


def details(root: Path, payee_key: str) -> list[dict]:
    """Everything known about each pending payment of a merchant: what the ledger holds, plus the
    full record the source sent (read from the archived raw files, so nothing is trimmed)."""
    from fin import cards, importers

    led = ledger.load(config.main_ledger(root))
    names = cards.load(root)
    out = []
    for e, spent, _ in _pending(led, payee_key):
        ref = e.meta["ref"]
        source, _, native = ref.partition(":")
        record = None
        try:
            lookup = getattr(importers.get(source), "raw_record", None)
            record = lookup(root, native) if lookup else None
        except KeyError:
            pass  # a source with no importer, e.g. a manual entry
        out.append(
            {
                "ref": ref,
                "date": e.date.isoformat(),
                "payee": e.payee,
                "narration": e.narration,
                "amount": str(spent.units.number),
                "currency": spent.units.currency,
                "category": spent.account,
                "tags": sorted(e.tags or ()),
                "card": e.meta.get("card"),
                "card_label": cards.label(names, ref.partition(":")[0], e.meta.get("card")),
                "meta": {  # text only: Beancount's own bookkeeping (like __tolerances__) is not shown
                    k: str(v)
                    for k, v in (e.meta or {}).items()
                    if k not in ("filename", "lineno", "ref") and not k.startswith("__")
                },
                "record": record,
            }
        )
    return sorted(out, key=lambda t: t["date"])
