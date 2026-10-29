import { useMemo, useState } from "react";
import { useApi } from "../lib/api";
import { money, n, pct } from "../lib/format";
import { fanChart } from "../lib/charts";
import { useMode } from "../lib/theme";
import { ChartCard } from "./Chart";
import { Card, CardHeader, DataTable, StatTile, StatusBadge, Loading } from "./ui";

interface Backtest {
  ready: boolean; reason?: string;
  years: number; equity: string; equity_share: number; windows: number; first_start: number; last_start: number;
  success_rate: number; failed_starts: Array<{ start: number; ran_out_in: number; after_years: number }>;
  end_p10: number; end_median: number; fan: Array<{ year: number; p10: number; p50: number; p90: number }>;
  by_spend: Array<{ multiple: number; spend: number; success_rate: number | null }>;
  currency: { applies: false } | { applies: true; years_of_data: [number, number]; real_return_chf: number; real_return_usd: number; success_rate_chf: number | null; success_rate_usd: number | null };
  start: string; spend: string; usd_share: string; choices: Record<string, string>; dataset_ends: number;
}

const tone = (r: number | null) => (r == null ? "muted" : r >= 0.95 ? "good" : r >= 0.85 ? "warn" : "bad") as "good" | "warn" | "bad" | "muted";

export default function BacktestSection({ ccy }: { ccy: string }) {
  const [years, setYears] = useState("50");
  const [equity, setEquity] = useState("us");
  const [share, setShare] = useState("100");
  const mode = useMode();
  const y = Math.min(80, Math.max(1, Math.round(Number(years)) || 50));
  const s = Math.min(100, Math.max(0, Number(share.replace(",", ".")) || 0));
  const { data, isFetching } = useApi<Backtest>(`/api/fire/backtest?years=${y}&equity=${equity}&equity_share=${s / 100}`);
  const fan = useMemo(() => (data?.ready ? fanChart(data.fan, mode) : null), [data, mode]);
  if (!data) return <Loading />;
  const c = data.ready ? data.currency : null;
  const usd = data.ready ? n(data.usd_share) : 0;
  return (
    <div className="space-y-6">
      <Card>
        <CardHeader title="Would the money have lasted?" subtitle="Your assets and spend, run through every historical start year" />
        <div className="mb-4 flex flex-wrap items-end gap-4 text-xs text-ink-2">
          <label className="flex flex-col gap-1">Years it must last
            <input value={years} onChange={(e) => setYears(e.target.value)} inputMode="numeric" className="w-24 rounded-lg border border-hairline bg-surface px-2 py-1.5 text-sm text-ink" />
          </label>
          <label className="flex flex-col gap-1">Equities
            <select value={equity} onChange={(e) => setEquity(e.target.value)} className="rounded-lg border border-hairline bg-surface px-2 py-1.5 text-sm text-ink">
              {Object.entries(data.choices ?? { us: "US equity", world: "World", swiss: "Swiss" }).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-1">Share in equities (%)
            <input value={share} onChange={(e) => setShare(e.target.value)} inputMode="decimal" className="w-24 rounded-lg border border-hairline bg-surface px-2 py-1.5 text-sm text-ink" />
          </label>
        </div>
        {isFetching && <div className="mb-3"><Loading what="Running every historical start year" /></div>}
        {!data.ready ? (
          <p className="text-sm text-ink-2">{data.reason}</p>
        ) : (
          <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(165px,1fr))]">
            <StatTile hero label={`Lasted ${data.years} years`} value={pct(data.success_rate, 0)} hint={`${data.windows} start years, ${data.first_start} to ${data.last_start}`} />
            <StatTile label="Starting pot" value={money(data.start, ccy, 0)} hint="your investable assets" />
            <StatTile label="Spend a year" value={money(data.spend, ccy, 0)} hint={`${pct(n(data.spend) / n(data.start), 1)} of the pot, fixed in today's money`} />
            <StatTile label="Median after" value={money(data.end_median, ccy, 0)} hint={`weaker 10%: ${money(data.end_p10, ccy, 0)}`} />
          </div>
        )}
      </Card>

      {data.ready && c?.applies && (
        <Card className="border-accent">
          <CardHeader title="Dollar exposure" subtitle="You hold dollar assets but spend francs" />
          <p className="text-sm text-ink-2">
            <strong className="text-ink">{pct(usd, 0)}</strong> of your investments are in dollars. Between {c.years_of_data[0]} and {c.years_of_data[1]} this equity returned{" "}
            <strong className="text-ink">{pct(c.real_return_usd, 1)}</strong> a year after inflation to a dollar spender, but{" "}
            <strong className="text-ink">{pct(c.real_return_chf, 1)}</strong> to a franc spender: the franc gained on the dollar, and Swiss prices are what your spending follows.
          </p>
          <div className="mt-3 flex flex-wrap gap-6 text-sm">
            <div><div className="text-xs text-muted">Lasted {data.years} years, spending francs</div><StatusBadge kind={tone(c.success_rate_chf)}>{pct(c.success_rate_chf, 0)}</StatusBadge></div>
            <div><div className="text-xs text-muted">Same, if you spent dollars</div><StatusBadge kind={tone(c.success_rate_usd)}>{pct(c.success_rate_usd, 0)}</StatusBadge></div>
          </div>
          <p className="mt-3 text-xs text-muted">The gap between the two is what the franc/dollar rate alone did. Holding some francs, or spending part of your life in dollars, narrows it.</p>
        </Card>
      )}

      {data.ready && fan && (
        <>
          <ChartCard
            title={`The pot over ${data.years} years`}
            subtitle="Across all historical start years; in today's money"
            loading={isFetching}
            option={fan}
            columns={[{ key: "year", label: "Year" }, { key: "p10", label: "Weaker 10%", align: "right" }, { key: "p50", label: "Median", align: "right" }, { key: "p90", label: "Stronger 10%", align: "right" }]}
            rows={data.fan.filter((_, i) => i % 5 === 4).map((f) => ({ year: String(f.year), p10: money(f.p10, ccy, 0), p50: money(f.p50, ccy, 0), p90: money(f.p90, ccy, 0) }))}
          />
          <Card>
            <CardHeader title="If you spend more or less" subtitle={`Share of start years that lasted ${data.years} years`} />
            <DataTable
              columns={[
                { key: "spend", label: `Spend a year (${ccy})`, render: (r) => money(r.spend, ccy, 0) },
                { key: "multiple", label: "Versus now", render: (r) => `${Math.round(r.multiple * 100)}%` },
                { key: "success_rate", label: "Lasted", align: "right", render: (r) => pct(r.success_rate, 0) },
              ]}
              rows={data.by_spend}
            />
            {data.failed_starts.length > 0 && (
              <p className="mt-3 text-xs text-muted">
                Start years that ran out: {data.failed_starts.map((f) => `${f.start} (after ${f.after_years})`).join(", ")}.
              </p>
            )}
          </Card>
          <p className="text-xs text-muted">
            Data: Jordà-Schularick-Taylor, annual real returns to {data.dataset_ends} (the latest years, including the 2022 bond fall, are not in it). A start year counts only if all {data.years} years of data exist. This is a description of the past, not a probability. Pension and later savings are not counted, spending is fixed, and your own holdings are more concentrated than an index.
          </p>
        </>
      )}
    </div>
  );
}
