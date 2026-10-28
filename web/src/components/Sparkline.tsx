/** A tiny trend line. Axes are left out on purpose: it shows the shape, the table has the numbers. */
export function Sparkline({ values, width = 96, height = 26, label }: { values: number[]; width?: number; height?: number; label: string }) {
  if (values.length < 2) return <span className="text-muted">–</span>;
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = hi - lo || 1;
  const pad = 3;
  const x = (i: number) => pad + (i * (width - 2 * pad)) / (values.length - 1);
  const y = (v: number) => (hi === lo ? height / 2 : height - pad - ((v - lo) * (height - 2 * pad)) / span);
  const d = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const last = values.length - 1;
  return (
    <svg width={width} height={height} role="img" aria-label={label} className="block">
      <path d={d} fill="none" stroke="var(--accent)" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" />
      <circle cx={x(last)} cy={y(values[last])} r={3} fill="var(--accent)" stroke="var(--surface)" strokeWidth={2} />
    </svg>
  );
}
