import { ReactNode } from "react";

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`rounded-xl border border-hairline bg-surface p-4 sm:p-5 ${className}`}>{children}</section>;
}

export function CardHeader({ title, subtitle, right }: { title: string; subtitle?: string; right?: ReactNode }) {
  return (
    <div className="mb-3 flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
      <div>
        <h3 className="text-sm font-semibold text-ink">{title}</h3>
        {subtitle && <p className="mt-0.5 text-xs text-muted">{subtitle}</p>}
      </div>
      {right}
    </div>
  );
}

export function StatTile({ label, value, delta, hint, hero = false }: { label: string; value: string; delta?: { text: string; good: boolean | null }; hint?: string; hero?: boolean }) {
  const tone = delta?.good == null ? "text-ink-2" : delta.good ? "text-good" : "text-bad";
  return (
    <div className="min-w-0">
      <div className="text-xs text-muted">{label}</div>
      <div className={`${hero ? "text-4xl sm:text-5xl" : "text-xl"} mt-1 whitespace-nowrap font-semibold tracking-tight text-ink`}>{value}</div>
      {delta && (
        <div className={`mt-1 text-xs ${tone}`}>
          {delta.good == null ? "" : delta.good ? "▲ " : "▼ "}
          {delta.text}
        </div>
      )}
      {hint && <div className="mt-1 text-xs text-muted">{hint}</div>}
    </div>
  );
}

export function Segmented<T extends string>({ value, options, onChange, label }: { value: T; options: Array<{ value: T; label: string }>; onChange: (v: T) => void; label: string }) {
  return (
    <div role="group" aria-label={label} className="inline-flex max-w-full overflow-x-auto rounded-lg border border-hairline bg-surface p-0.5 text-xs">
      {options.map((o) => (
        <button
          key={o.value}
          aria-pressed={o.value === value}
          onClick={() => onChange(o.value)}
          className={`shrink-0 whitespace-nowrap rounded-md px-3 py-1.5 transition-colors ${o.value === value ? "bg-ink text-surface" : "text-ink-2 hover:bg-hairline"}`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export interface Column<R> { key: string; label: string; align?: "right"; render?: (r: R) => ReactNode }

export function DataTable<R extends Record<string, any>>({ columns, rows, empty = "Nothing to show" }: { columns: Column<R>[]; rows: R[]; empty?: string }) {
  if (!rows.length) return <p className="py-6 text-center text-sm text-muted">{empty}</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-hairline text-left text-xs text-muted">
            {columns.map((c) => (
              <th key={c.key} className={`px-2 py-2 font-medium ${c.align === "right" ? "text-right" : ""}`}>{c.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-b border-hairline last:border-0">
              {columns.map((c) => (
                <td key={c.key} className={`tnum px-2 py-2 text-ink ${c.align === "right" ? "whitespace-nowrap text-right" : ""}`}>{c.render ? c.render(r) : String(r[c.key] ?? "")}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function StatusBadge({ kind, children }: { kind: "good" | "warn" | "bad" | "muted"; children: ReactNode }) {
  // status never rides on color alone: an icon and a label always come with it
  const icon = { good: "●", warn: "▲", bad: "■", muted: "○" }[kind];
  const tone = { good: "text-good", warn: "text-ink", bad: "text-bad", muted: "text-muted" }[kind];
  return <span className={`inline-flex items-center gap-1 text-xs ${tone}`}><span aria-hidden>{icon}</span>{children}</span>;
}

export function Chip({ on, onClick, children }: { on: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      aria-pressed={on}
      onClick={onClick}
      className={`rounded-full border px-3 py-1 text-xs transition-colors ${on ? "border-ink bg-ink text-surface" : "border-hairline text-ink-2 hover:bg-hairline"}`}
    >
      {children}
    </button>
  );
}
