import { useMemo, useState } from "react";
import { Alert, Series, Spending as SpendingData, useApi } from "../lib/api";
import { dateLabel, money, monthLabel, n, signedMoney } from "../lib/format";
import { Part, breakdown, donut, foldTail, sliceMonths, stackedBars } from "../lib/charts";
import { useMode } from "../lib/theme";
import { go } from "../lib/route";
import { ChartCard } from "../components/Chart";
import { Card, CardHeader, Column, DataTable, Segmented, StatTile, StatusBadge } from "../components/ui";

type View = "run_rate" | "cash";

function alertText(a: Alert, ccy: string): string {
  if (a.kind === "price_change") return `${a.payee} went from ${money(a.old, ccy)} to ${money(a.new, ccy)}`;
  if (a.kind === "lapsed") return `${a.payee} stopped after ${dateLabel(a.last_date!)} (${a.days_overdue} days overdue)`;
  return `${a.payee}: ${a.detail}`;
}
const ALERT_LABEL = { price_change: "Price change", lapsed: "Stopped", new: "To confirm" } as const;

export default function Spending({ ccy }: { ccy: string }) {
  const [view, setView] = useState<View>("run_rate");
  const [months, setMonths] = useState(12);
  const [group, setGroup] = useState<string | null>(null);
  const [picked, setPicked] = useState<{ month: string; group: string } | null>(null);
  const { data, isFetching } = useApi<SpendingData>(`/api/spending?view=${view}&months=240`);
  const mode = useMode();
  const cadence = useMemo(() => data && sliceMonths(data.by_cadence, months), [data, months]);
  const necessity = useMemo(() => data && sliceMonths(data.by_necessity, months), [data, months]);
  const category = useMemo(() => data && foldTail(sliceMonths(data.by_category, months)), [data, months]);
  const accounts = useMemo(() => data && sliceMonths(data.by_account, months), [data, months]);
  // major purchases follow the range shown: only those from the first displayed month on
  const from = accounts?.rows[0]?.month;
  const recurring = useMemo(() => (data ? data.recurring.filter((r) => !from || r.last_date.slice(0, 7) >= from) : []), [data, from]);
  const major = useMemo(() => (data ? data.major.filter((r) => !from || r.date.slice(0, 7) >= from) : []), [data, from]);
  if (!data || !cadence || !necessity || !category || !accounts) return <p className="text-sm text-muted">Loading…</p>;

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
  const h = data.headline;
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
        <Segmented label="Range" value={String(months) as "6" | "12" | "24"} onChange={(v) => setMonths(Number(v))} options={[{ value: "6", label: "6 months" }, { value: "12", label: "12 months" }, { value: "24", label: "24 months" }]} />
        <p className="text-xs text-muted">{note}</p>
      </div>
      <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(165px,1fr))]">
        <Card><StatTile label="Core monthly spend" value={money(h.core_monthly_spend, ccy, 0)} hint="last 3 full months, average" /></Card>
        <Card><StatTile label="Essential baseline" value={money(h.essential_baseline, ccy, 0)} hint="minimum monthly need" /></Card>
        <Card><StatTile label="Discretionary" value={money(h.discretionary, ccy, 0)} hint="average per month" /></Card>
        <Card><StatTile label="Major purchases this year" value={money(h.major_this_year, ccy, 0)} hint="not in monthly spend" /></Card>
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
          title={`${chosen.replace("Expenses:", "")} over the last ${months} months`}
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
        <CardHeader title="Recurring payments" subtitle={`${recurring.length} paid in the last ${months} months`} />
        <DataTable columns={recurringCols} rows={recurring} />
      </Card>
      <Card>
        <CardHeader title="Major purchases" subtitle={`Kept out of monthly spend, last ${months} months`} />
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
