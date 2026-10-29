"""Detect recurring payments (rent, insurance, subscriptions) from expense postings."""

import calendar
import itertools
import re
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd

PERIODS = {7: "weekly", 14: "biweekly", 30: "monthly", 91: "quarterly", 365: "yearly"}
PERIOD_MONTHS = {"weekly": 1, "biweekly": 1, "monthly": 1, "quarterly": 3, "yearly": 12}
TOLERANCE = Decimal("0.2")
MAX_CV = 0.15
PRICE_CHANGE = Decimal("0.05")


# an invoice or reference code: letters and at least two digits run together ("Spotify P32B87DC9F")
_CODE = re.compile(
    r"(?<![A-Za-z0-9])(?=(?:[A-Za-z]*\d){2})(?=\d*[A-Za-z])[A-Za-z0-9]{6,}(?![A-Za-z0-9])"
)


def _letters(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-zA-Z ]+", " ", text)).strip().lower()


def normalize_payee(payee: str) -> str:
    """Payee key for rules and series: letters only, reference codes dropped (unless that would
    leave nothing), so one merchant gets one key however its bill is numbered."""
    return _letters(_CODE.sub(" ", payee)) or _letters(payee)


@dataclass
class Series:
    id: str
    payee: str
    account: str
    period: str
    period_days: int
    typical_amount: Decimal
    first_date: date
    last_date: date
    next_expected: date
    status: str  # active | lapsed | proposed
    confirmed: bool = False
    occurrences: int = 0
    price_changes: list[tuple[date, Decimal, Decimal]] = field(default_factory=list)

    @property
    def applies(self) -> bool:
        """Whether postings in this series are treated as recurring."""
        return self.status != "proposed"


def load_state(led) -> tuple[set[str], set[str]]:
    """(confirmed, rejected) series ids, from `custom "fin-recurring"` directives in the ledger."""
    from fin import ledger_settings

    s = ledger_settings.read(led)
    return s.confirmed, s.rejected


def _cv(values: list[Decimal]) -> float:
    floats = [float(v) for v in values]
    mean = statistics.fmean(floats)
    return statistics.stdev(floats) / mean if len(floats) > 1 and mean else 0.0


def _period_for(dates: list[date]) -> int | None:
    intervals = [(b - a).days for a, b in itertools.pairwise(dates)]
    best, best_frac = None, 0.0
    for p in PERIODS:
        lo, hi = p * float(1 - TOLERANCE), p * float(1 + TOLERANCE)
        frac = sum(lo <= i <= hi for i in intervals) / len(intervals)
        if frac > best_frac:
            best, best_frac = p, frac
    return best if best_frac >= 0.8 else None


def _next_due(last: date, name: str, days: int) -> date:
    """Monthly, quarterly and yearly bills fall due on the same day of a later month."""
    months = {"monthly": 1, "quarterly": 3, "yearly": 12}.get(name)
    if months is None:
        return last + timedelta(days=days)
    idx = last.year * 12 + last.month - 1 + months
    y, m = idx // 12, idx % 12 + 1
    return date(y, m, min(last.day, calendar.monthrange(y, m)[1]))


def _tight(window: list[Decimal]) -> bool:
    mean = sum(window) / len(window)
    return all(abs(a - mean) / mean <= PRICE_CHANGE for a in window)


def _level_shifts(dates: list[date], amounts: list[Decimal]) -> list[tuple[date, Decimal, Decimal]]:
    """Price changes: two steady payments before and two steady payments after, at different
    levels. Variable bills (utilities) never settle into a level, so they raise no alerts."""
    out = []
    for i in range(2, len(amounts) - 1):
        before, after = amounts[i - 2 : i], amounts[i : i + 2]
        if not (_tight(before) and _tight(after)):
            continue
        b, a = sum(before) / 2, sum(after) / 2
        if abs(a - b) / b > PRICE_CHANGE:
            out.append((dates[i], amounts[i - 1], amounts[i]))
    return out


def detect(
    df: pd.DataFrame,
    as_of: date,
    confirmed: set[str] = frozenset(),
    rejected: set[str] = frozenset(),
    exclude_prefixes: tuple[str, ...] = (),
) -> list[Series]:
    """Group by (payee, account); a group is a series when intervals and amounts are regular."""
    out = []
    df = df[df["payee"] != ""]
    df = df[~df["account"].str.startswith(exclude_prefixes)] if exclude_prefixes else df
    df = df[df["amount"] > 0].sort_values("date", kind="stable")  # once, not for every group
    df = df.assign(norm=df["payee"].map(normalize_payee))
    for (norm, account), g in df.groupby(["norm", "account"]):
        sid = f"{norm}|{account}"
        if sid in rejected or len(g) < 2:
            continue
        dates, amounts = list(g["date"]), list(g["amount"])
        period = _period_for(dates)
        if period is None or _cv(amounts) > MAX_CV:
            continue
        name = PERIODS[period]
        if len(g) < 3 and name != "yearly":
            continue
        is_confirmed = sid in confirmed
        status = "active" if len(g) >= 3 or is_confirmed else "proposed"
        last = dates[-1]
        if status != "proposed" and (as_of - last).days > 1.5 * period:
            status = "lapsed"
        changes = _level_shifts(dates, amounts)
        out.append(
            Series(
                id=sid,
                payee=g["payee"].iloc[-1],
                account=account,
                period=name,
                period_days=period,
                typical_amount=statistics.median(amounts[-3:]),
                first_date=dates[0],
                last_date=last,
                next_expected=_next_due(last, name, period),
                status=status,
                confirmed=is_confirmed,
                occurrences=len(g),
                price_changes=changes,
            )
        )
    return out


def alerts(series: list[Series], as_of: date, window_days: int = 90) -> list[dict]:
    """Things worth a look: new proposals, recent price changes, series that stopped."""
    out = []
    for s in series:
        if s.status == "proposed":
            out.append(
                {
                    "kind": "new",
                    "series": s.id,
                    "payee": s.payee,
                    "detail": f"{s.period} payment of {s.typical_amount}, confirm to track",
                }
            )
        for d, old, new in s.price_changes:
            if (as_of - d).days <= window_days:
                out.append(
                    {
                        "kind": "price_change",
                        "series": s.id,
                        "payee": s.payee,
                        "date": d,
                        "old": old,
                        "new": new,
                    }
                )
        if s.status == "lapsed" and (as_of - s.last_date).days <= 365:
            out.append(
                {
                    "kind": "lapsed",
                    "series": s.id,
                    "payee": s.payee,
                    "last_date": s.last_date,
                    "days_overdue": (as_of - s.next_expected).days,
                }
            )
    return out
