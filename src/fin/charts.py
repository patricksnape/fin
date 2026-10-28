"""ECharts options from a Result. Built locally; the chat model only chooses axes and chart type.

Chart specs are presentation, so values are plain floats here (pct measures scaled to percent);
ledger arithmetic elsewhere stays Decimal.
"""

from decimal import Decimal
from typing import Literal

import pandas as pd

from fin.agent.engine import Result

ChartType = Literal["bar", "stacked_bar", "line", "area", "pie"]


def _num(v, unit: str):
    if pd.isna(v):
        return None
    return float(v * 100 if unit == "pct" else v)


def format_value(v, unit: str, ccy: str = "CHF") -> str:
    if pd.isna(v):
        return "–"
    if unit == "pct":
        return f"{Decimal(v) * 100:.1f}%"
    if unit == "int":
        return f"{int(v):,}"
    return f"{ccy} {Decimal(v):,.2f}"


PIE_KEEP = 6
STACK_KEEP = 5


def _fold(df: pd.DataFrame, group: str, x: str, y: str, keep: int) -> pd.DataFrame:
    """Keep the `keep` largest groups; sum the rest into "Other" (per x)."""
    totals = df.groupby(group)[y].sum().abs().sort_values(ascending=False)
    if len(totals) <= keep + 1:
        return df
    top = set(totals.index[:keep])
    tail = df[~df[group].isin(top)]
    other = tail.groupby(x, as_index=False, sort=False)[y].sum().assign(**{group: "Other"})
    return pd.concat([df[df[group].isin(top)], other], ignore_index=True)


def chart_option(
    res: Result, kind: ChartType, x: str, y: str, series: str | None, title: str, ccy: str = "CHF"
) -> dict:
    df, unit = res.table, res.units[y]
    if unit != "pct":  # summing percent changes would be meaningless
        if kind == "pie":
            df = _fold(df, x, x, y, PIE_KEEP)
        elif series:
            df = _fold(df, series, x, y, STACK_KEEP)
    xs = list(dict.fromkeys(df[x]))
    if x in ("month", "quarter", "year", "date"):
        xs = sorted(xs)
    base = {"title": {"text": title}, "tooltip": {"trigger": "item" if kind == "pie" else "axis"}}
    name = {"money": ccy, "pct": "%", "int": ""}[unit]
    if kind == "pie":
        sums = df.groupby(x, sort=False)[y].sum()
        data = [{"name": str(k), "value": _num(v, unit)} for k, v in sums.items()]
        return {
            **base,
            "legend": {"top": "bottom"},
            "series": [{"type": "pie", "radius": "60%", "data": data}],
        }
    names = list(dict.fromkeys(df[series])) if series else [y]
    out = []
    for n in names:
        part = df[df[series] == n] if series else df
        by_x = {r[x]: _num(r[y], unit) for _, r in part.iterrows()}
        s = {
            "name": str(n),
            "type": "bar" if kind in ("bar", "stacked_bar") else "line",
            "data": [by_x.get(k) for k in xs],
        }
        if kind == "stacked_bar":
            s["stack"] = "total"
        if kind == "area":
            s.update(areaStyle={}, stack="total")
        out.append(s)
    return {
        **base,
        "legend": {"top": "bottom"} if series else {"show": False},
        "xAxis": {"type": "category", "data": [str(k) for k in xs]},
        "yAxis": {"type": "value", "name": name},
        "series": out,
    }
