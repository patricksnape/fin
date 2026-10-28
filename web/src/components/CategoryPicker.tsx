import { useEffect, useRef, useState } from "react";

const pretty = (c: string) => c.replace(/^Expenses:/, "").replaceAll(":", " › ");

interface Props {
  value: string;
  categories: string[];
  onChange: (category: string) => void;
  /** Adds a "＋ New category…" entry at the end of the list. */
  onNew?: () => void;
  label: string;
  placeholder?: string;
  className?: string;
}

/** A category dropdown drawn by the page instead of the browser, with a filter box. The native
 *  <select> popup can close the instant it opens on some Linux/Wayland setups, committing the
 *  option under the pointer; this one only changes on an explicit click or Enter. */
export default function CategoryPicker({ value, categories, onChange, onNew, label, placeholder = "Choose a category…", className = "" }: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const q = query.trim().toLowerCase();
  const shown = categories.filter((c) => pretty(c).toLowerCase().includes(q));
  const close = () => { setOpen(false); setQuery(""); setActive(0); };

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) close(); };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);

  const pick = (c: string) => { onChange(c); close(); };
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") close();
    else if (e.key === "ArrowDown") { e.preventDefault(); setActive((i) => Math.min(i + 1, shown.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((i) => Math.max(i - 1, 0)); }
    else if (e.key === "Enter" && shown[active]) { e.preventDefault(); pick(shown[active]); }
  };

  return (
    <div ref={root} className={`relative ${className}`}>
      <button type="button" aria-label={label} aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-2 rounded-md border border-hairline bg-surface px-2 py-1 text-left text-sm text-ink">
        <span className={`truncate ${value ? "" : "text-muted"}`}>{value ? pretty(value) : placeholder}</span>
        <span aria-hidden className="text-muted">▾</span>
      </button>
      {open && (
        <div onKeyDown={onKey} className="absolute z-20 mt-1 w-64 rounded-md border border-hairline bg-surface p-1 shadow-lg">
          <input autoFocus value={query} onChange={(e) => { setQuery(e.target.value); setActive(0); }} placeholder="Filter…" aria-label={`Filter ${label}`}
            className="mb-1 w-full rounded border border-hairline bg-surface px-2 py-1 text-sm text-ink" />
          <ul role="listbox" aria-label={label} className="max-h-64 overflow-auto">
            {shown.map((c, i) => (
              <li key={c} role="option" aria-selected={c === value}>
                <button type="button" onClick={() => pick(c)} onMouseEnter={() => setActive(i)}
                  className={`w-full truncate rounded px-2 py-1 text-left text-sm text-ink ${i === active ? "bg-hairline" : ""} ${c === value ? "font-medium" : ""}`}>{pretty(c)}</button>
              </li>
            ))}
            {shown.length === 0 && <li className="px-2 py-1 text-sm text-muted">No match</li>}
            {onNew && (
              <li role="option" aria-selected={false} className="mt-1 border-t border-hairline pt-1">
                <button type="button" onClick={() => { close(); onNew(); }} className="w-full rounded px-2 py-1 text-left text-sm text-ink hover:bg-hairline">＋ New category…</button>
              </li>
            )}
          </ul>
        </div>
      )}
    </div>
  );
}
