import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Fire as FireData, errorText, saveFire } from "../lib/api";
import { dateLabel, money, n, pct } from "../lib/format";
import { curveChart, projectionChart } from "../lib/charts";
import { useMode } from "../lib/theme";
import { ChartCard } from "../components/Chart";
import { Card, CardHeader, Chip, DataTable, StatTile, StatusBadge } from "../components/ui";

interface Inputs { withdrawal: string; ret: string; spend: string; birth: string; access: string; target: string }

const fromSaved = (s: FireData["saved"]): Inputs => ({
  withdrawal: String(+(n(s.withdrawal_rate) * 100).toFixed(3)),
  ret: String(+(n(s.real_return) * 100).toFixed(3)),
  spend: s.target_spend ? String(n(s.target_spend)) : "",
  birth: s.birth_year ? String(s.birth_year) : "",
  access: String(s.pension_access_age),
  target: String(s.target_age),
});

function toParams(i: Inputs): Record<string, string> {
  const out: Record<string, string> = {};
  const pctOf = (v: string) => String(Number(v.replace(",", ".")) / 100);
  if (i.withdrawal.trim()) out.withdrawal_rate = pctOf(i.withdrawal);
  if (i.ret.trim()) out.real_return = pctOf(i.ret);
  if (i.spend.trim()) out.target_spend = i.spend.replace(/[\s,'’]/g, ""); // 200,000 or 200'000 is 200000
  if (i.birth.trim()) out.birth_year = i.birth.trim();
  if (i.access.trim()) out.pension_access_age = i.access.trim();
  if (i.target.trim()) out.target_age = i.target.trim();
  return out;
}

async function getFire(qs: string): Promise<FireData> {
  const res = await fetch(`/api/fire?${qs}`);
  if (!res.ok) throw new Error(await errorText(res));
  return res.json();
}

function Field({ label, value, onChange, suffix, hint }: { label: string; value: string; onChange: (v: string) => void; suffix?: string; hint?: string }) {
  return (
    <label className="block text-xs text-muted">
      {label}
      <div className="mt-1 flex items-center rounded-lg border border-hairline bg-surface focus-within:border-accent">
        <input value={value} onChange={(e) => onChange(e.target.value)} inputMode="decimal" className="w-full bg-transparent px-3 py-1.5 text-sm text-ink outline-none" />
        {suffix && <span className="pr-3 text-xs text-muted">{suffix}</span>}
      </div>
      {hint && <span className="mt-0.5 block">{hint}</span>}
    </label>
  );
}

const years = (s: string | null) => (s == null ? "never" : `${s} years`);

export default function Fire({ ccy }: { ccy: string }) {
  const mode = useMode();
  const qc = useQueryClient();
  const [pension, setPension] = useState(false);
  const [home, setHome] = useState(false);
  const [inputs, setInputs] = useState<Inputs | null>(null);
  const [qs, setQs] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  // first load gives us the saved assumptions to fill the form with
  const first = useQuery({ queryKey: ["fire-first"], queryFn: () => getFire(""), staleTime: Infinity });
  useEffect(() => {
    if (first.data && !inputs) setInputs(fromSaved(first.data.saved));
  }, [first.data, inputs]);
  useEffect(() => {
    if (!inputs) return;
    const id = setTimeout(() => setQs(new URLSearchParams({ ...toParams(inputs), include_pension: String(pension), include_home: String(home) }).toString()), 350);
    return () => clearTimeout(id);
  }, [inputs, pension, home]);
  const { data, error, isFetching } = useQuery({ queryKey: ["fire", qs], queryFn: () => getFire(qs), enabled: !!inputs && qs !== "", placeholderData: keepPreviousData });

  const s = data?.status;
  const charts = useMemo(() => {
    if (!s?.ready) return null;
    const cur = s.contributions.savings_rate == null ? null : n(s.contributions.savings_rate);
    return { projection: projectionChart(s.projection, mode), curve: curveChart(s.savings_curve, cur, mode) };
  }, [s, mode]);
  if (!inputs || first.isLoading) return <p className="text-sm text-muted">Loading…</p>;
  if (first.error) return <p className="text-sm text-bad">▲ {(first.error as Error).message}</p>;
  const set = (k: keyof Inputs) => (v: string) => {
    setMsg(null);
    setInputs({ ...inputs, [k]: v });
  };
  const save = async () => {
    try {
      const saved = await saveFire(toParams(inputs));
      qc.setQueryData(["fire-first"], saved);
      qc.invalidateQueries({ queryKey: ["fire"] });
      setMsg({ ok: true, text: "Saved to your ledger (ledger/manual/fire.beancount)." });
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    }
  };
  const reset = () => first.data && setInputs(fromSaved(first.data.saved));

  const base = s?.scenarios.find((x) => x.key === "base");
  const low = s?.scenarios.find((x) => x.key === "low");
  const high = s?.scenarios.find((x) => x.key === "high");
  const progress = s?.progress == null ? 0 : Math.min(1, n(s.progress));
  const sv = s?.ready ? s : null;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-2">
        <Chip on={pension} onClick={() => setPension((v) => !v)}>Count pension (from access age)</Chip>
        <Chip on={home} onClick={() => setHome((v) => !v)}>Count home equity</Chip>
        <span className="text-xs text-muted">Cash and investments always count.</span>
      </div>

      <Card>
        <CardHeader title="Assumptions" subtitle="Change anything to try it out. Save to keep it: it is written to your ledger, like the rest of your settings." />
        <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(150px,1fr))]">
          <Field label="Withdrawal rate" suffix="%" value={inputs.withdrawal} onChange={set("withdrawal")} hint="share of the pot you spend each year" />
          <Field label="Real return" suffix="%" value={inputs.ret} onChange={set("ret")} hint="after inflation, per year" />
          <Field label="Annual spend" suffix={ccy} value={inputs.spend} onChange={set("spend")} hint={inputs.spend ? "your own target" : "blank: your last 12 months"} />
          <Field label="Birth year" value={inputs.birth} onChange={set("birth")} hint="for pension access and Coast FIRE" />
          <Field label="Pension access age" value={inputs.access} onChange={set("access")} />
          <Field label="Target retirement age" value={inputs.target} onChange={set("target")} />
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <button onClick={save} disabled={!data?.differs_from_saved} className="rounded-lg bg-ink px-4 py-2 text-sm text-surface disabled:opacity-40">Save to ledger</button>
          <button onClick={reset} disabled={!data?.differs_from_saved} className="rounded-lg border border-hairline px-4 py-2 text-sm text-ink-2 hover:bg-hairline disabled:opacity-40">Back to saved</button>
          {data?.differs_from_saved && <StatusBadge kind="warn">trying values you have not saved</StatusBadge>}
          {error && <span className="text-sm text-bad">▲ {(error as Error).message}</span>}
          {msg && <span className={`text-sm ${msg.ok ? "text-good" : "text-bad"}`}>{msg.ok ? "● " : "▲ "}{msg.text}</span>}
        </div>
      </Card>

      {s && !s.ready && <Card><p className="text-sm text-ink-2">{s.reason}. FIRE needs at least one full month of transactions.</p></Card>}
      {sv && charts && (
        <div className={`space-y-6 transition-opacity ${isFetching ? "opacity-60" : ""}`}>
          <Card>
            <div className="grid items-end gap-6 [grid-template-columns:repeat(auto-fit,minmax(240px,1fr))]">
              <div>
                <StatTile hero label="Progress to financial independence" value={pct(sv.progress, 1)} hint={`${money(sv.assets.counted, ccy, 0)} of ${money(sv.fi_number, ccy, 0)}`} />
                <div className="mt-3 h-2 rounded-full bg-hairline" role="img" aria-label={`${(progress * 100).toFixed(0)} percent of the FI number`}>
                  <div className="h-2 rounded-full bg-accent" style={{ width: `${progress * 100}%` }} />
                </div>
              </div>
              <div>
                <div className="text-xs text-muted">At your current saving</div>
                <div className="mt-1 text-4xl font-semibold tracking-tight text-ink">{base?.years_to_fi == null ? "Not reached" : years(base.years_to_fi)}</div>
                <div className="mt-1 text-sm text-ink-2">{base?.fi_date ? `around ${dateLabel(base.fi_date).replace(/^\d+ /, "")}` : "within 60 years"}</div>
                <div className="mt-1 text-xs text-muted">Cautious {years(low?.years_to_fi ?? null)} · Optimistic {years(high?.years_to_fi ?? null)}</div>
              </div>
            </div>
          </Card>

          <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(165px,1fr))]">
            <Card><StatTile label="FI number" value={money(sv.fi_number, ccy, 0)} hint={`annual spend ÷ ${(n(sv.assumptions.withdrawal_rate) * 100).toFixed(2).replace(/\.?0+$/, "")}%`} /></Card>
            <Card><StatTile label="Lean FI number" value={money(sv.lean_number, ccy, 0)} hint="essential spending only" /></Card>
            <Card><StatTile label="Annual spend" value={money(sv.basis.annual_spend, ccy, 0)} hint={sv.basis.spend_source} /></Card>
            <Card><StatTile label="Saved per month" value={money(sv.contributions.investable_monthly, ccy, 0)} hint="into cash and investments" /></Card>
            <Card><StatTile label="Savings rate" value={pct(sv.contributions.savings_rate)} hint="of take-home income" /></Card>
          </div>

          <ChartCard
            title="Your path to the FI number"
            subtitle="Counted assets in today's money under three return assumptions (real return ± 2 points)"
            loading={isFetching}
            option={charts.projection}
            height={340}
            columns={[{ key: "y", label: "Year" }, { key: "low", label: "Cautious", align: "right" }, { key: "base", label: "Base", align: "right" }, { key: "high", label: "Optimistic", align: "right" }, { key: "fi", label: "FI number", align: "right" }]}
            rows={sv.projection.map((p) => ({ y: p.date.slice(0, 4), low: money(p.low, ccy, 0), base: money(p.base, ccy, 0), high: money(p.high, ccy, 0), fi: money(p.fi_number, ccy, 0) }))}
          />

          <div className="grid gap-6 xl:grid-cols-2">
            <Card>
              <CardHeader title="What counts" subtitle="Cash and investments always; the rest is your choice" />
              <DataTable
                columns={[
                  { key: "what", label: "Asset" },
                  { key: "value", label: ccy, align: "right" },
                  { key: "status", label: "Status", render: (r) => r.status },
                ]}
                rows={[
                  { what: "Cash and investments", value: money(sv.assets.investable, ccy, 0), status: <StatusBadge kind="good">counted</StatusBadge> },
                  {
                    what: "Pension (BVG / 3a)",
                    value: money(sv.assets.pension, ccy, 0),
                    status: !sv.assets.include_pension ? <StatusBadge kind="muted">not counted</StatusBadge> : sv.pension.months_until_access ? <StatusBadge kind="warn">locked until age {sv.pension.access_age} ({dateLabel(sv.pension.counted_from!).replace(/^\d+ /, "")})</StatusBadge> : <StatusBadge kind="good">counted</StatusBadge>,
                  },
                  { what: "Home equity", value: money(sv.assets.home_equity, ccy, 0), status: sv.assets.include_home ? <StatusBadge kind="good">counted</StatusBadge> : <StatusBadge kind="muted">not counted</StatusBadge> },
                ]}
              />
              <p className="mt-3 text-xs text-muted">Saved each month: {money(sv.contributions.investable_monthly, ccy, 0)} investable, {money(sv.contributions.pension_monthly, ccy, 0)} pension, {money(sv.contributions.mortgage_principal_monthly, ccy, 0)} mortgage principal.</p>
            </Card>
            <Card>
              <CardHeader title="Coast FIRE" subtitle="Enough today that growth alone reaches the FI number by your target age" />
              {sv.coast ? (
                <div className="space-y-3">
                  <StatTile label={`Needed today (retire at ${sv.coast.target_age}, ${sv.coast.years_left} years away)`} value={money(sv.coast.number, ccy, 0)} />
                  <div className="text-sm text-ink-2">{sv.coast.reached ? <StatusBadge kind="good">you are past it: no more saving needed to reach the number by then</StatusBadge> : <StatusBadge kind="warn">{pct(sv.coast.progress, 0)} of the way</StatusBadge>}</div>
                </div>
              ) : (
                <p className="text-sm text-ink-2">Add a birth year above to see Coast FIRE.</p>
              )}
            </Card>
          </div>

          <ChartCard
            title="Savings rate vs years to go"
            subtitle={`Each bar assumes that share of take-home income is saved. Yours is ${pct(sv.contributions.savings_rate)} (the colored bar).`}
            loading={isFetching}
            option={charts.curve}
            columns={[{ key: "rate", label: "Savings rate" }, { key: "years", label: "Years to FI", align: "right" }]}
            rows={sv.savings_curve.map((c) => ({ rate: `${Math.round(n(c.rate) * 100)}%`, years: c.years_to_fi ?? "never" }))}
          />

          <Card>
            <CardHeader title="How this is worked out" />
            <ul className="list-disc space-y-1 pl-5 text-sm text-ink-2">
              {sv.notes.map((x) => <li key={x}>{x}</li>)}
              {sv.basis.one_off_purchases_left_out !== "0.00" && <li>Left out as one-offs: {money(sv.basis.one_off_purchases_left_out, ccy, 0)} of major spending and home or car purchases in the last 12 months.</li>}
            </ul>
          </Card>
        </div>
      )}
    </div>
  );
}
