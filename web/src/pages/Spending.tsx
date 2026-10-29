import { useMemo, useState } from "react";
import { Alert, Pivot, Series, Spending as SpendingData, useApi } from "../lib/api";
import { dateLabel, money, monthLabel, n, signedMoney } from "../lib/format";
import { Part, breakdown, donut, foldTail, sliceMonths, stackedBars } from "../lib/charts";
import { useMode } from "../lib/theme";
import { go } from "../lib/route";
import { ChartCard } from "../components/Chart";
import { Card, CardHeader, Column, DataTable, Segmented, StatTile, StatusBadge, Loading } from "../components/ui";

type View = "run_rate" | "cash";

function alertText(a: Alert, ccy: string): string {
  if (a.kind === "price_change") return `${a.payee} went from ${money(a.old, ccy)} to ${money(a.new, ccy)}`;
  if (a.kind === "lapsed") return `${a.payee} stopped after ${dateLabel(a.last_date!)} (${a.days_overdue} days overdue)`;
  return `${a.payee}: ${a.detail}`;
}
const ALERT_LABEL = { price_change: "Price change", lapsed: "Stopped", new: "To confirm" } as const;

export default function Spending({ ccy }: { ccy: string }) {
  const [view, setView] = useState<View>("run_rate");
  const [range, setRange] = useState("12"); // "6" | "12" | "24" months back, or a calendar year
  const year = range.length === 4 ? range : null;
  const months = Number(range);
  const rangeText = year ?? `the last ${months} months`;
  const [group, setGroup] = useState<string | null>(null);
  const [picked, setPicked] = useState<{ month: string; group: string } | null>(null);
  const { data, isFetching } = useApi<SpendingData>(`/api/spending?view=${view}&months=240`);
  const { data: flow } = useApi<{ rows: Array<Record<string, string>> }>("/api/cashflow?months=240");
  const mode = useMode();
  const slice = (p: Pivot): Pivot => (year ? { ...p, rows: p.rows.filter((r) => r.month.startsWith(year)) } : sliceMonths(p, months));
  const cadence = useMemo(() => data && slice(data.by_cadence), [data, range]);
  const necessity = useMemo(() => data && slice(data.by_necessity), [data, range]);
  const category = useMemo(() => data && foldTail(slice(data.by_category)), [data, range]);
  const accounts = useMemo(() => data && slice(data.by_account), [data, range]);
  const years = useMemo(() => (data ? [...new Set(data.by_account.rows.map((r) => r.month.slice(0, 4)))].sort().reverse() : []), [data]);
  // major purchases and recurring payments follow the range shown, month for month
  const from = accounts?.rows[0]?.month;
  const to = year ? accounts?.rows[accounts.rows.length - 1]?.month : undefined;
  const inRange = (day: string) => (!from || day.slice(0, 7) >= from) && (!to || day.slice(0, 7) <= to);
  // totals over the range: what came in, what was spent without and with the major purchases
  const totals = useMemo(() => {
    const rows = (flow?.rows ?? []).filter((r) => (!from || r.month.slice(0, 7) >= from) && (!to || r.month.slice(0, 7) <= to));
    const sum = (k: string) => rows.reduce((t, r) => t + n(r[k]), 0);
    return { in: sum("income"), earnings: sum("salary"), spend: sum("spend"), major: sum("major"), taxes: sum("taxes") + sum("payroll_deductions"), out: sum("expenses") };
  }, [flow, from, to]);
  // the headline averages are over the months shown, complete months only
  const average = useMemo(() => {
    const rows = (data?.monthly_headline ?? []).filter((r) => r.month !== data?.partial_month && (!from || r.month >= from) && (!to || r.month <= to));
    const avg = (k: "core" | "essential" | "discretionary") => (rows.length ? rows.reduce((t, r) => t + n(r[k]), 0) / rows.length : 0);
    return { core: avg("core"), essential: avg("essential"), discretionary: avg("discretionary"), months: rows.length };
  }, [data, from, to]);
  const recurring = useMemo(() => (data ? data.recurring.filter((r) => (!from || r.last_date.slice(0, 7) >= from) && (!to || r.first_date.slice(0, 7) <= to)) : []), [data, from, to]);
  const major = useMemo(() => (data ? data.major.filter((r) => inRange(r.date)) : []), [data, from, to]);
  if (!data || !cadence || !necessity || !category || !accounts) return <Loading what="Computing spending" />;

  // a click on a bar or a month opens that slice of spending as transactions
  const open = (filter: Record<string, string>) => go("transactions", { kind: "expense", ...filter });
  const pickFrom = (p: { keys: string[]; rows: Array<Record<string, string>> }, field: "category" | "cadence" | "necessity") => (e: { seriesIndex: number; dataIndex: number }) => {
    const key = p.keys[e.seriesIndex];
    open({ month: p.rows[e.dataIndex].month, ...(key && key !== "Other" ? { [field]: key } : {}) });
  };
  const groups = data.by_category.keys;
  const chosen = group && groups.includes(group) ? group : groups[0];
  const parts = chosen ? breakdown(accounts, chosen) : [];
  const partsTotal = parts.reduce((t, x) => t + x.amount, 0);
  const pickGroup = (e: { seriesIndex: number; dataIndex: number }) => {
    const key = category.keys[e.seriesIndex];
    if (key && key !== "Other") setPicked({ month: category.rows[e.dataIndex].month, group: key });
  };
  const monthParts = picked ? breakdown(data.by_account, picked.group, picked.month) : [];
  const monthTotal = monthParts.reduce((t, x) => t + x.amount, 0);
  const partCols = (month?: string): Column<Part>[] => [
    { key: "label", label: "Category", render: (r) => <button onClick={() => open({ ...(month ? { month } : {}), category: r.account })} className="text-accent underline-offset-2 hover:underline">{r.label}</button> },
    { key: "amount", label: ccy, align: "right", render: (r) => money(r.amount, ccy, 0) },
    { key: "share", label: "Share", align: "right", render: (r) => `${Math.round(r.share * 100)}%` },
  ];
  const monthCols = (keys: string[]): Column<Record<string, string>>[] => [
    { key: "month", label: "Month", render: (r) => <button onClick={() => open({ month: r.month })} className="text-accent underline-offset-2 hover:underline">{monthLabel(r.month)}</button> },
    ...keys.map((k) => ({ key: k, label: k.replace("Expenses:", ""), align: "right" as const, render: (r: Record<string, string>) => money(r[k], ccy, 0) })),
    { key: "total", label: "Total", align: "right" as const, render: (r: Record<string, string>) => money(r.total, ccy, 0) },
  ];
  const note = view === "run_rate" ? "Yearly and quarterly bills are spread over the months they cover. Major purchases, taxes and payroll deductions are excluded." : "Payments in the month they were made. Major purchases, taxes and payroll deductions are excluded.";
  const recurringCols: Column<Series>[] = [
    { key: "payee", label: "Payee" },
    { key: "period", label: "Period" },
    { key: "typical_amount", label: "Typical", align: "right", render: (s) => money(s.typical_amount, ccy) },
    { key: "last_date", label: "Last paid", render: (s) => dateLabel(s.last_date) },
    { key: "next_expected", label: "Next expected", render: (s) => (s.status === "active" ? dateLabel(s.next_expected) : "–") },
    { key: "status", label: "Status", render: (s) => <StatusBadge kind={s.status === "active" ? "good" : s.status === "lapsed" ? "muted" : "warn"}>{s.status === "proposed" ? "to confirm" : s.status}</StatusBadge> },
  ];
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <Segmented label="View" value={view} onChange={setView} options={[{ value: "run_rate", label: "Run-rate" }, { value: "cash", label: "Cash" }]} />
        <Segmented label="Range" value={(year ? "" : range) as "6" | "12" | "24"} onChange={setRange} options={[{ value: "6", label: "6 months" }, { value: "12", label: "12 months" }, { value: "24", label: "24 months" }]} />
        <select aria-label="Calendar year" value={year ?? ""} onChange={(e) => e.target.value && setRange(e.target.value)} className={`rounded-lg border bg-surface px-2 py-1.5 text-xs ${year ? "border-ink font-medium text-ink" : "border-hairline text-ink-2"}`}>
          <option value="" disabled className="bg-surface text-ink">Year…</option>
          {years.map((y) => <option key={y} value={y} className="bg-surface text-ink">{y}</option>)}
        </select>
        <p className="text-xs text-muted">{note}</p>
      </div>
      <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(165px,1fr))]">
        <Card><StatTile label="Total in" value={money(totals.in, ccy, 0)} hint={`all income, ${rangeText}`} /></Card>
        <Card><StatTile label="Total out" value={money(totals.out, ccy, 0)} hint={`incl. ${money(totals.taxes, ccy, 0)} tax and deductions`} /></Card>
        <Card><StatTile label="In minus out" value={money(totals.in - totals.out, ccy, 0)} hint={rangeText} /></Card>
        <Card><StatTile label="Total earnings" value={money(totals.earnings, ccy, 0)} hint={`salary and bonus, ${rangeText}`} /></Card>
        <Card><StatTile label="Total spend" value={money(totals.spend + totals.major, ccy, 0)} hint={`with major purchases, ${rangeText}`} /></Card>
        <Card><StatTile label="Spend without major purchases" value={money(totals.spend, ccy, 0)} hint={`${money(totals.major, ccy, 0)} major, ${rangeText}`} /></Card>
        <Card><StatTile label="Core monthly spend" value={money(average.core, ccy, 0)} hint={`average per month, ${average.months} full months`} /></Card>
        <Card><StatTile label="Essential baseline" value={money(average.essential, ccy, 0)} hint="minimum monthly need, average" /></Card>
        <Card><StatTile label="Discretionary" value={money(average.discretionary, ccy, 0)} hint="average per month" /></Card>
        <Card><StatTile label="Major purchases" value={money(major.reduce((t, r) => t + n(r.amount), 0), ccy, 0)} hint={`not in monthly spend, ${rangeText}`} /></Card>
      </div>
      <ChartCard title="Spend by cadence" subtitle="Recurring payments, regular variable spend, and one-offs. Click a bar to see its transactions." onPick={pickFrom(cadence, "cadence")} loading={isFetching} option={stackedBars(cadence, mode, true)} columns={monthCols(cadence.keys)} rows={cadence.rows} />
      <div className="grid gap-6 xl:grid-cols-2">
        <ChartCard title="Spend by category" subtitle="Top five, the rest folded into Other. Click a bar to break that month down by sub-category." onPick={pickGroup} loading={isFetching} option={stackedBars(category, mode, false)} columns={monthCols(category.keys)} rows={category.rows} />
        <ChartCard title="Essential vs discretionary" onPick={pickFrom(necessity, "necessity")} loading={isFetching} option={stackedBars(necessity, mode, true)} columns={monthCols(necessity.keys)} rows={necessity.rows} />
      </div>
      {picked && (
        <ChartCard
          title={`${picked.group.replace("Expenses:", "")} in ${monthLabel(picked.month)}`}
          subtitle={`${money(monthTotal, ccy, 0)}, by sub-category. Click a slice to see its transactions.`}
          onPick={(e) => monthParts[e.dataIndex] && open({ month: picked.month, category: monthParts[e.dataIndex].account })}
          option={donut(monthParts, mode, ccy)}
          right={<button onClick={() => setPicked(null)} className="rounded-md border border-hairline px-2 py-1 text-xs text-ink-2 hover:bg-hairline">Close</button>}
          columns={partCols(picked.month)}
          rows={monthParts}
        />
      )}
      {chosen && (
        <ChartCard
          title={`${chosen.replace("Expenses:", "")} over ${rangeText}`}
          subtitle={`${money(partsTotal, ccy, 0)} in all, by sub-category. Pick a category; click a slice to see its transactions.`}
          onPick={(e) => parts[e.dataIndex] && open({ category: parts[e.dataIndex].account })}
          loading={isFetching}
          option={donut(parts, mode, ccy)}
          right={
            <div className="flex flex-wrap gap-1">
              {groups.map((g) => (
                <button key={g} onClick={() => setGroup(g)} aria-pressed={g === chosen} className={`rounded-md border border-hairline px-2 py-1 text-xs ${g === chosen ? "bg-ink text-surface" : "text-ink-2 hover:bg-hairline"}`}>{g.replace("Expenses:", "")}</button>
              ))}
            </div>
          }
          columns={partCols()}
          rows={parts}
        />
      )}
      {data.alerts.length > 0 && (
        <Card>
          <CardHeader title="Worth a look" subtitle="Detected from your recurring payments" />
          <ul className="divide-y divide-hairline text-sm">
            {data.alerts.map((a, i) => (
              <li key={i} className="flex items-center gap-3 py-2">
                <StatusBadge kind={a.kind === "price_change" ? "warn" : a.kind === "lapsed" ? "muted" : "good"}>{ALERT_LABEL[a.kind]}</StatusBadge>
                <span className="text-ink">{alertText(a, ccy)}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}
      <Card>
        <CardHeader title="Recurring payments" subtitle={`${recurring.length} paid in ${rangeText}`} />
        <DataTable columns={recurringCols} rows={recurring} />
      </Card>
      <Card>
        <CardHeader title="Major purchases" subtitle={`Kept out of monthly spend, ${rangeText}`} />
        <DataTable
          columns={[
            { key: "date", label: "Date", render: (r) => dateLabel(r.date) },
            { key: "payee", label: "Payee" },
            { key: "narration", label: "What" },
            { key: "amount", label: ccy, align: "right", render: (r) => money(r.amount, ccy, 0) },
          ]}
          rows={major}
        />
        {major.length > 0 && <p className="mt-2 text-right text-xs text-muted">Total {signedMoney(major.reduce((s, r) => s + n(r.amount), 0), ccy).replace("+", "")}</p>}
      </Card>
    </div>
  );
}
