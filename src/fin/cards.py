"""Which card a payment was made on, and whose card that is.

The source tells us a card only by an opaque id, so each transaction records a short form of it
(`card:` metadata) and `rules/cards.yaml` in the data repo says what to call it:

    swisscard:
      AA12BC34: "Patrick (Visa)"
      53FE90D1: "Anna (Visa)"

Naming a card relabels every payment at once; the ledger itself never holds the names.
"""

from collections import Counter
from pathlib import Path

import yaml

from fin import gitops

FILE = "rules/cards.yaml"


def load(root: Path) -> dict[str, dict[str, str]]:
    path = root / FILE
    return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}


def label(names: dict, source: str, card: str | None) -> str | None:
    return (names.get(source) or {}).get(card) if card else None


def set_label(root: Path, source: str, card: str, name: str | None) -> None:
    """Name a card (or, with no name, forget its name) and commit."""
    names = load(root)
    cards = dict(names.get(source) or {})
    if name and name.strip():
        if any(c in name for c in '"\n'):
            raise ValueError("a card name cannot contain quotes or line breaks")
        cards[card] = name.strip()
    else:
        cards.pop(card, None)
    names[source] = cards
    path = root / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(names, sort_keys=True, allow_unicode=True))
    gitops.commit_all(root, f"card: name {source} {card}")


def summary(led, root: Path) -> list[dict]:
    """Every card seen in the ledger, with its name (if any), payment count and date range."""
    from beancount.core import data

    names = load(root)
    seen: dict[tuple, dict] = {}
    for e in led.entries:
        if isinstance(e, data.Transaction) and (e.meta or {}).get("card"):
            source = (e.meta.get("ref") or "").partition(":")[0]
            c = seen.setdefault(
                (source, e.meta["card"]), {"count": 0, "first": e.date, "last": e.date}
            )
            c["count"] += 1
            c["first"], c["last"] = min(c["first"], e.date), max(c["last"], e.date)
    rows = [
        {
            "source": s,
            "card": card,
            "label": label(names, s, card),
            "count": v["count"],
            "first": v["first"].isoformat(),
            "last": v["last"].isoformat(),
        }
        for (s, card), v in seen.items()
    ]
    return sorted(rows, key=lambda r: (r["source"], -r["count"]))


def counts(rows) -> Counter:
    return Counter(r["card"] for r in rows)
