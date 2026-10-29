import { Tile, useApi } from "../lib/api";
import { Chart } from "../components/Chart";
import { Card, CardHeader, Loading } from "../components/ui";

export default function Dashboards() {
  const { data } = useApi<{ tiles: Tile[] }>("/api/dashboards");
  if (!data) return <Loading />;
  if (!data.tiles.length)
    return (
      <Card>
        <p className="text-sm text-ink-2">Nothing pinned yet. Ask the assistant for a chart, then say “pin that”. Tiles save only the query and chart layout, never numbers, and re-run against your ledger every time.</p>
      </Card>
    );
  return (
    <div className="grid gap-6 xl:grid-cols-2">
      {data.tiles.map((t) => (
        <Card key={t.title}>
          <CardHeader title={t.title} />
          {t.option ? <Chart option={t.option} /> : <p className="text-sm text-bad">▲ {t.error}</p>}
        </Card>
      ))}
    </div>
  );
}
