# fin

Plaintext accounting on Beancount: importers, analytics, a web UI, and an LLM assistant that
never sees your amounts. How it is put together, and the rules it follows, are in `docs/DESIGN.md`.

## Run it on demo data

```bash
uv sync --all-extras                      # Python 3.12 + dependencies
.venv/bin/fin demo-data ~/gits/fin-demo-data
export FIN_DATA=~/gits/fin-demo-data
(cd web && npm install && npm run build)  # builds web/dist
.venv/bin/fin serve                       # http://127.0.0.1:8000
```

`fin serve` binds to 127.0.0.1 only; it is your finances.

For UI work, run `fin serve` and `npm run dev` in `web/` (port 5173 proxies `/api`).

## Settings live in the ledger

Anything that changes what your numbers mean is stored in the Beancount files, so other tools see it
too: `custom "fin-major-threshold"`, `custom "fin-allocation-target"` and `custom "fin-recurring"`
directives (see `ledger/settings.beancount`), `major: TRUE` metadata on accounts or transactions,
`employer: TRUE` on a commodity. `fin recurring confirm|reject` appends the directive and commits.
`config.toml` keeps only preferences. `fin doctor` checks all of this.

## Accounts

The **Accounts** page lists every balance sheet account by institution (UBS, Wise, IBKR, Schwab, Swisscard,
the mortgage...) with its balance in its own currency or shares, its value in CHF, a two-year trend and when
it last moved; the institutions add up to net worth. Click an account for every posting with the running
balance after it, and "Show in Transactions" for the same account in the transaction list (which also has an
account filter of its own).

## How the assistant's context works

A chat is a conversation kept in memory on your machine: the model's own history (its questions, the tool
results it saw, which are names, ranks and row counts, never amounts) plus the results `r1`, `r2`... it
can refer back to, charts and tables. Each question builds on the earlier ones, so follow-ups like "now
just last year" work, and a long chat costs more per question. **New chat** (top of the assistant panel)
forgets all of it, on the page and on the server. Hiding the panel keeps the chat; reloading the page or
restarting `fin serve` starts a new one. Labels, tags and transactions you change mid-chat are seen by the
next question. The Settings model and effort apply to the next question.

## Tags

For anything that cuts across categories, such as everything from your trip to Canada, use a tag. Tags are
ordinary Beancount `#tags` on the transaction line (`2026-08-02 * "Hotel" "Stay" #canada-2026`), so any
Beancount tool sees them. In **Transactions**, tick the rows (or *Select all shown*), type a tag name and
*Add tag*; click a `#tag ✕` button to take it off. Pick a tag in the *Tag* filter to see just those
transactions, with how many there are, what they add up to and the dates they span. The tag filter combines
with month, category and the other filters, and changes are written to the ledger and committed like label
edits. A tag marks the whole transaction, so one payment can't be split between two trips. The demo's
trips are tagged (`#paris-2026`, `#vienna-2026`...).

## Accounts and groups

An account is whatever has an `open` line in `ledger/accounts.beancount`; nothing in the code lists them.
Two settings on that line say how it is shown, so they stay plaintext:

```beancount
2024-01-01 open Liabilities:UBS:CreditCard CHF
  institution: "UBS"
  group: "Liabilities"
```

`institution` is who it belongs to (default: the second part of the name), and `group` is where it counts
in net worth: Cash, Investments, Pension, Property, Vehicles or Other for assets, Liabilities or Mortgage
for debts (a Mortgage also feeds home equity; Property and Vehicles are valued by your `price` lines).
Without a group the old name rules apply (`Assets:Pension:*` is Pension, `*:Cash:*` is Cash) and anything else
is guessed: fiat only is Cash, otherwise Investments. `fin doctor` lists accounts that rely on a guess.

Manage them in **Settings > Accounts** (add a card, change a group or institution, close an empty account)
or with `fin account list | add | set | close`, for example
`fin account add Liabilities:UBS:CreditCard --institution UBS`. Each change is written to the ledger,
checked, and committed. Spending categories can be added the same way (`fin account add Expenses:Pets:Vet`).
Still convention rather than setting: income and expense names such as `Income:Employer:Salary` and
`Expenses:Taxes` (see `non_spend_prefixes` in config.toml).

## From a month's spending to its transactions

Click a bar on the **Spending** charts (or a month in their tables) and **Transactions** opens on that month,
filtered to that category, cadence or necessity. A "Where it went" panel lists the month's expenses by
group or category (switch with *Detail*) with amount, share and count. Tick any combination, say Housing and
Food or Food › Dining and Housing › Utilities; the table shows just those, with the selected total, and
*Clear* or *Show all expenses* returns to everything. It shows payments as made, so a yearly bill appears in the
month it was paid even though the run-rate view spreads it, and it includes taxes and major purchases.
Filters live in the link (`#transactions?month=2026-05&kind=expense&category=Expenses:Food,Expenses:Housing`), so views can be
bookmarked.

## Fixing labels

Open any expense in **Transactions** and use *Edit labels*: category, cadence, necessity and "major
purchase". Tick *Also apply to the N other payments* to change every payment to the same payee in one go.
Saving rewrites just those transactions in their ledger files (the category is the expense account; the
rest is `cadence:` / `necessity:` / `major:` metadata, a `#review` tag is dropped), checks that the ledger
still loads, and commits. If anything would break, nothing is written. "Automatic" removes a label so the
rules decide again.

Tick *Remember for future payments* to keep those labels as a rule for the payee. It is a plain
`custom "fin-rule"` directive in `ledger/manual/rules.beancount` (the latest per payee wins, an empty one
forgets it), listed under **Settings > Label rules** and with `fin rules list | apply | forget`. A rule with a
category fills in `Expenses:Uncategorized` only, never a category you chose; the other labels go on any
payment of that payee that doesn't already carry them. Importers call `labels.apply_rules(root)` after
writing, so new payments arrive labelled; run `fin rules apply` to do it by hand.

## Fixed assets, trends and FIRE

- **Home, car, mortgage:** modelled in the ledger. A home or car is a commodity (e.g. `HOME`) held at cost
  in `Assets:Property:*` / `Assets:Vehicle:*`; you update its value by adding a `price` line (a yearly
  appraisal, say). The mortgage is `Liabilities:Mortgage:*`; each payment splits into interest (an expense)
  and principal (reduces the debt). Buying a car is an asset purchase, not spending.
- **Trends** page: year by year, seasonality, 12-month averages, and how much of net worth growth came from
  saving versus markets and your own valuations.
- **FIRE** page: FI number, progress, years to go under three return assumptions, Coast FIRE, and how
  savings rate changes the answer. The assumptions are `custom "fin-fire"` directives in the ledger; the page
  can try values without saving and write them for you.

## Your own data

`fin init-data ~/gits/fin-data` creates an empty data repo and points `~/.config/fin/config.toml`
at it. Use `FIN_DATA` to switch between repos.

## The assistant

Chat uses the Claude Agent SDK with only the privacy-gated tools in `src/fin/agent/session.py`
(all built-in file/shell tools are disabled). It sees labels and the shape of results, never
amounts; the numbers you see are filled in locally. Every payload sent to the model is logged in
`<data>/state/llm_audit.jsonl`. Levels (`privacy.yaml`): `blind` (default), `relative`, `full`
(local models only). Disable with `FIN_AGENT_BACKEND=none`.

Model and effort are set on the **Settings** page (default: Haiku 5.5, medium) and stored per machine in
`~/.config/fin/settings.json`; a change applies to the next message.

## Development

```bash
.venv/bin/pytest          # includes the privacy leak tests
.venv/bin/ruff check .
cd web && npm run typecheck
```

Licensed under the MIT License (see LICENSE). All data in the demo is invented; your own ledger lives in a separate, private data repository and is never part of this one.
