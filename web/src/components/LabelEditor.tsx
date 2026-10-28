import { useQueryClient } from "@tanstack/react-query";
import CategoryPicker from "./CategoryPicker";
import { useState } from "react";
import { Txn, useApi } from "../lib/api";
import { dateLabel, money } from "../lib/format";

interface Options { categories: string[]; cadences: string[]; necessities: string[] }
interface Similar { payee: string; rows: Array<{ ref: string; date: string; category: string; amount: string }> }

const AUTO = "";
const label = (v: string) => v.replace("_", "-");
const field = "rounded-md border border-hairline bg-surface px-2 py-1 text-sm text-ink";

/** Change how one expense is labelled, and optionally every other payment to the same payee.
 *  Saving rewrites the transaction in the ledger file; the page then reloads from it. */
export default function LabelEditor({ t, ccy }: { t: Txn; ccy: string }) {
  const qc = useQueryClient();
  const options = useApi<Options>("/api/labels/options").data;
  const similar = useApi<Similar>(`/api/transactions/similar?ref=${encodeURIComponent(t.ref ?? "")}`).data;
  const [category, setCategory] = useState(t.category);
  const [cadence, setCadence] = useState(t.cadence ?? AUTO);
  const [necessity, setNecessity] = useState(t.necessity ?? AUTO);
  const [major, setMajor] = useState(t.major ? "yes" : "no");
  const [all, setAll] = useState(false);
  const [remember, setRemember] = useState(false);
  const [forType, setForType] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState<string | null>(null);
  if (!options) return <p className="text-xs text-muted">Loading…</p>;

  // only what changed is sent, so everything else keeps being worked out automatically
  const body: Record<string, unknown> = {};
  if (category !== t.category) body.category = category;
  if (cadence !== (t.cadence ?? AUTO)) body.cadence = cadence === AUTO ? null : cadence;
  if (necessity !== (t.necessity ?? AUTO)) body.necessity = necessity === AUTO ? null : necessity;
  if (major !== (t.major ? "yes" : "no")) body.major = major === "yes";
  const dirty = Object.keys(body).length > 0;
  const others = similar?.rows ?? [];

  const save = async () => {
    setBusy(true);
    setError("");
    const res = await fetch("/api/transactions/labels", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref: t.ref, also: all ? others.map((o) => o.ref) : [], remember, remember_for_merchant_type: remember && forType, ...body }),
    });
    setBusy(false);
    if (!res.ok) return setError((await res.json().catch(() => ({}))).detail ?? `${res.status}`);
    const done = await res.json();
    setSaved(`Saved ${done.changed} to the ledger${done.rule ? `, and remembered for future ${similar?.payee ?? "payments"}${done.rule_applied ? ` (${done.rule_applied} more labelled)` : ""}` : ""}`);
    await qc.invalidateQueries(); // every page reads from the ledger
  };

  return (
    <div className="space-y-2 border-t border-hairline pt-3">
      <div className="text-xs font-medium text-ink">Edit labels</div>
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1">Category
          <CategoryPicker label="Category" value={category} categories={options.categories.includes(category) ? options.categories : [category, ...options.categories]} onChange={setCategory} className="w-56" />
        </label>
        <label className="flex flex-col gap-1">Cadence
          <select value={cadence} onChange={(e) => setCadence(e.target.value)} className={field}>
            <option value={AUTO}>Automatic</option>
            {options.cadences.map((c) => <option key={c} value={c}>{label(c)}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1">Necessity
          <select value={necessity} onChange={(e) => setNecessity(e.target.value)} className={field}>
            <option value={AUTO}>Automatic</option>
            {options.necessities.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1">Major purchase
          <select value={major} onChange={(e) => setMajor(e.target.value)} className={field}>
            <option value="no">No</option>
            <option value="yes">Yes, keep out of monthly spend</option>
          </select>
        </label>
      </div>
      {others.length > 0 && (
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={all} onChange={(e) => { setAll(e.target.checked); setRemember(e.target.checked); }} />
          <span>Also apply to the {others.length} other {similar!.payee} payment{others.length === 1 ? "" : "s"}
            <span className="text-muted"> ({dateLabel(others[0].date)} to {dateLabel(others[others.length - 1].date)}, {money(others.reduce((s, o) => s - Number(o.amount), 0), ccy, 0)} in all)</span>
          </span>
        </label>
      )}
      {similar?.payee && (
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
          <span>Remember for future {similar.payee} payments <span className="text-muted">(a rule in the ledger; new imports get these labels)</span></span>
        </label>
      )}
      {remember && t.merchant_type && (
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={forType} onChange={(e) => setForType(e.target.checked)} />
          <span>Only for the merchant type “{t.merchant_type}” <span className="text-muted">(for shops that sell everything; other purchases keep their own category)</span></span>
        </label>
      )}
      <div className="flex items-center gap-3">
        <button onClick={save} disabled={!dirty || busy} className="rounded-md bg-ink px-3 py-1 text-sm text-surface disabled:opacity-40">
          {busy ? "Saving…" : all && others.length ? `Save ${others.length + 1} transactions` : "Save"}
        </button>
        {saved !== null && <span className="text-good">{saved}</span>}
        {error && <span role="alert" className="text-bad">{error}</span>}
        <span className="text-muted">Written into the ledger file and committed.</span>
      </div>
    </div>
  );
}
