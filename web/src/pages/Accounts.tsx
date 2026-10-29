import { useMemo, useState } from "react";
import { AccountLedger, Account, AccountsData, LedgerRow, useApi } from "../lib/api";
import { dateLabel, money, n } from "../lib/format";
import { lineChart } from "../lib/charts";
import { go } from "../lib/route";
import { useMode } from "../lib/theme";
import { ChartCard } from "../components/Chart";
import { Sparkline } from "../components/Sparkline";
import { Card, CardHeader, StatTile, StatusBadge, Loading } from "../components/ui";

const CASH = ["CHF", "USD", "EUR"];
const pretty = (a: string) => a.replace(/^(Expenses|Income|Assets|Liabilities):/, "").replaceAll(":", " › ");
const units = (v: string | number) => Number(v).toLocaleString("de-CH", { maximumFractionDigits: 4 });
export const native = (v: string | number, commodity: string) => (CASH.includes(commodity) ? money(v, commodity) : `${units(v)} ${commodity}`);
const monthName = (ym: string) => new Date(Number(ym.slice(0, 4)), Number(ym.slice(5)) - 1, 1).toLocaleString("en", { month: "long", year: "numeric" });

function Balance({ a, ccy }: { a: Account; ccy: string }) {
  if (a.positions.length === 0) return <span className="text-muted">{money(0, ccy)}</span>;
  return (
    <div className="space-y-0.5">
      {a.positions.map((p) => (
        <div key={p.commodity} className="tnum whitespace-nowrap text-ink">{native(p.units, p.commodity)}</div>
      ))}
    </div>
  );
}

function Overview({ ccy }: { ccy: string }) {
  const { data, isFetching } = useApi<AccountsData>("/api/accounts");
  if (!data) return <Loading what="Loading accounts" />;
  return (
    <div className={`space-y-6 transition-opacity ${isFetching ? "opacity-60" : ""}`}>
      <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(170px,1fr))]">
        <Card><StatTile label="Assets" value={money(data.assets, ccy, 0)} hint="all accounts, at market value" /></Card>
        <Card><StatTile label="Liabilities" value={money(data.liabilities, ccy, 0)} hint="mortgage and card" /></Card>
        <Card><StatTile label="Net worth" value={money(data.total, ccy, 0)} hint="the same figure as the Net worth page" /></Card>
      </div>
      {data.institutions.map((inst) => (
        <Card key={inst.name} className="!p-0">
          <div className="flex items-baseline justify-between gap-3 px-4 pt-4 sm:px-5">
            <h3 className="text-sm font-semibold text-ink">{inst.name}</h3>
            <span className="tnum text-sm font-medium text-ink">{money(inst.value, ccy, 0)}</span>
          </div>
          <div className="overflow-x-auto px-2 pb-2">
            <table className="mt-2 w-full border-collapse text-sm sm:min-w-[640px] sm:table-fixed">
              <colgroup className="hidden sm:table-column-group">
                <col style={{ width: "27%" }} /><col style={{ width: "21%" }} /><col style={{ width: "14%" }} />
                <col style={{ width: "15%" }} /><col style={{ width: "13%" }} /><col style={{ width: "10%" }} />
              </colgroup>
              <thead>
                <tr className="border-b border-hairline text-left text-xs text-muted">
                  <th className="px-3 py-2 font-medium">Account</th>
                  <th className="hidden px-2 py-2 font-medium sm:table-cell">Balance</th>
                  <th className="px-2 py-2 text-right font-medium">{ccy}</th>
                  <th className="hidden px-2 py-2 font-medium sm:table-cell">Trend</th>
                  <th className="hidden px-2 py-2 font-medium sm:table-cell">Last activity</th>
                  <th className="hidden px-2 py-2 font-medium sm:table-cell" title="Date of the latest balance assertion against a statement">Checked</th>
                </tr>
              </thead>
              <tbody>
                {inst.accounts.map((a) => (
                  <tr
                    key={a.account}
                    tabIndex={0}
                    role="link"
                    onClick={() => go("accounts", { account: a.account })}
                    onKeyDown={(e) => e.key === "Enter" && go("accounts", { account: a.account })}
                    className="cursor-pointer border-b border-hairline last:border-0 hover:bg-hairline/50 focus:bg-hairline/50 focus:outline-none"
                  >
                    <td className="px-3 py-2.5 text-ink">{a.name}<span className="ml-2 text-xs text-muted">{a.transactions} {a.transactions === 1 ? "transaction" : "transactions"}</span></td>
                    <td className="hidden px-2 py-2.5 sm:table-cell"><Balance a={a} ccy={ccy} /></td>
                    <td className="tnum whitespace-nowrap px-2 py-2.5 text-right text-ink">{money(a.value, ccy, 0)}</td>
                    <td className="hidden px-2 py-2.5 sm:table-cell"><Sparkline values={a.series.map((p) => n(p.value))} label={`${a.name} over the last two years`} /></td>
                    <td className="hidden whitespace-nowrap px-2 py-2.5 text-xs text-ink-2 sm:table-cell">{a.last_activity ? dateLabel(a.last_activity) : "–"}</td>
                    <td className="hidden whitespace-nowrap px-2 py-2.5 text-xs sm:table-cell">{a.last_checked ? <StatusBadge kind="good">{dateLabel(a.last_checked)}</StatusBadge> : <span className="text-muted">–</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ))}
      <p className="text-xs text-muted">Click or tap an account for every posting with its running balance. “Checked” will show the date of a statement balance once importers record one.</p>
    </div>
  );
}

function Row({ r }: { r: LedgerRow }) {
  const out = n(r.units) < 0;
  return (
    <tr className="border-b border-hairline align-top">
      <td className="tnum whitespace-nowrap px-4 py-2 text-ink-2">{dateLabel(r.date)}</td>
      <td className="px-2 py-2">
        <div className="text-ink">{r.payee || r.narration}{r.needs_review && <span className="ml-2 text-xs font-medium">⚑ Needs review</span>}</div>
        <div className="text-xs text-muted">{[r.payee && r.narration, pretty(r.counterparty)].filter(Boolean).join(" · ")}</div>
      </td>
      <td className={`tnum whitespace-nowrap px-2 py-2 text-right ${out ? "text-ink" : "text-good"}`}>{out ? "" : "+"}{native(r.units, r.commodity)}</td>
      <td className="tnum whitespace-nowrap px-4 py-2 text-right text-ink">{native(r.balance, r.commodity)}</td>
    </tr>
  );
}

function Detail({ account, ccy }: { account: string; ccy: string }) {
  const [month, setMonth] = useState("all");
  const { data, isFetching, error } = useApi<AccountLedger>(`/api/accounts/ledger?${new URLSearchParams({ account, month })}`);
  const mode = useMode();
  const info = data?.info ?? null;
  const chart = useMemo(() => (info ? lineChart(info.series.map((p) => dateLabel(p.date).replace(/^\d+ /, "")), info.series.map((p) => n(p.value)), mode, ccy, info.name) : null), [info, mode, ccy]);
  if (error) return <p className="text-sm text-bad">▲ {(error as Error).message}</p>;
  if (!data || !info || !chart) return <Loading what="Loading accounts" />;
  const rows = [...data.rows].reverse(); // newest first
  const months = [...data.months].reverse();
  return (
    <div className={`space-y-6 transition-opacity ${isFetching ? "opacity-60" : ""}`}>
      <div className="flex flex-wrap items-center gap-3">
        <button onClick={() => go("accounts")} className="rounded-lg border border-hairline px-3 py-1.5 text-sm text-ink-2 hover:bg-hairline">← All accounts</button>
        <div className="text-sm text-ink-2"><span className="font-medium text-ink">{pretty(account)}</span> · {info.kind}</div>
        <button onClick={() => go("transactions", { account })} className="ml-auto rounded-lg border border-hairline px-3 py-1.5 text-sm text-ink-2 hover:bg-hairline">Show in Transactions →</button>
      </div>
      <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(170px,1fr))]">
        <Card>
          <div className="text-xs text-muted">Balance</div>
          <div className="mt-1 space-y-0.5">
            {Object.entries(data.balances).map(([c, v]) => <div key={c} className="whitespace-nowrap text-xl font-semibold tracking-tight text-ink">{native(v, c)}</div>)}
          </div>
        </Card>
        <Card><StatTile label={`Value in ${ccy}`} value={money(info.value, ccy, 0)} hint="at market value" /></Card>
        <Card><StatTile label="Transactions" value={String(info.transactions)} hint={info.last_activity ? `last on ${dateLabel(info.last_activity)}` : ""} /></Card>
        <Card><StatTile label="Checked against a statement" value={info.last_checked ? dateLabel(info.last_checked) : "Not yet"} hint="latest balance assertion" /></Card>
      </div>
      <ChartCard
        title="Balance over time"
        subtitle={`Month-end value in ${ccy}`}
        option={chart}
        height={240}
        columns={[{ key: "d", label: "Month end" }, { key: "v", label: ccy, align: "right" }]}
        rows={info.series.map((p) => ({ d: dateLabel(p.date), v: money(p.value, ccy, 0) }))}
      />
      <Card className="!p-0">
        <div className="flex flex-wrap items-center justify-between gap-3 px-5 pt-4">
          <CardHeader title="Every posting" subtitle="Newest first; the balance column is the balance after that posting" />
          <select aria-label="Month" value={data.month} onChange={(e) => setMonth(e.target.value)} className="rounded-lg border border-hairline bg-surface px-2 py-1.5 text-sm text-ink">
            <option value="all">All months · {data.months.reduce((s, m) => s + m.count, 0)}</option>
            {months.map((m) => <option key={m.month} value={m.month}>{monthName(m.month)} · {m.count}</option>)}
          </select>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-hairline text-left text-xs text-muted">
                <th className="px-4 py-2 font-medium">Date</th>
                <th className="px-2 py-2 font-medium">Description</th>
                <th className="px-2 py-2 text-right font-medium">Amount</th>
                <th className="px-4 py-2 text-right font-medium">Balance</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => <Row key={`${r.ref}-${i}`} r={r} />)}
              {data.month !== "all" && Object.keys(data.balance_before).length > 0 && (
                <tr className="text-xs text-muted">
                  <td colSpan={3} className="px-4 py-2">Balance brought forward</td>
                  <td className="tnum whitespace-nowrap px-4 py-2 text-right">{Object.entries(data.balance_before).map(([c, v]) => native(v, c)).join(" · ")}</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="border-t border-hairline px-4 py-2 text-xs text-muted">{data.matching} postings{data.truncated ? ` (most recent ${rows.length} shown)` : ""}</div>
      </Card>
    </div>
  );
}

export default function Accounts({ ccy, account }: { ccy: string; account: string | null }) {
  return account ? <Detail key={account} account={account} ccy={ccy} /> : <Overview ccy={ccy} />;
}
