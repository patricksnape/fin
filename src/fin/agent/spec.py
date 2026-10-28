"""The only query language a cloud model gets: a validated JSON spec, never raw BQL.

Raw BQL can turn amounts into text (`str(sum(position))`) that would then leak as a "label", so
the spec lists exactly which dimensions and measures exist and compiles them locally.
"""

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

GroupDim = Literal[
    "account", "category", "payee", "tag", "cadence", "necessity", "series", "currency", "month", "quarter", "year"
]  # fmt: skip
TIME_DIMS = ("month", "quarter", "year")
Measure = Literal[
    "sum", "count", "avg", "share_of_total", "running_total", "pct_change_prev", "pct_change_yoy",
    "current", "baseline", "change", "change_pct",
]  # fmt: skip
COMPARE_MEASURES = ("current", "baseline", "change", "change_pct")
UNITS = {
    "sum": "money", "avg": "money", "running_total": "money", "count": "int",
    "share_of_total": "pct", "pct_change_prev": "pct", "pct_change_yoy": "pct",
    "current": "money", "baseline": "money", "change": "money", "change_pct": "pct",
}  # fmt: skip
PRESETS = {
    "ytd_vs_prior_year": "this year's complete months so far vs the same months last year",
    "last_12m_vs_prior_12m": "the last 12 complete months vs the 12 before",
    "last_month_vs_prior_month": "the last complete month vs the one before",
    "last_quarter_vs_prior_quarter": "the last complete quarter vs the one before",
    "last_quarter_vs_year_ago": "the last complete quarter vs the same quarter a year earlier",
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Filters(Strict):
    account_prefix: str | None = Field(None, description="e.g. Expenses:Food")
    payee: str | None = Field(None, description="case-insensitive substring")
    tags: list[str] = []
    date_from: date | None = None
    date_to: date | None = None
    cadence: Literal["recurring", "regular", "one_off"] | None = None
    necessity: Literal["essential", "discretionary"] | None = None


class MonthRange(Strict):
    from_month: str = Field(description="YYYY-MM, inclusive")
    to_month: str = Field(description="YYYY-MM, inclusive")

    @model_validator(mode="after")
    def _check(self):
        for m in (self.from_month, self.to_month):
            if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", m):
                raise ValueError(f"month must look like 2026-03, got {m!r}")
        if self.from_month > self.to_month:
            raise ValueError("from_month is after to_month")
        return self


class Compare(Strict):
    """Compare two periods per group in one result (current, baseline, change, change_pct)."""

    preset: Literal[tuple(PRESETS)] | None = Field(  # type: ignore[valid-type]
        None, description="; ".join(f"{k}: {v}" for k, v in PRESETS.items())
    )
    current: MonthRange | None = None
    baseline: MonthRange | None = None

    @model_validator(mode="after")
    def _one(self):
        explicit = self.current is not None and self.baseline is not None
        if (self.preset is None) == (not explicit) or (
            self.preset and (self.current or self.baseline)
        ):
            raise ValueError("give either a preset, or both current and baseline")
        return self


class OrderBy(Strict):
    measure: Measure
    direction: Literal["asc", "desc"] = "desc"


class QuerySpec(Strict):
    """Expense postings, grouped and aggregated locally."""

    scope: Literal["spend", "major", "all"] = Field(
        "spend", description="spend excludes major purchases, taxes and payroll deductions"
    )
    view: Literal["run_rate", "cash"] = "run_rate"
    filters: Filters = Filters()
    group_by: list[GroupDim] = []
    measures: list[Measure] = ["sum"]
    compare: Compare | None = Field(
        None, description="compare two periods per group; replaces month/quarter/year grouping"
    )
    order_by: OrderBy | None = Field(
        None,
        description="rank rows by a measure (default: first measure, largest first); "
        "not with a time grouping",
    )
    limit: int | None = Field(None, ge=1, le=200, description="keep the top N rows after ordering")

    @model_validator(mode="after")
    def _check(self):
        times = [d for d in self.group_by if d in TIME_DIMS]
        if len(times) > 1:
            raise ValueError("use at most one of month, quarter, year in group_by")
        if len(set(self.group_by)) != len(self.group_by):
            raise ValueError("duplicate group_by dimension")
        if not self.measures:
            raise ValueError("at least one measure is required")
        if self.compare:
            if times:
                raise ValueError(
                    "compare replaces the time grouping: remove month/quarter/year from group_by"
                )
            if "measures" not in self.model_fields_set:
                self.measures = list(COMPARE_MEASURES)
            if not set(self.measures) <= set(COMPARE_MEASURES):
                raise ValueError(
                    "with compare, measures must come from: " + ", ".join(COMPARE_MEASURES)
                )
        elif set(self.measures) & set(COMPARE_MEASURES):
            raise ValueError("current/baseline/change/change_pct need a compare")
        if any(m.startswith("pct_change_") for m in self.measures) and not times:
            raise ValueError(
                "pct_change measures need month, quarter or year in group_by (or use compare)"
            )
        if "running_total" in self.measures and not times:
            raise ValueError("running_total needs month, quarter or year in group_by")
        if self.limit and times:
            raise ValueError("limit cannot be combined with a time grouping")
        if self.order_by:
            if times:
                raise ValueError("order_by cannot be combined with a time grouping")
            if self.order_by.measure not in self.measures:
                raise ValueError("order_by.measure must be one of the requested measures")
        return self


class ReportSpec(Strict):
    report: Literal["networth", "cashflow", "allocation", "spend_headline"]
    by: Literal["asset_class", "currency", "commodity"] = Field(
        "asset_class", description="allocation only"
    )


class RunQueryInput(Strict):
    query: QuerySpec | None = None
    report: ReportSpec | None = None

    @model_validator(mode="after")
    def _one(self):
        if (self.query is None) == (self.report is None):
            raise ValueError("give exactly one of query or report")
        return self
