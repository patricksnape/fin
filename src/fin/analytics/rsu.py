"""Share awards: what employer stock cost, what it did, and what it would have done otherwise.

An *event* is a transaction that creates employer-stock lots, found by its shape rather than by a
ticker, so a second stock or program appears as soon as an importer books it the same way:
 * an RSU vest: a stock posting at cost plus a posting to `Income:Employer:RSU*` (and, when shares
   were withheld for tax, `Expenses:Taxes:RSU-Withholding`); the transaction's meta `award_date` is
   when the award was granted;
 * an ESPP purchase: a stock posting at its market price plus `Income:Employer:ESPP*` for the
   discount; meta `offering_start` is the start of the offering period.

Shares are followed by their cost key `(date, unit cost)`, which survives a move between brokers
(the lot label does not). All figures are in the cost currency (USD) and in the base currency (CHF),
the CHF ones at the rate on each event's own date, with the exchange-rate part kept apart from the
price part. A price on an award date comes from a daily history in the data repo
(sources/market-history/<SYMBOL>.csv) or the ledger's own prices, and is never guessed: without
one the award shows no growth.
"""

from __future__ import annotations

import csv
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from beancount.core import data
from beancount.core import prices as bprices
from beancount.core.amount import Amount

from fin.analytics.valuation import Market, balances_at

CENT = Decimal("0.01")
PCT = Decimal("0.0001")
HISTORY_DIR = Path("sources") / "market-history"
PROGRAMS = (("Income:Employer:RSU", "rsu"), ("Income:Employer:ESPP", "espp"))
WITHHELD = "Expenses:Taxes:RSU-Withholding"
FEES = "Expenses:Fees:Broker"
EXACT_WITHIN = timedelta(days=7)  # a quote this close to a date stands for it


def _q(v: Decimal | None) -> Decimal | None:
    return None if v is None else v.quantize(CENT)


def _ratio(num: Decimal, den: Decimal) -> Decimal | None:
    return (num / den).quantize(PCT) if den else None


@dataclass(frozen=True)
class Event:
    commodity: str
    program: str  # rsu | espp
    ref: str
    award: str
    name: str | None
    start: date | None  # the award date, or the start of an ESPP offering
    day: date  # the vest or purchase
    gross: Decimal  # shares vested or bought
    withheld: Decimal  # shares kept back for tax (RSU)
    net: Decimal  # shares that arrived
    fmv: Decimal  # market price per share on `day`, in the cost currency
    paid: Decimal  # price paid per share (0 for an RSU)
    ccy: str

    @property
    def key(self) -> tuple[date, Decimal]:
        return (self.day, self.fmv)


@dataclass(frozen=True)
class Sale:
    commodity: str
    day: date
    units: Decimal
    price: Decimal
    fee: Decimal  # this sale line's share of the transaction's broker fee
    key: tuple[date, Decimal]


def _meta_day(e, key: str) -> date | None:
    v = (e.meta or {}).get(key)
    try:
        return date.fromisoformat(str(v)) if v else None
    except ValueError:
        return None


def events(entries: list) -> list[Event]:
    out = []
    for e in entries:
        if not isinstance(e, data.Transaction):
            continue
        prog = next(
            (
                (p, name)
                for p in e.postings
                for prefix, name in PROGRAMS
                if p.account.startswith(prefix)
            ),
            None,
        )
        stock = [
            p for p in e.postings if p.cost and p.cost.number is not None and p.units.number > 0
        ]
        if not prog or len(stock) != 1:
            continue
        income, program = prog
        s = stock[0]
        fmv, ccy, net = s.cost.number, s.cost.currency, s.units.number
        if program == "rsu":
            held_back = sum(
                (p.units.number for p in e.postings if p.account == WITHHELD), Decimal(0)
            )
            withheld = held_back / fmv if held_back else Decimal(0)
            gross, paid, start = net + withheld, Decimal(0), _meta_day(e, "award_date")
        else:
            discount = abs(income.units.number) / net if net else Decimal(0)
            withheld, gross, paid = Decimal(0), net, fmv - discount
            start = _meta_day(e, "offering_start")
        out.append(
            Event(
                s.units.currency, program, e.meta.get("ref", ""), str((e.meta or {}).get("award", "")),
                (e.meta or {}).get("award_name"), start, e.date, gross.quantize(Decimal("0.0001")),
                withheld.quantize(Decimal("0.0001")), net, fmv, paid, ccy,
            )
        )  # fmt: skip
    return sorted(out, key=lambda v: (v.commodity, v.day, v.ref))


def sales(entries: list, commodities: set[str]) -> list[Sale]:
    out = []
    for e in entries:
        if not isinstance(e, data.Transaction):
            continue
        lines = [
            p
            for p in e.postings
            if p.units.currency in commodities
            and p.units.number < 0
            and p.price is not None
            and p.cost
            and p.cost.number is not None
        ]
        if not lines:
            continue
        fee = sum((p.units.number for p in e.postings if p.account == FEES), Decimal(0))
        total = sum((-p.units.number for p in lines), Decimal(0))
        for p in lines:
            out.append(
                Sale(
                    p.units.currency, e.date, -p.units.number, p.price.number,
                    fee * (-p.units.number) / total, (p.cost.date, p.cost.number),
                )
            )  # fmt: skip
    return out


class Prices:
    """What a share cost on a day: the history file, then the ledger's quotes, then (only where an
    approximate figure is fine) the market price at the latest vest or purchase."""

    def __init__(self, entries: list, root: Path | None, evs: list[Event], market: Market):
        self.market, self.root = market, root
        self.history: dict[str, tuple[list[date], list[Decimal]]] = {}
        self.points: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)
        for v in evs:
            self.points[v.commodity].append((v.day, v.fmv))
        for sym in self.points:
            self.points[sym].sort()
            self.history[sym] = self._read(sym)

    def _read(self, sym: str) -> tuple[list[date], list[Decimal]]:
        path = self.root / HISTORY_DIR / f"{sym}.csv" if self.root else None
        if not path or not path.exists():
            return [], []
        rows = []
        with path.open(newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                try:
                    rows.append((date.fromisoformat(r["Date"][:10]), Decimal(r["Close"])))
                except (KeyError, ValueError, ArithmeticError):
                    continue
        rows.sort()
        return [d for d, _ in rows], [v for _, v in rows]

    def source(self, sym: str) -> str:
        return "history" if self.history.get(sym, ([], []))[0] else "ledger"

    def exact(self, sym: str, d: date) -> Decimal | None:
        days, vals = self.history.get(sym, ([], []))
        i = bisect_right(days, d)
        if i and d - days[i - 1] <= EXACT_WITHIN:
            return vals[i - 1]
        quoted, price = bprices.get_price(self.market.pmap, (sym, "USD"), d)
        if price is not None and quoted and d - quoted <= EXACT_WITHIN:
            return price
        return None

    def approx(self, sym: str, d: date) -> Decimal | None:
        v = self.exact(sym, d)
        if v is not None:
            return v
        pts = [p for p in self.points.get(sym, []) if p[0] <= d]
        return pts[-1][1] if pts else None

    def latest(self, sym: str, d: date) -> Decimal | None:
        """The price now: the newest quote on or before `d`, however old."""
        days, vals = self.history.get(sym, ([], []))
        i = bisect_right(days, d)
        _, price = bprices.get_price(self.market.pmap, (sym, "USD"), d)
        if price is not None:
            return price
        return vals[i - 1] if i else self.approx(sym, d)

    def fx(self, ccy: str, d: date) -> Decimal:
        if ccy == self.market.base:
            return Decimal(1)
        return self.market.convert(Amount(Decimal(1), ccy), d)


def _held(entries: list, as_of: date, commodities: set[str]) -> dict[tuple, Decimal]:
    out: dict[tuple, Decimal] = defaultdict(Decimal)
    for positions in balances_at(entries, [as_of])[as_of].values():
        for pos in positions:
            if pos.units.currency in commodities and pos.cost and pos.cost.number is not None:
                out[(pos.units.currency, pos.cost.date, pos.cost.number)] += pos.units.number
    return out


def report(entries: list, root: Path | None, as_of: date, base: str = "CHF") -> dict:
    evs = events(entries)
    if not evs:
        return {"ready": False, "reason": "No vests or share purchases in the ledger yet"}
    market = Market.of(entries, base)
    px = Prices(entries, root, evs, market)
    syms = {v.commodity for v in evs}
    sold = defaultdict(list)
    for s in sales(entries, syms):
        sold[(s.commodity, *s.key)].append(s)
    held = _held(entries, as_of, syms)
    out = []
    for sym in sorted(syms):
        for program in sorted({v.program for v in evs if v.commodity == sym}):
            mine = [v for v in evs if v.commodity == sym and v.program == program]
            out.append(_series(sym, program, mine, evs, sold, held, px, as_of, base))
    return {"ready": True, "as_of": as_of, "base": base, "series": out}


def _series(sym, program, mine, all_events, sold, held, px: Prices, as_of, base) -> dict:
    now = px.latest(sym, as_of)
    fx_now = px.fx(mine[0].ccy, as_of)
    ccy = mine[0].ccy
    # one share-weighted group per cost key: events that vested on one day at one price are alike
    acquired = defaultdict(Decimal)
    for v in all_events:
        if v.commodity == sym:
            acquired[v.key] += v.net
    rows = []
    for v in mine:
        share = v.net / acquired[v.key] if acquired[v.key] else Decimal(0)
        fx_v = px.fx(ccy, v.day)
        sold_units = proceeds = fees = Decimal(0)
        real_chf = fx_part = real_usd = proceeds_chf = Decimal(0)
        for s in sold.get((sym, *v.key), []):
            u, fx_s = s.units * share, px.fx(ccy, s.day)
            gain = u * (s.price - v.fmv) - s.fee * share
            sold_units += u
            proceeds += u * s.price
            fees += s.fee * share
            real_usd += gain
            proceeds_chf += (u * s.price - s.fee * share) * fx_s
            real_chf += (u * s.price - s.fee * share) * fx_s - u * v.fmv * fx_v
            fx_part += (u * s.price - s.fee * share) * (fx_s - fx_v)
        left = held.get((sym, *v.key), Decimal(0)) * share
        unreal_usd = unreal_chf = unreal_fx = value_now = Decimal(0)
        if now is not None and left:
            value_now = left * now
            unreal_usd = left * (now - v.fmv)
            unreal_chf = left * now * fx_now - left * v.fmv * fx_v
            unreal_fx = left * now * (fx_now - fx_v)
        start_px = px.exact(sym, v.start) if v.start else None
        grow = {}
        if start_px:
            grow = {
                "start_price": start_px,
                "growth": (v.fmv / start_px - 1).quantize(PCT),
                "gain_usd": _q(v.gross * (v.fmv - start_px)),
            }
            try:  # the rate on the award date, when the rates reach back that far
                fx_a = px.fx(ccy, v.start)
                grow["growth_chf"] = (v.fmv * fx_v / (start_px * fx_a) - 1).quantize(PCT)
                grow["gain_chf"] = _q(v.gross * (v.fmv * fx_v - start_px * fx_a))
            except ValueError:
                pass
        actual_usd = proceeds - fees + value_now
        actual_chf = proceeds_chf + left * (now or 0) * fx_now
        rows.append(
            {
                "ref": v.ref, "award": v.award, "award_name": v.name, "start": v.start, "day": v.day,
                "days_to_event": (v.day - v.start).days if v.start else None,
                "gross": v.gross, "withheld": v.withheld, "net": v.net, "price": v.fmv, "paid": v.paid,
                "discount": _ratio(v.fmv - v.paid, v.fmv) if program == "espp" else None,
                "sold": sold_units.quantize(Decimal("0.0001")), "left": left.quantize(Decimal("0.0001")),
                "cost_usd": _q(v.net * v.fmv), "cost_chf": _q(v.net * v.fmv * fx_v),
                "realised_usd": _q(real_usd), "realised_chf": _q(real_chf), "realised_fx_chf": _q(fx_part),
                "unrealised_usd": _q(unreal_usd), "unrealised_chf": _q(unreal_chf), "unrealised_fx_chf": _q(unreal_fx),
                "sell_at_event_usd": _q(v.net * v.fmv), "sell_at_event_chf": _q(v.net * v.fmv * fx_v),
                "actual_usd": _q(actual_usd), "actual_chf": _q(actual_chf),
                "hold_all_usd": _q(v.net * now) if now else None,
                "hold_all_chf": _q(v.net * now * fx_now) if now else None,
                "discount_usd": _q(v.net * (v.fmv - v.paid)),
                **grow,
            }
        )  # fmt: skip
    return {
        "commodity": sym, "program": program, "currency": ccy, "price_now": now,
        "price_history": px.source(sym), "events": rows,
        "awards": _awards(rows), "summary": _summary(rows),
    }  # fmt: skip


def _sum(rows: list[dict], key: str) -> Decimal:
    return sum((r[key] for r in rows if r.get(key) is not None), Decimal(0))


def _summary(rows: list[dict]) -> dict:
    graded = [r for r in rows if r.get("start_price")]
    value_start = sum((r["gross"] * r["start_price"] for r in graded), Decimal(0))
    value_vest = sum((r["gross"] * r["price"] for r in graded), Decimal(0))
    return {
        "events": len(rows), "gross": _sum(rows, "gross"), "withheld": _sum(rows, "withheld"),
        "net": _sum(rows, "net"), "sold": _sum(rows, "sold"), "left": _sum(rows, "left"),
        "cost_usd": _sum(rows, "cost_usd"), "cost_chf": _sum(rows, "cost_chf"),
        "realised_usd": _sum(rows, "realised_usd"), "realised_chf": _sum(rows, "realised_chf"),
        "realised_fx_chf": _sum(rows, "realised_fx_chf"),
        "unrealised_usd": _sum(rows, "unrealised_usd"), "unrealised_chf": _sum(rows, "unrealised_chf"),
        "unrealised_fx_chf": _sum(rows, "unrealised_fx_chf"),
        "sell_at_event_chf": _sum(rows, "sell_at_event_chf"), "actual_chf": _sum(rows, "actual_chf"),
        "hold_all_chf": _sum(rows, "hold_all_chf"),
        "sell_at_event_usd": _sum(rows, "sell_at_event_usd"), "actual_usd": _sum(rows, "actual_usd"),
        "hold_all_usd": _sum(rows, "hold_all_usd"),
        "growth": (value_vest / value_start - 1).quantize(PCT) if value_start else None,
        "discount_usd": _sum(rows, "discount_usd"),
        "growth_events": len(graded), "growth_gain_usd": _q(value_vest - value_start) if graded else None,
        "growth_gain_chf": _sum(graded, "gain_chf") if graded else None,
        "missing_start_price": sum(1 for r in rows if r["start"] and not r.get("start_price")),
        "missing_start_date": sum(1 for r in rows if not r["start"]),
    }  # fmt: skip


def _awards(rows: list[dict]) -> list[dict]:
    """One row per award (or ESPP offering): the price when it was granted against what its vests
    were worth, share-weighted."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["award"], r["start"])].append(r)
    out = []
    for (award, start), rs in sorted(
        groups.items(), key=lambda kv: (kv[0][1] or date.min, kv[0][0])
    ):
        graded = [r for r in rs if r.get("start_price")]
        v0 = sum((r["gross"] * r["start_price"] for r in graded), Decimal(0))
        v1 = sum((r["gross"] * r["price"] for r in graded), Decimal(0))
        out.append(
            {
                "award": award, "start": start, "name": rs[0]["award_name"], "events": len(rs),
                "first": min(r["day"] for r in rs), "last": max(r["day"] for r in rs),
                "gross": _sum(rs, "gross"), "net": _sum(rs, "net"),
                "start_price": graded[0]["start_price"] if graded else None,
                "avg_price": (v1 / sum((r["gross"] for r in graded), Decimal(0))).quantize(CENT) if graded else None,
                "growth": (v1 / v0 - 1).quantize(PCT) if v0 else None,
                "gain_usd": _q(v1 - v0) if graded else None, "gain_chf": _sum(graded, "gain_chf") if graded else None,
            }
        )  # fmt: skip
    return out


def yearly_effect(
    entries: list, root: Path | None, years: list[int], as_of: date, base: str = "CHF"
) -> dict[int, Decimal]:
    """What the market and the exchange rate did to employer shares each year, in the base currency:
    value at the year's end - value at its start - shares added (at their price on the day) +
    shares sold (at their price on the day). Zero for a year with no employer shares."""
    evs = events(entries)
    if not evs:
        return {y: Decimal(0) for y in years}
    market = Market.of(entries, base)
    px = Prices(entries, root, evs, market)
    syms = {v.commodity for v in evs}
    moves: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)  # shares in (+) and out (-)
    flows: list[tuple[date, str, Decimal]] = []  # base-currency value of shares in (+) and out (-)
    for v in evs:
        moves[v.commodity].append((v.day, v.net))
        flows.append((v.day, v.commodity, v.net * v.fmv * px.fx(v.ccy, v.day)))
    for s in sales(entries, syms):
        moves[s.commodity].append((s.day, -s.units))
        flows.append((s.day, s.commodity, -s.units * s.price * px.fx("USD", s.day)))

    def shares(sym: str, d: date) -> Decimal:
        return sum((n for day, n in moves[sym] if day <= d), Decimal(0))

    def value(d: date) -> Decimal:
        total = Decimal(0)
        for sym in syms:
            n = shares(sym, d)
            price = px.approx(sym, d) if n else None
            if n and price:
                total += n * price * px.fx("USD", d)
        return total

    out = {}
    for y in years:
        start, end = date(y - 1, 12, 31), min(date(y, 12, 31), as_of)
        added = sum((f for day, _, f in flows if start < day <= end), Decimal(0))
        out[y] = (value(end) - value(start) - added).quantize(CENT)
    return out
