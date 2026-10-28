import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useApi } from "../lib/api";
import { Card, CardHeader } from "./ui";

interface Row {
  account: string; kind: "asset" | "liability"; institution: string; group: string | null; explicit_group: string | null;
  explicit_institution: string | null; guessed: boolean; problem: string | null; currencies: string[]; opened: string | null;
  closed: string | null; transactions: number;
}
interface Data { accounts: Row[]; groups: { asset: string[]; liability: string[] }; institutions: string[]; cadences: string[]; necessities: string[] }

const field = "rounded-md border border-hairline bg-surface px-2 py-1 text-sm text-ink";
const TYPES = [
  { id: "Assets", label: "Account or investment", hint: "bank, broker, cash, pension, property" },
  { id: "Liabilities", label: "Debt or credit card", hint: "credit card, mortgage, loan" },
  { id: "Expenses", label: "Spending category", hint: "a place spending can be filed" },
] as const;

async function call(method: string, url: string, body?: unknown): Promise<string | null> {
  const res = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  return res.ok ? null : (await res.json().catch(() => ({}))).detail ?? `${res.status}`;
}

function AddForm({ data, done }: { data: Data; done: () => void }) {
  const [type, setType] = useState<(typeof TYPES)[number]["id"]>("Liabilities");
  const [inst, setInst] = useState("");
  const [name, setName] = useState("");
  const [group, setGroup] = useState("");
  const [ccy, setCcy] = useState("CHF");
  const [cadence, setCadence] = useState("regular");
  const [necessity, setNecessity] = useState("discretionary");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const balance = type !== "Expenses";
  const account = `${type}:${inst.trim().replace(/\s+/g, "")}${name.trim() ? ":" + name.trim().replace(/\s+/g, "") : ""}`;
  const groups = type === "Liabilities" ? data.groups.liability : data.groups.asset;
  const save = async () => {
    setBusy(true);
    setError("");
    const err = await call("POST", "/api/account-settings", {
      account,
      group: balance ? group || null : null,
      institution: balance ? inst.trim() || null : null,
      currencies: balance ? ccy.split(/[ ,]+/).filter(Boolean) : [],
      cadence: balance ? null : cadence,
      necessity: balance ? null : necessity,
    });
    setBusy(false);
    if (err) return setError(err);
    setInst(""); setName("");
    done();
  };
  return (
    <div className="space-y-3 rounded-lg border border-hairline p-3 text-sm text-ink-2">
      <div className="font-medium text-ink">Add an account</div>
      <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Type">
        {TYPES.map((t) => (
          <button key={t.id} role="radio" aria-checked={type === t.id} onClick={() => { setType(t.id); setGroup(""); }}
            className={`rounded-lg border px-3 py-1.5 text-left ${type === t.id ? "border-ink bg-ink text-surface" : "border-hairline hover:bg-hairline"}`}>
            <div>{t.label}</div><div className={`text-xs ${type === t.id ? "opacity-80" : "text-muted"}`}>{t.hint}</div>
          </button>
        ))}
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1">{balance ? "Institution" : "Group"}
          <input list="institutions" value={inst} onChange={(e) => setInst(e.target.value)} placeholder={balance ? "UBS" : "Food"} className={`${field} w-36`} />
          <datalist id="institutions">{balance && data.institutions.map((i) => <option key={i} value={i} />)}</datalist>
        </label>
        <label className="flex flex-col gap-1">{balance ? "Account name" : "Category"}
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder={balance ? "CreditCard" : "Groceries"} className={`${field} w-40`} />
        </label>
        {balance ? (
          <>
            <label className="flex flex-col gap-1">Counts as
              <select value={group} onChange={(e) => setGroup(e.target.value)} className={field}>
                <option value="">{type === "Liabilities" ? "Liabilities (default)" : "Choose…"}</option>
                {groups.map((g) => <option key={g} value={g}>{g}</option>)}
              </select>
            </label>
            <label className="flex flex-col gap-1">Currency
              <input value={ccy} onChange={(e) => setCcy(e.target.value)} className={`${field} w-24`} />
            </label>
          </>
        ) : (
          <>
            <label className="flex flex-col gap-1">Cadence
              <select value={cadence} onChange={(e) => setCadence(e.target.value)} className={field}>{data.cadences.map((c) => <option key={c} value={c}>{c.replace("_", "-")}</option>)}</select>
            </label>
            <label className="flex flex-col gap-1">Necessity
              <select value={necessity} onChange={(e) => setNecessity(e.target.value)} className={field}>{data.necessities.map((c) => <option key={c} value={c}>{c}</option>)}</select>
            </label>
          </>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <button onClick={save} disabled={busy || !inst.trim() || (!balance && !name.trim())} className="rounded-md bg-ink px-3 py-1 text-surface disabled:opacity-40">{busy ? "Adding…" : "Add"}</button>
        <span className="text-xs text-muted">Opens <code className="text-ink">{account}</code> in ledger/accounts.beancount</span>
        {error && <span role="alert" className="text-bad">{error}</span>}
      </div>
    </div>
  );
}

/** Every balance sheet account: which institution it belongs to and which net worth group it counts in.
 *  Saved as metadata on the account's `open` directive. */
export default function AccountSettings() {
  const qc = useQueryClient();
  const { data } = useApi<Data>("/api/account-settings");
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState("");
  if (!data) return null;
  const refresh = () => qc.invalidateQueries();
  const edit = async (account: string, changes: Record<string, string | null>) => {
    setError("");
    const err = await call("PUT", `/api/account-settings/${encodeURIComponent(account)}`, changes);
    if (err) setError(`${account}: ${err}`);
    refresh();
  };
  const close = async (account: string) => {
    const err = await call("POST", `/api/account-settings/${encodeURIComponent(account)}/close`);
    setError(err ? `${account}: ${err}` : "");
    refresh();
  };
  const needGroup = data.accounts.filter((a) => a.guessed).length;
  return (
    <Card>
      <CardHeader
        title="Accounts"
        subtitle="Which institution each account belongs to and which net worth group it counts in. Saved on the account's open line in the ledger."
        right={<button onClick={() => setAdding((v) => !v)} aria-pressed={adding} className="rounded-md border border-hairline px-2 py-1 text-xs text-ink-2 hover:bg-hairline">{adding ? "Cancel" : "Add account"}</button>}
      />
      <div className="space-y-3">
        {adding && <AddForm data={data} done={() => { setAdding(false); refresh(); }} />}
        {needGroup > 0 && <p className="text-xs text-ink">⚑ {needGroup} account{needGroup === 1 ? "" : "s"} counted by a guess: pick a group to make it explicit.</p>}
        {error && <p role="alert" className="text-sm text-bad">{error}</p>}
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-sm">
            <thead><tr className="border-b border-hairline text-left text-xs text-muted">
              <th className="py-2 pr-3 font-medium">Account</th><th className="px-2 font-medium">Institution</th><th className="px-2 font-medium">Counts as</th><th className="px-2 text-right font-medium">Txns</th><th />
            </tr></thead>
            <tbody>
              {data.accounts.map((a) => (
                <tr key={a.account} className={`border-b border-hairline ${a.closed ? "opacity-60" : ""}`}>
                  <td className="py-1.5 pr-3 text-ink">{a.account}{a.closed && <span className="ml-2 text-xs text-muted">closed</span>}{a.problem && <div className="text-xs text-bad">{a.problem}</div>}</td>
                  <td className="px-2">
                    <input defaultValue={a.institution} aria-label={`Institution of ${a.account}`} key={a.institution}
                      onBlur={(e) => e.target.value.trim() !== a.institution && edit(a.account, { institution: e.target.value.trim() || null })}
                      className="w-32 rounded-md border border-transparent bg-transparent px-1 py-0.5 text-ink hover:border-hairline focus:border-accent focus:outline-none" />
                  </td>
                  <td className="px-2">
                    <select value={a.group ?? ""} aria-label={`Group of ${a.account}`} onChange={(e) => edit(a.account, { group: e.target.value })} className={`${field} ${a.guessed ? "border-ink" : ""}`}>
                      {(a.kind === "liability" ? data.groups.liability : data.groups.asset).map((g) => <option key={g} value={g}>{g}</option>)}
                    </select>
                    {a.guessed && <button onClick={() => edit(a.account, { group: a.group })} className="ml-2 text-xs text-ink underline">⚑ guessed: confirm {a.group}</button>}
                  </td>
                  <td className="tnum px-2 text-right text-ink-2">{a.transactions}</td>
                  <td className="pl-2 text-right">{!a.closed && <button onClick={() => close(a.account)} className="text-xs text-muted hover:text-ink hover:underline">Close</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </Card>
  );
}
