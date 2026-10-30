"""Every transaction as one row, with its label and flags, for reviewing what the charts count."""

import functools
from decimal import Decimal
from pathlib import Path

import pandas as pd
from beancount.core import convert, data

from fin import cards, registry
from fin.analytics.spend import SpendModel
from fin.analytics.valuation import Market
from fin.ledger import Ledger

CASH = ("CHF", "USD", "EUR")
UNCATEGORIZED = ("Expenses:Uncategorized", "Income:Uncategorized")
CENT = Decimal("0.01")

COLUMNS = [
    "date", "ref", "payee", "narration", "tags", "kind", "category", "account", "amount",
    "orig_amount", "orig_ccy", "cadence", "necessity", "recurring", "series_id", "major",
    "card", "card_label", "categorized", "merchant_type", "needs_review", "source", "file", "line", "postings",
]  # fmt: skip


def _detail(p) -> dict:
    return {
        "account": p.account,
        "amount": str(p.units.number),
        "ccy": p.units.currency,
        "cost": f"{p.cost.number} {p.cost.currency}" if p.cost else None,
        "price": f"{p.price.number} {p.price.currency}" if p.price else None,
    }


EARNED = ("Income:Employer:", "Income:Investments:Dividends")  # income even when shares move
FEES = "Expenses:Fees"


def _is_security(p) -> bool:
    return p.cost is not None or p.units.currency not in CASH


def _biggest(posts: list, value: dict):
    return max(posts, key=lambda p: abs(value[id(p)]))


def _classify(posts: list, value: dict, reg):
    """(kind, primary posting or None, amount in base currency) for one transaction.

    income      salary, bonus, share compensation, dividends (anything earned)
    asset       a home or vehicle bought or sold (valued by your own estimates afterwards)
    investment  shares bought or sold, or a lot opened; the fee on it does not make it an expense
    expense     spending, taxes, fees
    transfer    between your own accounts, including a currency conversion that only costs a fee
    opening     opening balances
    """
    income = [p for p in posts if p.account.startswith("Income:")]
    expense = [p for p in posts if p.account.startswith("Expenses:")]
    sheet = [p for p in posts if p.account.startswith(("Assets:", "Liabilities:"))]
    securities = [p for p in posts if _is_security(p) and p.account.startswith("Assets:")]
    fixed = [p for p in posts if reg.is_role(p.account, "property", "vehicle")]
    if fixed:
        return "asset", None, sum(abs(value[id(p)]) for p in fixed)
    if any(p.account.startswith(EARNED) for p in income):
        return "income", _biggest(income, value), -sum(value[id(p)] for p in income)
    if securities:
        return "investment", None, sum(abs(value[id(p)]) for p in securities)
    if income:
        return "income", _biggest(income, value), -sum(value[id(p)] for p in income)
    only_fees = bool(expense) and all(p.account.startswith(FEES) for p in expense)
    if expense and not (only_fees and len({p.account for p in sheet}) >= 2):
        return "expense", _biggest(expense, value), -sum(value[id(p)] for p in expense)
    positive = sum((v for v in value.values() if v > 0), Decimal(0))
    if any(p.account.startswith("Equity:Opening") for p in posts):
        return "opening", None, positive
    # what moved: the credits to your accounts, or, when the money left for somewhere that is not one of
    # your accounts (an account closed and not imported), what left
    moved = sum((value[id(p)] for p in sheet if value[id(p)] > 0), Decimal(0))
    return (
        "transfer",
        None,
        moved or sum((-value[id(p)] for p in sheet if value[id(p)] < 0), Decimal(0)),
    )


def transactions(led: Ledger, model: SpendModel, base: str = "CHF") -> pd.DataFrame:
    """One row per transaction. `amount` is in the base currency: negative for expenses,
    positive for income; for transfers and investments the amount that moved."""
    market = Market.of(led.entries, base)
    reg = registry.of(led)
    classified = {(r.ref, r.account): r for r in model.postings.itertuples() if r.ref is not None}
    root = led.path.parent.parent  # the data repo: paths read ledger/imported/...
    card_names = cards.load(root)
    rows = []
    for e in led.entries:
        if not isinstance(e, data.Transaction):
            continue
        posts = [p for p in e.postings if p.units.number is not None]
        value = {id(p): market.convert(convert.get_weight(p), e.date) for p in posts}
        kind, primary, amount = _classify(posts, value, reg)
        sheet = [p for p in posts if p.account.startswith(("Assets:", "Liabilities:"))]
        if primary is not None:
            category = primary.account
            account = _biggest(sheet, value).account if sheet else ""
        elif kind in ("investment", "asset"):
            category = ", ".join(sorted({p.units.currency for p in posts if _is_security(p)}))
            account = _biggest(sheet, value).account if sheet else ""
        else:
            category = ""
            out = [p for p in sheet if value[id(p)] < 0]
            into = [p for p in sheet if value[id(p)] > 0]
            both = bool(out and into)
            account = (
                f"{_biggest(out, value).account} → {_biggest(into, value).account}"
                if both
                else (_biggest(sheet, value).account if sheet else "")
            )
        meta = e.meta or {}
        ref = meta.get("ref")
        cls = classified.get((ref, primary.account)) if kind == "expense" else None
        tags = sorted(e.tags or ())
        rows.append(
            {
                "date": e.date,
                "ref": ref,
                "payee": e.payee or "",
                "narration": e.narration or "",
                "tags": tags,
                "kind": kind,
                "category": category,
                "account": account,
                "amount": amount.quantize(CENT),
                "orig_amount": str(primary.units.number) if primary is not None else None,
                "orig_ccy": primary.units.currency if primary is not None else None,
                "cadence": cls.cadence if cls else None,
                "necessity": cls.necessity if cls else None,
                "recurring": bool(cls and pd.notna(cls.series_id)),
                "series_id": cls.series_id if cls and pd.notna(cls.series_id) else None,
                "major": bool(cls.major) if cls else False,
                "card": meta.get("card"),
                "card_label": cards.label(card_names, ref.split(":")[0], meta.get("card"))
                if ref
                else None,
                "categorized": meta.get("categorized"),
                "merchant_type": meta.get("merchant_category"),
                "needs_review": category.startswith(UNCATEGORIZED) or "review" in tags,
                "source": ref.split(":")[0] if ref else "ledger",
                "file": _relative(meta.get("filename"), root),
                "line": meta.get("lineno"),
                "postings": [_detail(p) for p in posts],
            }
        )
    return pd.DataFrame(rows, columns=COLUMNS)


@functools.lru_cache(maxsize=8192)
def _relative_to(filename: str, root: str) -> str:
    try:
        return str(Path(filename).relative_to(root))
    except ValueError:
        return filename


def _relative(filename: str | None, root: Path) -> str | None:
    """The file a transaction was read from, relative to the data repo. A file holds many
    transactions, so the answer is remembered."""
    return _relative_to(filename, str(root)) if filename else None
