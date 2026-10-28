import { FormEvent, useEffect, useRef, useState } from "react";
import { ChatEvent, streamChat } from "../lib/api";
import { Chart } from "./Chart";
import { DataTable } from "./ui";

type Item = { id: number; ev: ChatEvent | { type: "user"; text: string } };

const SUGGESTIONS = [
  "Spend by category over the last 12 months",
  "How does my grocery spend compare to last year?",
  "Show net worth over time",
  "What is my allocation by currency?",
];

export default function Chat({ enabled, emptyLedger = false }: { enabled: boolean; emptyLedger?: boolean }) {
  const [items, setItems] = useState<Item[]>([]);
  const [busy, setBusy] = useState(false);
  const [text, setText] = useState("");
  const convo = useRef(crypto.randomUUID());
  const [asked, setAsked] = useState(0);
  const bottom = useRef<HTMLDivElement>(null);
  const nextId = useRef(0);
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [items]);

  const push = (ev: Item["ev"]) => setItems((xs) => [...xs, { id: nextId.current++, ev }]);

  async function send(message: string) {
    if (!message.trim() || busy) return;
    push({ type: "user", text: message });
    setAsked((n) => n + 1);
    setText("");
    setBusy(true);
    try {
      for await (const ev of streamChat(message, convo.current)) if (ev.type !== "done") push(ev);
    } catch (e) {
      push({ type: "error", message: String(e) });
    } finally {
      setBusy(false);
    }
  }
  /** Start over: the model forgets the conversation, and so does the page. */
  async function reset() {
    if (busy) return;
    void fetch(`/api/chat/${convo.current}`, { method: "DELETE" }).catch(() => undefined);
    convo.current = crypto.randomUUID();
    setItems([]);
    setAsked(0);
    setText("");
  }
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void send(text);
  };

  return (
    <div className="flex h-full flex-col">
      {asked > 0 && (
        <div className="flex items-center justify-between border-b border-hairline px-4 py-2 text-xs text-muted">
          <span>{asked} question{asked === 1 ? "" : "s"} in this chat. Each builds on the earlier ones.</span>
          <button onClick={reset} disabled={busy} className="rounded-md border border-hairline px-2 py-1 text-ink-2 hover:bg-hairline disabled:opacity-40">New chat</button>
        </div>
      )}
      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        {items.length === 0 && (
          <div className="space-y-2">
            <p className="text-sm text-ink-2">Ask about your finances. The assistant never sees amounts: it asks your machine to compute, and you see the real numbers here.</p>
            {enabled && SUGGESTIONS.map((s) => (
              <button key={s} onClick={() => send(s)} className="block w-full rounded-lg border border-hairline px-3 py-2 text-left text-sm text-ink hover:bg-hairline">{s}</button>
            ))}
            {!enabled && emptyLedger && <p className="text-sm text-ink-2">Add some transactions first; there is nothing to ask about yet.</p>}
            {!enabled && !emptyLedger && <p className="text-sm text-bad">▲ No model backend is configured. Install the agent extra and restart `fin serve`.</p>}
          </div>
        )}
        {items.map(({ id, ev }) => (
          <Message key={id} ev={ev} />
        ))}
        {busy && <p className="text-xs text-muted">Working…</p>}
        <div ref={bottom} />
      </div>
      <form onSubmit={submit} className="flex gap-2 border-t border-hairline p-3">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={!enabled}
          placeholder={enabled ? "Ask about spending, net worth…" : "Chat unavailable"}
          aria-label="Message"
          className="min-w-0 flex-1 rounded-lg border border-hairline bg-surface px-3 py-2 text-sm text-ink outline-none focus:border-accent"
        />
        <button disabled={!enabled || busy || !text.trim()} className="rounded-lg bg-ink px-3 py-2 text-sm text-surface disabled:opacity-40">Send</button>
      </form>
    </div>
  );
}

function Message({ ev }: { ev: Item["ev"] }) {
  switch (ev.type) {
    case "user":
      return <div className="ml-8 rounded-lg bg-hairline px-3 py-2 text-sm text-ink">{ev.text}</div>;
    case "assistant":
      return <p className="text-sm text-ink-2">{ev.text}</p>;
    case "tool":
      return <p className="text-xs text-muted">· {ev.name.replace("_", " ")}</p>;
    case "text":
      return <p className="text-sm font-medium text-ink">{ev.text}</p>;
    case "chart":
      return <div className="rounded-lg border border-hairline p-2"><Chart option={ev.option} height={240} /></div>;
    case "table":
      return (
        <div className="rounded-lg border border-hairline p-2">
          <DataTable columns={ev.columns.map((c, i) => ({ key: String(i), label: c.replace("_", " "), align: i === 0 ? undefined : ("right" as const) }))} rows={ev.rows.map((r) => Object.fromEntries(r.map((v, i) => [String(i), v])))} />
        </div>
      );
    case "error":
      return <p className="text-sm text-bad">▲ {ev.message}</p>;
    default:
      return null;
  }
}
