import { useMemo } from "react";
import { Overview as OverviewData, useApi } from "../lib/api";
import { dateLabel, money, monthLabel, n, pct, signedMoney } from "../lib/format";
import { singleLine } from "../lib/charts";
import { useMode } from "../lib/theme";
import { ChartCard } from "../components/Chart";
import { Card, StatTile } from "../components/ui";

export default function Overview({ ccy }: { ccy: string }) {
  const { data: o, isFetching } = useApi<OverviewData>("/api/overview");
  const mode = useMode();
  const series = o?.net_worth_series ?? [];
  const option = useMemo(
    () => singleLine(series.map((p) => dateLabel(p.date).replace(/^\d+ /, "")), series.map((p) => n(p.value)), mode, "Net worth"),
    [series, mode],
  );
  if (!o) return <p className="text-sm text-muted">Loading…</p>;
  const up = (s: string) => n(s) >= 0;
  const g = o.net_worth.by_group;
  const spend = o.spend;
  const l = o.last_full_month;
  return (
    <div className="space-y-6">
      <Card>
        <StatTile
          hero
          label="Net worth"
          value={money(o.net_worth.total, ccy, 0)}
          delta={{ text: `${signedMoney(o.net_worth.change_1m, ccy)} since last month end · ${signedMoney(o.net_worth.change_12m, ccy)} over 12 months`, good: up(o.net_worth.change_1m) }}
        />
      </Card>
      <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(165px,1fr))]">
        <Card><StatTile label="Core monthly spend" value={money(spend.core_monthly_spend, ccy, 0)} hint="3-month average, run-rate" /></Card>
        <Card><StatTile label="Essential baseline" value={money(spend.essential_baseline, ccy, 0)} hint="recurring + regular essentials" /></Card>
        <Card><StatTile label={`Savings rate · ${monthLabel(l.month.slice(0, 7))}`} value={pct(l.savings_rate)} hint={`${money(l.net, ccy, 0)} saved of ${money(l.income, ccy, 0)}`} /></Card>
        <Card><StatTile label="Major purchases this year" value={money(spend.major_this_year, ccy, 0)} hint="excluded from monthly spend" /></Card>
      </div>
      <ChartCard
        title="Net worth over time"
        subtitle={`Market value, ${ccy}`}
        loading={isFetching}
        option={option}
        height={300}
        columns={[{ key: "date", label: "Month end", render: (r) => dateLabel(r.date) }, { key: "value", label: ccy, align: "right", render: (r) => money(r.value, ccy, 0) }]}
        rows={series}
      />
      <Card>
        <h3 className="mb-3 text-sm font-semibold">By group</h3>
        <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(140px,1fr))]">
          {Object.entries(g).filter(([, v]) => n(v) !== 0).map(([k, v]) => (
            <StatTile key={k} label={k} value={money(v, ccy, 0)} />
          ))}
        </div>
      </Card>
    </div>
  );
}
