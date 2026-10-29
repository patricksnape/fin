import { useMemo } from "react";
import { AllocRow, Allocation as AllocationData, Holding, useApi } from "../lib/api";
import { money, n, pct } from "../lib/format";
import { bars } from "../lib/charts";
import { useMode } from "../lib/theme";
import { ChartCard } from "../components/Chart";
import { Card, CardHeader, Column, DataTable, StatTile, Loading } from "../components/ui";

const label = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

export default function Allocation({ ccy }: { ccy: string }) {
  const { data, isFetching } = useApi<AllocationData>("/api/allocation");
  const mode = useMode();
  const classOption = useMemo(() => {
    if (!data) return null;
    const rows = data.by_asset_class;
    const series = [{ name: "Actual", values: rows.map((r) => n(r.weight) * 100), slot: 0 }];
    if (rows.some((r) => r.target != null)) series.push({ name: "Target", values: rows.map((r) => n(r.target) * 100), slot: 1 });
    return bars(rows.map((r) => label(r.asset_class!)), series, mode, "%");
  }, [data, mode]);
  const ccyOption = useMemo(() => data && bars(data.by_currency.map((r) => r.currency!), [{ name: "Exposure", values: data.by_currency.map((r) => n(r.weight) * 100), slot: 0 }], mode, "%"), [data, mode]);
  if (!data || !classOption || !ccyOption) return <Loading what="Loading holdings" />;

  const total = data.holdings.reduce((s, h) => s + n(h.value), 0);
  const cols = (key: "asset_class" | "currency"): Column<AllocRow>[] => [
    { key, label: key === "currency" ? "Currency" : "Class", render: (r) => label(String(r[key])) },
    ...(key === "currency" ? [{ key: "native_value", label: "In its own currency", align: "right" as const, render: (r: AllocRow) => (r.native_value ? money(r.native_value, String(r.currency), 0) : "–") }] : []),
    { key: "value", label: ccy, align: "right", render: (r) => money(r.value, ccy, 0) },
    { key: "weight", label: "Weight", align: "right", render: (r) => pct(r.weight) },
    { key: "target", label: "Target", align: "right", render: (r) => (r.target != null ? pct(r.target, 0) : "–") },
    { key: "drift", label: "Drift", align: "right", render: (r) => (r.drift != null ? `${n(r.drift) > 0 ? "+" : ""}${(n(r.drift) * 100).toFixed(1)} pts` : "–") },
  ];
  const holdingCols: Column<Holding>[] = [
    { key: "name", label: "Holding", render: (h) => <span>{h.name}<span className="ml-2 text-xs text-muted">{h.commodity}</span></span> },
    { key: "asset_class", label: "Class", render: (h) => label(h.asset_class) },
    { key: "units", label: "Units", align: "right", render: (h) => Number(h.units).toLocaleString("de-CH", { maximumFractionDigits: 2 }) },
    { key: "price", label: "Price", align: "right", render: (h) => (h.price ? money(h.price, h.currency, 2) : "–") },
    { key: "native_value", label: "Value, own currency", align: "right", render: (h) => money(h.native_value, h.currency, 0) },
    { key: "value", label: `Value, ${ccy}`, align: "right", render: (h) => money(h.value, ccy, 0) },
    {
      key: "unrealized",
      label: "Unrealized",
      align: "right",
      render: (h) =>
        h.unrealized == null ? "–" : <span className={n(h.unrealized) >= 0 ? "text-good" : "text-bad"}>{n(h.unrealized) >= 0 ? "▲" : "▼"} {money(Math.abs(n(h.unrealized)), h.currency, 0)}</span>,
    },
    { key: "weight", label: "Weight", align: "right", render: (h) => pct(h.weight) },
  ];
  return (
    <div className="space-y-6">
      <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(165px,1fr))]">
        <Card><StatTile label="Investable assets" value={money(total, ccy, 0)} hint="cash and investments, excl. pension" /></Card>
        {data.employer && <Card><StatTile label={`${data.employer.commodity} concentration`} value={pct(data.employer.weight)} hint="employer stock share of the total" /></Card>}
      </div>
      <div className="grid gap-6 xl:grid-cols-2">
        <ChartCard title="Asset class vs target" subtitle="Share of investable assets" loading={isFetching} option={classOption} columns={cols("asset_class")} rows={data.by_asset_class} />
        <ChartCard title="Currency exposure" subtitle="Share by the currency each holding is priced in" loading={isFetching} option={ccyOption} columns={cols("currency")} rows={data.by_currency} />
      </div>
      <Card>
        <CardHeader title="Holdings" subtitle="Unrealized gain in each holding's own currency" />
        <DataTable columns={holdingCols} rows={data.holdings} />
      </Card>
    </div>
  );
}
