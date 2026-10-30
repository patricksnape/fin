# fin: design

fin is a plaintext-accounting stack in Python: a Beancount ledger in its own git repo, deterministic
importers, an app with charts, and an LLM chat that never sees your amounts.

## Goals
- Track income, spending and investments (lots, cost basis, prices) across many accounts.
- Make importers and categorization rules easy to write.
- Have a good-looking UI that is easy to extend, with charts shared by pages, the CLI and the agent.
- Chat with an LLM about your finances **without exposing amounts, balances or identifiers** to a
  cloud model. Payee and category names are allowed.
- Keep the ledger in git, with backups that stay encrypted on any remote.

## Two repos
```
fin/          CODE (this repo): src/fin, web/, tests/. Safe to publish; holds nothing personal.
fin-data/     DATA: ledger/, sources/ (raw exports as received), rules/ (your configuration),
              privacy.yaml, documents/, inbox/ (git-ignored). Pushed only through git-remote-gcrypt.
```
fin finds the data through `FIN_DATA` or `~/.config/fin/config.toml`.

**Everything specific to your own setup lives in `fin-data`**, never in the code: which ledger
account each bank sub-account maps to, loan numbers, which recipients are your own accounts, card
names, category mappings, the people to redact. The code ships neutral defaults and reads
`rules/*.yaml` (for example `rules/ubs.yaml accounts:`, `rules/mortgages.yaml pays_from:`,
`rules/wise.yaml own_recipients:`). Tests use synthetic data only.

## Stack
- Python 3.12, uv, Beancount 3 (lots and cost basis), beanquery, FastAPI, Typer, pdfplumber,
  pytest, ruff. Money is `decimal.Decimal` everywhere; JSON carries amounts as strings.
- Web: React + Vite + TypeScript, Tailwind, ECharts. TypeScript is presentation only; all logic is in
  Python.

## Privacy: the "blind analyst"
A cloud model only ever gets the schema, account, category and payee names, dates, and the shape of
results.
- The in-app agent has privacy-gated tools only (`fin/agent/tools.py`): list names, run a
  structured query that executes locally and returns a handle with labels but no numbers, render a
  chart or table from a handle, and write text with placeholders that are filled in locally.
- Privacy levels per backend in `privacy.yaml`: `blind` (default for cloud models), `relative`
  (percentages only), `full` (local models only).
- Every outgoing string is redacted: amounts, IBAN/card/phone patterns, long digit runs, and the
  names in `privacy.yaml`. Payments to people are marked and never sent. Every payload is logged
  locally to `state/llm_audit.jsonl`.
- A terminal agent with shell access inside `fin-data` bypasses all this; treat that as an explicit
  opt-in.

## Imports: parsing is code, never an LLM
```
fetch()   raw API/file output, archived under sources/<source>/ before parsing
parse()   list[RawTxn]: date, account, amount, currency, payee, narration, ref, meta
write     dedupe by ref -> append-only, one file per source and month
match     pair transfers between own accounts through the clearing account
label     payee rules -> source category mappings -> optional LLM (names only) -> #review
check     bean-check; balance assertions from statements fail loudly on gaps or doubles
commit    git commit in fin-data
```
- Fetchers use an API where there is one, otherwise the site's own JSON from a browser you log in
  to yourself. No password, cookie or token is ever stored; a token, when needed, is read from a
  password manager at fetch time.
- Every source has regression tests on synthetic fixtures. An unknown export format is an error,
  never a guess.

## Ledger rules
- **Every transaction has a unique `ref`** (`<source>:<native id>`, or a hash of the row when the
  source has none). Imports never modify existing transactions; your edits keep the `ref`, so
  re-imports skip them. Writes are serialized by a lock and committed only after `bean-check`
  passes, otherwise reverted.
- **Pending transactions are never imported**, only booked ones. The booking date is the date;
  a different value date is kept as metadata.
- **Balances:** a statement's closing balance on day D is asserted on D+1. Opening balances come
  from the first statement, or a `pad` from `Equity:Opening-Balances`.
- **Transfers between own accounts:** each leg is imported on its own against
  `Assets:Transfers:Pending`. The matcher pairs legs (same currency and opposite amount within a
  few days; cross-currency moves within a rate tolerance; a fee taken on the way on its own line)
  and links them, so the clearing account nets to zero. Ambiguous candidates are left unmatched.
- **FX:** conversions are one transaction with a `@@` total price. Card spending abroad posts what
  was charged and keeps the original amount as metadata. Analytics convert at the rate on or before
  each day (ECB reference rates) and raise rather than guess when a rate is missing.
- **Investments:** lots carry cost and acquisition date. Shares moved between brokers keep their
  lots. Sales close the lots the broker says they closed; gains go to
  `Income:Investments:Gains:<SYM>`, commissions to `Expenses:Fees:Broker`, dividends gross with
  withholding as an expense. Splits are automated; other corporate actions stop the import for a
  manual fix.
- **Equity awards:** a vest books the gross shares at the vest-day price as income, the shares
  withheld for tax as tax. The vest's meta carries `award`, `award_date` (when it was granted) and
  `award_name`; an ESPP purchase carries `offering_start`. The Share awards page finds events by this
  shape (a stock lot at its market price plus a posting to `Income:Employer:RSU*` or
  `Income:Employer:ESPP*`), never by ticker, so any importer that books it this way is picked up.
  Sales close lots by their cost date and price, which survive a move between brokers. Prices on
  award dates come from a daily history file in the data repo (`sources/market-history/<SYMBOL>.csv`)
  or the ledger's own prices; without one the growth before the vest is left out, not guessed.
- **Plan-broker statements (E*TRADE).** The monthly brokerage statements (PDF, `sources/etrade/`)
  list receipts, sales, dividends, wires, deliveries to another broker and splits, with closing cash
  and shares, but no cost, grant or tax kept at release. The account therefore holds plain shares,
  not lots: a receipt is plan income at the market price of the day (marked as an estimate), a sale
  books the broker's price and fees with no gain, and a delivery books the value the receiving broker
  booked, naming that transaction in `moved_to`, so the clearing account nets to zero. Each statement
  is checked against its own cash and against its neighbours; a month with no statement shows up as
  an opening balance on the next one. The parser is versioned by layout (`v1` = 2017-2020); an
  unknown layout is refused, so a newer export gets a parser of its own.
  Release and purchase confirmations (`sources/etrade/release-confirmations/`,
  `purchase-confirmations/`) are matched to receipts by symbol, size and date: the vest is then
  booked as it happened (all shares as income at that day's value, the ones held back as
  `RSU-Withholding`; an ESPP purchase splits the discount from the payroll contribution, which is
  booked as salary). A receipt with no confirmation is grossed up by the share the confirmations
  show was held back, and says so in its `basis`. `rules/etrade.yaml` names the months in which a
  receipt is an ESPP purchase.

## Categorization and spend
- Uncategorized postings go to `Expenses:Uncategorized` / `Income:Uncategorized`, tagged
  `#review`. Payee rules (`custom "fin-rule"` directives) apply first, then the source's own
  merchant categories mapped in `rules/categories.yaml`, then optionally an LLM that sees names
  only.
- Each expense has a `cadence` (recurring, regular, one-off), a `necessity` (essential,
  discretionary) and a `major` flag. Defaults come from the account's `open` metadata. Major
  purchases are left out of monthly spend and shown separately. A recurring detector spreads
  payments that come less often than monthly.

## Encrypted data repo
- The working copy is plain text; git-remote-gcrypt encrypts on push, so the remote holds opaque
  packs only (names, messages and history are hidden).
- Writes from several machines are to be made safe by pull-rebase before writing, and a push that
  verifies the remote head afterwards.
- Losing the GPG key means losing the backups: keep an offline copy.
