import { Fragment, useEffect, useState } from "react";
import { AccountsData, Txn, TxnKind, TxnResponse, useApi } from "../lib/api";
import { dateLabel, int, money, n } from "../lib/format";

const shortDate = (iso: string, withYear: boolean) => (withYear ? dateLabel(iso) : dateLabel(iso).replace(/ \d{4}$/, ""));
const shortAccount = (a: string) => a.replace(/(Assets|Liabilities):/g, "").replaceAll(":", " › ");
import { useQueryClient } from "@tanstack/react-query";
import LabelEditor from "../components/LabelEditor";
import { Card, Chip, Segmented, StatTile } from "../components/ui";

type KindFilter = "all" | Exclude<TxnKind, "opening">;

const pretty = (c: string) => c.replace(/^(Expenses|Income):/, "").replaceAll(":", " › ");

function monthName(ym: string) {
  const [y, m] = ym.split("-");
  return new Date(Number(y), Number(m) - 1, 1).toLocaleString("en", { month: "long", year: "numeric" });
}

function Category({ t }: { t: Txn }) {
  if (t.kind === "transfer") return <span className="text-muted">Transfer</span>;
  if (t.kind === "opening") return <span className="text-muted">Opening balance</span>;
  if (t.kind === "investment") return <span>Investment <span className="text-muted">· {t.category}</span></span>;
  if (t.kind === "asset") return <span>Asset purchase <span className="text-muted">· {t.category}</span></span>;
  return (
    <span className={t.needs_review ? "font-medium text-ink" : ""}>
      {pretty(t.category)}
    </span>
  );
}

function Flags({ t }: { t: Txn }) {
  // each flag is an icon plus a word: never colour alone
  return (
    <span className="flex flex-wrap gap-x-2 gap-y-0.5 text-xs text-ink-2">
      {t.tags.filter((x) => x !== "review").map((x) => <span key={x}>#{x}</span>)}
      {t.needs_review && <span className="font-medium text-ink">⚑ Needs review</span>}
      {t.card && <span title={`Card ${t.card}`}>💳 {t.card_label ?? t.card}</span>}
      {t.categorized && <span title="Categorized automatically; edit it to take over">auto · {t.categorized === "mapping" ? "merchant type" : t.categorized}</span>}
      {t.recurring && <span>↻ Recurring</span>}
      {t.major && <span>★ Major</span>}
    </span>
  );
}

function Amount({ t, ccy }: { t: Txn; ccy: string }) {
  const v = n(t.amount);
  const foreign = t.orig_ccy && t.orig_ccy !== ccy ? `${t.orig_ccy} ${Math.abs(n(t.orig_amount)).toLocaleString("de-CH", { minimumFractionDigits: 2 })}` : null;
  const tone = t.kind === "income" ? "text-good" : "text-ink";
  return (
    <div className="text-right">
      <div className={`tnum ${tone}`}>{t.kind === "income" ? "+" : ""}{money(v, ccy)}</div>
      {foreign && <div className="tnum text-xs text-muted">{foreign}</div>}
    </div>
  );
}

function Detail({ t, ccy }: { t: Txn; ccy: string }) {
  return (
    <div className="space-y-3 bg-hairline/40 px-4 py-3 text-xs text-ink-2">
      <table className="w-full max-w-2xl">
        <tbody>
          {t.postings.map((p, i) => (
            <tr key={i}>
              <td className="py-0.5 pr-4 text-ink">{p.account}</td>
              <td className="tnum py-0.5 pr-3 text-right text-ink">{Number(p.amount).toLocaleString("de-CH", { minimumFractionDigits: 2 })} {p.ccy}</td>
              <td className="py-0.5 text-muted">{[p.cost && `{${p.cost}}`, p.price && `@ ${p.price}`].filter(Boolean).join(" ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="flex flex-wrap gap-x-6 gap-y-1">
        <span>ref <code className="text-ink">{t.ref}</code></span>
        {t.file && <span>file <code className="text-ink">{t.file}:{t.line}</code></span>}
        {t.cadence && <span>{t.cadence.replace("_", "-")} · {t.necessity}</span>}
        {t.series_id && <span>series <code className="text-ink">{t.series_id}</code></span>}
        {t.tags.length > 0 && <span>{t.tags.map((x) => `#${x}`).join(" ")}</span>}
      </div>
      {t.kind === "expense" && t.ref && t.postings.filter((p) => p.account.startsWith("Expenses:")).length === 1 && <LabelEditor key={t.ref} t={t} ccy={ccy} />}
    </div>
  );
}

/** Links carry a selection: #transactions?month=2026-03&category=Expenses:Food&kind=expense */
export interface Initial { tag?: string; account?: string; month?: string; category?: string; kind?: string; cadence?: string; necessity?: string }

const KINDS = ["expense", "income", "transfer", "investment", "asset"];
const NAMES: Record<string, string> = { one_off: "one-off" };

export default function Transactions({ ccy, initial = {} }: { ccy: string; initial?: Initial }) {
  const initialAccount = initial.account ?? "";
  const [month, setMonth] = useState(initial.month ?? (initialAccount ? "all" : "latest")); // from an account link: its whole history
  const [kind, setKind] = useState<KindFilter>(KINDS.includes(initial.kind ?? "") ? (initial.kind as KindFilter) : "all");
  const qc = useQueryClient();
  const [tag, setTag] = useState(initial.tag ?? "");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [tagText, setTagText] = useState("");
  const [tagError, setTagError] = useState("");
  const [cats, setCats] = useState<string[]>((initial.category ?? "").split(",").filter(Boolean));
  const [depth, setDepth] = useState<"2" | "3">("2");
  const [cadence, setCadence] = useState(initial.cadence ?? "");
  const [necessity, setNecessity] = useState(initial.necessity ?? "");
  const [review, setReview] = useState(false);
  const [card, setCard] = useState("");
  const [flag, setFlag] = useState<"" | "recurring" | "major">("");
  const [text, setText] = useState("");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [account, setAccount] = useState(initialAccount);
  const [before, setBefore] = useState("latest"); // the month to return to when the review queue is closed
  useEffect(() => {
    const id = setTimeout(() => setQ(text), 250);
    return () => clearTimeout(id);
  }, [text]);

  const params = new URLSearchParams({ month, kind, q, flag });
  if (account) params.set("account", account);
  if (cats.length) params.set("category", cats.join(","));
  if (tag) params.set("tag", tag);
  params.set("depth", depth);
  if (cadence) params.set("cadence", cadence);
  if (necessity) params.set("necessity", necessity);
  if (review) params.set("review", "true");
  if (card) params.set("card", card);
  const { data, isFetching } = useApi<TxnResponse>(`/api/transactions?${params}`);
  const accountList = useApi<AccountsData>("/api/accounts").data;
  if (!data) return <p className="text-sm text-muted">Loading…</p>;

  const months = [...data.months].reverse();
  const idx = months.findIndex((m) => m.month === data.month);
  const go = (delta: number) => months[idx + delta] && setMonth(months[idx + delta].month);
  // the review queue spans every month: turning it on shows all of them, turning it off goes back
  const toggleReview = () => {
    if (review) {
      setReview(false);
      setMonth(before);
    } else {
      setBefore(month);
      setMonth("all");
      setReview(true);
    }
  };
  const s = data.summary;
  const tagInfo = data.tags.find((x) => x.tag === tag);
  const refs = data.rows.map((r) => r.ref).filter((r): r is string => !!r);
  const chosen = data.rows.filter((r) => r.ref && picked.has(r.ref));
  const allPicked = refs.length > 0 && refs.every((r) => picked.has(r));
  const flip = (ref: string) => setPicked((p) => { const n = new Set(p); n.has(ref) ? n.delete(ref) : n.add(ref); return n; });
  const chosenTags = [...new Set(chosen.flatMap((r) => r.tags))].filter((x) => x !== "review");
  const retag = async (change: { tags_add?: string[]; tags_remove?: string[] }) => {
    setTagError("");
    const [first, ...also] = chosen.map((r) => r.ref!);
    const res = await fetch("/api/transactions/labels", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ref: first, also, ...change }) });
    if (!res.ok) return setTagError((await res.json().catch(() => ({}))).detail ?? `${res.status}`);
    setPicked(new Set());
    setTagText("");
    await qc.invalidateQueries();
  };
  const spentTotal = data.by_category.reduce((t, c) => t + n(c.amount), 0);
  const biggest = Math.max(1, ...data.by_category.map((c) => n(c.amount)));
  // a picked category covers everything under it, so picking a group ticks its sub-categories
  const covered = (c: string) => cats.some((k) => c === k || c.startsWith(k + ":"));
  const toggle = (c: string) => {
    setKind("expense");
    const owner = cats.find((k) => c === k || c.startsWith(k + ":"));
    if (!owner) return setCats([...cats.filter((k) => !k.startsWith(c + ":")), c]);
    if (owner === c) return setCats(cats.filter((k) => k !== c));
    // unticking one part of a ticked group keeps the other parts that are on show
    const rest = data.by_category.map((x) => x.category).filter((x) => x !== c && x.startsWith(owner + ":"));
    setCats([...cats.filter((k) => k !== owner), ...rest]);
  };
  const untick = (c: string) => setCats(cats.filter((k) => k !== c));
  const label = (c: string) => pretty(c);
  const scope = data.month === "all" ? "All months" : monthName(data.month);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <div className="inline-flex items-center rounded-lg border border-hairline bg-surface text-sm">
          <button aria-label="Previous month" disabled={data.month === "all" || idx >= months.length - 1} onClick={() => go(1)} className="px-3 py-1.5 text-ink-2 hover:bg-hairline disabled:opacity-30">◀</button>
          <select aria-label="Month" value={data.month} onChange={(e) => setMonth(e.target.value)} className="bg-surface px-2 py-1.5 text-ink outline-none">
            <option value="all">All months</option>
            {months.map((m) => (
              <option key={m.month} value={m.month}>{monthName(m.month)} · {m.count}{m.needs_review ? ` · ⚑${m.needs_review}` : ""}</option>
            ))}
          </select>
          <button aria-label="Next month" disabled={data.month === "all" || idx <= 0} onClick={() => go(-1)} className="px-3 py-1.5 text-ink-2 hover:bg-hairline disabled:opacity-30">▶</button>
        </div>
        <Segmented<KindFilter>
          label="Kind"
          value={kind}
          onChange={setKind}
          options={[{ value: "all", label: "All" }, { value: "expense", label: "Expenses" }, { value: "income", label: "Income" }, { value: "transfer", label: "Transfers" }, { value: "investment", label: "Investments" }, { value: "asset", label: "Assets" }]}
        />
        <select aria-label="Account" value={account} onChange={(e) => setAccount(e.target.value)} className="max-w-[210px] rounded-lg border border-hairline bg-surface px-2 py-1.5 text-sm text-ink">
          <option value="">All accounts</option>
          {accountList?.institutions.map((inst) => {
            const prefix = `inst:${inst.name}`;
            return (
              <optgroup key={inst.name} label={inst.name}>
                <option value={prefix}>{inst.name}, all accounts</option>
                {inst.accounts.length > 1 && inst.accounts.map((a) => <option key={a.account} value={a.account}>{inst.name} › {a.name}</option>)}
              </optgroup>
            );
          })}
        </select>
        {cadence && <Chip on onClick={() => setCadence("")}>{NAMES[cadence] ?? cadence} ✕</Chip>}
        {necessity && <Chip on onClick={() => setNecessity("")}>{necessity} ✕</Chip>}
        {data.tags.length > 0 && (
          <select aria-label="Tag" value={tag} onChange={(e) => setTag(e.target.value)} className="max-w-[210px] rounded-lg border border-hairline bg-surface px-2 py-1.5 text-sm text-ink">
            <option value="">All tags</option>
            {data.tags.map((x) => <option key={x.tag} value={x.tag}>#{x.tag} · {x.count}</option>)}
          </select>
        )}
        {data.cards.length > 1 && (
          <select aria-label="Card" value={card} onChange={(e) => setCard(e.target.value)} className="max-w-[210px] rounded-lg border border-hairline bg-surface px-2 py-1.5 text-sm text-ink">
            <option value="">All cards</option>
            {data.cards.map((c) => <option key={c.value} value={c.value}>💳 {c.value} · {c.count}</option>)}
          </select>
        )}
        <Chip on={review} onClick={toggleReview}>⚑ Needs review ({data.review_total})</Chip>
        <Chip on={flag === "recurring"} onClick={() => setFlag(flag === "recurring" ? "" : "recurring")}>↻ Recurring</Chip>
        <Chip on={flag === "major"} onClick={() => setFlag(flag === "major" ? "" : "major")}>★ Major</Chip>
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Search payee, note, account…"
          aria-label="Search"
          className="min-w-[200px] flex-1 rounded-lg border border-hairline bg-surface px-3 py-1.5 text-sm text-ink outline-none focus:border-accent"
        />
      </div>

      <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(165px,1fr))]">
        <Card><StatTile label={`${scope}`} value={int(s.count)} hint="transactions in total" /></Card>
        <Card><StatTile label="Income" value={money(s.income, ccy, 0)} hint="salary, shares, dividends" /></Card>
        <Card><StatTile label="Expenses" value={money(s.expenses, ccy, 0)} hint="incl. taxes and major purchases" /></Card>
        <Card>
          <StatTile label="Needs review" value={int(s.needs_review)} hint={s.needs_review ? `${money(s.needs_review_spend, ccy, 0)} not yet categorized` : "everything is labelled"} />
          {data.review_total > 0 && !review && <button onClick={toggleReview} className="mt-2 text-xs text-accent underline">Show all {data.review_total} in every month</button>}
        </Card>
      </div>

      {tagInfo && (
        <div className="flex flex-wrap items-center gap-3 rounded-lg border border-hairline bg-surface px-4 py-2 text-sm">
          <span className="font-medium text-ink">#{tagInfo.tag}</span>
          <span className="text-ink-2">{tagInfo.count} transaction{tagInfo.count === 1 ? "" : "s"} · {money(tagInfo.spend, ccy, 0)} spent · {dateLabel(tagInfo.first)} to {dateLabel(tagInfo.last)}</span>
          <button onClick={() => setTag("")} className="ml-auto text-xs text-accent hover:underline">Show all tags</button>
        </div>
      )}

      {(data.by_category.length > 0 || cats.length > 0) && (
        <Card>
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-medium text-ink">
              Where it went
              <span className="ml-2 font-normal text-muted">{scope}, as paid. Tick categories to show only those.</span>
            </div>
            <div className="flex items-center gap-2">
              <Segmented<"2" | "3"> label="Detail" value={depth} onChange={setDepth} options={[{ value: "2", label: "Groups" }, { value: "3", label: "Categories" }]} />
              <button onClick={() => setCats(data.by_category.map((c) => c.category))} className="text-xs text-accent hover:underline">Select all</button>
              <button onClick={() => setCats([])} disabled={cats.length === 0} className="text-xs text-accent hover:underline disabled:text-muted disabled:no-underline">Clear</button>
            </div>
          </div>
          {cats.length > 0 && (
            <div className="mb-2 flex flex-wrap items-center gap-1.5" aria-label="Selected categories">
              {cats.map((c) => <Chip key={c} on onClick={() => untick(c)}>{label(c)} ✕</Chip>)}
              <button onClick={() => setCats([])} className="ml-1 text-xs text-accent hover:underline">Show all expenses</button>
            </div>
          )}
          <ul className="max-h-[22rem] space-y-0.5 overflow-y-auto">
            {data.by_category.map((c) => {
              const on = covered(c.category);
              return (
                <li key={c.category}>
                  <label className={`grid cursor-pointer grid-cols-[auto_minmax(0,12rem)_1fr_auto_auto] items-center gap-3 rounded-md px-1 py-1 text-sm hover:bg-hairline/50 ${on ? "bg-hairline/40" : ""}`}>
                    <input type="checkbox" checked={on} onChange={() => toggle(c.category)} aria-label={label(c.category)} />
                    <span className="truncate text-ink">{label(c.category)}</span>
                    <span className="block h-3 rounded-r bg-accent" style={{ width: `${Math.max(2, (n(c.amount) / biggest) * 100)}%`, opacity: cats.length && !on ? 0.35 : 1 }} aria-hidden />
                    <span className="tnum text-right text-ink">{money(c.amount, ccy, 0)}</span>
                    <span className="tnum w-24 text-right text-xs text-muted">{spentTotal ? Math.round((n(c.amount) / spentTotal) * 100) : 0}% · {c.count} txn{c.count === 1 ? "" : "s"}</span>
                  </label>
                </li>
              );
            })}
          </ul>
          <p className="mt-2 text-right text-xs text-muted">
            {cats.length > 0 && `Selected ${money(data.by_category.filter((c) => covered(c.category)).reduce((t, c) => t + n(c.amount), 0), ccy, 0)} of `}
            {money(spentTotal, ccy, 0)} total, including taxes and major purchases, which Spending leaves out.
          </p>
        </Card>
      )}

      {chosen.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-ink bg-surface px-4 py-2 text-sm" role="region" aria-label="Tag selected transactions">
          <span className="font-medium text-ink">{chosen.length} selected</span>
          <input list="all-tags" value={tagText} onChange={(e) => setTagText(e.target.value)} placeholder="Tag, e.g. canada-2026" aria-label="Tag name"
            onKeyDown={(e) => e.key === "Enter" && tagText.trim() && retag({ tags_add: [tagText] })}
            className="w-52 rounded-md border border-hairline bg-surface px-2 py-1 text-ink outline-none focus:border-accent" />
          <datalist id="all-tags">{data.tags.map((x) => <option key={x.tag} value={x.tag} />)}</datalist>
          <button onClick={() => retag({ tags_add: [tagText] })} disabled={!tagText.trim()} className="rounded-md bg-ink px-3 py-1 text-surface disabled:opacity-40">Add tag</button>
          {chosenTags.map((x) => <button key={x} onClick={() => retag({ tags_remove: [x] })} className="rounded-md border border-hairline px-2 py-1 text-xs text-ink-2 hover:bg-hairline" aria-label={`Remove tag ${x}`}>#{x} ✕</button>)}
          <button onClick={() => setPicked(new Set())} className="ml-auto text-xs text-accent hover:underline">Clear selection</button>
          {tagError && <span role="alert" className="w-full text-bad">{tagError}</span>}
        </div>
      )}

      <Card className={`p-0 transition-opacity ${isFetching ? "opacity-60" : ""}`}>
        {data.rows.length === 0 ? (
          <div className="py-10 text-center text-sm text-muted">
            <p>No transactions match{data.month !== "all" ? ` in ${scope}` : ""}.</p>
            {data.month !== "all" && <button onClick={() => setMonth("all")} className="mt-2 text-accent underline">Search all months</button>}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-hairline text-left text-xs text-muted">
                  <th className="w-8 py-2 pl-2 sm:pl-4"><input type="checkbox" checked={allPicked} onChange={() => setPicked(allPicked ? new Set() : new Set(refs))} aria-label="Select all shown" /></th>
                  <th className="px-2 py-2 font-medium">Date</th>
                  <th className="px-2 py-2 font-medium">Description</th>
                  <th className="hidden px-2 py-2 font-medium sm:table-cell">Category</th>
                  <th className="hidden px-2 py-2 font-medium sm:table-cell">Flags</th>
                  <th className="px-4 py-2 text-right font-medium">{ccy}</th>
                </tr>
              </thead>
              <tbody>
                {data.rows.map((t, i) => {
                  const key = `${t.ref}-${i}`;
                  const expanded = open === key;
                  return (
                    <Fragment key={key}>
                      <tr
                        tabIndex={0}
                        role="button"
                        aria-expanded={expanded}
                        onClick={() => setOpen(expanded ? null : key)}
                        onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), setOpen(expanded ? null : key))}
                        className={`cursor-pointer border-b border-hairline align-top hover:bg-hairline/50 focus:bg-hairline/50 focus:outline-none ${t.needs_review ? "bg-hairline/30" : ""}`}
                      >
                        <td className="py-2 pl-2 align-top sm:pl-4" onClick={(e) => e.stopPropagation()}>
                          {t.ref && <input type="checkbox" checked={picked.has(t.ref)} onChange={() => flip(t.ref!)} aria-label={`Select ${t.payee || t.narration}`} />}
                        </td>
                        <td className="tnum whitespace-nowrap px-2 py-2 text-ink-2">{shortDate(t.date, data.month === "all")}</td>
                        <td className="px-2 py-2">
                          <div className="text-ink">{t.payee || t.narration}</div>
                          <div className="text-xs text-muted">{[t.payee && t.narration, shortAccount(t.account)].filter(Boolean).join(" · ")}</div>
                          {/* phones: category and flags under the description, so the amount stays on screen */}
                          <div className="mt-1 flex flex-wrap items-center gap-x-2 text-xs text-ink-2 sm:hidden"><Category t={t} /><Flags t={t} /></div>
                        </td>
                        <td className="hidden px-2 py-2 text-ink sm:table-cell"><Category t={t} /></td>
                        <td className="hidden px-2 py-2 sm:table-cell"><Flags t={t} /></td>
                        <td className="whitespace-nowrap py-2 pl-2 pr-4"><Amount t={t} ccy={ccy} /></td>
                      </tr>
                      {expanded && (
                        <tr className="border-b border-hairline"><td colSpan={6} className="p-0"><Detail t={t} ccy={ccy} /></td></tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <div className="flex justify-between border-t border-hairline px-4 py-2 text-xs text-muted">
          <span>{int(data.matching)} shown{data.truncated ? ` (first ${int(data.rows.length)})` : ""}</span>
          <span>Tap a row for its postings and file</span>
        </div>
      </Card>
    </div>
  );
}
