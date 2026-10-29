import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

export async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

export function useApi<T>(path: string) {
  return useQuery({ queryKey: [path], queryFn: () => getJson<T>(path), placeholderData: keepPreviousData });
}

export interface Meta { empty: boolean; data_dir: string; base_currency: string; as_of?: string; first_date?: string; privacy_level: string; chat: boolean }
export interface Pivot { keys: string[]; rows: Array<Record<string, string>> }
export interface Series {
  id: string; payee: string; account: string; period: string; typical_amount: string;
  first_date: string; last_date: string; next_expected: string; status: "active" | "lapsed" | "proposed"; confirmed: boolean; occurrences: number;
  price_changes: Array<[string, string, string]>;
}
export interface Alert { kind: "new" | "price_change" | "lapsed"; series: string; payee: string; detail?: string; date?: string; old?: string; new?: string; last_date?: string; days_overdue?: number }
export interface Spending {
  view: string; headline: Record<string, string>;
  by_cadence: Pivot; by_necessity: Pivot; by_category: Pivot; by_account: Pivot;
  major: Array<{ date: string; payee: string; narration: string; account: string; amount: string }>;
  recurring: Series[]; alerts: Alert[];
}
export interface Overview {
  net_worth: { total: string; by_group: Record<string, string>; change_1m: string; change_12m: string };
  spend: Record<string, string>;
  last_full_month: { month: string; income: string; net: string; savings_rate: string | null };
  net_worth_series: Array<{ date: string; value: string }>;
}
export interface FixedAsset { account: string; kind: "property" | "vehicle"; commodity: string; name: string; value: string; cost: string; unrealized: string; valued_on: string | null; stale: boolean }
export interface FixedAssets {
  assets: FixedAsset[]; mortgages: Array<{ account: string; balance: string }>;
  property_value: string; mortgage_balance: string; home_equity: string; loan_to_value: string | null;
}
export interface NetWorth { groups: string[]; rows: Array<Record<string, string>>; fixed: FixedAssets }
export interface Holding { account: string; commodity: string; name: string; asset_class: string; currency: string; units: string; avg_cost: string | null; price: string | null; value: string; unrealized: string | null; weight: string }
export interface AllocRow { asset_class?: string; currency?: string; value: string; weight: string; target?: string; drift?: string }
export interface Allocation { holdings: Holding[]; by_asset_class: AllocRow[]; by_currency: AllocRow[]; employer: { commodity: string; weight: string } | null }
export interface Tile { title: string; option?: any; error?: string }

export type ChatEvent =
  | { type: "assistant"; text: string }
  | { type: "tool"; name: string }
  | { type: "chart"; chart_id: string; option: any }
  | { type: "table"; columns: string[]; rows: string[][] }
  | { type: "text"; text: string }
  | { type: "error"; message: string }
  | { type: "done" };

/** POST a chat message and yield server-sent events as they arrive. */
export async function* streamChat(message: string, conversationId: string): AsyncGenerator<ChatEvent> {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, conversation_id: conversationId }),
  });
  if (!res.ok || !res.body) {
    const detail = await res.json().catch(() => ({}));
    yield { type: "error", message: detail.detail ?? `${res.status} ${res.statusText}` };
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let i: number;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i);
      buf = buf.slice(i + 2);
      if (chunk.startsWith("data: ")) yield JSON.parse(chunk.slice(6));
    }
  }
}

export interface Choice { id: string; label: string; note: string }
export interface SettingsData {
  agent: { model: string; effort: string };
  models: Choice[]; efforts: Choice[]; privacy_level: string; settings_file: string;
}

export function useSaveSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (agent: { model: string; effort: string }) => {
      const res = await fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(agent) });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? `${res.status}`);
      return (await res.json()) as SettingsData;
    },
    onSuccess: (data) => qc.setQueryData(["/api/settings"], data),
  });
}

export type TxnKind = "expense" | "income" | "transfer" | "investment" | "asset" | "opening";
export interface Txn {
  date: string; ref: string | null; payee: string; narration: string; tags: string[]; kind: TxnKind;
  category: string; account: string; amount: string; orig_amount: string | null; orig_ccy: string | null;
  cadence: string | null; necessity: string | null; recurring: boolean; series_id: string | null; major: boolean;
  needs_review: boolean; card: string | null; card_label: string | null; categorized: "mapping" | "model" | "rule" | null; merchant_type: string | null; source: string; file: string | null; line: number | null;
  postings: Array<{ account: string; amount: string; ccy: string; cost: string | null; price: string | null }>;
}
export interface TxnResponse {
  month: string; months: Array<{ month: string; count: number; needs_review: number }>; review_total: number;
  cards: Array<{ value: string; count: number }>;
  by_category: Array<{ category: string; amount: string; count: number }>;
  tags: Array<{ tag: string; count: number; spend: string; first: string; last: string }>;
  summary: { count: number; income: string; expenses: string; needs_review: number; needs_review_spend: string };
  matching: number; truncated: boolean; rows: Txn[];
}

export interface AnnualRow {
  year: number; months: number; partial: boolean; income: string; salary: string; equity_comp: string;
  investment_income: string; realized_gains: string; spend: string; major: string; taxes: string;
  payroll_deductions: string; expenses: string; net: string; savings_rate: string | null;
  avg_monthly_spend: string | null; net_worth_end: string; net_worth_change: string; from_saving: string;
  from_markets_and_revaluation: string;
}
export interface Trends {
  through: string; annual: AnnualRow[]; overlay: Record<string, Array<string | null>>;
  rolling: Array<{ month: string; spend_12m: string | null; savings_rate_12m: string | null; net_worth: string | null }>;
  categories: { keys: string[]; rows: Array<Record<string, string | number>> };
}

export interface FireAssumptions { withdrawal_rate: string; real_return: string; target_spend: string | null; birth_year: number | null; pension_access_age: number; target_age: number }
export interface FireScenario { key: "low" | "base" | "high"; label: string; real_return: string; years_to_fi: string | null; fi_date: string | null }
export interface FireStatus {
  ready: boolean; reason?: string; as_of: string; assumptions: FireAssumptions;
  basis: { months_used: number; annual_spend: string; spend_source: string; measured_annual_spend: string; essential_annual_spend: string; one_off_purchases_left_out: string };
  fi_number: string; lean_number: string;
  assets: { investable: string; pension: string; home_equity: string; counted: string; include_pension: boolean; include_home: boolean };
  progress: string | null;
  contributions: { investable_monthly: string; pension_monthly: string; mortgage_principal_monthly: string; savings_rate: string | null };
  pension: { access_age: number; months_until_access: number | null; counted_from: string | null };
  scenarios: FireScenario[];
  projection: Array<{ date: string; low: string; base: string; high: string; fi_number: string }>;
  coast: { target_age: number; years_left: string; number: string; reached: boolean; progress: string | null } | null;
  savings_curve: Array<{ rate: string; years_to_fi: string | null }>;
  notes: string[];
}
export interface Fire { status: FireStatus; saved: FireAssumptions; differs_from_saved: boolean; limits: Record<string, [string, string]> }

export async function saveFire(body: Record<string, string | number | null>): Promise<Fire> {
  const res = await fetch("/api/fire/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? `${res.status}`);
  return res.json();
}

export interface AccountPosition { commodity: string; units: string; value: string; lots: number }
export interface Account {
  account: string; name: string; kind: "asset" | "liability"; value: string; positions: AccountPosition[];
  series: Array<{ date: string; value: string }>; last_activity: string | null; transactions: number; last_checked: string | null;
}
export interface AccountsData { as_of: string; institutions: Array<{ name: string; value: string; accounts: Account[] }>; assets: string; liabilities: string; total: string }
export interface LedgerRow {
  date: string; ref: string | null; payee: string; narration: string; units: string; commodity: string; value: string;
  balance: string; counterparty: string; kind: TxnKind | null; needs_review: boolean;
}
export interface AccountLedger {
  account: string; info: Account | null; month: string; months: Array<{ month: string; count: number }>;
  balances: Record<string, string>; balance_before: Record<string, string>; matching: number; truncated: boolean; rows: LedgerRow[];
}
