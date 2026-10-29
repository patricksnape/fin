"""`fin` command line interface."""

import sys
from pathlib import Path
from typing import Annotated

import typer

from fin import config, gitops, ledger, scaffold

app = typer.Typer(no_args_is_help=True, help="Plaintext accounting on Beancount.")


def _fail(msg: str) -> "typer.Exit":
    typer.echo(msg, err=True)
    return typer.Exit(1)


def _root() -> Path:
    try:
        return config.data_dir()
    except config.ConfigError as e:
        raise _fail(str(e))


@app.command("init-data")
def init_data(
    path: Annotated[Path, typer.Argument(help="Where to create the data repo")],
    set_default: Annotated[bool, typer.Option(help="Write data_dir to ~/.config/fin")] = True,
):
    """Create a new data repo with the standard layout."""
    path = path.expanduser().resolve()
    try:
        scaffold.init_data(path)
    except FileExistsError as e:
        raise _fail(str(e))
    if set_default:
        config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.CONFIG_FILE.write_text(f'data_dir = "{path}"\n')
    typer.echo(f"Created {path}")


@app.command("demo-data")
def demo_data(path: Annotated[Path, typer.Argument(help="Where to create the demo repo")]):
    """Create a repo of invented transactions for UI development (does not touch your config)."""
    from fin import demo

    path = path.expanduser().resolve()
    try:
        n = demo.create_demo(path)
    except FileExistsError as e:
        raise _fail(str(e))
    typer.echo(f"Created {n} demo transactions in {path}\nUse it with: export FIN_DATA={path}")


@app.command()
def check():
    """Load the ledger and report Beancount errors. Exits 1 on any error."""
    led = ledger.load(config.main_ledger(_root()))
    if led.errors:
        for line in ledger.format_errors(led.errors):
            typer.echo(line, err=True)
        raise typer.Exit(1)
    typer.echo(f"OK: {len(led.entries)} entries")


@app.command()
def query(
    bql: Annotated[str, typer.Argument(help="BQL query")],
    fmt: Annotated[str, typer.Option("--format", help="table|csv|json")] = "table",
):
    """Run a BQL query against the ledger (full access: local use only)."""
    import csv
    import json

    cols, rows = ledger.query(config.main_ledger(_root()), bql)
    if fmt == "json":
        typer.echo(json.dumps([dict(zip(cols, map(str, r))) for r in rows], indent=2))
    elif fmt == "csv":
        w = csv.writer(sys.stdout)
        w.writerow(cols)
        w.writerows(rows)
    elif fmt == "table":
        cells = [cols] + [[str(v) for v in r] for r in rows]
        widths = [max(len(r[i]) for r in cells) for i in range(len(cols))]
        for i, r in enumerate(cells):
            typer.echo("  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip())
            if i == 0:
                typer.echo("  ".join("-" * w for w in widths))
    else:
        raise _fail(f"Unknown format: {fmt}")


report = typer.Typer(help="Built-in reports.")
app.add_typer(report, name="report")


def _load_model():
    from fin.analytics import recurring, spend

    root = _root()
    led = ledger.load(config.main_ledger(root))
    settings = config.load_settings(root, led)
    confirmed, rejected = recurring.load_state(led)
    return led, settings, spend.build(led, settings, confirmed, rejected)


@report.command("spend")
def report_spend(months: Annotated[int, typer.Option(help="Months to show")] = 6):
    """Monthly spend (run-rate: majors, taxes and payroll deductions excluded) by cadence."""
    from fin.analytics import spend

    _, _, model = _load_model()
    for name, value in spend.headline(model).items():
        typer.echo(f"{name.replace('_', ' '):22} {value:>12,.2f}")
    table = (
        spend.spend_by(model, "cadence")
        .pivot(index="month", columns="key", values="amount")
        .fillna(0)
    )
    table["total"] = table.sum(axis=1)
    typer.echo("\n" + table.tail(months).to_string(float_format=lambda v: f"{v:,.2f}"))


@report.command("networth")
def report_networth(months: Annotated[int, typer.Option(help="Months to show")] = 6):
    """Net worth by group (market value, base currency)."""
    from fin.analytics import networth

    led, settings, _ = _load_model()
    df = networth.net_worth(led, settings.base_currency)
    table = df.pivot(index="date", columns="group", values="value")[networth.GROUPS]
    table["total"] = table.sum(axis=1)
    typer.echo(table.tail(months).to_string(float_format=lambda v: f"{v:,.2f}"))


@report.command("cashflow")
def report_cashflow(months: Annotated[int, typer.Option(help="Months to show")] = 6):
    """Monthly income, expenses and savings rate."""
    from fin.analytics import cashflow

    led, settings, model = _load_model()
    df = cashflow.cashflow(led, model, settings.base_currency).set_index("month")
    cols = ["income", "spend", "major", "taxes", "payroll_deductions", "net", "savings_rate"]
    typer.echo(df[cols].tail(months).to_string(float_format=lambda v: f"{v:,.2f}"))


@report.command("allocation")
def report_allocation():
    """Holdings and allocation by asset class and currency, with drift from targets."""
    from fin.analytics import allocation

    led, settings, _ = _load_model()
    h = allocation.holdings(led, settings.base_currency)
    fmt = lambda v: f"{v:,.4f}" if abs(v) < 1 else f"{v:,.2f}"
    typer.echo(
        h[
            ["account", "commodity", "asset_class", "units", "value", "unrealized", "weight"]
        ].to_string(float_format=fmt)
    )
    typer.echo(
        "\n" + allocation.allocation(h, "asset_class", settings.targets).to_string(float_format=fmt)
    )
    typer.echo("\n" + allocation.allocation(h, "currency").to_string(float_format=fmt))


@app.command()
def serve(
    port: Annotated[int, typer.Option(help="Port")] = 8000,
    host: Annotated[
        str, typer.Option(help="Bind address; keep 127.0.0.1, this is your finances")
    ] = "127.0.0.1",
):
    """Serve the web UI and API."""
    import uvicorn

    from fin.api.app import create_app

    uvicorn.run(create_app(_root()), host=host, port=port)


@app.command()
def commit(message: Annotated[str, typer.Option("-m", help="Commit message")] = "update"):
    """Check the ledger, then commit all changes in the data repo."""
    root = _root()
    with gitops.write_lock(root):
        led = ledger.load(config.main_ledger(root))
        if led.errors:
            for line in ledger.format_errors(led.errors):
                typer.echo(line, err=True)
            raise _fail("Ledger has errors; not committing.")
        typer.echo("Committed." if gitops.commit_all(root, message) else "Nothing to commit.")


@app.command()
def status():
    """Show data repo location, uncommitted changes and sync state (as of the last fetch)."""
    from fin import sync

    root = _root()
    typer.echo(f"data: {root}")
    out = gitops.git(root, "status", "--short").strip()
    typer.echo(out or "clean")
    for name, s in sync.status(root).items():
        note = "" if s["fetched"] else " (never fetched)"
        typer.echo(f"{name}: {s['ahead']} to push, {s['behind']} to pull{note}")


remote = typer.Typer(help="Encrypted remotes (git-remote-gcrypt).")
app.add_typer(remote, name="remote")


@remote.command("add")
def remote_add(
    name: Annotated[str, typer.Argument(help="Remote name, e.g. backup")],
    location: Annotated[str, typer.Argument(help="Folder path or git URL; stored encrypted")],
    key: Annotated[str, typer.Option(help="GPG key fingerprint that can decrypt it")],
):
    """Add an encrypted remote and configure the repo for safe multi-machine sync."""
    from fin import sync

    try:
        sync.add_remote(_root(), name, location, key)
    except (sync.SyncError, gitops.GitError) as e:
        raise _fail(str(e))
    typer.echo(f"Added encrypted remote {name}. Run `fin backup` to push.")


@remote.command("list")
def remote_list():
    """List encrypted remotes."""
    from fin import sync

    for name, url in sync.remotes(_root()).items():
        typer.echo(f"{name}\t{url}")


@app.command()
def pull(name: Annotated[str, typer.Argument(help="Remote name")] = "origin"):
    """Fetch and merge another machine's changes (transactions are unioned by ref)."""
    from fin import sync

    root = _root()
    try:
        with gitops.write_lock(root):
            typer.echo(f"{name}: {sync.pull(root, name)}")
    except sync.ConflictError as e:
        raise _fail(f"{e}\nNothing was changed. Resolve by hand, or ask me to.")
    except (sync.SyncError, gitops.GitError) as e:
        raise _fail(str(e))


@app.command()
def backup(
    names: Annotated[list[str] | None, typer.Argument(help="Remotes (default: all)")] = None,
):
    """Pull, check the ledger, push to the encrypted remote(s), and verify the remote matches."""
    from fin import sync

    try:
        for name, result in sync.backup(_root(), names or None).items():
            typer.echo(f"{name}: {result}")
    except sync.ConflictError as e:
        raise _fail(f"{e}\nNothing was pushed.")
    except (sync.SyncError, gitops.GitError) as e:
        raise _fail(str(e))


@app.command()
def doctor():
    """Check settings in the ledger and the backup setup (gpg, key, remote, repo size)."""
    from fin import ledger_settings, sync

    root = _root()
    bad = False
    checks = ledger_settings.doctor(root, ledger.load(config.main_ledger(root))) + sync.doctor(root)
    for name, ok, detail in checks:
        bad |= not ok
        typer.echo(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")
    raise typer.Exit(1 if bad else 0)


recurring_cmds = typer.Typer(help="Recurring payments. Confirmations are written to the ledger.")
app.add_typer(recurring_cmds, name="recurring")


@recurring_cmds.command("list")
def recurring_list():
    """List detected recurring payments and their status."""
    _, _, model = _load_model()
    for s in sorted(model.series, key=lambda s: s.id):
        typer.echo(f"{s.status:9} {s.period:9} {s.typical_amount:>10}  {s.id}")


def _record(status: str, series: str):
    from fin import ledger_settings

    payee, sep, account = series.partition("|")
    if not sep:
        raise _fail('Give the series id as "payee|Account", as shown by `fin recurring list`.')
    try:
        ledger_settings.record_recurring(_root(), status, payee, account)
    except ValueError as e:
        raise _fail(str(e))
    typer.echo(f"{status}: {series}")


@recurring_cmds.command("confirm")
def recurring_confirm(
    series: Annotated[str, typer.Argument(help='e.g. "serafe|Expenses:Government:Serafe"')],
):
    """Confirm a recurring series (appends a custom directive to the ledger and commits)."""
    _record("confirmed", series)


@recurring_cmds.command("reject")
def recurring_reject(
    series: Annotated[str, typer.Argument(help='e.g. "interactive brokers|Expenses:Fees:Broker"')],
):
    """Reject a detected series so it is not offered again."""
    _record("rejected", series)


rules_cmds = typer.Typer(
    help="Label rules: a payee's category and labels, remembered in the ledger."
)
app.add_typer(rules_cmds, name="rules")


@rules_cmds.command("list")
def rules_list():
    """Show every remembered rule."""
    from fin import labels

    for payee, found in sorted(labels.read_rules(_load_model()[0]).items()):
        typer.echo(f"{payee:30} " + " ".join(f"{k}={v}" for k, v in found.items()))


@rules_cmds.command("apply")
def rules_apply():
    """Label transactions the rules cover that are still unlabelled (importers run this too)."""
    from fin import labels

    typer.echo(f"{labels.apply_rules(_root())} transaction(s) labelled")


@rules_cmds.command("forget")
def rules_forget(payee: Annotated[str, typer.Argument(help='normalized payee, e.g. "twint p p"')]):
    """Stop applying a rule (appends an empty rule, so the history stays in the file)."""
    from fin import labels

    try:
        labels.record_rule(_root(), payee, {})
    except ValueError as e:
        raise _fail(str(e))
    typer.echo(f"forgot {payee}")


rsu_cmds = typer.Typer(help="Share awards: tidy what the importers recorded about vests.")
app.add_typer(rsu_cmds, name="rsu")


@rsu_cmds.command("backfill")
def rsu_backfill():
    """Give vests booked before the importer wrote it their award date and name (from the archived
    Schwab files). Safe to run again: lines already there are left alone."""
    from fin import labels
    from fin.importers import schwab

    root = _root()
    found = schwab.award_meta(schwab.source_files(root))
    try:
        n = labels.add_meta(root, found, f"rsu: award dates for {len(found)} vests")
    except KeyError as e:
        raise _fail(
            f"{e.args[0]}: a vest in the files is not in the ledger yet; run an import first"
        )
    typer.echo(f"{n} vest(s) updated, {len(found) - n} already had them")


account_cmds = typer.Typer(
    help="Accounts: add, list, regroup and close. Written to the ledger as plain directives."
)
app.add_typer(account_cmds, name="account")


@account_cmds.command("list")
def account_list():
    """Balance sheet accounts with their institution and net worth group."""
    from fin import registry

    reg = registry.of(_load_model()[0])
    for a in sorted(reg.balance_sheet(), key=lambda a: (a.institution, a.account)):
        flag = "  <- guessed, set a group" if a.guessed else "  (closed)" if a.closed else ""
        typer.echo(f"{a.institution:12} {a.choice:12} {a.account}{flag}")


@account_cmds.command("add")
def account_add(
    account: Annotated[str, typer.Argument(help="e.g. Liabilities:UBS:CreditCard")],
    group: Annotated[
        str | None,
        typer.Option(
            help="Cash, Investments, Pension, Property, Vehicles, Other (assets); Liabilities, Mortgage (debts)"
        ),
    ] = None,
    institution: Annotated[
        str | None, typer.Option(help="defaults to the second part of the name")
    ] = None,
    currency: Annotated[list[str] | None, typer.Option(help="repeat for several")] = None,
    cadence: Annotated[
        str | None, typer.Option(help="expense accounts: recurring, regular, one_off")
    ] = None,
    necessity: Annotated[
        str | None, typer.Option(help="expense accounts: essential, discretionary")
    ] = None,
    since: Annotated[str | None, typer.Option(help="open date YYYY-MM-DD (default today)")] = None,
):
    """Open an account (appends an `open` directive with metadata and commits)."""
    from datetime import date

    from fin import account_admin

    try:
        text = account_admin.add_account(
            _root(),
            account,
            group,
            institution,
            tuple(currency or ()),
            cadence,
            necessity,
            date.fromisoformat(since) if since else None,
        )
    except ValueError as e:
        raise _fail(str(e))
    typer.echo(text.rstrip())


@account_cmds.command("set")
def account_set(
    account: Annotated[str, typer.Argument()],
    group: Annotated[str | None, typer.Option()] = None,
    institution: Annotated[str | None, typer.Option()] = None,
):
    """Change an account's group and/or institution."""
    from fin import account_admin

    changes = {k: v for k, v in (("group", group), ("institution", institution)) if v is not None}
    if not changes:
        raise _fail("give --group and/or --institution")
    try:
        account_admin.set_account(_root(), account, changes)
    except (KeyError, ValueError) as e:
        raise _fail(str(e))
    typer.echo(f"updated {account}")


@account_cmds.command("close")
def account_close(account: Annotated[str, typer.Argument()]):
    """Close an account that holds nothing."""
    from fin import account_admin

    try:
        account_admin.close_account(_root(), account)
    except (KeyError, ValueError) as e:
        raise _fail(str(e))
    typer.echo(f"closed {account}")


def _start(mod, root: Path, source: str):
    """Where an incremental fetch begins: the source's own rule if it has one."""
    from fin.importers import base

    own = getattr(mod, "default_start", None)
    return (own(root) if own else None) or base.default_start(root, source)


def _fetch_and_import(
    root: Path, source: str, since: str = "", until: str = "", fetch: bool = True
):
    """Fetch (opens a browser to log in) and import one source; returns the summary line."""
    from datetime import date

    from fin import importers, labels
    from fin.importers import base, categorize

    mod = importers.get(source)
    if fetch:
        end = date.fromisoformat(until) if until else date.today()
        start = date.fromisoformat(since) if since else _start(mod, root, source)
        typer.echo(f"{source}: fetching {start} .. {end}")
        mod.fetch(root, start, end, log=typer.echo)
    files = mod.source_files(root)
    if not files:
        raise base.ImportFailed(f"nothing in sources/{source}/: run `fin fetch {source}` first")
    # a source that needs the data folder to parse (account mappings, where a sibling source starts)
    parse_root = getattr(mod, "parse_root", None)
    parsed = parse_root(root, files) if parse_root else mod.parse_files(files)
    needed = getattr(mod, "needed_accounts", lambda _: getattr(mod, "NEEDED_ACCOUNTS", {}))
    base.ensure_accounts(root, needed(root))
    written, skipped = base.write_new(root, mod.SOURCE, parsed)
    filled = base.backfill_meta(root, parsed)  # details a newer parser records, for older imports
    gitops.commit_all(root, f"import {source}: {written} new")
    labelled = labels.apply_rules(root) if written else 0  # your payee rules first
    mapped = categorize.apply_mapping(root, mod.SOURCE) if written else 0
    from fin import transfers

    if written:
        transfers.match(root)  # the other half of a transfer may have come from another source
    for line in getattr(mod, "after_import", lambda r: [])(root):
        typer.echo(f"{source}: {line}")
    if written:
        transfers.match(
            root
        )  # again: a source's hook may have added the exchange rates pairing needs
    if written and categorize.load_map(root).get("llm"):
        from fin.importers import llm_categorize

        _report_llm(llm_categorize.run(root, log=typer.echo))
    return (
        f"{source}: imported {written} new ({skipped} already in the ledger); "
        f"categorized {labelled + mapped}"
        + (f"; added details to {filled} older" if filled else "")
    )


def _source_arg(source: str):
    from fin import importers

    try:
        importers.get(source)
    except KeyError as e:
        raise _fail(str(e.args[0]))
    return source


@app.command("fetch")
def fetch_cmd(
    source: Annotated[str, typer.Argument(help="e.g. swisscard")],
    since: Annotated[str, typer.Option(help="First day, YYYY-MM-DD (default: incremental)")] = "",
    until: Annotated[str, typer.Option(help="Last day, YYYY-MM-DD (default: today)")] = "",
):
    """Open a browser, log in yourself, and archive the source's raw data under sources/."""
    from datetime import date

    from fin import importers
    from fin.importers import base

    root = _root()
    mod = importers.get(_source_arg(source))
    end = date.fromisoformat(until) if until else date.today()
    start = date.fromisoformat(since) if since else _start(mod, root, source)
    try:
        files = mod.fetch(root, start, end, log=typer.echo)
    except base.ImportFailed as e:
        raise _fail(str(e))
    typer.echo(f"Archived {len(files)} files. Now run: fin import {source}")


@app.command("import")
def import_cmd(
    source: Annotated[str, typer.Argument(help="e.g. swisscard")],
    files: Annotated[
        list[Path] | None, typer.Argument(help="Files you downloaded from the source (optional)")
    ] = None,
):
    """Parse a source's archived raw files and append new transactions to the ledger. Files you
    downloaded yourself (e.g. a Schwab export) are checked and archived first."""
    from fin import importers
    from fin.importers import base

    root, name = _root(), _source_arg(source)
    try:
        if files:
            mod = importers.get(name)
            if not hasattr(mod, "accept"):
                raise base.ImportFailed(f"{name} reads only what `fin fetch {name}` archives")
            for f in files:
                typer.echo(f"{name}: kept {mod.accept(root, f).name}")
        typer.echo(_fetch_and_import(root, name, fetch=False))
    except base.ImportFailed as e:
        raise _fail(f"Import failed, ledger unchanged: {e}")


@app.command("transfers")
def transfers_cmd(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Only count what would match")] = False,
):
    """Pair the two halves of transfers between your own accounts (card payments included)."""
    from fin import transfers

    r = transfers.match(_root(), dry_run=dry_run)
    typer.echo(
        f"{'would match' if dry_run else 'matched'} {r['pairs']} pair(s); "
        f"{r['ambiguous']} ambiguous (left alone); "
        f"{r['card_payments_unmatched']} card payment(s) with no card-side match"
    )


@app.command("prices")
def prices_cmd():
    """Fetch the daily exchange rates still missing for every foreign currency in the ledger."""
    from fin import prices

    try:
        n = prices.update(_root())
    except (OSError, ValueError) as e:
        raise _fail(f"could not update exchange rates: {e}")
    typer.echo(f"added {n} exchange rate(s)")


@app.command("opening")
def opening_cmd(source: Annotated[str, typer.Argument(help="e.g. ubs")]):
    """Write opening balances (what the accounts held before the first archived transaction)."""
    from fin import opening
    from fin.importers import base

    try:
        n = opening.write(_root(), _source_arg(source))
    except base.ImportFailed as e:
        raise _fail(str(e))
    typer.echo(f"{source}: wrote {n} opening balance(s)")


@app.command("mortgage")
def mortgage_cmd():
    """Split UBS mortgage payments into interest and amortization, and check the loan balances."""
    from fin import mortgage
    from fin.importers import base

    try:
        done = mortgage.apply(_root())
    except base.ImportFailed as e:
        raise _fail(str(e))
    if not done:
        raise _fail("no mortgage snapshot yet: run `fin fetch ubs` first")
    typer.echo(f"split {done['split']} payment(s); wrote {done['opening']} opening balance(s)")
    for problem in mortgage.check(_root()):
        typer.echo(f"MISMATCH {problem}")


@app.command("opening-set")
def opening_set_cmd(
    account: Annotated[str, typer.Argument(help="e.g. Liabilities:Swisscard:MilesMore")],
    amount: Annotated[
        str, typer.Argument(help="signed balance of the account: a debt is negative")
    ],
    day: Annotated[str, typer.Option("--date", help="YYYY-MM-DD the balance holds from")],
    currency: Annotated[str, typer.Option(help="default CHF")] = "CHF",
):
    """Record an opening balance read off a statement (balanced against Equity:Opening-Balances)."""
    from datetime import date
    from decimal import Decimal, InvalidOperation

    from fin import opening
    from fin.importers import base

    try:
        n = opening.set_manual(_root(), account, Decimal(amount), date.fromisoformat(day), currency)
    except (InvalidOperation, ValueError) as e:
        raise _fail(f"bad amount or date: {e}")
    except base.ImportFailed as e:
        raise _fail(str(e))
    typer.echo(
        f"wrote {n} opening balance(s)" if n else f"{account} already has an opening balance"
    )


@app.command("sync")
def sync_cmd(
    sources: Annotated[list[str] | None, typer.Argument(help="Sources (default: all)")] = None,
    backup_after: Annotated[
        bool, typer.Option("--backup", help="Push to encrypted remotes after")
    ] = False,
):
    """Pull other machines' changes, fetch and import each source, apply rules, commit.

    Sources that need a login open a browser window. Nothing is pushed unless you pass --backup."""
    from fin import importers, sync
    from fin.importers import base

    root = _root()
    names = [_source_arg(s) for s in sources] if sources else list(importers.SOURCES)
    try:
        for remote_name in sync.remotes(root):
            with gitops.write_lock(root):
                typer.echo(f"{remote_name}: {sync.pull(root, remote_name)}")
    except sync.ConflictError as e:
        raise _fail(f"{e}\nNothing was changed. Resolve by hand, or ask me to.")
    except (sync.SyncError, gitops.GitError) as e:
        raise _fail(str(e))
    failed = []
    for name in names:
        try:
            typer.echo(_fetch_and_import(root, name))
        except base.ImportFailed as e:
            typer.echo(f"{name}: FAILED, ledger unchanged for this source: {e}", err=True)
            failed.append(name)
    if backup_after:
        try:
            for name, result in sync.backup(root, None).items():
                typer.echo(f"{name}: {result}")
        except (sync.SyncError, gitops.GitError) as e:
            raise _fail(str(e))
    if failed:
        raise _fail(f"failed: {', '.join(failed)}")


@app.command("categorize")
def categorize_cmd(
    source: Annotated[str | None, typer.Argument(help="e.g. swisscard")] = None,
    llm: Annotated[
        bool, typer.Option("--llm", help="Also ask a model about generic merchants")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option(help="With --llm: print exactly what would be sent")
    ] = False,
):
    """Categorize from your payee rules and rules/categories.yaml; --llm refines what is left."""
    from fin import labels
    from fin.importers import categorize, llm_categorize

    root = _root()
    if llm and dry_run:
        llm_categorize.run(root, dry_run=True, log=typer.echo)
        return
    ruled = labels.apply_rules(root)
    mapped = categorize.apply_mapping(root, source)
    typer.echo(f"categorized {ruled + mapped} transaction(s) ({ruled} by payee rules)")
    if llm:
        _report_llm(llm_categorize.run(root, log=typer.echo))


def _report_llm(r):
    typer.echo(
        f"model: asked about {r.asked} merchants; applied {r.applied_high} confident and "
        f"{r.applied_medium} tentative (kept #review); {r.skipped} merchants left alone; "
        f"{r.by_rule} by existing payee rules"
    )
    if r.new_accounts:
        typer.echo("new accounts: " + ", ".join(r.new_accounts))
    for account, n in sorted(r.by_account.items(), key=lambda kv: -kv[1]):
        typer.echo(f"  {n:4} {account}")


card = typer.Typer(help="Which card each payment was made on.")
app.add_typer(card, name="card")


@card.command("list")
def card_list():
    """Cards seen in the ledger, with the names you gave them."""
    from fin import cards

    root = _root()
    for r in cards.summary(ledger.load(config.main_ledger(root)), root):
        typer.echo(
            f"{r['source']}\t{r['card']}\t{r['label'] or '(unnamed)'}\t{r['count']} payments\t"
            f"{r['first']} .. {r['last']}"
        )


@card.command("name")
def card_name(
    source: Annotated[str, typer.Argument(help="e.g. swisscard")],
    card_id: Annotated[str, typer.Argument(help="the id shown by `fin card list`")],
    name: Annotated[str, typer.Argument(help='e.g. "Anna (Visa)"; empty to forget')] = "",
):
    """Name a card, so payments show whose card they were made on."""
    from fin import cards

    try:
        cards.set_label(_root(), source, card_id, name)
    except ValueError as e:
        raise _fail(str(e))
    typer.echo(f"{card_id}: {name or '(name removed)'}")
