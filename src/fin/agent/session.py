"""The privacy gateway: the only way a model touches the ledger.

`Session.call(tool, args)` runs a tool locally and returns what the model is allowed to see.
Data for the person (charts, tables, filled-in text) goes to `session.events`, never to the model.
Every call is appended to state/llm_audit.jsonl.
"""

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fin.agent import engine
from fin.agent import spec as S
from fin.agent.engine import Data, Result
from fin.charts import ChartType, chart_option, format_value
from fin.privacy.redact import Policy

MAX_LABELS = 200
STATS = ("total", "avg", "min", "max", "first", "last", "change", "change_pct")
# these skip periods that are only partly covered by data (see partial_periods)
COMPLETE_ONLY = ("avg", "first", "last", "change", "change_pct")
PLACEHOLDER = re.compile(r"\{\{\s*(r\d+)\.(\w+)(?:\.(\w+)|\[([^\]]+)\])?\s*\}\}(%?)")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListPayeesInput(_In):
    contains: str | None = None


class MakeChartInput(_In):
    handle: str
    chart_type: ChartType
    x: str = Field(description="a dimension of the result")
    y: str = Field(description="a measure of the result")
    series: str | None = Field(None, description="a second dimension, one line/bar group each")
    title: str


class MakeTableInput(_In):
    handle: str
    columns: list[str] | None = None
    sort_by: str | None = None
    descending: bool = True
    limit: int = Field(50, ge=1, le=500)


class SayInput(_In):
    template: str = Field(
        description="Text with placeholders like {{r1.sum.total}}, {{r1.sum.change_pct}} or "
        "{{r1.sum[Expenses:Food:Groceries]}}. To name or quote a ranked row use [#n]: "
        "{{r1.label[#1]}} is the first row's name and {{r1.change[#1]}} its value. Stats: "
        + ", ".join(STATS)
        + ". Numbers are filled in locally."
    )


class PinInput(_In):
    chart_id: str
    name: str = Field(description="dashboard tile name")


class Empty(_In):
    pass


TOOLS: dict[str, tuple[type[BaseModel], str]] = {
    "schema": (Empty, "Describe the dimensions, measures and reports available. Contains no amounts."),
    "list_accounts": (Empty, "Account names by type."),
    "list_payees": (ListPayeesInput, "Merchant names seen in expenses (no amounts)."),
    "run_query": (
        S.RunQueryInput,
        (
            "Run a structured query or built-in report locally. Returns a handle and the shape of "
            "the result (labels in result order, columns), never the numbers. Use order_by and "
            "limit for top-N, and compare for two periods (a preset or explicit months)."
        ),
    ),
    "make_chart": (MakeChartInput, "Draw a chart of a result for the person; you will not see the values."),
    "make_table": (MakeTableInput, "Show a table of a result to the person; you will not see the values."),
    "say": (SayInput, "Show the person a sentence; {{placeholders}} are filled with real numbers locally."),
    "pin": (PinInput, "Save a chart to the dashboards (query + chart layout only, no data)."),
}  # fmt: skip


def tool_specs() -> list[dict]:
    """Backend-agnostic tool definitions (name, description, JSON schema)."""
    return [
        {"name": n, "description": d, "input_schema": m.model_json_schema()}
        for n, (m, d) in TOOLS.items()
    ]


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "tile"


def partial_periods(res: Result, first: date, last: date) -> list[str]:
    """Time labels covering only part of the period (data starts or ends mid-period)."""
    dim = res.time_dim
    if dim not in S.TIME_DIMS:
        return []
    out = []
    for label in dict.fromkeys(res.table[dim]):
        if dim == "month":
            y, m = map(int, label.split("-"))
            lo, hi = date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1)
        elif dim == "quarter":
            y, q = int(label[:4]), int(label[-1])
            lo, hi = date(y, 3 * q - 2, 1), date(y + (q == 4), 3 * q % 12 + 1, 1)
        else:
            lo, hi = date(int(label), 1, 1), date(int(label) + 1, 1, 1)
        if lo < first or hi > last:
            out.append(label)
    return out


class Session:
    def __init__(self, data: Data, root: Path, policy: Policy):
        self.root, self.base_policy = root, policy
        self.results: dict[str, Result] = {}
        self.charts: dict[str, dict] = {}
        self.events: list[dict] = []
        self.refresh(data)

    def refresh(self, data: Data) -> None:
        """Point at the current ledger. Results already computed keep their numbers; new queries see
        the latest labels, tags and transactions."""
        self.data = data
        self.policy = self.base_policy.with_ledger(data.led.entries)
        self.ccy = data.settings.base_currency
        dates = data.model.postings["date"]
        self.first, self.last = dates.min(), data.model.as_of

    # ---- entry point -------------------------------------------------------------------------
    def call(self, tool: str, args: dict) -> dict:
        try:
            if tool not in TOOLS:
                raise ValueError(f"unknown tool {tool}")
            payload = getattr(self, f"_{tool}")(TOOLS[tool][0].model_validate(args))
        except (ValidationError, ValueError, KeyError) as e:
            payload = {"error": str(e)[:500]}
        except Exception as e:  # noqa: BLE001 - the text of an unexpected error may quote data
            payload = {"error": f"internal error ({type(e).__name__})"}
        visible = self.policy.redact_obj(payload)
        self._audit(tool, args, visible)
        return visible

    def _audit(self, tool: str, args: dict, visible: dict) -> None:
        path = self.root / "state" / "llm_audit.jsonl"
        path.parent.mkdir(exist_ok=True)
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "tool": tool,
            "args": args,
            "to_model": visible,
        }
        with path.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    # ---- what the model may see --------------------------------------------------------------
    def _describe(self, handle: str, res: Result) -> dict:
        t = res.table
        out = {
            "handle": handle,
            "dimensions": res.dims,
            "measures": {m: res.units[m] for m in res.measures},
            "row_count": len(t),
            "labels": {d: list(dict.fromkeys(t[d]))[:MAX_LABELS] for d in res.dims},
            "partial_periods": partial_periods(res, self.first, self.last),
        }
        out.update(res.meta)
        if res.notes:
            out["warnings"] = res.notes
        out["row_order"] = self._row_order(res)
        if self.policy.sees_ratios:
            keep = res.dims + [
                m for m in res.measures if res.units[m] == "pct" or self.policy.sees_values
            ]
            out["rows"] = _plain(t[keep].head(MAX_LABELS))
        return out

    @staticmethod
    def _row_order(res: Result) -> str:
        if res.time_dim:
            return "chronological"
        spec = res.spec.get("query") or {}
        by = spec.get("order_by") or {"measure": res.measures[0], "direction": "desc"}
        return f"ranked by {by['measure']}, {'largest' if by['direction'] == 'desc' else 'smallest'} first"

    def _schema(self, _):
        return {
            "base_currency": self.ccy,
            "privacy_level": self.policy.level,
            "data_range": [str(self.first), str(self.last)],
            "query": S.QuerySpec.model_json_schema(),
            "reports": {
                "networth": "net worth by group over time (dimensions: date, group; measure: value)",
                "cashflow": "monthly income, expenses, net, savings rates (dimension: month)",
                "allocation": "holdings by asset_class|currency|commodity with targets and drift",
                "spend_headline": "core monthly spend, essential baseline, discretionary, major this year",
            },
            "notes": "You never receive amounts. Use run_query, then make_chart / make_table / say.",
        }

    def _list_accounts(self, _):
        from beancount.core import data as bdata

        names = sorted(e.account for e in self.data.led.entries if isinstance(e, bdata.Open))
        out: dict[str, list[str]] = {}
        for n in names:
            out.setdefault(n.split(":")[0], []).append(n)
        return out

    def _list_payees(self, a: ListPayeesInput):
        payees = sorted({p for p in self.data.model.postings["payee"] if p})
        if a.contains:
            payees = [p for p in payees if a.contains.lower() in p.lower()]
        return {"payees": payees[:MAX_LABELS], "truncated": len(payees) > MAX_LABELS}

    def _run_query(self, a: S.RunQueryInput):
        res = engine.run(self.data, a)
        handle = f"r{len(self.results) + 1}"
        self.results[handle] = res
        res.spec = a.model_dump(mode="json", exclude_none=True)
        return self._describe(handle, res)

    # ---- what only the person sees -----------------------------------------------------------
    def _result(self, handle: str) -> Result:
        if handle not in self.results:
            raise ValueError(f"unknown handle {handle}")
        return self.results[handle]

    def _make_chart(self, a: MakeChartInput):
        res = self._result(a.handle)
        if a.x not in res.dims or (a.series and a.series not in res.dims) or a.series == a.x:
            raise ValueError(f"x/series must be distinct dimensions of {a.handle}: {res.dims}")
        if a.y not in res.measures:
            raise ValueError(f"y must be a measure of {a.handle}: {res.measures}")
        chart_id = f"c{len(self.charts) + 1}"
        self.charts[chart_id] = {"handle": a.handle, "args": a.model_dump(exclude={"handle"})}
        option = chart_option(res, a.chart_type, a.x, a.y, a.series, a.title, self.ccy)
        self.events.append({"type": "chart", "chart_id": chart_id, "option": option})
        return {"ok": True, "chart_id": chart_id}

    def _make_table(self, a: MakeTableInput):
        res = self._result(a.handle)
        cols = a.columns or res.dims + res.measures
        if bad := [c for c in cols if c not in res.table.columns]:
            raise ValueError(f"unknown columns {bad}")
        df = res.table
        if a.sort_by:
            df = df.sort_values(a.sort_by, ascending=not a.descending, na_position="last")
        df = df.head(a.limit)
        rows = [
            [
                format_value(r[c], res.units[c], self.ccy) if c in res.units else str(r[c])
                for c in cols
            ]
            for _, r in df.iterrows()
        ]
        self.events.append({"type": "table", "columns": cols, "rows": rows})
        return {"ok": True, "rows_shown": len(rows)}

    def _say(self, a: SayInput):
        text = self.fill(a.template)
        self.events.append({"type": "text", "text": text})
        return {"ok": True}

    def _pin(self, a: PinInput):
        chart = self.charts.get(a.chart_id)
        if chart is None:
            raise ValueError(f"unknown chart {a.chart_id}")
        res = self._result(chart["handle"])
        path = self.root / "dashboards" / f"{_slug(a.name)}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            json.dumps({"title": a.name, "query": res.spec, "chart": chart["args"]}, indent=2)
            + "\n"
        )
        return {"ok": True, "saved_as": path.name}

    # ---- placeholders ------------------------------------------------------------------------
    def fill(self, template: str) -> str:
        def sub(m: re.Match) -> str:
            handle, measure, stat, label, percent = m.groups()
            try:
                value = self._value(handle, measure, stat or "total", label)
                # percentages already carry their sign; a model-written "%" after the placeholder is dropped
                return value if value.endswith("%") else value + percent
            except (KeyError, ValueError, ZeroDivisionError, ArithmeticError):
                return "[?]"

        return PLACEHOLDER.sub(sub, template)

    def _value(self, handle: str, measure: str, stat: str, label: str | None) -> str:
        res = self._result(handle)
        if label is not None and label.startswith("#"):
            return self._at_rank(res, measure, int(label[1:]))
        if measure not in res.measures:
            raise ValueError(measure)
        t = res.table
        if label is not None:
            t = t[t[res.dims].eq(label).any(axis=1)]
            if t.empty:
                raise ValueError(label)
        if res.time_dim in t.columns and label is None:
            t = t.sort_values(res.time_dim)
        if stat not in STATS:
            raise ValueError(stat)
        if label is None and stat in COMPLETE_ONLY:
            # "last" must mean the last finished period, not a half-filled current one
            partial = set(partial_periods(res, self.first, self.last))
            complete = t[~t[res.time_dim].isin(partial)] if res.time_dim in t.columns else t
            t = complete if len(complete) else t
        vals = [Decimal(v) for v in t[measure] if not pd.isna(v)]
        unit = res.units[measure]
        number = {
            "total": sum(vals, Decimal(0)),
            "avg": sum(vals, Decimal(0)) / len(vals),
            "min": min(vals), "max": max(vals), "first": vals[0], "last": vals[-1],
            "change": vals[-1] - vals[0],
            "change_pct": (vals[-1] - vals[0]) / vals[0],
        }[stat]  # fmt: skip
        return format_value(number, "pct" if stat == "change_pct" else unit, self.ccy)

    def _at_rank(self, res: Result, measure: str, rank: int) -> str:
        """{{r1.label[#1]}} names the first row; {{r1.change[#1]}} is its value. Rank follows the
        result's order, which the model chose (order_by) but whose contents it never sees."""
        if not 1 <= rank <= len(res.table):
            raise ValueError(rank)
        row = res.table.iloc[rank - 1]
        if measure == "label":
            return str(row[res.dims[0]])
        if measure not in res.measures:
            raise ValueError(measure)
        return format_value(row[measure], res.units[measure], self.ccy)

    # ---- pinned dashboards -------------------------------------------------------------------
    def render_pinned(self, name: str) -> dict:
        """Re-run a saved tile against the current ledger and return its chart event."""
        saved = json.loads((self.root / "dashboards" / f"{_slug(name)}.json").read_text())
        handle = self.call("run_query", saved["query"])["handle"]
        self.call("make_chart", {"handle": handle, **saved["chart"]})
        return self.events[-1]


def _plain(df) -> list[dict]:
    return [
        {k: (float(v) if isinstance(v, Decimal) else v) for k, v in row.items()}
        for row in df.to_dict("records")
    ]


Level = Literal["blind", "relative", "full"]
