"""Strip identifying strings before anything goes to a cloud model.

Merchant names pass through (the user is fine with that); amounts, IBANs, card and phone numbers,
long digit runs, the names listed in privacy.yaml and the people you pay person-to-person do not.
"""

import dataclasses
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any, Literal

import yaml
from beancount.core import data

CCY = r"(?:CHF|USD|EUR|GBP|Fr\.?|SFr\.?|\$|€|£)"
NUM = r"\d[\d'’,.\s]*\d|\d"
PATTERNS = [
    (re.compile(rf"{CCY}\s*[-+]?\s*(?:{NUM})", re.IGNORECASE), "[amount]"),
    (re.compile(rf"[-+]?(?:{NUM})\s*{CCY}\b", re.IGNORECASE), "[amount]"),
    (re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b"), "[iban]"),
    (re.compile(r"\+\d[\d ()/.-]{7,}\d"), "[#]"),  # international phone numbers
    (re.compile(r"\b0\d{2}[ /.-]\d{3}[ .-]\d{2}[ .-]\d{2}\b"), "[#]"),  # Swiss phone numbers
]
# Digit runs of 4+ could be amounts ("Rent 2400") or references; only plausible years survive.
LONG_DIGITS = re.compile(r"\d{4,}")

Level = Literal["blind", "relative", "full"]

# A UBS TWINT transfer to or from a phone number is a payment to a person, not a shop.
PERSON_HINT = re.compile(r"TWINT-Acc\.?:\s*\+?\d")


def is_person(entry: data.Transaction) -> bool:
    """Whether the payee is a private person (an importer says so, or the bank text shows it)."""
    return (entry.meta or {}).get("counterparty") == "person" or bool(
        PERSON_HINT.search(entry.narration or "")
    )


def ledger_people(entries) -> list[str]:
    """Payees of person-to-person payments: redacted like the people in privacy.yaml."""
    return sorted(
        {e.payee for e in entries if isinstance(e, data.Transaction) and e.payee and is_person(e)}
    )


def _words(words: list[str]) -> re.Pattern | None:
    words = sorted({w.strip() for w in words if w and w.strip()}, key=len, reverse=True)
    if not words:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, words)) + r")(?!\w)", re.IGNORECASE)


@dataclass
class Policy:
    level: Level = "blind"
    names: list[str] = field(default_factory=list)  # replaced by [name]
    people: list[str] = field(default_factory=list)  # replaced by [person]

    @cached_property
    def _replacements(self) -> list[tuple[re.Pattern, str]]:
        pairs = ((_words(self.names), "[name]"), (_words(self.people), "[person]"))
        return [(p, repl) for p, repl in pairs if p is not None]

    def with_ledger(self, entries) -> "Policy":
        """This policy plus everyone the ledger shows you paying person-to-person."""
        return dataclasses.replace(self, people=[*self.people, *ledger_people(entries)])

    def redact(self, text: str) -> str:
        # names first: a payee recorded with digits in it must still match as a whole
        for pattern, repl in [*self._replacements, *PATTERNS]:
            text = pattern.sub(repl, text)
        return LONG_DIGITS.sub(
            lambda m: m.group() if re.fullmatch(r"(?:19|20)\d\d", m.group()) else "[#]", text
        )

    def redact_obj(self, obj: Any) -> Any:
        """Redact every string in a JSON-like structure (keys are left alone)."""
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {k: self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, list | tuple):
            return [self.redact_obj(v) for v in obj]
        return obj

    @property
    def sees_values(self) -> bool:
        return self.level == "full"

    @property
    def sees_ratios(self) -> bool:
        return self.level in ("relative", "full")


def load_policy(root: Path) -> Policy:
    path = root / "privacy.yaml"
    raw = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    return Policy(
        level=raw.get("level", "blind"),
        names=list(raw.get("redact_names") or []),
        people=list(raw.get("people") or []),
    )


_FIGURE = re.compile(r"(?<![\w:-])\d[\d'’,.]*(?![\w-])")


def strip_figures(text: str) -> str:
    """Model prose must not carry numbers: it never saw any, so any it writes are guesses.
    Years, small counts (<= 31) and labels like 2026-03 or Q1 are kept; other figures become [?]."""

    def keep(m: re.Match) -> str:
        tok = m.group().rstrip(".,")
        if re.fullmatch(r"(?:19|20)\d\d", tok) or (tok.isdigit() and int(tok) <= 31):
            return m.group()
        return "[?]"

    return _FIGURE.sub(keep, text)
