import { useMemo } from "react";
import { NetWorth as NetWorthData, useApi } from "../lib/api";
import { dateLabel, money, n, pct, signedMoney } from "../lib/format";
import { netWorthChart } from "../lib/charts";
import { useMode } from "../lib/theme";
import { ChartCard } from "../components/Chart";
import { Card, CardHeader, Column, DataTable, StatTile, StatusBadge } from "../components/ui";

const TILES = ["Cash", "Investments", "Pension", "Property", "Vehicles"];

export default function NetWorth({ ccy }: { ccy: string }) {
  const { data, isFetching } = useApi<NetWorthData>("/api/networth");
  const mode = useMode();
  const option = useMemo(() => data && netWorthChart(data.rows, mode), [data, mode]);
  if (!data || !option) return <p className="text-sm text-muted">Loading…</p>;
  const last = data.rows[data.rows.length - 1];
  const prev = data.rows[data.rows.length - 2];
  const shown = TILES.filter((g) => data.rows.some((r) => n(r[g]) !== 0));
  const fx = data.fixed;
  const assetCols: Column<(typeof fx.assets)[number]>[] = [
    { key: "name", label: "Asset", render: (a) => <span>{a.name}<span className="ml-2 text-xs text-muted">{a.account.replace("Assets:", "")}</span></span> },
    { key: "value", label: `Value (${ccy})`, align: "right", render: (a) => money(a.value, ccy, 0) },
    { key: "cost", label: "Paid", align: "right", render: (a) => money(a.cost, ccy, 0) },
    { key: "unrealized", label: "Since purchase", align: "right", render: (a) => <span className={n(a.unrealized) >= 0 ? "text-good" : "text-bad"}>{n(a.unrealized) >= 0 ? "▲" : "▼"} {money(Math.abs(n(a.unrealized)), ccy, 0)}</span> },
    { key: "valued_on", label: "Your valuation", render: (a) => (a.valued_on ? <span>{dateLabel(a.valued_on)}{a.stale && <span className="ml-2"><StatusBadge kind="warn">over a year old</StatusBadge></span>}</span> : "–") },
  ];
  return (
    <div className="space-y-6">
      <Card>
        <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(150px,1fr))]">
          <StatTile label="Net worth" value={money(last.total, ccy, 0)} delta={{ text: `${signedMoney(n(last.total) - n(prev.total), ccy)} since ${dateLabel(prev.date)}`, good: n(last.total) >= n(prev.total) }} />
          {shown.map((g) => (
            <StatTile key={g} label={g} value={money(last[g], ccy, 0)} delta={{ text: signedMoney(n(last[g]) - n(prev[g]), ccy), good: n(last[g]) >= n(prev[g]) }} />
          ))}
          <StatTile label="Liabilities" value={money(last.Liabilities, ccy, 0)} hint="mortgage and card" />
        </div>
      </Card>
      <ChartCard
        title="Net worth"
        subtitle={`Assets stack above zero, debts below it, the dark line is the net. Market value, ${ccy}.`}
        loading={isFetching}
        option={option}
        height={360}
        columns={[
          { key: "date", label: "Month end", render: (r) => dateLabel(r.date) },
          ...data.groups.filter((g) => data.rows.some((r) => n(r[g]) !== 0)).map((g) => ({ key: g, label: g, align: "right" as const, render: (r: Record<string, string>) => money(r[g], ccy, 0) })),
          { key: "total", label: "Net worth", align: "right" as const, render: (r: Record<string, string>) => money(r.total, ccy, 0) },
        ]}
        rows={data.rows}
      />
      {fx.assets.length > 0 && (
        <Card>
          <CardHeader title="Property and vehicles" subtitle="Valued by your own estimates: add a price line to the ledger to update one." />
          <DataTable columns={assetCols} rows={fx.assets} />
          {fx.mortgages.length > 0 && (
            <div className="mt-4 grid gap-4 border-t border-hairline pt-4 [grid-template-columns:repeat(auto-fit,minmax(150px,1fr))]">
              <StatTile label="Mortgage" value={money(fx.mortgage_balance, ccy, 0)} />
              <StatTile label="Home equity" value={money(fx.home_equity, ccy, 0)} hint="home value minus mortgage" />
              <StatTile label="Loan to value" value={pct(fx.loan_to_value)} hint="mortgage as a share of the home" />
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
