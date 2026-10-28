import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useApi } from "../lib/api";
import { dateLabel, int } from "../lib/format";
import { Card, CardHeader } from "./ui";

interface CardRow { source: string; card: string; label: string | null; count: number; first: string; last: string }

function Row({ r }: { r: CardRow }) {
  const qc = useQueryClient();
  const [name, setName] = useState(r.label ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const save = async () => {
    setBusy(true);
    setError("");
    const res = await fetch(`/api/cards/${r.source}/${r.card}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ label: name }) });
    setBusy(false);
    if (!res.ok) return setError((await res.json().catch(() => ({}))).detail ?? `${res.status}`);
    await qc.invalidateQueries();
  };
  return (
    <li className="flex flex-wrap items-center gap-3 py-2 text-sm">
      <span className="w-24 shrink-0 font-mono text-xs text-muted" title="The start of the id the card issuer uses">{r.card}</span>
      <span className="w-52 shrink-0 text-xs text-ink-2">{int(r.count)} payments<br />{dateLabel(r.first)} to {dateLabel(r.last)}</span>
      <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Whose card? e.g. Anna (Visa)" aria-label={`Name for card ${r.card}`} className="min-w-[200px] flex-1 rounded-md border border-hairline bg-surface px-2 py-1 text-sm text-ink" />
      <button onClick={save} disabled={busy || name.trim() === (r.label ?? "")} className="rounded-md bg-ink px-3 py-1 text-sm text-surface disabled:opacity-40">{busy ? "Saving…" : "Save"}</button>
      {error && <span role="alert" className="text-bad">{error}</span>}
    </li>
  );
}

/** Name each card so payments show whose card they were made on. Names live in rules/cards.yaml in
 *  your data repo; the ledger only holds the card's short id, so renaming never rewrites it. */
export default function Cards() {
  const { data } = useApi<CardRow[]>("/api/cards");
  if (!data || data.length === 0) return null;
  return (
    <Card>
      <CardHeader title="Cards" subtitle="Payments are tagged with the card they were made on. Open a payment in Transactions to see which card, then name it here." />
      <ul className="divide-y divide-hairline">{data.map((r) => <Row key={`${r.source}-${r.card}`} r={r} />)}</ul>
    </Card>
  );
}
