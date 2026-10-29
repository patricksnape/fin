import { useMemo } from "react";
import { AnnualRow, Trends as TrendsData, useApi } from "../lib/api";
import { dateLabel, money, monthLabel, n, pct } from "../lib/format";
import { lineChart, overlayChart, simpleBars, stackedBars } from "../lib/charts";
import { useMode } from "../lib/theme";
import { go } from "../lib/route";
import { ChartCard } from "../components/Chart";
import { Card, CardHeader, Column, DataTable, Loading } from "../components/ui";

const label = (r: { year: number; partial?: boolean; months: number }) => (r.partial ? `${r.year}*` : String(r.year));
const plain = (v: string | null, ccy: string) => (v == null ? "–" : money(v, ccy, 0).replace(`${ccy} `, ""));

export default function Trends({ ccy }: { ccy: string }) {
  const { data, isFetching } = useApi<TrendsData>("/api/trends");
  const mode = useMode();
  const charts = useMemo(() => {
    if (!data) return null;
    const annual = data.annual;
    const windowed = data.rolling.filter((r) => r.spend_12m != null); // a 12-month average needs 12 months
    const monthly = (r: { year: number; months: number; total: number | string; [k: string]: string | number }) => r.months;
    // average per month, so a part-year is not mistaken for lower spending
    const keep = 5;
    const top = data.categories.keys.slice(0, keep);
    const rest = data.categories.keys.slice(keep);
    const rows = data.categories.rows.map((r) => {
      const per = (k: string) => String(n(String(r[k])) / Math.max(1, monthly(r as any)));
      const other = rest.reduce((s, k) => s + n(per(k)), 0);
      return { month: String(r.year), ...Object.fromEntries(top.map((k) => [k, per(k)])), ...(rest.length ? { Other: String(other) } : {}), total: "0" };
    });
    const pivot = { keys: [...top, ...(rest.length ? ["Other"] : [])], rows };
    const partial = new Set(annual.filter((a) => a.partial).map((a) => String(a.year)));
    const byYear = stackedBars(pivot, mode, false);
    byYear.xAxis.data = pivot.rows.map((r) => (partial.has(r.month) ? `${r.month}*` : r.month));
    return {
      pivot,
      rest,
      byYear,
      savings: simpleBars(annual.map(label), annual.map((a) => n(a.savings_rate) * 100), mode, "%", "Savings rate"),
      growth: (() => {
        const o = stackedBars({ keys: ["from_saving", "from_markets_and_revaluation"], rows: annual.map((a) => ({ month: String(a.year), from_saving: a.from_saving, from_markets_and_revaluation: a.from_markets_and_revaluation, total: "0" })) }, mode, false);
        o.xAxis.data = annual.map(label);
        o.series[0].name = "Saved";
        o.series[1].name = "Markets and revaluation";
        return o;
      })(),
      overlay: overlayChart(data.overlay, mode),
      rollSpend: lineChart(windowed.map((r) => monthLabel(r.month.slice(0, 7))), windowed.map((r) => n(r.spend_12m)), mode, "CHF", "Average monthly spend"),
      rollSave: lineChart(windowed.map((r) => monthLabel(r.month.slice(0, 7))), windowed.map((r) => n(r.savings_rate_12m) * 100), mode, "%", "Savings rate"),
    };
  }, [data, mode]);
  if (!data || !charts) return <Loading what="Computing trends" />;
  // a bar opens that year's transactions (and its category, when it is a category bar)
  const yearRange = (y: string | number) => ({ start: `${String(y).replace("*", "")}-01`, end: `${String(y).replace("*", "")}-12` });
  const pickCategory = (e: { seriesIndex: number; dataIndex: number }) => {
    const key = charts.pivot.keys[e.seriesIndex];
    const year = charts.pivot.rows[e.dataIndex]?.month;
    if (key && year) go("transactions", { kind: "expense", ...yearRange(year), category: key === "Other" ? charts.rest.join(",") : key });
  };
  const pickYear = (e: { dataIndex: number }) => {
    const a = data.annual[e.dataIndex];
    if (a) go("transactions", yearRange(a.year));
  };

  const cols: Column<AnnualRow>[] = [
    { key: "year", label: "Year", render: (a) => <span className="whitespace-nowrap">{a.year}{a.partial && <span className="ml-1 text-xs text-muted">· {a.months} mo*</span>}</span> },
    { key: "income", label: "Income", align: "right", render: (a) => plain(a.income, ccy) },
    { key: "spend", label: "Spend", align: "right", render: (a) => plain(a.spend, ccy) },
    { key: "avg_monthly_spend", label: "Per month", align: "right", render: (a) => plain(a.avg_monthly_spend, ccy) },
    { key: "taxes", label: "Taxes", align: "right", render: (a) => plain(a.taxes, ccy) },
    { key: "net", label: "Saved", align: "right", render: (a) => plain(a.net, ccy) },
    { key: "savings_rate", label: "Rate", align: "right", render: (a) => pct(a.savings_rate) },
    { key: "net_worth_end", label: "Net worth", align: "right", render: (a) => plain(a.net_worth_end, ccy) },
    { key: "from_markets_and_revaluation", label: "From markets", align: "right", render: (a) => plain(a.from_markets_and_revaluation, ccy) },
  ];
  const tableCols = (keys: string[]): Column<Record<string, string>>[] => [
    { key: "month", label: "Year" },
    ...keys.map((k) => ({ key: k, label: k.replace("Expenses:", ""), align: "right" as const, render: (r: Record<string, string>) => money(r[k], ccy, 0) })),
  ];
  const catRows = (charts.byYear.series as any[])[0].data.map((_: unknown, i: number) => Object.fromEntries([["month", charts.byYear.xAxis.data[i]], ...(charts.byYear.series as any[]).map((s: any) => [s.name, String(s.data[i])])]));
  const catKeys = (charts.byYear.series as any[]).map((s: any) => s.name);
  const simpleRows = (labels: string[], values: number[], fmt: (v: number) => string) => labels.map((l, i) => ({ month: l, v: fmt(values[i]) }));
  return (
    <div className="space-y-6">
      <p className="text-xs text-muted">Averages use complete months only (through {dateLabel(data.through).replace(/^\d+ /, "")}). A * marks a year that is not complete yet.</p>
      <Card>
        <CardHeader title="Year by year" subtitle={`Amounts in ${ccy}. Spend excludes major purchases, taxes and payroll deductions; Saved is income minus all expenses.`} />
        <DataTable columns={cols} rows={data.annual} />
      </Card>
      <div className="grid gap-6 xl:grid-cols-2">
        <ChartCard title="Spend per month, by year" subtitle="Average per complete month, top five categories. Click a bar to see its transactions." onPick={pickCategory} loading={isFetching} option={charts.byYear} columns={tableCols(catKeys)} rows={catRows} />
        <ChartCard title="Savings rate by year" subtitle="Saved as a share of income. Click a bar to see that year." onPick={pickYear} loading={isFetching} option={charts.savings} columns={[{ key: "month", label: "Year" }, { key: "v", label: "Rate", align: "right" }]} rows={simpleRows(data.annual.map(label), data.annual.map((a) => n(a.savings_rate)), (v) => `${(v * 100).toFixed(1)}%`)} />
      </div>
      <ChartCard
        title="Where net worth growth came from"
        subtitle="Saved is income minus expenses; the rest is markets, FX and your own valuations of home and car"
        onPick={pickYear}
        loading={isFetching}
        option={charts.growth}
        height={300}
        columns={[{ key: "month", label: "Year" }, { key: "s", label: "Saved", align: "right" }, { key: "m", label: "Markets and revaluation", align: "right" }, { key: "t", label: "Change", align: "right" }]}
        rows={data.annual.map((a) => ({ month: label(a), s: money(a.from_saving, ccy, 0), m: money(a.from_markets_and_revaluation, ccy, 0), t: money(a.net_worth_change, ccy, 0) }))}
      />
      <ChartCard
        title="Spend by month, year over year"
        subtitle="The same month across years: seasonality and drift (lighter lines are older years)"
        loading={isFetching}
        option={charts.overlay}
        columns={[{ key: "month", label: "Year" }, ...["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"].map((m, i) => ({ key: String(i), label: m, align: "right" as const }))]}
        rows={Object.entries(data.overlay).map(([y, vals]) => ({ month: y, ...Object.fromEntries(vals.map((v, i) => [String(i), v == null ? "–" : money(v, ccy, 0).replace(`${ccy} `, "")])) }))}
      />
      <div className="grid gap-6 xl:grid-cols-2">
        <ChartCard title="12-month average spend" subtitle="Smooths out one-off months" loading={isFetching} option={charts.rollSpend} columns={[{ key: "month", label: "Month" }, { key: "v", label: ccy, align: "right" }]} rows={data.rolling.filter((r) => r.spend_12m).map((r) => ({ month: monthLabel(r.month.slice(0, 7)), v: money(r.spend_12m, ccy, 0) }))} />
        <ChartCard title="12-month savings rate" subtitle="Saved over the last 12 months as a share of income" loading={isFetching} option={charts.rollSave} columns={[{ key: "month", label: "Month" }, { key: "v", label: "Rate", align: "right" }]} rows={data.rolling.filter((r) => r.savings_rate_12m).map((r) => ({ month: monthLabel(r.month.slice(0, 7)), v: pct(r.savings_rate_12m) }))} />
      </div>
    </div>
  );
}
