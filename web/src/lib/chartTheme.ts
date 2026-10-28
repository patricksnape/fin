import { esc, int, money, short } from "./format";
import { Mode, SERIES, TOKENS } from "./theme";

const FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif';

/** Apply the mark specs to any ECharts option (page charts and model-requested charts alike):
 *  thin marks, 4px rounded data-ends, 2px surface gaps, hairline solid grid, text in ink tokens. */
export function themeOption(opt: any, mode: Mode): any {
  const t = TOKENS[mode];
  const o = structuredClone(opt);
  const unit: string = o.yAxis?.name ?? o.unit ?? "";
  const fmt = (v: number) => (unit === "%" ? `${v.toFixed(1)}%` : unit ? money(v, unit) : int(v));
  const series: any[] = o.series ?? [];
  const isPie = series.some((s) => s.type === "pie");
  const hasLegend = o.legend?.show !== false && (series.length > 1 || isPie);

  o.color = o.color ?? SERIES[mode];
  o.backgroundColor = "transparent";
  o.textStyle = { color: t.ink2, fontFamily: FONT };
  o.animationDuration = 250;
  if (o.title) o.title = { ...o.title, left: 0, textStyle: { color: t.ink, fontSize: 14, fontWeight: 600, fontFamily: FONT } };
  const lineKey = series[0]?.type === "line"; // a short 2px stroke for lines, a small bar otherwise
  o.legend = hasLegend
    ? { type: "scroll", bottom: 0, left: 0, itemWidth: 14, itemHeight: lineKey ? 2 : 6, icon: lineKey ? "rect" : "roundRect", itemStyle: { borderWidth: 0 }, textStyle: { color: t.ink2, fontSize: 12 } }
    : { show: false };
  o.grid = isPie ? undefined : { left: 4, right: 30, top: o.title ? 40 : 12, bottom: hasLegend ? 36 : 4, containLabel: true };
  o.tooltip = {
    trigger: isPie ? "item" : "axis",
    backgroundColor: t.surface,
    borderColor: t.ring,
    borderWidth: 1,
    padding: [8, 10],
    transitionDuration: 0,
    extraCssText: "pointer-events:none;",
    // Float beside the pointer, but keep clear of the x-axis labels and the legend below the plot.
    position: (point: number[], _p: unknown, _d: unknown, _r: unknown, size: { contentSize: number[]; viewSize: number[] }) => {
      const [w, h] = size.contentSize;
      const [vw, vh] = size.viewSize;
      const floor = vh - (hasLegend ? 36 : 4) - 28; // legend band + x-axis label band
      const x = point[0] + 18 + w > vw ? point[0] - w - 18 : point[0] + 18;
      const y = Math.max(4, Math.min(point[1] - h / 2, floor - h));
      return [Math.max(4, x), y];
    },
    textStyle: { color: t.ink, fontFamily: FONT, fontSize: 12 },
    axisPointer: { type: series.some((s) => s.type === "line") ? "line" : "shadow", lineStyle: { color: t.baseline, width: 1 }, shadowStyle: { color: t.hairline, opacity: 0.35 } },
    // Values lead, names follow; every string from the data is escaped.
    formatter: (p: any) => {
      const items = Array.isArray(p) ? p : [p];
      const head = isPie ? "" : `<div style="color:${t.ink2};margin-bottom:4px">${esc(items[0].axisValueLabel ?? items[0].name)}</div>`;
      const rows = items
        .filter((i) => i.value != null && (Array.isArray(i.value) ? i.value[1] != null : true))
        .map((i) => {
          const v = Number(Array.isArray(i.value) ? i.value[1] : i.value);
          return `<div style="display:flex;gap:8px;align-items:center;justify-content:space-between"><span style="display:flex;gap:6px;align-items:center"><span style="width:10px;height:2px;background:${i.color};display:inline-block"></span><span style="color:${t.ink2}">${esc(isPie ? i.name : i.seriesName || i.name)}</span></span><b style="color:${t.ink};font-variant-numeric:tabular-nums">${fmt(v)}${isPie && i.percent != null ? ` <span style="color:${t.ink2};font-weight:400">· ${Math.round(i.percent)}%</span>` : ""}</b></div>`;
        });
      return head + rows.join("");
    },
  };
  for (const k of ["xAxis", "yAxis"] as const) {
    if (!o[k]) continue;
    const isY = k === "yAxis";
    o[k] = {
      ...o[k],
      name: isY ? undefined : o[k].name,
      axisLine: { show: !isY, lineStyle: { color: t.baseline, width: 1 } },
      axisTick: { show: false },
      axisLabel: { color: t.muted, fontSize: 11, ...(isY ? { formatter: (v: number) => (unit === "%" ? `${v}%` : short(v)) } : { hideOverlap: true }) },
      splitLine: { show: isY, lineStyle: { color: t.hairline, width: 1, type: "solid" } },
    };
  }
  o.series = series.map((s, idx) => {
    const stacked = !!s.stack;
    const last = idx === series.length - 1;
    if (s.type === "bar") {
      return {
        ...s,
        barMaxWidth: 24,
        itemStyle: { ...s.itemStyle, borderColor: t.surface, borderWidth: 1, borderRadius: stacked ? (last ? [4, 4, 0, 0] : 0) : [4, 4, 0, 0] },
        emphasis: { focus: "none", itemStyle: { opacity: 0.85 } },
      };
    }
    if (s.type === "line") {
      return {
        ...s,
        symbol: "circle",
        symbolSize: 8,
        showSymbol: false,
        smooth: false,
        lineStyle: { ...s.lineStyle, width: 2, cap: "round", join: "round" },
        itemStyle: { ...s.itemStyle, borderColor: t.surface, borderWidth: 2 },
        areaStyle: s.areaStyle ? { ...s.areaStyle, opacity: 0.1 } : undefined,
        emphasis: { disabled: false, scale: true },
      };
    }
    if (s.type === "pie") {
      return { ...s, radius: ["45%", "70%"], itemStyle: { borderColor: t.surface, borderWidth: 2 }, label: { show: false } };
    }
    return s;
  });
  return o;
}

/** Color one entity (series name) with the same slot every time. */
export function seriesColor(mode: Mode, slot: number): string {
  return SERIES[mode][slot % SERIES[mode].length];
}
