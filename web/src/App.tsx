import { useEffect, useState } from "react";
import Chat from "./components/Chat";
import EmptyState from "./components/EmptyState";
import { ProgressBar } from "./components/ui";
import { Meta, useApi } from "./lib/api";
import { dateLabel } from "./lib/format";
import { useRoute } from "./lib/route";
import { Pref, applyPref, loadPref } from "./lib/theme";
import Accounts from "./pages/Accounts";
import Allocation from "./pages/Allocation";
import Fire from "./pages/Fire";
import Dashboards from "./pages/Dashboards";
import NetWorth from "./pages/NetWorth";
import Overview from "./pages/Overview";
import Review from "./pages/Review";
import Settings from "./pages/Settings";
import Spending from "./pages/Spending";
import Transactions from "./pages/Transactions";
import Trends from "./pages/Trends";

const PAGES = [
  { id: "overview", label: "Overview" },
  { id: "spending", label: "Spending" },
  { id: "transactions", label: "Transactions" },
  { id: "review", label: "Review" },
  { id: "accounts", label: "Accounts" },
  { id: "trends", label: "Trends" },
  { id: "networth", label: "Net worth" },
  { id: "fire", label: "FIRE" },
  { id: "allocation", label: "Allocation" },
  { id: "dashboards", label: "Dashboards" },
  { id: "settings", label: "Settings" },
] as const;
type PageId = (typeof PAGES)[number]["id"];

export default function App() {
  const route = useRoute();
  const page: PageId = PAGES.find((p) => p.id === route.page)?.id ?? "overview";
  const [chatOpen, setChatOpen] = useState(false);
  const [pref, setPref] = useState<Pref>(loadPref);
  const { data: meta } = useApi<Meta>("/api/meta");
  useEffect(() => {
    applyPref(pref);
  }, [pref]);
  const ccy = meta?.base_currency ?? "CHF";
  const empty = meta?.empty === true && page !== "settings";

  const [menuOpen, setMenuOpen] = useState(false);
  useEffect(() => setMenuOpen(false), [page]);
  const title = PAGES.find((p) => p.id === page)!.label;
  const navBody = (
    <>
      <ul className="space-y-1">
        {PAGES.map((p) => (
          <li key={p.id}>
            <a href={`#${p.id}`} aria-current={page === p.id ? "page" : undefined} className={`block rounded-lg px-3 py-2 text-sm ${page === p.id ? "bg-ink text-surface" : "text-ink-2 hover:bg-hairline"}`}>{p.label}</a>
          </li>
        ))}
      </ul>
      <div className="mt-auto space-y-2 pt-6 text-xs text-muted">
        {meta && <p>{meta.as_of ? `Data as of ${dateLabel(meta.as_of)}` : "No transactions yet"}<br />Privacy: {meta.privacy_level}</p>}
        <select aria-label="Theme" value={pref} onChange={(e) => setPref(e.target.value as Pref)} className="w-full rounded-md border border-hairline bg-surface px-2 py-1 text-ink-2">
          <option value="system">Theme: system</option>
          <option value="light">Theme: light</option>
          <option value="dark">Theme: dark</option>
        </select>
      </div>
    </>
  );
  const askButton = (
    <button onClick={() => setChatOpen((v) => !v)} aria-pressed={chatOpen} className="shrink-0 rounded-lg border border-hairline bg-surface px-3 py-1.5 text-sm text-ink-2 hover:bg-hairline">
      {chatOpen ? "Hide assistant" : <><span className="sm:hidden">Ask</span><span className="hidden sm:inline">Ask the assistant</span></>}
    </button>
  );

  return (
    <div className="flex h-[100dvh] flex-col text-ink md:flex-row">
      <ProgressBar />
      {/* phones: a top bar and a drawer instead of the sidebar */}
      <div className="safe-top safe-x flex shrink-0 items-center gap-3 border-b border-hairline bg-surface px-4 py-2 md:hidden">
        <button onClick={() => setMenuOpen(true)} aria-label="Menu" aria-expanded={menuOpen} className="-ml-2 rounded-lg px-2 py-1 text-xl leading-none text-ink-2 hover:bg-hairline">☰</button>
        <span className="font-semibold tracking-tight">Fin</span>
        <span className="min-w-0 flex-1 truncate text-sm text-muted">{title}</span>
        {askButton}
      </div>
      {menuOpen && (
        <div className="fixed inset-0 z-30 md:hidden" role="dialog" aria-label="Menu">
          <div className="absolute inset-0 bg-black/40" onClick={() => setMenuOpen(false)} />
          <nav className="safe-top safe-bottom absolute inset-y-0 left-0 flex w-64 max-w-[80vw] flex-col overflow-y-auto bg-surface p-4 shadow-2xl">
            <div className="mb-4 flex items-center justify-between">
              <span className="text-lg font-semibold tracking-tight">Fin</span>
              <button onClick={() => setMenuOpen(false)} aria-label="Close menu" className="rounded-lg px-2 py-1 text-ink-2 hover:bg-hairline">✕</button>
            </div>
            {navBody}
          </nav>
        </div>
      )}
      <nav className="hidden w-52 shrink-0 flex-col overflow-y-auto border-r border-hairline bg-surface p-4 md:flex">
        <div className="mb-6 text-lg font-semibold tracking-tight">Fin</div>
        {navBody}
      </nav>
      <main className="safe-x safe-bottom min-h-0 min-w-0 flex-1 overflow-y-auto p-4 md:p-6">
        <header className="mb-6 hidden items-center justify-between md:flex">
          <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
          {askButton}
        </header>
        {empty && meta && <EmptyState dataDir={meta.data_dir} />}
        {!empty && page === "overview" && <Overview ccy={ccy} />}
        {!empty && page === "spending" && <Spending ccy={ccy} />}
        {!empty && page === "transactions" && <Transactions key={route.params.toString()} ccy={ccy} initial={Object.fromEntries(route.params)} />}
        {!empty && page === "review" && <Review />}
        {!empty && page === "accounts" && <Accounts ccy={ccy} account={route.params.get("account")} />}
        {!empty && page === "trends" && <Trends ccy={ccy} />}
        {!empty && page === "networth" && <NetWorth ccy={ccy} />}
        {!empty && page === "fire" && <Fire ccy={ccy} />}
        {!empty && page === "allocation" && <Allocation ccy={ccy} />}
        {!empty && page === "dashboards" && <Dashboards />}
        {page === "settings" && <Settings />}
      </main>
      {/* hidden, not unmounted, so closing the drawer does not lose the conversation */}
      <aside className={`${chatOpen ? "flex" : "hidden"} safe-top safe-bottom fixed inset-0 z-20 flex-col bg-surface sm:left-auto sm:w-[400px] sm:border-l sm:border-hairline sm:shadow-2xl xl:static xl:shadow-none`}>
        <div className="flex shrink-0 items-center justify-between border-b border-hairline px-4 py-2 sm:hidden">
          <span className="text-sm font-medium">Assistant</span>
          <button onClick={() => setChatOpen(false)} aria-label="Close assistant" className="rounded-lg px-2 py-1 text-ink-2 hover:bg-hairline">✕</button>
        </div>
        <div className="min-h-0 flex-1"><Chat enabled={(meta?.chat ?? false) && !meta?.empty} emptyLedger={meta?.empty === true} /></div>
      </aside>
    </div>
  );
}
