import { useMemo, useState } from "react";
import { ShareAwards, ShareEvent, ShareSeries, useApi } from "../lib/api";
import { dateLabel, money, n, pct } from "../lib/format";
import { bars } from "../lib/charts";
import { useMode } from "../lib/theme";
import { ChartCard } from "../components/Chart";
import { Card, CardHeader, Loading, Segmented, StatTile } from "../components/ui";

type Cur = "chf" | "usd";
type SortKey = "day" | "award" | "net" | "price" | "start_price" | "growth" | "sold" | "left" | "realised" | "unrealised";

const PROGRAM = { rsu: "RSU", espp: "ESPP" } as const;
const TILES = "grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(165px,1fr))]";
const sign = (v: number) => (v > 0 ? "text-good" : v < 0 ? "text-bad" : "text-ink");

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-2">
      <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">{title}</h2>
      <div className={TILES}>{children}</div>
    </section>
  );
}

/** Share awards: what each vest (or ESPP purchase) cost, what it did since, the growth between the award and the
 *  vest, and what holding or selling at the vest would have meant. One tab per stock and program. */
export default function Rsu({ ccy }: { ccy: string }) {
  const { data, isFetching } = useApi<ShareAwards>("/api/rsu");
  const mode = useMode();
  const [pick, setPick] = useState(0);
  const [cur, setCur] = useState<Cur>("chf");
  const [sort, setSort] = useState<{ key: SortKey; asc: boolean }>({ key: "day", asc: false });
  const series: ShareSeries[] = data && data.ready ? data.series : [];
  const s = series[Math.min(pick, Math.max(0, series.length - 1))];
  const unit = cur === "chf" ? ccy : s?.currency ?? "USD";
  const f = (e: { [k: string]: any }, base: string) => n(e[`${base}_${cur}`]);

  const rows = useMemo(() => {
    if (!s) return [];
    const value = (e: ShareEvent): number | string => {
      switch (sort.key) {
        case "day": return e.day;
        case "award": return e.start ?? "";
        case "net": return n(e.net);
        case "price": return n(e.price);
        case "start_price": return e.start_price ? n(e.start_price) : -1;
        case "growth": return e.growth ? n(e.growth) : -1e9;
        case "sold": return n(e.sold);
        case "left": return n(e.left);
        case "realised": return n(e[`realised_${cur}` as "realised_usd"]);
        case "unrealised": return n(e[`unrealised_${cur}` as "unrealised_usd"]);
      }
    };
    return [...s.events].sort((a, b) => (value(a) < value(b) ? -1 : value(a) > value(b) ? 1 : 0) * (sort.asc ? 1 : -1));
  }, [s, sort, cur]);

  const charts = useMemo(() => {
    if (!s) return null;
    const graded = s.awards.filter((a) => a.start_price && a.avg_price);
    const award = bars(
      graded.map((a) => dateLabel(a.start ?? "").replace(/^\d+ /, "")),
      [
        { name: "Price on the award date", values: graded.map((a) => n(a.start_price)), slot: 0 },
        { name: "Average price at vest", values: graded.map((a) => n(a.avg_price)), slot: 2 },
      ],
      mode,
      s.currency,
    );
    const byDay = new Map<string, { sell: number; actual: number; hold: number }>();
    for (const e of s.events) {
      const k = e.day;
      const cur0 = byDay.get(k) ?? { sell: 0, actual: 0, hold: 0 };
      cur0.sell += f(e, "sell_at_event");
      cur0.actual += f(e, "actual");
      cur0.hold += e[`hold_all_${cur}` as "hold_all_usd"] ? n(e[`hold_all_${cur}` as "hold_all_usd"]) : 0;
      byDay.set(k, cur0);
    }
    const days = [...byDay.keys()].sort();
    const held = bars(
      days.map((d) => dateLabel(d).replace(/^\d+ /, "")),
      [
        { name: "Actual, against selling at the vest", values: days.map((d) => Math.round(byDay.get(d)!.actual - byDay.get(d)!.sell)), slot: 0 },
        { name: "Holding everything, against selling at the vest", values: days.map((d) => Math.round(byDay.get(d)!.hold - byDay.get(d)!.sell)), slot: 3 },
      ],
      mode,
      unit,
    );
    const sized = s.awards.filter((a) => a.award_value);
    const worth = sized.length
      ? bars(
          sized.map((a) => dateLabel(a.start ?? "").replace(/^\d+ /, "")),
          [
            { name: "Value when awarded", values: sized.map((a) => n(a.award_value)), slot: 0 },
            { name: "Worth now (vested at vest price, the rest at the latest price)", values: sized.map((a) => n(a.worth_now)), slot: 2 },
          ],
          mode,
          s.currency,
        )
      : null;
    // the shares that have vested, award by award: what they were worth when awarded, at vest, what was kept for tax,
    // what arrived, and what that turned into (sales after fees plus what is still held, at the latest price)
    const by = new Map<string, { start: string; gross: number; atAward: number; atVest: number; tax: number; received: number; now: number }>();
    for (const e of s.events) {
      if (!e.start || !e.start_price) continue;
      const g = by.get(e.start) ?? { start: e.start, gross: 0, atAward: 0, atVest: 0, tax: 0, received: 0, now: 0 };
      g.gross += n(e.gross);
      g.atAward += n(e.gross) * n(e.start_price);
      g.atVest += n(e.gross) * n(e.price);
      g.tax += n(e.withheld) * n(e.price);
      g.received += n(e.net) * n(e.price);
      g.now += n(e.actual_usd);
      by.set(e.start, g);
    }
    const vested = [...by.values()].sort((a, b) => (a.start < b.start ? -1 : 1));
    const vestedChart = vested.length
      ? bars(
          vested.map((v) => dateLabel(v.start).replace(/^\d+ /, "")),
          [
            { name: "Value when awarded", values: vested.map((v) => Math.round(v.atAward)), slot: 0 },
            { name: "Value at vest", values: vested.map((v) => Math.round(v.atVest)), slot: 2 },
            { name: "What you have now (after tax kept back, sales and fees)", values: vested.map((v) => Math.round(v.now)), slot: 3 },
          ],
          mode,
          s.currency,
        )
      : null;
    return { award, held, days, byDay, worth, sized, vested, vestedChart };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [s, mode, cur]);

  if (!data) return <Loading what="Computing share awards" />;
  if (!data.ready) return <Card><CardHeader title="Share awards" subtitle={data.reason} /></Card>;
  if (!s || !charts) return <Loading what="Computing share awards" />;
  const sm = s.summary;
  const real = n(sm[`realised_${cur}`]);
  const unreal = n(sm[`unrealised_${cur}`]);
  const sellAt = n(sm[`sell_at_event_${cur}`]);
  const actual = n(sm[`actual_${cur}`]);
  const holdAll = n(sm[`hold_all_${cur}`]);
  const head = (key: SortKey, label: string, right = false) => (
    <th className={`px-2 py-2 font-medium ${right ? "text-right" : ""}`} aria-sort={sort.key === key ? (sort.asc ? "ascending" : "descending") : "none"}>
      <button onClick={() => setSort(sort.key === key ? { key, asc: !sort.asc } : { key, asc: true })} className="hover:text-ink">
        {label}{sort.key === key ? (sort.asc ? " ▲" : " ▼") : ""}
      </button>
    </th>
  );
  const label = (x: ShareSeries) => `${x.commodity} ${PROGRAM[x.program]}`;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        {series.length > 1 && <Segmented label="Stock and program" value={String(Math.min(pick, series.length - 1))} onChange={(v) => setPick(Number(v))} options={series.map((x, i) => ({ value: String(i), label: label(x) }))} />}
        <Segmented label="Currency" value={cur} onChange={(v) => setCur(v as Cur)} options={[{ value: "chf", label: ccy }, { value: "usd", label: s.currency }]} />
        <p className="text-xs text-muted">
          {label(s)}, {s.events.length} {s.program === "rsu" ? "vests" : "purchases"} through {dateLabel(data.as_of)}.
          {s.price_now && ` Latest price ${money(s.price_now, s.currency, 2)}.`}
        </p>
      </div>
      {isFetching && <Loading what="Updating" />}
      {(sm.missing_start_price > 0 || sm.missing_start_date > 0) && (
        <p className="rounded-lg border border-hairline px-3 py-2 text-xs text-ink-2">
          {sm.missing_start_price > 0 && <>{sm.missing_start_price} of {sm.events} have no price on their {s.program === "rsu" ? "award" : "offering start"} date, so the growth before the {s.program === "rsu" ? "vest" : "purchase"} is left out for them. {s.price_history === "ledger" ? <>Prices come from the ledger only. A daily history file at <code>sources/market-history/{s.commodity}.csv</code> in the data repo fills the gap.</> : <>The history file does not reach back that far.</>} </>}
          {sm.missing_start_date > 0 && <>{sm.missing_start_date} have no award date recorded (run <code>fin rsu backfill</code>).</>}
        </p>
      )}

      <Section title="Shares">
        <Card><StatTile label={s.program === "rsu" ? "Vested, gross" : "Bought"} value={n(sm.gross).toLocaleString("de-CH", { maximumFractionDigits: 0 })} hint={`${sm.events} ${s.program === "rsu" ? "vests" : "purchases"}`} /></Card>
        {s.program === "rsu" && <Card><StatTile label="Kept for tax" value={n(sm.withheld).toLocaleString("de-CH", { maximumFractionDigits: 0 })} hint="shares not delivered" /></Card>}
        <Card><StatTile label="Received" value={n(sm.net).toLocaleString("de-CH", { maximumFractionDigits: 0 })} hint="shares that arrived" /></Card>
        <Card><StatTile label="Sold" value={n(sm.sold).toLocaleString("de-CH", { maximumFractionDigits: 0 })} hint={`${n(sm.left).toLocaleString("de-CH", { maximumFractionDigits: 0 })} still held`} /></Card>
      </Section>

      <Section title={`Results in ${unit}`}>
        <Card><StatTile label="Realised" value={money(real, unit, 0)} hint="sales, after fees" /></Card>
        <Card><StatTile label="Unrealised" value={money(unreal, unit, 0)} hint="what is left, at the latest price" /></Card>
        <Card><StatTile label="Total" value={money(real + unreal, unit, 0)} hint={cur === "chf" ? `of which exchange rate ${money(n(sm.realised_fx_chf) + n(sm.unrealised_fx_chf), unit, 0)}` : "against the price at each vest"} /></Card>
        <Card><StatTile label="Cost" value={money(n(sm[`cost_${cur}`]), unit, 0)} hint="value of the shares when they arrived" /></Card>
      </Section>

      <Section title={s.program === "rsu" ? "Before the vest" : "Before the purchase"}>
        <Card><StatTile label={s.program === "rsu" ? "Award to vest" : "Offering to purchase"} value={sm.growth ? pct(sm.growth, 0) : "–"} hint={sm.growth ? `share-weighted, ${sm.growth_events} of ${sm.events}` : "no price on the start dates"} /></Card>
        <Card><StatTile label="Value gained" value={cur === "chf" ? (sm.growth_gain_chf ? money(sm.growth_gain_chf, ccy, 0) : "–") : sm.growth_gain_usd ? money(sm.growth_gain_usd, s.currency, 0) : "–"} hint="between the start date and the vest" /></Card>
        {s.program === "espp" && <Card><StatTile label="Discount" value={money(sm.discount_usd, s.currency, 0)} hint="below the market price at purchase" /></Card>}
      </Section>

      {charts.vested.length > 0 && (() => {
        const t = charts.vested.reduce((a, v) => ({ gross: a.gross + v.gross, atAward: a.atAward + v.atAward, atVest: a.atVest + v.atVest, tax: a.tax + v.tax, received: a.received + v.received, now: a.now + v.now }), { gross: 0, atAward: 0, atVest: 0, tax: 0, received: 0, now: 0 });
        return (
          <Section title={`Vested shares only, in ${s.currency}`}>
            <Card><StatTile label="Value when awarded" value={money(t.atAward, s.currency, 0)} hint={`${t.gross.toLocaleString("de-CH", { maximumFractionDigits: 0 })} vested shares at their award-date price`} /></Card>
            <Card><StatTile label="Value at vest" value={money(t.atVest, s.currency, 0)} hint={`${pct(t.atVest / t.atAward - 1, 0)} on the award, before tax`} /></Card>
            <Card><StatTile label="Kept back for tax" value={money(t.tax, s.currency, 0)} hint={`${pct(t.tax / t.atVest, 0)} of the vest value, in shares`} /></Card>
            <Card><StatTile label="Received" value={money(t.received, s.currency, 0)} hint="net shares at their vest price" /></Card>
            <Card><StatTile label="What you have now" value={money(t.now, s.currency, 0)} hint="sales after fees plus shares still held, at the latest price" /></Card>
            <Card><StatTile label="Against the award" value={pct(t.now / t.atAward - 1, 0)} hint={`after tax kept back; ${money(t.now - t.received, s.currency, 0)} since the vests`} /></Card>
          </Section>
        );
      })()}

      {charts.sized.length > 0 && (() => {
        const sum = (k: "award_value" | "vested_value" | "unvested_value" | "worth_now") => charts.sized.reduce((t, a) => t + n(a[k]), 0);
        const granted = charts.sized.reduce((t, a) => t + n(a.granted), 0);
        const unvested = charts.sized.reduce((t, a) => t + n(a.unvested), 0);
        return (
          <Section title="Awarded against worth now">
            <Card><StatTile label="Shares awarded" value={granted.toLocaleString("de-CH", { maximumFractionDigits: 0 })} hint={`${unvested.toLocaleString("de-CH", { maximumFractionDigits: 0 })} still to vest`} /></Card>
            <Card><StatTile label="Value when awarded" value={money(sum("award_value"), s.currency, 0)} hint="shares awarded at the price of the award date" /></Card>
            <Card><StatTile label="Worth now" value={money(sum("worth_now"), s.currency, 0)} hint={`vested ${money(sum("vested_value"), s.currency, 0)} at vest, to vest ${money(sum("unvested_value"), s.currency, 0)} at the latest price`} /></Card>
            <Card><StatTile label="Against the award" value={sum("award_value") ? pct(sum("worth_now") / sum("award_value") - 1, 0) : "–"} hint="before tax kept back at vest" /></Card>
          </Section>
        );
      })()}

      <Section title="Held against sold at the vest">
        <Card><StatTile label="Selling at each vest" value={money(sellAt, unit, 0)} hint="net shares at the vest price" /></Card>
        <Card><StatTile label="What happened" value={money(actual, unit, 0)} hint="sales after fees plus what is left" /></Card>
        <Card><StatTile label="Holding all of it" value={money(holdAll, unit, 0)} hint="net shares at the latest price" /></Card>
        <Card><StatTile label="Your sales against holding" value={money(actual - holdAll, unit, 0)} hint="negative: selling cost you" /></Card>
      </Section>

      <ChartCard
        title={s.program === "rsu" ? "Award to vest" : "Offering to purchase"}
        subtitle="The share price when each award was granted, and what its shares were worth when they vested (weighted by shares)"
        loading={isFetching}
        option={charts.award}
        columns={[{ key: "label", label: "Award" }, { key: "start", label: "Price then", align: "right" }, { key: "avg", label: "Price at vest", align: "right" }, { key: "growth", label: "Growth", align: "right" }, { key: "gain", label: `Gained, ${unit}`, align: "right" }]}
        rows={s.awards.map((a) => ({ label: `${a.start ? dateLabel(a.start) : "?"} (${a.events})`, start: a.start_price ? money(a.start_price, s.currency, 2) : "–", avg: a.avg_price ? money(a.avg_price, s.currency, 2) : "–", growth: a.growth ? pct(a.growth, 0) : "–", gain: a.gain_usd ? money(cur === "chf" ? a.gain_chf ?? 0 : a.gain_usd, unit, 0) : "–" }))}
      />
      {charts.vestedChart && (
        <ChartCard
          title="Vested shares: awarded, vested, and what you have now"
          subtitle={`In ${s.currency}, by award, for the shares that have vested so far. The last bar is after the shares kept back for tax, and includes sales and what you still hold`}
          loading={isFetching}
          option={charts.vestedChart}
          columns={[{ key: "label", label: "Award" }, { key: "shares", label: "Vested", align: "right" }, { key: "award", label: "At award", align: "right" }, { key: "vest", label: "At vest", align: "right" }, { key: "tax", label: "Kept for tax", align: "right" }, { key: "now", label: "You have now", align: "right" }, { key: "vs", label: "Against award", align: "right" }]}
          rows={charts.vested.map((v) => ({ label: dateLabel(v.start), shares: v.gross.toLocaleString("de-CH", { maximumFractionDigits: 0 }), award: money(v.atAward, s.currency, 0), vest: money(v.atVest, s.currency, 0), tax: money(v.tax, s.currency, 0), now: money(v.now, s.currency, 0), vs: pct(v.now / v.atAward - 1, 0) }))}
        />
      )}
      {charts.worth && (
        <ChartCard
          title="Awarded against worth now"
          subtitle={`In ${s.currency}, by award: what the shares awarded were worth on the award date against what they are worth now`}
          loading={isFetching}
          option={charts.worth}
          columns={[{ key: "label", label: "Award" }, { key: "granted", label: "Granted", align: "right" }, { key: "unvested", label: "To vest", align: "right" }, { key: "award", label: "At award", align: "right" }, { key: "now", label: "Worth now", align: "right" }, { key: "vs", label: "Against", align: "right" }]}
          rows={charts.sized.map((a) => ({ label: a.start ? dateLabel(a.start) : "?", granted: n(a.granted).toLocaleString("de-CH", { maximumFractionDigits: 0 }), unvested: n(a.unvested).toLocaleString("de-CH", { maximumFractionDigits: 0 }), award: money(a.award_value, s.currency, 0), now: money(a.worth_now, s.currency, 0), vs: a.vs_award ? pct(a.vs_award, 0) : "–" }))}
        />
      )}
      <ChartCard
        title="Held against sold, by vest date"
        subtitle={`In ${unit}: what each vest day turned out to be worth, against selling the shares the day they arrived`}
        loading={isFetching}
        option={charts.held}
        columns={[{ key: "day", label: "Vest" }, { key: "a", label: "Actual vs selling", align: "right" }, { key: "h", label: "Holding all vs selling", align: "right" }]}
        rows={charts.days.map((d) => ({ day: dateLabel(d), a: money(charts.byDay.get(d)!.actual - charts.byDay.get(d)!.sell, unit, 0), h: money(charts.byDay.get(d)!.hold - charts.byDay.get(d)!.sell, unit, 0) }))}
      />

      <Card>
        <CardHeader title={s.program === "rsu" ? "Every vest" : "Every purchase"} subtitle={`Results in ${unit}. Click a heading to sort.`} />
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-hairline text-left text-xs text-muted">
                {head("day", s.program === "rsu" ? "Vest" : "Purchase")}
                {head("award", s.program === "rsu" ? "Award" : "Offering")}
                {head("net", "Shares", true)}
                {head("price", "Price", true)}
                {head("start_price", "Price then", true)}
                {head("growth", "Growth", true)}
                {head("sold", "Sold", true)}
                {head("left", "Left", true)}
                {head("realised", "Realised", true)}
                {head("unrealised", "Unrealised", true)}
              </tr>
            </thead>
            <tbody>
              {rows.map((e) => {
                const r = n(e[`realised_${cur}` as "realised_usd"]);
                const u = n(e[`unrealised_${cur}` as "unrealised_usd"]);
                return (
                  <tr key={e.ref} className="border-b border-hairline last:border-0">
                    <td className="tnum whitespace-nowrap px-2 py-2 text-ink">{dateLabel(e.day)}</td>
                    <td className="tnum whitespace-nowrap px-2 py-2 text-ink-2">{e.start ? dateLabel(e.start) : "–"}</td>
                    <td className="tnum px-2 py-2 text-right text-ink">{n(e.net).toLocaleString("de-CH", { maximumFractionDigits: 0 })}</td>
                    <td className="tnum px-2 py-2 text-right text-ink">{money(e.price, s.currency, 2)}</td>
                    <td className="tnum px-2 py-2 text-right text-ink-2">{e.start_price ? money(e.start_price, s.currency, 2) : "–"}</td>
                    <td className={`tnum px-2 py-2 text-right ${e.growth ? sign(n(e.growth)) : "text-muted"}`}>{e.growth ? pct(e.growth, 0) : "–"}</td>
                    <td className="tnum px-2 py-2 text-right text-ink-2">{n(e.sold).toLocaleString("de-CH", { maximumFractionDigits: 0 })}</td>
                    <td className="tnum px-2 py-2 text-right text-ink-2">{n(e.left).toLocaleString("de-CH", { maximumFractionDigits: 0 })}</td>
                    <td className={`tnum whitespace-nowrap px-2 py-2 text-right ${sign(r)}`}>{n(e.sold) > 0 ? money(r, unit, 0) : "–"}</td>
                    <td className={`tnum whitespace-nowrap px-2 py-2 text-right ${sign(u)}`}>{n(e.left) > 0 ? money(u, unit, 0) : "–"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <Card>
        <CardHeader title="How this is worked out" />
        <ul className="list-disc space-y-1 pl-5 text-sm text-ink-2">
          <li>Cost is the market price on the day the shares arrived. Shares kept back for tax are not counted as received; their value was income and tax at the vest.</li>
          <li>Realised results are after broker fees. Francs are converted at the rate on the day of the vest and of each sale, so the exchange-rate part is shown apart from the price part.</li>
          <li>Unrealised results use the latest price, and the rate today against the rate at the vest.</li>
          <li>The price on an award date is a closing price near it, a stand-in for the grant value (set per stock in the data repo's rules/rsu.yaml: on the day, or the day before, which is how the vest prices of this plan are defined); it is left out when no price is near that date.</li>
          <li>Sales are matched to a vest by the shares' cost date and price, which survive a move between brokers; vests on one day at one price are treated alike.</li>
        </ul>
      </Card>
    </div>
  );
}
