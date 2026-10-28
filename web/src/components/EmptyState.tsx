import { useQueryClient } from "@tanstack/react-query";
import { ReactNode } from "react";
import { Card } from "./ui";

const EXAMPLE_ACCOUNTS = `; ledger/accounts.beancount
2026-01-01 open Assets:Bank:Checking CHF
2026-01-01 open Expenses:Food:Groceries CHF
  cadence: "regular"
  necessity: "essential"`;

const EXAMPLE_TXN = `; ledger/manual/2026.beancount
2026-01-05 * "Coop" "Groceries"
  ref: "manual:1"
  Expenses:Food:Groceries   42.50 CHF
  Assets:Bank:Checking`;

function Code({ children }: { children: string }) {
  return <pre className="overflow-x-auto rounded-lg bg-hairline p-3 text-xs leading-relaxed text-ink">{children}</pre>;
}

function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <Card>
      <div className="flex gap-4">
        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-hairline text-sm font-semibold text-ink">{n}</div>
        <div className="min-w-0 flex-1 space-y-2 text-sm text-ink-2">
          <h3 className="font-semibold text-ink">{title}</h3>
          {children}
        </div>
      </div>
    </Card>
  );
}

/** Shown instead of the report pages while the ledger has no transactions. */
export default function EmptyState({ dataDir }: { dataDir: string }) {
  const qc = useQueryClient();
  return (
    <div className="max-w-3xl space-y-4">
      <Card>
        <h2 className="text-lg font-semibold text-ink">Your ledger is empty</h2>
        <p className="mt-1 text-sm text-ink-2">
          There is nothing to chart yet. Fin reads plain Beancount files, so anything you add to them shows up here. Your data lives in{" "}
          <code className="rounded bg-hairline px-1 py-0.5 text-xs text-ink">{dataDir}</code>.
        </p>
        <button onClick={() => qc.invalidateQueries()} className="mt-3 rounded-lg bg-ink px-3 py-1.5 text-sm text-surface">
          Check again
        </button>
      </Card>
      <Step n={1} title="Look around with demo data">
        <p>Two years of invented transactions, so you can try every page and the assistant without touching your own data. In a terminal, from the fin folder:</p>
        <Code>{`.venv/bin/fin demo-data ~/gits/fin-demo-data\nFIN_DATA=~/gits/fin-demo-data .venv/bin/fin serve --port 8001`}</Code>
      </Step>
      <Step n={2} title="Add a first transaction by hand">
        <p>Open the accounts you use, then record a transaction. Save, press “Check again”.</p>
        <Code>{EXAMPLE_ACCOUNTS}</Code>
        <Code>{EXAMPLE_TXN}</Code>
        <p>
          <code className="rounded bg-hairline px-1 py-0.5 text-xs text-ink">fin check</code> reports mistakes, and{" "}
          <code className="rounded bg-hairline px-1 py-0.5 text-xs text-ink">fin commit</code> saves a version in git.
        </p>
      </Step>
      <Step n={3} title="Import statements">
        <p>Importers for UBS, Swisscard, Wise, IBKR and Schwab are not built yet. They come next and need one sample export from each.</p>
      </Step>
    </div>
  );
}
