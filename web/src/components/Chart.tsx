import ReactEChartsCore from "echarts-for-react/lib/core";
import { BarChart, LineChart, PieChart } from "echarts/charts";
import { GridComponent, LegendComponent, TitleComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { ReactNode, useMemo, useState } from "react";
import { themeOption } from "../lib/chartTheme";
import { useMode } from "../lib/theme";
import { Card, CardHeader, Column, DataTable } from "./ui";

echarts.use([BarChart, LineChart, PieChart, GridComponent, LegendComponent, TitleComponent, TooltipComponent, CanvasRenderer]);

export interface Pick { seriesIndex: number; dataIndex: number }

export function Chart({ option, height = 280, onPick }: { option: any; height?: number; onPick?: (p: Pick) => void }) {
  const mode = useMode();
  const themed = useMemo(() => themeOption(option, mode), [option, mode]);
  const onEvents = useMemo(() => (onPick ? { click: (e: any) => onPick({ seriesIndex: e.seriesIndex, dataIndex: e.dataIndex }) } : undefined), [onPick]);
  return <ReactEChartsCore echarts={echarts} option={themed} notMerge style={{ height, cursor: onPick ? "pointer" : undefined }} opts={{ renderer: "canvas" }} onEvents={onEvents} />;
}

/** A chart with its table twin: every value is reachable without hovering. */
export function ChartCard<R extends Record<string, any>>({ title, subtitle, option, columns, rows, height, right, loading, onPick }: {
  title: string; subtitle?: string; option: any; columns: Column<R>[]; rows: R[]; height?: number; right?: ReactNode; loading?: boolean; onPick?: (p: Pick) => void;
}) {
  const [asTable, setAsTable] = useState(false);
  return (
    <Card className={loading ? "opacity-60 transition-opacity" : "transition-opacity"}>
      <CardHeader
        title={title}
        subtitle={subtitle}
        right={
          <div className="flex items-center gap-2">
            {right}
            <button onClick={() => setAsTable((v) => !v)} className="rounded-md border border-hairline px-2 py-1 text-xs text-ink-2 hover:bg-hairline" aria-pressed={asTable}>
              {asTable ? "Chart" : "Table"}
            </button>
          </div>
        }
      />
      {asTable ? <div className="max-h-[340px] overflow-y-auto"><DataTable columns={columns} rows={rows} /></div> : <Chart option={option} height={height} onPick={onPick} />}
    </Card>
  );
}
