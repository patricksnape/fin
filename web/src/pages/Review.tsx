import { useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import CategoryPicker from "../components/CategoryPicker";
import { Card, CardHeader, Loading } from "../components/ui";
import { useApi } from "../lib/api";
import { dateLabel, int, money } from "../lib/format";

interface Merchant {
  payee_key: string; payee: string; count: number; category: string; needs_category: boolean;
  source: "model" | "mapping" | "rule" | "unknown"; merchant_types: string[]; total: string; currency: string;
  payments: Array<[string, string, string]>; // [date, amount, ref]
}
interface Queue { merchants: Merchant[]; transactions: number }
interface Options { categories: string[] }

const SOURCE: Record<string, string> = { model: "Model guess", mapping: "From merchant type", rule: "From a rule", unknown: "Not categorized" };
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const field = "rounded-md border border-hairline bg-surface px-2 py-1 text-sm text-ink";

async function confirm(m: Merchant, category: string, remember: boolean, perType: boolean, refs: string[] | null = null): Promise<string | null> {
  const res = await fetch("/api/review/confirm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ payee_key: m.payee_key, category, remember, per_type: remember && perType, merchant_type: remember && perType ? m.merchant_types[0] : null, refs }),
  });
  return res.ok ? null : ((await res.json().catch(() => ({}))).detail ?? `${res.status}`);
}

interface Detail {
  ref: string; date: string; payee: string; narration: string; amount: string; currency: string;
  category: string; tags: string[]; card: string | null; card_label: string | null; meta: Record<string, string>; record: Record<string, unknown> | null;
}

const words = (k: string) => k.replace(/([a-z])([A-Z])/g, "$1 $2").replaceAll("_", " ").toLowerCase();
/** Nested record -> flat [label, text] rows; empty values are left out, nothing is shortened. */
function flatten(o: unknown, path: string[] = []): Array<[string, string]> {
  if (o === null || o === undefined || o === "") return [];
  if (Array.isArray(o)) return o.flatMap((v, i) => flatten(v, [...path, String(i + 1)]));
  if (typeof o === "object") return Object.entries(o as Record<string, unknown>).flatMap(([k, v]) => flatten(v, [...path, words(k)]));
  return [[path.join(" › "), String(o)]];
}

/** Everything known about each payment of one merchant, including the source's own full record. */
function Details({ payeeKey, selected, onToggle }: { payeeKey: string; selected: Set<string>; onToggle: (ref: string) => void }) {
  const { data } = useApi<Detail[]>(`/api/review/details?payee_key=${encodeURIComponent(payeeKey)}`);
  if (!data) return <Loading />;
  return (
    <div className="w-full space-y-3">
      {data.map((d) => (
        <div key={d.ref} className="rounded-lg border border-hairline p-3 text-xs">
          <div className="mb-2 flex flex-wrap items-baseline gap-x-4 gap-y-1">
            <input type="checkbox" checked={selected.has(d.ref)} onChange={() => onToggle(d.ref)} aria-label={`Select the payment of ${dateLabel(d.date)}`} />
            <span className="font-medium text-ink">{dateLabel(d.date)}</span>
            <span className="font-medium text-ink">{money(d.amount, d.currency)}</span>
            {d.card && <span className="text-ink" title={`Card ${d.card}`}>💳 {d.card_label ?? `card ${d.card} (not named yet: Settings › Cards)`}</span>}
            <span className="break-words text-ink-2">{d.narration || d.payee}</span>
          </div>
          <dl className="grid grid-cols-[minmax(8rem,12rem)_1fr] gap-x-4 gap-y-1">
            {(d.record ? flatten(d.record) : Object.entries(d.meta)).map(([k, v], i) => (
              <div key={`${k}-${i}`} className="contents">
                <dt className="capitalize text-muted">{k.replaceAll("_", " ")}</dt>
                <dd className="break-words text-ink">{v}</dd>
              </div>
            ))}
          </dl>
          {!d.record && <p className="mt-2 text-muted">No archived source record for this payment; showing what the ledger holds.</p>}
        </div>
      ))}
    </div>
  );
}

const CADENCES = ["one_off", "regular", "recurring"];
const NECESSITIES = ["discretionary", "essential"];

/** Open an expense account without leaving the page. It opens in 2020, like the other
 *  accounts, so it can hold a payment of any past date. */
function NewCategory({ onCreated, onCancel }: { onCreated: (account: string) => void; onCancel: () => void }) {
  const [name, setName] = useState("");
  const [cadence, setCadence] = useState("one_off");
  const [necessity, setNecessity] = useState("discretionary");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const account = `Expenses:${name.trim().replace(/^Expenses:/, "").replace(/\s+/g, "")}`;
  const create = async () => {
    setBusy(true);
    setError("");
    const res = await fetch("/api/account-settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account, cadence, necessity, since: "2020-01-01" }),
    });
    setBusy(false);
    if (!res.ok) return setError((await res.json().catch(() => ({}))).detail ?? `${res.status}`);
    onCreated(account);
  };
  return (
    <div className="flex w-full flex-wrap items-end gap-3 rounded-lg bg-hairline/40 p-3 text-xs">
      <label className="flex flex-col gap-1">New category <span className="text-muted">e.g. Travel:CarRental</span>
        <input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="Group:Name" className={`${field} w-48`} />
      </label>
      <label className="flex flex-col gap-1">Cadence
        <select value={cadence} onChange={(e) => setCadence(e.target.value)} className={field}>{CADENCES.map((c) => <option key={c} value={c}>{c.replace("_", "-")}</option>)}</select>
      </label>
      <label className="flex flex-col gap-1">Necessity
        <select value={necessity} onChange={(e) => setNecessity(e.target.value)} className={field}>{NECESSITIES.map((c) => <option key={c}>{c}</option>)}</select>
      </label>
      <button onClick={create} disabled={!name.trim() || busy} className="rounded-md bg-ink px-3 py-1 text-sm text-surface disabled:opacity-40">{busy ? "Creating…" : "Create"}</button>
      <button onClick={onCancel} className="rounded-md border border-hairline px-3 py-1 text-sm text-ink-2 hover:bg-hairline">Cancel</button>
      {error && <span role="alert" className="text-bad">{error}</span>}
    </div>
  );
}

function Row({ m, categories, onDone, onCategoryAdded, subset }: { m: Merchant; categories: string[]; onDone: () => void; onCategoryAdded: () => Promise<void>; subset: string[] | null }) {
  const [category, setCategory] = useState(m.needs_category ? "" : m.category);
  const [adding, setAdding] = useState(false);
  const [open, setOpen] = useState(false);
  const [remember, setRemember] = useState(false);
  const [perType, setPerType] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const toggle = (ref: string) => setSelected((s) => { const n = new Set(s); if (n.has(ref)) n.delete(ref); else n.add(ref); return n; });
  // a period filter or a ticked selection limits Confirm to those payments
  const picked = selected.size > 0 ? [...selected] : subset;
  const save = async () => {
    setBusy(true);
    const err = await confirm(m, category, remember, perType, picked);
    setBusy(false);
    if (err) setError(err);
    else onDone();
  };
  return (
    <li className="flex flex-wrap items-center gap-3 py-3 text-sm">
      <button onClick={() => setOpen((v) => !v)} aria-expanded={open} aria-label={`${open ? "Hide" : "Show"} details for ${m.payee}`} className="w-44 shrink-0 text-left">
        <div className="truncate font-medium text-ink" title={m.payee}><span aria-hidden className="mr-1 text-muted">{open ? "▾" : "▸"}</span>{m.payee}</div>
        <div className="truncate pl-4 text-xs text-muted" title={m.merchant_types.join(", ")}>{m.merchant_types[0] ?? "no merchant type"}{m.merchant_types.length > 1 ? ` +${m.merchant_types.length - 1}` : ""}</div>
      </button>
      <div className="w-28 shrink-0 text-ink-2">
        {int(m.count)} × <span className="text-muted">{money(m.total, m.currency, 0)}</span>
        {m.payments.length > 0 && <div className="text-xs text-muted">{m.payments.length > 1 && m.payments[0][0] !== m.payments[m.payments.length - 1][0] ? `${dateLabel(m.payments[0][0])} – ${dateLabel(m.payments[m.payments.length - 1][0])}` : dateLabel(m.payments[0][0])}</div>}
      </div>
      <span className={`w-28 shrink-0 text-xs ${m.source === "model" ? "font-medium text-ink" : "text-muted"}`}>{SOURCE[m.source]}</span>
      <CategoryPicker label={`Category for ${m.payee}`} value={category} categories={categories} onChange={(c) => { setAdding(false); setCategory(c); }} onNew={() => setAdding(true)} className="w-52 shrink-0" />
      <label className="flex items-center gap-1 text-xs text-ink-2" title="Label the future payments from this merchant the same way">
        <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
        make a rule
      </label>
      {remember && m.merchant_types.length > 0 && (
        <label className="flex items-center gap-1 text-xs text-ink-2" title="Digitec-style shops: the rule then only applies to this merchant type">
          <input type="checkbox" checked={perType} onChange={(e) => setPerType(e.target.checked)} />
          only this merchant type
        </label>
      )}
      <button onClick={save} disabled={!category || busy} className="rounded-md bg-ink px-3 py-1 text-sm text-surface disabled:opacity-40">{busy ? "Saving…" : selected.size > 0 ? `Confirm ${selected.size} selected` : subset ? `Confirm these ${m.count}` : "Confirm"}</button>
      {error && <span role="alert" className="text-bad">{error}</span>}
      {open && <Details payeeKey={m.payee_key} selected={selected} onToggle={toggle} />}
      {adding && (
        <NewCategory
          onCancel={() => setAdding(false)}
          onCreated={async (account) => { await onCategoryAdded(); setCategory(account); setAdding(false); }}
        />
      )}
    </li>
  );
}

/** Tentative automatic guesses and uncategorized payments, one row per merchant. Confirming can also write
 *  a payee rule into the ledger (opt-in), so the next import already knows. */
export default function Review() {
  const qc = useQueryClient();
  const { data } = useApi<Queue>("/api/review");
  const options = useApi<Options>("/api/labels/options").data;
  const [bulk, setBulk] = useState<string | null>(null);
  const [year, setYear] = useState("");
  const [month, setMonth] = useState("");
  const [sort, setSort] = useState<"default" | "date" | "amount">("default");
  const [desc, setDesc] = useState(true);
  const years = useMemo(() => [...new Set((data?.merchants ?? []).flatMap((m) => m.payments.map(([d]) => d.slice(0, 4))))].sort().reverse(), [data]);
  // with a period picked, each merchant shows only its payments in it (count, total and dates follow)
  const rows = useMemo(() => {
    if (!data) return [];
    const inPeriod = (d: string) => (!year || d.startsWith(year)) && (!month || d.slice(5, 7) === month);
    const shown = data.merchants
      .map((m) => {
        const ps = m.payments.filter(([d]) => inPeriod(d));
        return { m: { ...m, payments: ps, count: ps.length, total: String(ps.reduce((t, [, a]) => t + Number(a), 0)) }, ps, all: m.count };
      })
      .filter((r) => r.ps.length > 0);
    if (sort === "default") return shown;
    const key = (r: (typeof shown)[number]) => (sort === "date" ? r.ps[r.ps.length - 1][0] : Math.abs(Number(r.m.total)));
    return [...shown].sort((a, b) => (key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0) * (desc ? -1 : 1));
  }, [data, year, month, sort, desc]);
  if (!data || !options) return <Loading />;
  const refresh = () => qc.invalidateQueries();
  const guesses = data.merchants.filter((m) => !m.needs_category);
  const filtered = year !== "" || month !== "";

  const confirmAll = async () => {
    for (const [i, m] of guesses.entries()) {
      setBulk(`Confirming ${i + 1} of ${guesses.length}…`);
      const err = await confirm(m, m.category, false, false);
      if (err) { setBulk(`Stopped at ${m.payee}: ${err}`); await refresh(); return; }
    }
    setBulk(null);
    await refresh();
  };

  return (
    <Card>
      <CardHeader
        title={data.merchants.length === 0 ? "Nothing to review" : `${int(data.merchants.length)} merchants to decide`}
        subtitle={`${int(data.transactions)} transactions. Confirm keeps the suggested category (or the one you pick). Tick “make a rule” to label future payments from that merchant the same way.`}
        right={guesses.length > 1 && (
          <button onClick={confirmAll} disabled={bulk !== null} className="whitespace-nowrap rounded-md border border-hairline px-3 py-1 text-sm text-ink-2 hover:bg-hairline disabled:opacity-40">
            {bulk ?? `Confirm all ${guesses.length} suggestions`}
          </button>
        )}
      />
      {data.merchants.length > 0 && (
        <>
          <div className="mb-3 flex flex-wrap items-end gap-3 text-xs text-ink-2">
            <label className="flex flex-col gap-1">Year
              <select value={year} onChange={(e) => setYear(e.target.value)} className={field}>
                <option value="">All</option>
                {years.map((y) => <option key={y}>{y}</option>)}
              </select>
            </label>
            <label className="flex flex-col gap-1">Month
              <select value={month} onChange={(e) => setMonth(e.target.value)} className={field}>
                <option value="">All</option>
                {MONTHS.map((name, i) => <option key={name} value={String(i + 1).padStart(2, "0")}>{name}</option>)}
              </select>
            </label>
            <label className="flex flex-col gap-1">Sort by
              <select value={sort} onChange={(e) => setSort(e.target.value as typeof sort)} className={field}>
                <option value="default">To decide first, then biggest</option>
                <option value="date">Date of latest payment</option>
                <option value="amount">Amount</option>
              </select>
            </label>
            {sort !== "default" && (
              <button onClick={() => setDesc((v) => !v)} className="rounded-md border border-hairline px-3 py-1 text-sm text-ink-2 hover:bg-hairline" aria-label="Reverse the order">
                {sort === "date" ? (desc ? "Newest first ↓" : "Oldest first ↑") : desc ? "Largest first ↓" : "Smallest first ↑"}
              </button>
            )}
            {filtered && <button onClick={() => { setYear(""); setMonth(""); }} className="text-sm text-accent underline-offset-2 hover:underline">Clear filter</button>}
          </div>
          {filtered && <p className="mb-2 text-xs text-muted">{int(rows.length)} merchants with payments in this period. Counts and totals are for the period, and Confirm labels only those payments (a merchant’s others stay pending).</p>}
          {rows.length === 0 ? <p className="text-sm text-ink-2">Nothing to review in this period.</p> : (
            <ul className="divide-y divide-hairline">
              {rows.map(({ m, ps, all }) => <Row key={m.payee_key} m={m} categories={options.categories} onDone={refresh} onCategoryAdded={refresh} subset={filtered && ps.length < all ? ps.map(([, , ref]) => ref) : null} />)}
            </ul>
          )}
        </>
      )}
    </Card>
  );
}
