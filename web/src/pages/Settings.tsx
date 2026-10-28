import { useEffect, useState } from "react";
import { Choice, SettingsData, useApi, useSaveSettings } from "../lib/api";
import AccountSettings from "../components/AccountSettings";
import Cards from "../components/Cards";
import LabelRules from "../components/LabelRules";
import { Card, CardHeader } from "../components/ui";

function RadioCards({ label, value, choices, onChange }: { label: string; value: string; choices: Choice[]; onChange: (id: string) => void }) {
  return (
    <div role="radiogroup" aria-label={label} className="grid gap-2 [grid-template-columns:repeat(auto-fit,minmax(200px,1fr))]">
      {choices.map((c) => {
        const on = c.id === value;
        return (
          <button
            key={c.id}
            role="radio"
            aria-checked={on}
            onClick={() => onChange(c.id)}
            className={`rounded-lg border p-3 text-left transition-colors ${on ? "border-accent bg-hairline" : "border-hairline hover:bg-hairline"}`}
          >
            <div className="flex items-center gap-2 text-sm font-medium text-ink">
              <span aria-hidden className={`inline-block h-3 w-3 rounded-full border ${on ? "border-accent bg-accent" : "border-muted"}`} />
              {c.label}
            </div>
            <div className="mt-1 text-xs text-muted">{c.note}</div>
          </button>
        );
      })}
    </div>
  );
}

export default function Settings() {
  const { data } = useApi<SettingsData>("/api/settings");
  const save = useSaveSettings();
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState("");
  useEffect(() => {
    if (data) {
      setModel(data.agent.model);
      setEffort(data.agent.effort);
    }
  }, [data]);
  if (!data) return <p className="text-sm text-muted">Loading…</p>;
  const dirty = model !== data.agent.model || effort !== data.agent.effort;
  return (
    <div className="max-w-3xl space-y-6">
      <Card>
        <CardHeader title="Assistant model" subtitle="Which Claude model answers your questions. It only ever sees names and the shape of results, never amounts." />
        <RadioCards label="Model" value={model} choices={data.models} onChange={setModel} />
      </Card>
      <Card>
        <CardHeader title="Effort" subtitle="How much the model thinks before answering. Higher is slower and uses more of your plan." />
        <RadioCards label="Effort" value={effort} choices={data.efforts} onChange={setEffort} />
      </Card>
      <div className="flex items-center gap-3">
        <button
          onClick={() => save.mutate({ model, effort })}
          disabled={!dirty || save.isPending}
          className="rounded-lg bg-ink px-4 py-2 text-sm text-surface disabled:opacity-40"
        >
          {save.isPending ? "Saving…" : "Save"}
        </button>
        {save.isError && <span className="text-sm text-bad">▲ {(save.error as Error).message}</span>}
        {save.isSuccess && !dirty && <span className="text-sm text-good">● Saved. Applies to your next message.</span>}
      </div>
      <AccountSettings />
      <Cards />
      <LabelRules />
      <Card>
        <CardHeader title="Other" />
        <dl className="space-y-2 text-sm">
          <div className="flex justify-between gap-4"><dt className="text-muted">Privacy level</dt><dd className="text-ink">{data.privacy_level} <span className="text-xs text-muted">(set in the data repo's privacy.yaml)</span></dd></div>
          <div className="flex justify-between gap-4"><dt className="text-muted">Settings file</dt><dd className="break-all text-right text-xs text-ink-2">{data.settings_file}</dd></div>
        </dl>
        <p className="mt-3 text-xs text-muted">These preferences belong to this machine. They are not part of your ledger and are never synced or backed up.</p>
      </Card>
    </div>
  );
}
