"""Run specs locally. Output is a Result table; the model never receives its values."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd

from fin import ledger
from fin.agent import spec as S
from fin.analytics import allocation, cashflow, networth, rsu, spend, transactions

PCT = Decimal("0.0001")
MONEY = Decimal("0.01")


@dataclass
class Result:
    table: pd.DataFrame
    dims: list[str]
    measures: list[str]
    units: dict[str, str]
    spec: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)  # caveats the model may repeat (no amounts)
    meta: dict = field(default_factory=dict)  # e.g. the resolved compare periods

    @property
    def time_dim(self) -> str | None:
        return next((d for d in self.dims if d in S.TIME_DIMS or d in ("date", "month")), None)


class Data:
    """Everything the engine reads, loaded once per session."""

    def __init__(self, led, settings, model, root=None):
        self.led, self.settings, self.model, self.root = led, settings, model, root
        self._cashflow = None
        self._transactions = None
        self._rsu = None

    @property
    def transactions(self) -> pd.DataFrame:
        if self._transactions is None:
            self._transactions = transactions.transactions(
                self.led, self.model, self.settings.base_currency
            )
        return self._transactions

    @property
    def rsu(self) -> dict:
        """Share awards (vests, purchases, their results), worked out once per loaded ledger."""
        if self._rsu is None:
            self._rsu = rsu.report(
                self.led.entries,
                self.root,
                ledger.valuation_date(self.led),
                self.settings.base_currency,
            )
        return self._rsu

    @property
    def cashflow(self) -> pd.DataFrame:
        if self._cashflow is None:
            self._cashflow = cashflow.cashflow(self.led, self.model, self.settings.base_currency)
        return self._cashflow


def _label(row: dict, dim: str):
    m = row["month"]
    return {
        "account": row["account"], "category": row["category"], "payee": row["payee"] or "(none)",
        "cadence": row["cadence"], "necessity": row["necessity"], "currency": row["orig_ccy"],
        "series": row["series_id"] if pd.notna(row["series_id"]) else "(none)",
        "month": f"{m.year}-{m.month:02d}", "quarter": f"{m.year}-Q{(m.month - 1) // 3 + 1}", "year": str(m.year),
    }[dim]  # fmt: skip


def _filtered(data: Data, q: S.QuerySpec) -> list[dict]:
    rows = spend.monthly_rows(data.model, q.view).to_dict("records")
    f = q.filters
    out = []
    for r in rows:
        if q.scope == "spend" and not r["in_spend"]:
            continue
        if q.scope == "major" and not r["major"]:
            continue
        if f.account_prefix and not r["account"].startswith(f.account_prefix):
            continue
        if f.payee and f.payee.lower() not in (r["payee"] or "").lower():
            continue
        if f.tags and not set(f.tags) <= set(r["tags"]):
            continue
        if (f.date_from and r["date"] < f.date_from) or (f.date_to and r["date"] > f.date_to):
            continue
        if f.cadence and r["cadence"] != f.cadence or f.necessity and r["necessity"] != f.necessity:
            continue
        out.append(r)
    return out


def _prior_year(label: str) -> str:
    y, _, rest = label.partition("-")
    return f"{int(y) - 1}-{rest}" if rest else str(int(y) - 1)


def _order(rows: list, measure: str, direction: str, get=lambda r, m: r[m]) -> list:
    """Stable sort by a measure; rows with no value (None) always go last."""

    def key(r):
        v = get(r, measure)
        missing = v is None or pd.isna(v)
        return (missing, 0 if missing else (-v if direction == "desc" else v))

    return sorted(rows, key=key)


def _first_month(data: Data) -> date:
    return spend.month_start(data.model.postings["date"].min())


def last_complete_month(as_of: date) -> date:
    nxt = date(as_of.year + (as_of.month == 12), as_of.month % 12 + 1, 1)
    this = spend.month_start(as_of)
    return this if as_of == nxt - timedelta(days=1) else spend.add_months(this, -1)


def resolve_compare(c: S.Compare, as_of: date) -> tuple[tuple[date, date], tuple[date, date]]:
    """((current from, to), (baseline from, to)) as first-of-month dates, inclusive."""
    if c.preset is None:
        month = lambda s: date(int(s[:4]), int(s[5:7]), 1)
        return (month(c.current.from_month), month(c.current.to_month)), (
            month(c.baseline.from_month),
            month(c.baseline.to_month),
        )
    lcm, add = last_complete_month(as_of), spend.add_months
    if c.preset == "ytd_vs_prior_year":
        start = date(lcm.year, 1, 1)
        return (start, lcm), (add(start, -12), add(lcm, -12))
    if c.preset == "last_12m_vs_prior_12m":
        return (add(lcm, -11), lcm), (add(lcm, -23), add(lcm, -12))
    if c.preset == "last_month_vs_prior_month":
        return (lcm, lcm), (add(lcm, -1), add(lcm, -1))
    q_end = lcm if lcm.month % 3 == 0 else add(lcm, -(lcm.month % 3))
    current = (add(q_end, -2), q_end)
    if c.preset == "last_quarter_vs_prior_quarter":
        return current, (add(q_end, -5), add(q_end, -3))
    return current, (add(q_end, -14), add(q_end, -12))


def _span(window: tuple[date, date]) -> str:
    return f"{window[0]:%Y-%m}..{window[1]:%Y-%m}"


def _run_compare(data: Data, q: S.QuerySpec) -> Result:
    cur_w, base_w = resolve_compare(q.compare, data.model.as_of)
    dims = list(q.group_by)
    cur: dict[tuple, Decimal] = defaultdict(Decimal)
    base: dict[tuple, Decimal] = defaultdict(Decimal)
    for r in _filtered(data, q):
        tags = r["tags"] or ("(none)",) if "tag" in dims else (None,)
        for tag in tags:
            key = tuple(tag if d == "tag" else _label(r, d) for d in dims) or ("total",)
            cur[key] += r["amount"] if cur_w[0] <= r["month"] <= cur_w[1] else Decimal(0)
            base[key] += r["amount"] if base_w[0] <= r["month"] <= base_w[1] else Decimal(0)
    names = dims or ["scope"]
    rows = []
    for key, c in cur.items():
        b = base[key]
        row = dict(zip(names, key, strict=True))
        row |= {
            "current": c.quantize(MONEY),
            "baseline": b.quantize(MONEY),
            "change": (c - b).quantize(MONEY),
            "change_pct": ((c - b) / abs(b)).quantize(PCT) if b else None,
        }
        rows.append(row)
    by = q.order_by or S.OrderBy(measure=q.measures[0])
    rows = _order(rows, by.measure, by.direction)
    if q.limit:
        rows = rows[: q.limit]
    notes = []
    last = spend.month_start(data.model.as_of)
    if cur_w[1] >= last and data.model.as_of != last_complete_month(data.model.as_of):
        notes.append(f"the current period includes {last:%Y-%m}, which is not finished")
    if base_w[0] < _first_month(data):
        notes.append(
            f"the baseline period starts before your data does ({_first_month(data):%Y-%m})"
        )
    table = pd.DataFrame(rows, columns=[*names, *q.measures])
    result = Result(
        table,
        names,
        list(q.measures),
        {m: S.UNITS[m] for m in q.measures},
        q.model_dump(mode="json"),
    )
    result.notes = notes
    result.meta = {"compare": {"current": _span(cur_w), "baseline": _span(base_w)}}
    return result


def run_query(data: Data, q: S.QuerySpec) -> Result:
    if q.compare:
        return _run_compare(data, q)
    dims = list(q.group_by)
    sums: dict[tuple, Decimal] = defaultdict(Decimal)
    counts: dict[tuple, int] = defaultdict(int)
    for r in _filtered(data, q):
        tags = r["tags"] or ("(none)",) if "tag" in dims else (None,)
        for tag in tags:
            key = tuple(tag if d == "tag" else _label(r, d) for d in dims) or ("total",)
            sums[key] += r["amount"]
            counts[key] += 1
    time_i = next((i for i, d in enumerate(dims) if d in S.TIME_DIMS), None)
    keys = sorted(
        sums,
        key=(lambda k: tuple(k[i] for i in range(len(k)) if i != time_i) + (k[time_i],))
        if time_i is not None
        else lambda k: -sums[k],
    )
    names = dims or ["scope"]
    total = sum(
        (sums[k] for k in keys), Decimal(0)
    )  # shares are of everything, even if limited later
    cols: dict[str, list] = {n: [k[i] for k in keys] for i, n in enumerate(names)}
    running: dict[tuple, Decimal] = defaultdict(Decimal)
    prev: dict[tuple, Decimal] = {}
    for m in q.measures:
        vals = []
        for k in keys:
            group = tuple(v for i, v in enumerate(k) if i != time_i)
            if m == "sum":
                vals.append(sums[k].quantize(MONEY))
            elif m == "count":
                vals.append(counts[k])
            elif m == "avg":
                vals.append((sums[k] / counts[k]).quantize(MONEY))
            elif m == "share_of_total":
                vals.append((sums[k] / total).quantize(PCT) if total else None)
            elif m == "running_total":
                running[group] += sums[k]
                vals.append(running[group].quantize(MONEY))
            elif m == "pct_change_prev":
                p = prev.get(group)
                vals.append(((sums[k] - p) / p).quantize(PCT) if p else None)
                prev[group] = sums[k]
            elif m == "pct_change_yoy":
                other = list(k)
                other[time_i] = _prior_year(k[time_i])
                p = sums.get(tuple(other))
                vals.append(((sums[k] - p) / p).quantize(PCT) if p else None)
        cols[m] = vals
    table = pd.DataFrame(cols)
    if q.order_by:
        ordered = _order(table.to_dict("records"), q.order_by.measure, q.order_by.direction)
        table = pd.DataFrame(ordered, columns=table.columns)
    if q.limit:
        table = table.head(q.limit).reset_index(drop=True)
    return Result(
        table,
        names,
        list(q.measures),
        {m: S.UNITS[m] for m in q.measures},
        q.model_dump(mode="json"),
    )


def run_report(data: Data, r: S.ReportSpec) -> Result:
    base = data.settings.base_currency
    if r.report == "networth":
        df = networth.net_worth(data.led, base)
        df["date"] = df["date"].map(str)
        return Result(
            df, ["date", "group"], ["value"], {"value": "money"}, r.model_dump(mode="json")
        )
    if r.report == "cashflow":
        df = data.cashflow.copy()
        df["month"] = df["month"].map(lambda m: f"{m.year}-{m.month:02d}")
        measures = [c for c in df.columns if c != "month"]
        units = {c: "pct" if c.startswith("savings_rate") else "money" for c in measures}
        return Result(df, ["month"], measures, units, r.model_dump(mode="json"))
    if r.report == "allocation":
        h = allocation.holdings(data.led, base)
        df = allocation.allocation(h, r.by, data.settings.targets or None)
        measures = [c for c in df.columns if c != r.by]
        units = {c: "money" if c == "value" else "pct" for c in measures}
        return Result(df, [r.by], measures, units, r.model_dump(mode="json"))
    head = spend.headline(data.model)
    df = pd.DataFrame({"metric": list(head), "value": list(head.values())})
    return Result(df, ["metric"], ["value"], {"value": "money"}, r.model_dump(mode="json"))


def run(data: Data, inp: S.RunQueryInput) -> Result:
    return run_query(data, inp.query) if inp.query else run_report(data, inp.report)
