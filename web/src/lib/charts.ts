import type { Pivot } from "./api";
import { monthLabel, n } from "./format";
import { ENTITY_SLOT, Mode, TOKENS } from "./theme";
import { seriesColor } from "./chartTheme";

export interface Part { account: string; label: string; amount: number; share: number }

const MAX_STACK = 5;

function colorFor(key: string, idx: number, mode: Mode, entitySlots: boolean): string {
  if (key === "Other") return TOKENS[mode].baseline;
  return seriesColor(mode, entitySlots && key in ENTITY_SLOT ? ENTITY_SLOT[key] : idx);
}

/** Keep the top keys (already sorted by total) and fold the tail into "Other". */
export function foldTail(p: Pivot, keep = MAX_STACK): Pivot {
  if (p.keys.length <= keep + 1) return p;
  const top = p.keys.slice(0, keep);
  const rest = p.keys.slice(keep);
  const rows = p.rows.map((r) => {
    const other = rest.reduce((s, k) => s + n(r[k]), 0);
    return { ...r, Other: String(other) };
  });
  return { keys: [...top, "Other"], rows };
}

export function sliceMonths(p: Pivot, months: number): Pivot {
  return { ...p, rows: p.rows.slice(-months) };
}

export function stackedBars(p: Pivot, mode: Mode, entitySlots: boolean): any {
  return {
    xAxis: { type: "category", data: p.rows.map((r) => monthLabel(r.month)) },
    yAxis: { type: "value", name: "CHF" },
    series: p.keys.map((k, i) => ({
      name: k.replace("Expenses:", "").replace("_", "-"),
      type: "bar",
      stack: "total",
      itemStyle: { color: colorFor(k, i, mode, entitySlots) },
      data: p.rows.map((r) => n(r[k])),
    })),
  };
}

export function stackedAreas(rows: Array<Record<string, string>>, keys: string[], mode: Mode): any {
  return {
    xAxis: { type: "category", data: rows.map((r) => r.date.slice(0, 7)).map(monthLabel), boundaryGap: false },
    yAxis: { type: "value", name: "CHF" },
    series: keys.map((k, i) => ({
      name: k,
      type: "line",
      stack: "total",
      areaStyle: {},
      lineStyle: { color: colorFor(k, i, mode, true) },
      itemStyle: { color: colorFor(k, i, mode, true) },
      data: rows.map((r) => n(r[k])),
    })),
  };
}

export function singleLine(labels: string[], values: number[], mode: Mode, name = "CHF"): any {
  return {
    xAxis: { type: "category", data: labels, boundaryGap: false },
    yAxis: { type: "value", name: "CHF", scale: true },
    series: [{ name, type: "line", areaStyle: {}, data: values, lineStyle: { color: seriesColor(mode, 0) }, itemStyle: { color: seriesColor(mode, 0) } }],
  };
}

export function bars(labels: string[], series: Array<{ name: string; values: number[]; slot: number }>, mode: Mode, unit: string): any {
  return {
    xAxis: { type: "category", data: labels },
    yAxis: { type: "value", name: unit },
    series: series.map((s) => ({ name: s.name, type: "bar", itemStyle: { color: seriesColor(mode, s.slot) }, data: s.values })),
  };
}

const ASSET_GROUPS = ["Cash", "Investments", "Pension", "Property", "Vehicles"];

/** Assets stacked above zero, debts below it, and the net worth line on top. */
export function netWorthChart(rows: Array<Record<string, string>>, mode: Mode): any {
  const has = (k: string) => rows.some((r) => n(r[k]) !== 0);
  const series: any[] = ASSET_GROUPS.filter(has).map((k) => ({
    name: k, type: "line", stack: "assets", areaStyle: {},
    lineStyle: { color: colorFor(k, 0, mode, true) }, itemStyle: { color: colorFor(k, 0, mode, true) },
    data: rows.map((r) => n(r[k])),
  }));
  if (has("Liabilities")) {
    const gray = TOKENS[mode].baseline;
    series.push({ name: "Liabilities", type: "line", stack: "debt", areaStyle: {}, lineStyle: { color: gray }, itemStyle: { color: gray }, data: rows.map((r) => n(r.Liabilities)) });
  }
  const ink = TOKENS[mode].ink;
  series.push({ name: "Net worth", type: "line", lineStyle: { color: ink }, itemStyle: { color: ink }, z: 10, data: rows.map((r) => n(r.total)) });
  return { xAxis: { type: "category", data: rows.map((r) => monthLabel(r.date.slice(0, 7))), boundaryGap: false }, yAxis: { type: "value", name: "CHF" }, series };
}

// Older years lighter, the latest boldest: steps of one hue, spread across the ramp so neighbours differ.
// Light starts at step 250 (lighter would fade into the surface); dark never goes below step 500.
const YEAR_RAMP: Record<Mode, string[]> = {
  light: ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281", "#0d366b"],
  dark: ["#256abf", "#3987e5", "#5598e7", "#86b6ef", "#b7d3f6", "#cde2fb"],
};
export function yearColor(i: number, count: number, mode: Mode): string {
  const ramp = YEAR_RAMP[mode];
  return ramp[count <= 1 ? ramp.length - 1 : Math.min(ramp.length - 1, Math.round((i * (ramp.length - 1)) / (count - 1)))];
}

/** One line per year over Jan-Dec, to compare the same month across years. */
export function overlayChart(overlay: Record<string, Array<string | null>>, mode: Mode): any {
  const years = Object.keys(overlay).sort();
  const names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return {
    xAxis: { type: "category", data: names, boundaryGap: false },
    yAxis: { type: "value", name: "CHF", scale: true },
    series: years.map((y, i) => ({
      name: y, type: "line",
      lineStyle: { color: yearColor(i, years.length, mode) }, itemStyle: { color: yearColor(i, years.length, mode) },
      data: overlay[y].map((v) => (v == null ? null : n(v))), connectNulls: false,
    })),
  };
}

export function lineChart(labels: string[], values: Array<number | null>, mode: Mode, unit: string, name: string): any {
  return {
    xAxis: { type: "category", data: labels, boundaryGap: false },
    yAxis: { type: "value", name: unit, scale: true },
    series: [{ name, type: "line", areaStyle: {}, data: values, lineStyle: { color: seriesColor(mode, 0) }, itemStyle: { color: seriesColor(mode, 0) } }],
  };
}

/** Counted assets under three return scenarios, against the FI number. */
export function projectionChart(points: Array<{ date: string; low: string; base: string; high: string; fi_number: string }>, mode: Mode): any {
  const one = (name: string, key: "low" | "base" | "high", slot: number) => ({
    name, type: "line", lineStyle: { color: seriesColor(mode, slot) }, itemStyle: { color: seriesColor(mode, slot) }, data: points.map((p) => n(p[key])),
  });
  const ink = TOKENS[mode].ink;
  return {
    xAxis: { type: "category", data: points.map((p) => p.date.slice(0, 4)), boundaryGap: false },
    yAxis: { type: "value", name: "CHF" },
    series: [one("Cautious", "low", 1), one("Base", "base", 0), one("Optimistic", "high", 2), { name: "FI number", type: "line", lineStyle: { color: ink, width: 1 }, itemStyle: { color: ink }, data: points.map((p) => n(p.fi_number)) }],
  };
}

/** Years to FI at each savings rate; the bar nearest your current rate is the only one in color. */
export function curveChart(curve: Array<{ rate: string; years_to_fi: string | null }>, currentRate: number | null, mode: Mode): any {
  const nearest = currentRate == null ? -1 : curve.reduce((best, c, i) => (Math.abs(n(c.rate) - currentRate) < Math.abs(n(curve[best].rate) - currentRate) ? i : best), 0);
  return {
    xAxis: { type: "category", data: curve.map((c) => `${Math.round(n(c.rate) * 100)}%`) },
    yAxis: { type: "value", name: "years" },
    series: [{ name: "Years to FI", type: "bar", data: curve.map((c, i) => ({ value: c.years_to_fi == null ? null : n(c.years_to_fi), itemStyle: { color: i === nearest ? seriesColor(mode, 0) : TOKENS[mode].baseline } })) }],
  };
}

export function simpleBars(labels: string[], values: number[], mode: Mode, unit: string, name: string, slot = 0): any {
  return {
    xAxis: { type: "category", data: labels },
    yAxis: { type: "value", name: unit },
    series: [{ name, type: "bar", itemStyle: { color: seriesColor(mode, slot) }, data: values }],
  };
}

const subLabel = (account: string, group: string) => (account === group ? "General" : account.slice(group.length + 1).replaceAll(":", " › "));

/** Accounts inside one category group (e.g. Expenses:Shopping), summed over the pivot's months
 *  (or just one `month`, "YYYY-MM"), biggest first. */
export function breakdown(p: Pivot, group: string, month?: string): Part[] {
  const mine = p.keys.filter((k) => k === group || k.startsWith(`${group}:`));
  const rows = month ? p.rows.filter((r) => r.month === month) : p.rows;
  const sums = mine.map((k) => ({ account: k, amount: rows.reduce((s, r) => s + n(r[k]), 0) })).filter((x) => x.amount > 0);
  const total = sums.reduce((s, x) => s + x.amount, 0);
  return sums
    .sort((a, b) => b.amount - a.amount)
    .map((x) => ({ ...x, label: subLabel(x.account, group), share: total ? x.amount / total : 0 }));
}

export function donut(parts: Part[], mode: Mode, unit: string): any {
  return {
    unit,
    tooltip: { trigger: "item", formatter: "{b}: {c} ({d}%)" },
    series: [{ type: "pie", data: parts.map((x, i) => ({ name: x.label, value: Math.round(x.amount), itemStyle: { color: seriesColor(mode, i) } })) }],
  };
}
