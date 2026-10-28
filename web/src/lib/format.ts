const chf = new Intl.NumberFormat("de-CH", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const whole = new Intl.NumberFormat("de-CH", { maximumFractionDigits: 0 });
const compact = new Intl.NumberFormat("de-CH", { notation: "compact", maximumFractionDigits: 1 });

/** Amounts arrive as decimal strings; parse only for display. */
export const n = (s: string | number | null | undefined): number => (s == null ? NaN : Number(s));

export function money(s: string | number | null | undefined, ccy = "CHF", digits = 2): string {
  const v = n(s);
  if (Number.isNaN(v)) return "–";
  const body = `${ccy} ${digits === 0 ? whole.format(Math.abs(v)) : chf.format(Math.abs(v))}`;
  return v < 0 ? `−${body}` : body;
}
export const signedMoney = (s: string | number, ccy = "CHF", digits = 0) => `${n(s) > 0 ? "+" : n(s) < 0 ? "−" : ""}${money(Math.abs(n(s)), ccy, digits)}`;
export const int = (v: number) => whole.format(v);
export const short = (v: number) => compact.format(v);
export function pct(s: string | number | null | undefined, digits = 1): string {
  const v = n(s);
  return Number.isNaN(v) ? "–" : `${(v * 100).toFixed(digits)}%`;
}
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export function monthLabel(ym: string): string {
  const [y, m] = ym.split("-");
  return `${MONTHS[Number(m) - 1]} ${y.slice(2)}`;
}
export function dateLabel(iso: string): string {
  const [y, m, d] = iso.split("-");
  return `${Number(d)} ${MONTHS[Number(m) - 1]} ${y}`;
}
export const esc = (s: unknown) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
