import { useQueryClient } from "@tanstack/react-query";
import { useApi } from "../lib/api";
import { Card, CardHeader } from "./ui";

interface Rule { payee: string; labels: Record<string, string | boolean> }
const pretty = (k: string, v: string | boolean) =>
  k === "category" ? String(v).replace(/^Expenses:/, "").replaceAll(":", " › ") : k === "major" ? (v ? "major" : "not major") : String(v).replace("_", "-");

/** Labels remembered per payee; later imports of the payee get them. Stored in the ledger. */
export default function LabelRules() {
  const qc = useQueryClient();
  const { data } = useApi<Rule[]>("/api/rules");
  const forget = async (payee: string) => {
    await fetch(`/api/rules/${encodeURIComponent(payee)}`, { method: "DELETE" });
    await qc.invalidateQueries();
  };
  return (
    <Card>
      <CardHeader title="Label rules" subtitle="Remembered when you edit a transaction and tick “Remember for future payments”. New transactions from these payees are labelled the same way." />
      {!data || data.length === 0 ? (
        <p className="text-sm text-muted">No rules yet. Open an expense in Transactions to make one.</p>
      ) : (
        <ul className="divide-y divide-hairline text-sm">
          {data.map((r) => (
            <li key={r.payee} className="flex flex-wrap items-center gap-3 py-2">
              <span className="w-44 shrink-0 text-ink">{r.payee.split("|")[0]}{r.payee.includes("|") && <span className="block text-xs text-muted">only {r.payee.split("|")[1]}</span>}</span>
              <span className="min-w-0 flex-1 text-ink-2">{Object.entries(r.labels).map(([k, v]) => pretty(k, v)).join(" · ")}</span>
              <button onClick={() => forget(r.payee)} className="rounded-md border border-hairline px-2 py-1 text-xs text-ink-2 hover:bg-hairline">Forget</button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
